"""M9-02 source ledger contract, drift, and safety tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast

from scripts.source_ledger import (
    ROOT,
    LedgerError,
    inspect_ledger,
    render_json,
    validate_ledger,
)


def _source(
    source_id: str = "SRC-1",
    *,
    canary: bool = False,
    local_path: str | None = None,
    sha256: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "source_id": source_id,
        "url": "https://example.com/source",
        "revision": "rev-1",
        "retrieved_at": "2026-08-07",
        "evidence_class": "official",
        "role": "guide",
        "canary": canary,
    }
    if local_path is not None:
        result["local_path"] = local_path
    if sha256 is not None:
        result["sha256"] = sha256
    return result


def _record(
    record_id: str = "CLAIM-1",
    *,
    kind: str = "claim",
    source_ids: list[str] | None = None,
    related_record_ids: list[str] | None = None,
    resolution: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "record_id": record_id,
        "kind": kind,
        "statement": "A bounded evidence statement.",
        "status": "accepted" if kind == "claim" else "open",
        "evidence_class": "official" if kind == "claim" else "experimental",
        "confidence": "high" if kind == "claim" else "unknown",
        "source_ids": source_ids or ["SRC-1"],
        "owning_tests": ["M9-02-P0-001"],
        "drift_policy": "fail_on_change" if kind == "claim" else "report_only",
        "affected_profiles": ["h3-base-v1"],
    }
    if related_record_ids is not None:
        result["related_record_ids"] = related_record_ids
    if resolution is not None:
        result["resolution"] = resolution
    return result


def _document(
    sources: list[dict[str, object]] | None = None,
    records: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema": "h3-context-source-ledger/1",
        "ledger_id": "h3-context-evidence",
        "ledger_version": "1.0.0",
        "updated_at": "2026-08-07",
        "sources": sources or [_source()],
        "records": records or [_record()],
    }


class SourceLedgerTests(unittest.TestCase):
    def test_public_schema_is_versioned_and_declares_traceability(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "source_ledger_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"], "comfyui-h3-context://contracts/source_ledger_v1.schema.json"
        )
        self.assertEqual(schema["properties"]["schema"]["const"], "h3-context-source-ledger/1")
        self.assertEqual(
            schema["$defs"]["record"]["required"],
            [
                "record_id",
                "kind",
                "statement",
                "status",
                "evidence_class",
                "confidence",
                "source_ids",
                "owning_tests",
                "drift_policy",
                "affected_profiles",
            ],
        )

    def test_valid_non_canary_ledger_loads_as_immutable_typed_contract(self) -> None:
        ledger = validate_ledger(_document())
        self.assertEqual(ledger.ledger_id, "h3-context-evidence")
        self.assertEqual(ledger.sources[0].source_id, "SRC-1")
        self.assertEqual(ledger.records[0].owning_tests, ("M9-02-P0-001",))
        self.assertEqual(inspect_ledger(Path("does-not-exist.json")).status, "INVALID")

    def test_canary_match_is_pass_and_report_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory(prefix="h3-ledger-test-") as directory:
            root = Path(directory)
            canary = root / "guide.txt"
            payload = b"stable guide\n"
            canary.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            document = _document(
                [_source(canary=True, local_path="guide.txt", sha256=digest)],
            )
            ledger_path = root / "ledger.json"
            encoded = json.dumps(document, indent=2, sort_keys=True).encode("utf-8")
            ledger_path.write_bytes(encoded)
            before = ledger_path.read_bytes()
            first = inspect_ledger(ledger_path, root)
            second = inspect_ledger(ledger_path, root)
            self.assertEqual(first.status, "PASS")
            self.assertEqual(first.canaries[0].status, "PASS")
            self.assertEqual(render_json(first), render_json(second))
            self.assertEqual(ledger_path.read_bytes(), before)

    def test_canary_drift_is_reported_without_rewriting_accepted_digest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="h3-ledger-test-") as directory:
            root = Path(directory)
            canary = root / "guide.txt"
            canary.write_text("changed", encoding="utf-8")
            expected = hashlib.sha256(b"original").hexdigest()
            document = _document([_source(canary=True, local_path="guide.txt", sha256=expected)])
            ledger_path = root / "ledger.json"
            ledger_path.write_text(json.dumps(document), encoding="utf-8")
            before = ledger_path.read_bytes()
            report = inspect_ledger(ledger_path, root)
            self.assertEqual(report.status, "DRIFT")
            self.assertEqual(report.canaries[0].expected_sha256, expected)
            self.assertNotEqual(report.canaries[0].actual_sha256, expected)
            self.assertEqual(ledger_path.read_bytes(), before)

    def test_missing_canary_is_distinct_from_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="h3-ledger-test-") as directory:
            root = Path(directory)
            document = _document(
                [_source(canary=True, local_path="missing.txt", sha256="0" * 64)],
            )
            path = root / "ledger.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            report = inspect_ledger(path, root)
            self.assertEqual(report.status, "MISSING")
            self.assertEqual(report.canaries[0].status, "MISSING")

    def test_conflict_requires_two_traceable_records_and_explicit_resolution(self) -> None:
        first = _record("CLAIM-1")
        second = _record("CLAIM-2")
        conflict = _record(
            "CONFLICT-1",
            kind="conflict",
            related_record_ids=["CLAIM-1", "CLAIM-2"],
            resolution="Keep both observations and require an explicit profile boundary.",
        )
        document = _document(records=[first, second, conflict])
        ledger = validate_ledger(document)
        self.assertEqual(ledger.records[-1].kind, "conflict")
        invalid = _document(
            records=[
                _record(
                    "CONFLICT-1",
                    kind="conflict",
                    related_record_ids=["CLAIM-1"],
                )
            ]
        )
        with self.assertRaisesRegex(LedgerError, "conflicts require"):
            validate_ledger(invalid)

    def test_unknown_source_and_record_references_fail_closed(self) -> None:
        unknown_source = _document(records=[_record(source_ids=["NO-SUCH-SOURCE"])])
        with self.assertRaisesRegex(LedgerError, "unknown sources"):
            validate_ledger(unknown_source)
        unknown_record = _document(
            records=[
                _record(
                    "CONFLICT-1",
                    kind="conflict",
                    related_record_ids=["CLAIM-1", "NO-SUCH-RECORD"],
                    resolution="Review required.",
                ),
                _record("CLAIM-1"),
            ]
        )
        with self.assertRaisesRegex(LedgerError, "unknown records"):
            validate_ledger(unknown_record)

    def test_url_credentials_signed_queries_and_path_traversal_are_rejected(self) -> None:
        credential_source = _source()
        credential_source["url"] = (
            "https://user:password@example.com/source"  # pragma: allowlist secret
        )
        with self.assertRaisesRegex(LedgerError, "userinfo"):
            validate_ledger(_document([credential_source]))
        signed_source = _source()
        signed_source["url"] = "https://example.com/source?X-Amz-Signature=secret"
        with self.assertRaisesRegex(LedgerError, "signed"):
            validate_ledger(_document([signed_source]))
        traversal_source = _source(canary=True, local_path="../outside.txt", sha256="0" * 64)
        with self.assertRaisesRegex(LedgerError, "safe repository-relative"):
            validate_ledger(_document([traversal_source]))

    def test_cli_emits_json_and_nonzero_for_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="h3-ledger-test-") as directory:
            root = Path(directory)
            (root / "guide.txt").write_text("actual", encoding="utf-8")
            path = root / "ledger.json"
            path.write_text(
                json.dumps(
                    _document([_source(canary=True, local_path="guide.txt", sha256="0" * 64)])
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "source_ledger.py"),
                    "--ledger",
                    str(path),
                    "--repo-root",
                    str(root),
                    "--json",
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 2)
            report = cast(dict[str, object], json.loads(result.stdout))
            self.assertEqual(report["status"], "DRIFT")
            self.assertEqual(result.stderr, "")

    def test_symlink_canary_is_invalid_when_platform_allows_symlinks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="h3-ledger-test-") as directory:
            root = Path(directory)
            target = root / "target.txt"
            target.write_text("target", encoding="utf-8")
            link = root / "link.txt"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable in this environment")
            document = _document(
                [
                    _source(
                        canary=True,
                        local_path="link.txt",
                        sha256=hashlib.sha256(b"target").hexdigest(),
                    )
                ]
            )
            path = root / "ledger.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            report = inspect_ledger(path, root)
            self.assertEqual(report.status, "INVALID")
            self.assertEqual(report.canaries[0].status, "INVALID")


if __name__ == "__main__":
    unittest.main()
