"""Experimental direct-send signing adapter for the undocumented web protocol."""

import json
import os
from pathlib import Path
import subprocess
from typing import Any
from urllib.parse import urljoin

CHATGPT_ORIGIN = "https://chatgpt.com"


class ChatGPTWebError(RuntimeError):
    pass


class SentinelCompatibilityError(ChatGPTWebError):
    """Signing failed before a conversation submission was attempted."""


class SentinelSigner:
    def __init__(self, request):
        self.request = request

    def run_sentinel_node(
        self, arguments: list[str], payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        helper = Path(__file__).with_name("sentinel-node-probe.js")
        environment = os.environ.copy()
        try:
            result = subprocess.run(
                ["node", str(helper), *arguments],
                input=json.dumps(payload) if payload is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
                env=environment,
            )
        except FileNotFoundError as error:
            raise SentinelCompatibilityError(
                "Node.js 22 or newer is required for signing"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise SentinelCompatibilityError("Sentinel signing timed out") from error
        if result.returncode != 0:
            detail = result.stderr.strip() or "Sentinel signing failed"
            raise SentinelCompatibilityError(detail)
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise SentinelCompatibilityError(
                "Sentinel signer returned invalid JSON"
            ) from error
        if not isinstance(value, dict):
            raise SentinelCompatibilityError(
                "Sentinel signer returned an invalid result"
            )
        return value

    def create_sentinel_headers(self) -> dict[str, str]:
        initial = self.run_sentinel_node(["--emit-initial"]).get("initial")
        if not isinstance(initial, str) or not initial:
            raise SentinelCompatibilityError(
                "Sentinel signer did not generate an initial proof"
            )
        requirements = self.request(
            "POST",
            urljoin(CHATGPT_ORIGIN, "/backend-api/sentinel/chat-requirements/prepare"),
            json={"p": initial},
        ).json()
        if not isinstance(requirements, dict):
            raise SentinelCompatibilityError("Sentinel prepare returned an invalid response")
        prepare_token = requirements.get("prepare_token")
        if not isinstance(prepare_token, str) or not prepare_token:
            raise SentinelCompatibilityError("Sentinel prepare returned no prepare token")
        challenge = requirements.get("turnstile", {})
        if not isinstance(challenge, dict):
            raise SentinelCompatibilityError("Sentinel prepare returned an invalid challenge")
        if challenge.get("required"):
            raise SentinelCompatibilityError(
                "Sentinel requires a browser challenge; use browser send mode"
            )
        enforcement = self.run_sentinel_node(
            ["--requirements-stdin", "--emit-enforcement-json"], requirements
        )
        proof = enforcement.get("proof")
        turnstile = enforcement.get("turnstile")
        if not isinstance(proof, str) or not proof:
            raise SentinelCompatibilityError(
                "Sentinel did not return required enforcement tokens"
            )
        result = {
            "openai-sentinel-chat-requirements-prepare-token": prepare_token,
            "openai-sentinel-proof-token": proof,
        }
        if isinstance(turnstile, str) and turnstile:
            result["openai-sentinel-turnstile-token"] = turnstile
        return result

