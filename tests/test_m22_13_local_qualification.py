"""Hermetic coverage for the M22-13 local qualification tool.

Every test here drives the real tool through its injected exchange seam. Nothing opens a socket,
nothing reads the operator's Ollama installation, and no test asserts that the shipped catalog's
own evidence matches any particular live model -- that claim belongs to the separately authorized
live run, not to the Full Gate.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import ModuleType
from typing import cast

from historical_prompt_model_fixtures import load_historical_catalog as load_prompt_model_catalog

from comfyui_h3_context.core.prompt_model_provider import (
    PromptModelQualificationState,
    build_qualification_evidence,
)

ROOT = Path(__file__).resolve().parents[1]


def _load_tool() -> ModuleType:
    """Load the script by path: `scripts/` is a tool directory, not an installed package."""

    spec = importlib.util.spec_from_file_location(
        "m22_13_local_qualification", ROOT / "scripts" / "m22_13_local_qualification.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL = _load_tool()
#: The tool lookup these tests stand in for. Named once so a rename in the tool surfaces here.
SELECTED_PROFILE = "_selected_profile"

ENDPOINT = "http://127.0.0.1:11434"
LICENSE_TEXT = "Fixture licence notice, present only so that something can be hashed away."
LICENSE_SHA = "sha256:" + sha256(LICENSE_TEXT.encode("utf-8")).hexdigest()
WIRE_DIGEST = "5a" * 32
MODEL_SIZE_BYTES = 17_741_872_132

SHIPPED = load_prompt_model_catalog().profiles[0]
MODEL_ID = SHIPPED.model_id
#: The one draft contract this lane speaks. The catalog row names it as its parser and the
#: request sends it as the response schema; the test states it once so a divergence shows up
#: here rather than as two independently-passing halves.
DRAFT_SCHEMA_ID = "h3.prompt_model.draft_json.v1"

EVIDENCE = build_qualification_evidence(
    model_id=MODEL_ID,
    digest=WIRE_DIGEST,
    model_size_bytes=MODEL_SIZE_BYTES,
    model_format="gguf",
    model_family="qwen35",
    parameter_size="27.3B",
    quantization_level="Q4_K_M",
    context_length=262_144,
    capabilities=("completion", "thinking", "tools", "vision"),
    license_text_sha256=LICENSE_SHA,
    adapter_version=SHIPPED.adapter_version,
    parser_version=SHIPPED.parser_version,
    evidence_basis_id="M22-13.fixture.1",
)
#: The catalog row the fixture runtime is holding. Substituted for the shipped one so the tool can
#: be exercised end to end without asserting anything about the operator's real weights.
FIXTURE_PROFILE = replace(
    SHIPPED,
    model_digest=EVIDENCE.model_digest,
    license_text_sha256=LICENSE_SHA,
    qualification_state=PromptModelQualificationState.QUALIFIED,
    qualification_evidence=EVIDENCE,
)


def tags_response(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "name": MODEL_ID,
        "model": MODEL_ID,
        "digest": WIRE_DIGEST,
        "size": MODEL_SIZE_BYTES,
        "details": {"format": "gguf", "family": "qwen35"},
    }
    row.update(overrides)
    return {"models": [row]}


def show_response(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "license": LICENSE_TEXT,
        "capabilities": ["completion", "thinking", "tools", "vision"],
        "details": {
            "format": "gguf",
            "family": "qwen35",
            "parameter_size": "27.3B",
            "quantization_level": "Q4_K_M",
        },
        "model_info": {"qwen35.context_length": 262_144},
    }
    payload.update(overrides)
    return payload


def draft_answer(text: str = "a quiet street at dusk, static camera") -> dict[str, object]:
    return {
        "model": MODEL_ID,
        "done": True,
        "message": {
            "role": "assistant",
            "content": json.dumps({"schema": DRAFT_SCHEMA_ID, "prompt_text": text}),
        },
    }


def _message_of(answer: dict[str, object]) -> dict[str, object]:
    """The message half of a chat answer, as a mapping mypy can reason about."""

    message = answer["message"]
    assert isinstance(message, dict)
    return message


class FixtureRuntime:
    """Answers the three routes the tool is allowed to use, and records every call."""

    def __init__(
        self,
        *,
        tags: dict[str, object] | None = None,
        show: dict[str, object] | None = None,
        chat: object = None,
    ) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self._tags = tags_response() if tags is None else tags
        self._show = show_response() if show is None else show
        self._chat = draft_answer() if chat is None else chat

    def factory(self, _destination: object, _timeout_seconds: float) -> FixtureRuntime:
        return self

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> object:
        del timeout_seconds
        self.calls.append((method, path, payload))
        if path == "/api/tags":
            return self._tags
        if path == "/api/show":
            return self._show
        if path == "/api/chat":
            if isinstance(self._chat, BaseException):
                raise self._chat
            return self._chat
        raise AssertionError(path)

    @property
    def paths(self) -> list[str]:
        return [path for _method, path, _payload in self.calls]


class ToolInvocationTests(unittest.TestCase):
    def run_tool(self, *arguments: str, runtime: FixtureRuntime | None = None) -> int:
        return cast(int, TOOL.main(list(arguments), (runtime or FixtureRuntime()).factory))

    def test_every_argument_is_required(self) -> None:
        """No default may exist that could cause this tool to contact a service nobody named."""

        for missing in (
            ["--endpoint", ENDPOINT, "--model", MODEL_ID, "--output", "out.json"],
            ["--mode", "census", "--model", MODEL_ID, "--output", "out.json"],
            ["--mode", "census", "--endpoint", ENDPOINT, "--output", "out.json"],
            ["--mode", "census", "--endpoint", ENDPOINT, "--model", MODEL_ID],
        ):
            with self.subTest(arguments=" ".join(missing)):
                with self.assertRaises(SystemExit) as caught:
                    self.run_tool(*missing)
                self.assertNotEqual(caught.exception.code, 0)

    def test_a_mode_outside_the_two_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_tool(
                "--mode", "pull", "--endpoint", ENDPOINT, "--model", MODEL_ID, "--output", "x.json"
            )

    def test_a_non_loopback_endpoint_never_reaches_a_transport(self) -> None:
        runtime = FixtureRuntime()
        for endpoint in ("http://ollama.example.com:11434", "https://127.0.0.1:11434/../etc"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(SystemExit) as caught:
                    with tempfile.TemporaryDirectory() as directory:
                        self.run_tool(
                            "--mode",
                            "census",
                            "--endpoint",
                            endpoint,
                            "--model",
                            MODEL_ID,
                            "--output",
                            str(Path(directory) / "census.json"),
                            runtime=runtime,
                        )
                self.assertEqual(caught.exception.code, 2)
        self.assertEqual(runtime.calls, [])


class CensusTests(unittest.TestCase):
    def census(self, runtime: FixtureRuntime) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "census.json"
            code = TOOL.main(
                [
                    "--mode",
                    "census",
                    "--endpoint",
                    ENDPOINT,
                    "--model",
                    MODEL_ID,
                    "--output",
                    str(output),
                ],
                runtime.factory,
            )
            self.assertEqual(code, 0)
            return cast("dict[str, object]", json.loads(output.read_text(encoding="utf-8")))

    def test_a_census_costs_two_reads_and_issues_no_generation_request(self) -> None:
        runtime = FixtureRuntime()
        receipt = self.census(runtime)
        self.assertEqual(runtime.paths, ["/api/tags", "/api/show"])
        self.assertEqual(runtime.calls[1][2], {"model": MODEL_ID, "verbose": False})
        self.assertEqual(receipt["generation_requests"], 0)

    def test_a_census_says_in_its_own_output_that_it_is_not_a_pass(self) -> None:
        receipt = self.census(FixtureRuntime())
        self.assertEqual(receipt["status"], "PROPOSED")
        self.assertIs(receipt["activates_execution"], False)
        self.assertNotIn("PASS", json.dumps(receipt))

    def test_the_receipt_carries_identities_and_no_content(self) -> None:
        receipt = self.census(FixtureRuntime())
        identity = receipt["identity"]
        assert isinstance(identity, dict)
        self.assertEqual(identity["model_digest"], "sha256:" + WIRE_DIGEST)
        self.assertEqual(identity["license_text_sha256"], LICENSE_SHA)
        self.assertEqual(identity["show_identity_sha256"], EVIDENCE.show_identity_sha256)
        serialized = json.dumps(receipt)
        for forbidden in (LICENSE_TEXT, ENDPOINT, "127.0.0.1", "11434"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, serialized)

    def test_an_ambiguous_or_unreadable_listing_stops_before_show(self) -> None:
        listing = tags_response()
        rows = listing["models"]
        assert isinstance(rows, list)
        listing["models"] = rows * 2
        runtime = FixtureRuntime(tags=listing)
        with self.assertRaises(SystemExit) as caught:
            self.census(runtime)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(runtime.paths, ["/api/tags"])

    def test_a_receipt_is_never_written_over(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "census.json"
            output.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(SystemExit) as caught:
                TOOL.main(
                    [
                        "--mode",
                        "census",
                        "--endpoint",
                        ENDPOINT,
                        "--model",
                        MODEL_ID,
                        "--output",
                        str(output),
                    ],
                    FixtureRuntime().factory,
                )
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual(output.read_text(encoding="utf-8"), "{}\n")


class QualifyTests(unittest.TestCase):
    def setUp(self) -> None:
        # The tool reads the shipped catalog; the fixture row stands in for it so these tests
        # prove the tool's behaviour rather than the operator's installation.
        # The tool is loaded by path, so its private lookup is replaced by name. `setattr` is
        # the honest spelling of that: a static reference would be claiming the loader resolved
        # something it cannot see.
        self._real = getattr(TOOL, SELECTED_PROFILE)
        setattr(TOOL, SELECTED_PROFILE, lambda _model_id: FIXTURE_PROFILE)

    def tearDown(self) -> None:
        setattr(TOOL, SELECTED_PROFILE, self._real)

    def qualify(self, runtime: FixtureRuntime) -> tuple[int, dict[str, object]]:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "qualify.json"
            code = TOOL.main(
                [
                    "--mode",
                    "qualify",
                    "--endpoint",
                    ENDPOINT,
                    "--model",
                    MODEL_ID,
                    "--output",
                    str(output),
                ],
                runtime.factory,
            )
            return code, json.loads(output.read_text(encoding="utf-8"))

    def test_a_matching_identity_earns_one_draft_and_a_pass(self) -> None:
        runtime = FixtureRuntime()
        code, receipt = self.qualify(runtime)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["generation_requests"], 1)
        # tags and show for the reobservation, tags again as the pre-chat recheck, then one chat.
        self.assertEqual(runtime.paths, ["/api/tags", "/api/show", "/api/tags", "/api/chat"])

    def test_the_receipt_records_the_dispositions_the_request_actually_carried(self) -> None:
        _code, receipt = self.qualify(FixtureRuntime())
        self.assertEqual(
            receipt["request_dispositions"],
            {
                "stream": False,
                "think": False,
                "keep_alive": 0,
                "format_schema_id": DRAFT_SCHEMA_ID,
                "carries_tools": False,
                "carries_media": False,
                "carries_credential": False,
            },
        )

    def test_drift_fails_before_a_single_generation_request_is_spent(self) -> None:
        for label, runtime in (
            ("replaced weights", FixtureRuntime(tags=tags_response(digest="6b" * 32))),
            (
                "shrunken window",
                FixtureRuntime(show=show_response(model_info={"qwen35.context_length": 8_192})),
            ),
            ("amended licence", FixtureRuntime(show=show_response(license=LICENSE_TEXT + "!"))),
        ):
            with self.subTest(drift=label):
                code, receipt = self.qualify(runtime)
                self.assertEqual(code, 1)
                self.assertEqual(receipt["status"], "FAIL")
                self.assertEqual(receipt["generation_requests"], 0)
                self.assertNotIn("/api/chat", runtime.paths)

    def test_an_answer_outside_the_closed_schema_is_not_a_pass(self) -> None:
        for label, chat in (
            (
                "prose instead of json",
                {
                    "model": MODEL_ID,
                    "done": True,
                    "message": {"role": "assistant", "content": "Sure! Here is your prompt."},
                },
            ),
            (
                "extra key",
                {
                    "model": MODEL_ID,
                    "done": True,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {"schema": DRAFT_SCHEMA_ID, "prompt_text": "x", "notes": "y"}
                        ),
                    },
                },
            ),
            ("unfinished", {**draft_answer(), "done": False}),
        ):
            with self.subTest(answer=label):
                code, receipt = self.qualify(FixtureRuntime(chat=chat))
                self.assertEqual(code, 1)
                self.assertEqual(receipt["status"], "FAIL")

    def test_separate_thinking_is_ignored_and_never_projected_into_the_draft_receipt(self) -> None:
        chat = {
            "model": MODEL_ID,
            "done": True,
            "message": {**_message_of(draft_answer()), "thinking": "private reasoning fixture"},
        }
        code, receipt = self.qualify(FixtureRuntime(chat=chat))
        self.assertEqual(code, 0)
        self.assertNotIn("private reasoning fixture", json.dumps(receipt))

    def test_the_draft_receipt_carries_a_length_and_never_the_draft(self) -> None:
        distinctive = "a very specific phrase that must not survive into evidence"
        _code, receipt = self.qualify(FixtureRuntime(chat=draft_answer(distinctive)))
        self.assertNotIn(distinctive, json.dumps(receipt))
        draft = receipt["draft"]
        assert isinstance(draft, dict)
        self.assertGreater(draft["characters"], 0)

    def test_an_unqualified_catalog_row_cannot_be_qualified_by_running_the_tool(self) -> None:
        setattr(
            TOOL,
            SELECTED_PROFILE,
            lambda _model_id: replace(
                FIXTURE_PROFILE,
                qualification_state=PromptModelQualificationState.CATALOG_ONLY,
                qualification_evidence=None,
            ),
        )
        runtime = FixtureRuntime()
        with self.assertRaises(SystemExit) as caught:
            self.qualify(runtime)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(runtime.calls, [])


if __name__ == "__main__":
    unittest.main()
