"""Offline transport selection, challenge handling, and duplicate-send regressions."""
import os
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx
from test_rate_limit import load_client_module
from sentinel import ChatGPTWebError, SentinelSigner, SentinelCompatibilityError


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


class DirectAuthenticationTests(unittest.TestCase):
    def test_direct_startup_prefers_saved_session_with_browser_available(self):
        app = load_client_module()
        for mode in ("direct", "node", "auto"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"CHATGPT_WEB_SEND_MODE": mode}), \
                 patch.object(app.httpx, "Client"), \
                 patch.object(app, "load_auth_snapshot", return_value=("https://chatgpt.com/backend-api/conversations", {})) as saved, \
                 patch.object(app, "chrome_is_ready", return_value=True), \
                 patch.object(app, "capture_auth") as capture:
                with app.ChatGPTWebClient("http://127.0.0.1:9222"):
                    pass
                saved.assert_called_once()
                capture.assert_not_called()


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
    def signer(self, requirements, finalized=None, evidence=None):
        replies = [requirements, {"token": "final-fixture"} if finalized is None else finalized]
        request = Mock(side_effect=[SimpleNamespace(json=lambda value=value: value) for value in replies])
        signer = SentinelSigner(request)
        signer.run_sentinel_node = Mock(side_effect=[
            {"initial": "original-fixture"},
            {"proof": "proof-fixture", "turnstile": "vm-fixture"} if evidence is None else evidence,
        ])
        return signer

    def test_required_vm_evidence_keeps_original_preparation_proof(self):
        requirements = {"prepare_token": "prepared-fixture", "turnstile": {"required": True, "dx": "fixture"},
                        "proofofwork": {"required": True}}
        signer = self.signer(requirements)
        headers = signer.create_sentinel_headers()
        self.assertEqual(signer.run_sentinel_node.call_args.args[1],
                         {"requirements": requirements, "initial": "original-fixture"})
        self.assertEqual(signer.request.call_args.kwargs["json"], {
            "prepare_token": "prepared-fixture", "proofofwork": "proof-fixture", "turnstile": "vm-fixture",
        })
        self.assertTrue(signer.request.call_args.args[1].endswith("/finalize"))
        self.assertEqual(headers, {
            "openai-sentinel-chat-requirements-token": "final-fixture",
            "openai-sentinel-proof-token": "proof-fixture",
            "openai-sentinel-turnstile-token": "vm-fixture",
        })
        self.assertEqual(signer.request.call_count, 2)

    def test_prepare_can_return_a_finalized_token_directly(self):
        signer = self.signer({"token": "already-final"}, evidence={"proof": None, "turnstile": None})
        self.assertEqual(signer.create_sentinel_headers(), {
            "openai-sentinel-chat-requirements-token": "already-final",
        })
        self.assertEqual(signer.request.call_count, 1)

    def test_force_login_is_an_authentication_failure_before_enforcement(self):
        signer = self.signer({"force_login": True})
        with self.assertRaises(ChatGPTWebError) as error:
            signer.create_sentinel_headers()
        self.assertNotIsInstance(error.exception, SentinelCompatibilityError)
        self.assertEqual(signer.run_sentinel_node.call_count, 1)
        self.assertEqual(signer.request.call_count, 1)

    def test_invalid_json_is_reported_without_the_response_body(self):
        for stage in ("prepare", "finalize"):
            with self.subTest(stage=stage):
                signer = self.signer({"prepare_token": "fixture"})
                invalid = SimpleNamespace(json=Mock(side_effect=ValueError("private-response")))
                prepare = SimpleNamespace(json=lambda: {"prepare_token": "fixture"})
                signer.request.side_effect = [invalid] if stage == "prepare" else [prepare, invalid]
                with self.assertRaisesRegex(SentinelCompatibilityError,
                        "^Sentinel " + stage + " returned invalid JSON$"):
                    signer.create_sentinel_headers()

    def test_missing_vm_payload_fails_before_enforcement(self):
        signer = self.signer({"prepare_token": "fixture", "turnstile": {"required": True}})
        with self.assertRaisesRegex(SentinelCompatibilityError, "without a payload"):
            signer.create_sentinel_headers()
        self.assertEqual(signer.run_sentinel_node.call_count, 1)
        self.assertEqual(signer.request.call_count, 1)

    def test_malformed_prepare_is_compatibility_failure(self):
        for response in (None, [], {}, {"prepare_token": ""},
                         {"prepare_token": "fixture", "turnstile": []},
                         {"prepare_token": "fixture", "proofofwork": []}):
            with self.subTest(response=response):
                with self.assertRaises(SentinelCompatibilityError):
                    self.signer(response).create_sentinel_headers()

    def test_optional_evidence_is_omitted_from_finalize_and_headers(self):
        signer = self.signer({"prepare_token": "fixture"}, evidence={"proof": None, "turnstile": None})
        self.assertEqual(signer.create_sentinel_headers(), {
            "openai-sentinel-chat-requirements-token": "final-fixture",
        })
        self.assertEqual(signer.request.call_args.kwargs["json"], {"prepare_token": "fixture"})

    def test_missing_required_evidence_never_finalizes(self):
        for requirements, evidence in (
            ({"proofofwork": {"required": True}}, {"proof": None}),
            ({"turnstile": {"required": True, "dx": "fixture"}}, {"turnstile": None}),
        ):
            with self.subTest(requirements=requirements):
                signer = self.signer({"prepare_token": "fixture", **requirements}, evidence=evidence)
                with self.assertRaises(SentinelCompatibilityError):
                    signer.create_sentinel_headers()
                self.assertEqual(signer.request.call_count, 1)

    def test_invalid_finalized_response_never_becomes_a_header(self):
        for response in ([], {}, {"token": ""}, {"token": 123}):
            with self.subTest(response=response):
                with self.assertRaises(SentinelCompatibilityError):
                    self.signer({"prepare_token": "fixture"}, finalized=response).create_sentinel_headers()

    def test_sdk_error_stops_before_finalize(self):
        signer = self.signer({"prepare_token": "fixture"})
        signer.run_sentinel_node.side_effect = [
            {"initial": "fixture"}, SentinelCompatibilityError("VM could not complete"),
        ]
        with self.assertRaises(SentinelCompatibilityError):
            signer.create_sentinel_headers()
        self.assertEqual(signer.request.call_count, 1)


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


class DirectStreamTests(unittest.TestCase):
    def test_finalized_headers_and_supported_compression_reach_the_stream(self):
        app = load_client_module()
        client = object.__new__(app.ChatGPTWebClient)
        client.headers = {"accept-encoding": "br, zstd"}
        client.create_sentinel_headers = Mock(side_effect=AssertionError("must reuse finalized headers"))
        client.create_conversation_body = Mock(return_value={"messages": [{"id": "sent-fixture"}]})
        received = []
        def handle(request):
            received.append(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=(
                'data: {"conversation_id":"conversation-fixture","message":{"author":{"role":"assistant"},'
                '"content":{"content_type":"text","parts":["sentinel-ok"]}}}\n\ndata: [DONE]\n\n'))
        events = []
        with httpx.Client(transport=httpx.MockTransport(handle)) as client.http:
            result = client.send_prompt_direct(None, "hello", [], events.append,
                sentinel_headers={"openai-sentinel-chat-requirements-token": "final-fixture"})
        self.assertEqual(result, ("conversation-fixture", "sent-fixture"))
        self.assertEqual(events, [{"type": "delta", "text": "sentinel-ok"}])
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].headers["accept-encoding"], "gzip, deflate")
        self.assertEqual(received[0].headers["openai-sentinel-chat-requirements-token"], "final-fixture")
