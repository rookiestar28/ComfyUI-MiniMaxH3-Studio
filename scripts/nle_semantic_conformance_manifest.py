"""M25-20: freeze the machine-readable semantic conformance manifest.

The manifest is the exact-set join of two things that already exist: the closed corpus expanded by
``comfyui_h3_context.core.semantic_conformance.build_corpus`` from the accepted M25-10 contract
domains and 34 M25-11 command identifiers, and the M25-16 ``NleControlCoverageManifestV1`` control
rows. It is generated rather than authored precisely because a hand-written copy of 300-odd case
identifiers is a copy that drifts: the day a contract bound moves, an authored manifest keeps
reporting the old boundary as covered.

Two deliberate omissions. The manifest carries no runtime pin -- no Chromium build, no renderer or
extractor fingerprint -- because those are facts about a particular execution and belong to the
report each qualification run emits, not to the frozen membership. And it carries no result: a
manifest that could hold a disposition would be a place to write down a pass.

Run with ``--write`` to refresh, ``--check`` to verify.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core.composition_contract import (  # noqa: E402
    ENGINE_PROFILE_ID,
    OPERATION_PROFILE_ID,
    OUTPUT_PROFILE_ID,
)
from comfyui_h3_context.core.semantic_conformance import (  # noqa: E402
    CONFORMANCE_MANIFEST_SCHEMA,
    CORPUS_VERSION,
    MAX_CASE_OUTPUT_FRAMES,
    MAX_CASE_PAYLOAD_BYTES,
    MAX_CORPUS_CASES,
    MAX_REPORT_BYTES,
    TOLERANCE_PROFILE,
    TOLERANCE_VERSION,
    build_corpus,
    join_control_manifest,
)

MANIFEST_PATH = ROOT / "governance" / "contracts" / "nle_semantic_conformance_manifest_v1.json"
CONTROL_MANIFEST_PATH = ROOT / "governance" / "contracts" / "nle_control_coverage_manifest_v1.json"
SHELL_CONTRACT_PATH = ROOT / "frontend" / "src" / "contracts" / "sidebarEditorUiContract.ts"


def _sha256(path: Path) -> str:
    # `sha256:`-prefixed, matching every other fingerprint in this repository. The prefix is not
    # decoration: a bare 64-character hex string reads to `detect-secrets` as a high-entropy
    # credential, and the alternative is a hand-maintained baseline row that has to be moved by
    # hand every time the joined file changes.
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _control_manifest() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(CONTROL_MANIFEST_PATH.read_text(encoding="utf-8")))


def build() -> dict[str, Any]:
    corpus = build_corpus()
    control = _control_manifest()
    # The join is on the backend `command`, never on the manifest's display `operation_id`.
    operation_by_command = dict(join_control_manifest(corpus, control))

    rows: dict[str, list[dict[str, Any]]] = {
        "command_rows": [],
        "property_rows": [],
        "ui_invariant_rows": [],
        "import_integration_rows": [],
        "deferred_negative_rows": [],
    }
    for case in corpus.cases:
        row: dict[str, Any] = {
            "case_id": case.case_id,
            "family": case.family,
            "leaf": case.leaf,
            "supported": case.supported,
            "refusal_expected": case.refusal_expected,
        }
        if case.evidence is not None:
            row["evidence"] = case.evidence
        if case.command is not None:
            row["command"] = case.command
            row["control_operation_id"] = operation_by_command[case.command]
        rows[f"{case.case_class}_rows"].append(row)

    return {
        "schema": CONFORMANCE_MANIFEST_SCHEMA,
        "version": 1,
        "corpus_version": CORPUS_VERSION,
        "tolerance_version": TOLERANCE_VERSION,
        "operation_profile_id": OPERATION_PROFILE_ID,
        "engine_profile_id": ENGINE_PROFILE_ID,
        "output_profile_id": OUTPUT_PROFILE_ID,
        "joined_sources": {
            # Digests of the two authored files this membership was joined against. A change to
            # either regenerates the manifest, which is how the join stays exact rather than
            # historical.
            "control_coverage_manifest_sha256": _sha256(CONTROL_MANIFEST_PATH),
            "sidebar_editor_ui_contract_sha256": _sha256(SHELL_CONTRACT_PATH),
        },
        "limits": {
            "max_corpus_cases": MAX_CORPUS_CASES,
            "max_case_payload_bytes": MAX_CASE_PAYLOAD_BYTES,
            "max_report_bytes": MAX_REPORT_BYTES,
            "max_case_output_frames": MAX_CASE_OUTPUT_FRAMES,
        },
        "tolerance": TOLERANCE_PROFILE.as_wire(),
        # Reported separately and never summed: a UI invariant is not command coverage.
        "counts_by_class": corpus.counts_by_class(),
        **rows,
    }


def render() -> str:
    return json.dumps(build(), indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="refresh the conformance manifest")
    group.add_argument("--check", action="store_true", help="verify the conformance manifest")
    args = parser.parse_args(argv)
    expected = render()
    if args.write:
        # Byte-exact LF output: Windows text-mode `write_text` would emit CRLF and `--check`
        # must hold identically on every platform.
        MANIFEST_PATH.write_bytes(expected.encode("utf-8"))
        print(f"wrote {MANIFEST_PATH.relative_to(ROOT).as_posix()}")
        return 0
    actual = MANIFEST_PATH.read_bytes().decode("utf-8") if MANIFEST_PATH.exists() else ""
    if actual != expected:
        print("semantic conformance manifest drifted; rerun with --write", file=sys.stderr)
        return 1
    print("semantic conformance manifest is current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
