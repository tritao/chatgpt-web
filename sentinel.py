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
    def __init__(self, request, on_progress=None):
        self.request = request
        self.on_progress = on_progress or (lambda _stage: None)

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
        self.on_progress("generating_proof")
        initial = self.run_sentinel_node(["--emit-initial"]).get("initial")
        if not isinstance(initial, str) or not initial:
            raise SentinelCompatibilityError(
                "Sentinel signer did not generate an initial proof"
            )
        self.on_progress("preparing_requirements")
        try:
            requirements = self.request(
                "POST",
                urljoin(CHATGPT_ORIGIN, "/backend-api/sentinel/chat-requirements/prepare"),
                json={"p": initial},
            ).json()
        except ValueError as error:
            raise SentinelCompatibilityError("Sentinel prepare returned invalid JSON") from error
        if not isinstance(requirements, dict):
            raise SentinelCompatibilityError("Sentinel prepare returned an invalid response")
        if requirements.get("force_login"):
            raise ChatGPTWebError("ChatGPT requires reauthentication; run chatgpt-web login")
        token = requirements.get("token")
        prepare_token = requirements.get("prepare_token")
        if not (isinstance(token, str) and token) and not (
            isinstance(prepare_token, str) and prepare_token
        ):
            raise SentinelCompatibilityError("Sentinel prepare returned no requirements or prepare token")
        challenge = requirements.get("turnstile", {})
        if not isinstance(challenge, dict):
            raise SentinelCompatibilityError("Sentinel prepare returned an invalid challenge")
        if challenge.get("required") and (
            not isinstance(challenge.get("dx"), str) or not challenge["dx"]
        ):
            raise SentinelCompatibilityError("Sentinel requested VM evidence without a payload")
        proof_requirement = requirements.get("proofofwork", {})
        if not isinstance(proof_requirement, dict):
            raise SentinelCompatibilityError("Sentinel prepare returned invalid proof requirements")
        self.on_progress("evaluating_evidence")
        enforcement = self.run_sentinel_node(
            ["--requirements-stdin", "--emit-enforcement-json"],
            {"requirements": requirements, "initial": initial},
        )
        proof = enforcement.get("proof")
        turnstile = enforcement.get("turnstile")
        if proof_requirement.get("required") and (not isinstance(proof, str) or not proof):
            raise SentinelCompatibilityError("Sentinel returned no required proof of work")
        if challenge.get("required") and (not isinstance(turnstile, str) or not turnstile):
            raise SentinelCompatibilityError("Sentinel returned no required VM evidence")

        if not isinstance(token, str) or not token:
            # The current frontend sends these fields to finalize. Evidence remains
            # tied to this preparation; do not generate another initial proof here.
            body = {"prepare_token": prepare_token}
            for key, value in (("proofofwork", proof), ("turnstile", turnstile)):
                if isinstance(value, str) and value:
                    body[key] = value
            self.on_progress("finalizing")
            try:
                finalized = self.request(
                    "POST",
                    urljoin(CHATGPT_ORIGIN, "/backend-api/sentinel/chat-requirements/finalize"),
                    json=body,
                ).json()
            except ValueError as error:
                raise SentinelCompatibilityError("Sentinel finalize returned invalid JSON") from error
            if not isinstance(finalized, dict):
                raise SentinelCompatibilityError("Sentinel finalize returned an invalid response")
            if finalized.get("force_login"):
                raise ChatGPTWebError("ChatGPT requires reauthentication; run chatgpt-web login")
            token = finalized.get("token")
            if not isinstance(token, str) or not token:
                raise SentinelCompatibilityError("Sentinel finalize returned no requirements token")
        result = {"openai-sentinel-chat-requirements-token": token}
        for name, value in (("proof", proof), ("turnstile", turnstile)):
            if isinstance(value, str) and value:
                result[f"openai-sentinel-{name}-token"] = value
        return result
