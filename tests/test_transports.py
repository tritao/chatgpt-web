"""Offline transport selection, challenge handling, and duplicate-send regressions."""
import os
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx
from test_rate_limit import load_client_module
from sentinel import SentinelSigner, SentinelCompatibilityError


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.app = load_client_module()
        self.client = object.__new__(self.app.ChatGPTWebClient)
        self.client.create_sentinel_headers = Mock(return_value={"proof": "fixture"})
        self.client.send_prompt_browser = Mock(return_value=("existing", {}, {"old"}))
        self.client.send_prompt_direct = Mock(return_value=("existing", "new"))
        self.client.get_conversation = Mock(return_value={
            "id": "existing", "messages": [{"id": "old"}, {"id": "new"}],
        })
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_default_browser_keeps_conversation_and_history(self):
        self.client.send_prompt("existing", "hello")
        self.client.send_prompt_browser.assert_called_once_with(
            "existing", "hello", {"old", "new"}, None)
        self.client.create_sentinel_headers.assert_not_called()
        self.client.send_prompt_direct.assert_not_called()

    def test_aliases_and_invalid_configuration(self):
        for raw, expected in (("node", "direct"), ("chrome", "browser"),
                              ("auto", "auto"), ("recipe", "recipe")):
            os.environ["CHATGPT_WEB_SEND_MODE"] = raw
            self.assertEqual(self.app.send_mode(), expected)
        os.environ["CHATGPT_WEB_SEND_MODE"] = "typo"
        with self.assertRaises(self.app.ChatGPTWebError):
            self.client.send_prompt(None, "hello")
        self.client.send_prompt_browser.assert_not_called()

    def test_auto_falls_back_only_before_submission(self):
        os.environ["CHATGPT_WEB_SEND_MODE"] = "auto"
        self.client.create_sentinel_headers.side_effect = SentinelCompatibilityError("changed")
        self.client.send_prompt("existing", "hello")
        self.client.send_prompt_browser.assert_called_once()
        self.client.send_prompt_direct.assert_not_called()

    def test_direct_never_starts_browser_on_signing_failure(self):
        os.environ["CHATGPT_WEB_SEND_MODE"] = "direct"
        self.client.create_sentinel_headers.side_effect = SentinelCompatibilityError("changed")
        with self.assertRaises(SentinelCompatibilityError):
            self.client.send_prompt(None, "hello")
        self.client.send_prompt_browser.assert_not_called()

    def test_auto_does_not_fallback_after_submission_or_auth_failure(self):
        os.environ["CHATGPT_WEB_SEND_MODE"] = "auto"
        for error in (httpx.ReadTimeout("timeout"), self.app.ChatGPTWebError("HTTP 429"),
                      SentinelCompatibilityError("unexpected late error")):
            with self.subTest(error=error):
                self.client.send_prompt_direct.side_effect = error
                with self.assertRaises(type(error)):
                    self.client.send_prompt(None, "hello")
        self.client.send_prompt_browser.assert_not_called()
        self.client.create_sentinel_headers.side_effect = self.app.ChatGPTWebError("HTTP 401")
        with self.assertRaises(self.app.ChatGPTWebError):
            self.client.send_prompt(None, "hello")
        self.client.send_prompt_browser.assert_not_called()

    def test_attachments_are_never_lost_on_fallback(self):
        os.environ["CHATGPT_WEB_SEND_MODE"] = "auto"
        self.client.create_sentinel_headers.side_effect = SentinelCompatibilityError("challenge")
        with self.assertRaises(SentinelCompatibilityError):
            self.client.send_prompt(None, "hello", [{"id": "image"}])
        self.client.send_prompt_browser.assert_not_called()

    def test_successful_auto_submits_once(self):
        os.environ["CHATGPT_WEB_SEND_MODE"] = "auto"
        self.client.send_prompt(None, "hello")
        self.client.create_sentinel_headers.assert_called_once()
        self.client.send_prompt_direct.assert_called_once()
        self.client.send_prompt_browser.assert_not_called()


class BrowserProcessTests(unittest.TestCase):
    def test_browser_startup_and_existing_conversation_result(self):
        app = load_client_module()
        client = object.__new__(app.ChatGPTWebClient)
        client.cdp_url = "http://127.0.0.1:9222"
        client.get_conversation = Mock(return_value={"id": "existing"})
        process = Mock()
        process.stdin = Mock()
        process.stdout = io.StringIO('{"type":"completed","conversation_id":"existing"}\n')
        process.wait.return_value = 0
        with patch.object(app, "chrome_is_ready", return_value=False), \
             patch.object(app, "launch_login_browser") as launch, \
             patch.object(app.subprocess, "Popen", return_value=process) as spawn:
            result = client.send_prompt_browser("existing", "hello", {"old"})
        launch.assert_called_once_with(client.cdp_url)
        spawn.assert_called_once()
        self.assertEqual(result, ("existing", {"id": "existing"}, {"old"}))
        self.assertIn('"conversation_id": "existing"', process.stdin.write.call_args.args[0])

    def test_invalid_browser_event_terminates_helper_without_retry(self):
        app = load_client_module()
        client = object.__new__(app.ChatGPTWebClient)
        client.cdp_url = "http://127.0.0.1:9222"
        process = Mock()
        process.stdout = io.StringIO('not JSON\n')
        process.poll.return_value = None
        with patch.object(app, "chrome_is_ready", return_value=True), \
             patch.object(app.subprocess, "Popen", return_value=process) as spawn:
            with self.assertRaisesRegex(app.ChatGPTWebError, "invalid stream"):
                client.send_prompt_browser(None, "hello", set())
        spawn.assert_called_once()
        process.terminate.assert_called_once()


class SentinelTests(unittest.TestCase):
    def signer(self, requirements):
        signer = SentinelSigner(Mock(return_value=SimpleNamespace(json=lambda: requirements)))
        signer.run_sentinel_node = Mock(side_effect=[{"initial": "fixture"}, {"proof": "fixture"}])
        return signer

    def test_required_challenge_fails_before_enforcement(self):
        signer = self.signer({"prepare_token": "fixture", "turnstile": {"required": True}})
        with self.assertRaisesRegex(SentinelCompatibilityError, "browser challenge"):
            signer.create_sentinel_headers()
        self.assertEqual(signer.run_sentinel_node.call_count, 1)

    def test_malformed_prepare_is_compatibility_failure(self):
        for response in (None, [], {}, {"prepare_token": ""},
                         {"prepare_token": "fixture", "turnstile": []}):
            with self.subTest(response=response):
                with self.assertRaises(SentinelCompatibilityError):
                    self.signer(response).create_sentinel_headers()

    def test_valid_preparation_preserves_existing_header_flow(self):
        headers = self.signer({"prepare_token": "fixture"}).create_sentinel_headers()
        self.assertEqual(headers, {
            "openai-sentinel-chat-requirements-prepare-token": "fixture",
            "openai-sentinel-proof-token": "fixture",
        })


class SignerProcessTests(unittest.TestCase):
    def test_changed_sdk_and_invalid_output_are_clean_compatibility_errors(self):
        for result in (
            SimpleNamespace(returncode=1, stderr="Sentinel proof engine signature changed (SDK fixture-hash)", stdout=""),
            SimpleNamespace(returncode=0, stderr="", stdout="not JSON"),
            SimpleNamespace(returncode=0, stderr="", stdout="[]"),
        ):
            with self.subTest(result=result), patch("sentinel.subprocess.run", return_value=result):
                with self.assertRaises(SentinelCompatibilityError):
                    SentinelSigner(Mock()).run_sentinel_node(["--emit-initial"])
