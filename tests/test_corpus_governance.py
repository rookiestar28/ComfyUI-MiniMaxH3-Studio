"""M9-03 corpus partition, annotation, privacy, and blind-review tests."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast

from scripts.corpus_governance import (
    MAX_CORPUS_BYTES,
    ROOT,
    CorpusError,
    inspect_corpus,
    render_json,
    validate_corpus,
)

FIXTURE = ROOT / "tests" / "fixtures" / "m9_03_corpus_governance.json"


def document() -> dict[str, object]:
    return cast(dict[str, object], json.loads(FIXTURE.read_text(encoding="utf-8")))


class CorpusGovernanceTests(unittest.TestCase):
    def test_versioned_fixture_covers_modes_dimensions_and_partitions(self) -> None:
        fixture = document()
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "corpus_governance_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        corpus = validate_corpus(fixture)
        self.assertEqual(
            schema["$id"], "comfyui-h3-context://contracts/corpus_governance_v1.schema.json"
        )
        self.assertEqual(corpus.corpus_id, "m9-03-synthetic-governance")
        self.assertEqual(corpus.case_count, 5)
        self.assertEqual(set(corpus.modes), {"t2va", "i2va", "fl2va", "l2va", "ref2va"})
        self.assertEqual(
            set(corpus.dimensions),
            {
                "identity",
                "style",
                "motion",
                "camera",
                "editing",
                "dialogue",
                "ocr",
                "audio",
                "ambiguity",
                "adversarial",
                "resource",
            },
        )
        self.assertEqual(
            {item["kind"] for item in cast(list[dict[str, object]], fixture["partitions"])},
            {"train", "dev", "test", "restricted_oracle"},
        )
        coverage = cast(dict[str, object], fixture["coverage_policy"])
        self.assertEqual(
            set(cast(list[object], coverage["languages"])),
            {"chinese", "english", "code_switch", "multilingual"},
        )
        self.assertEqual(
            set(cast(list[object], coverage["timing_semantics"])),
            {"explicit", "implicit", "mixed"},
        )
        self.assertEqual(coverage["quota_policy"], "evidence_justified_not_fixed")

    def test_public_fixture_contains_fingerprints_not_content_or_locators(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")
        self.assertNotIn("https://", text)
        self.assertNotIn("/mnt/", text)
        self.assertNotIn("prompt_text", text)
        self.assertNotIn("raw_media_bytes", text)
        for case in cast(list[dict[str, object]], document()["cases"]):
            fingerprints = cast(list[object], case["media_fingerprints"])
            self.assertTrue(all(str(item).startswith("sha256:") for item in fingerprints))

    def test_source_bundle_derivation_and_fingerprint_split_leaks_fail_closed(self) -> None:
        source_leak = document()
        cases = cast(list[dict[str, object]], source_leak["cases"])
        leaked = copy.deepcopy(cases[0])
        leaked["case_id"] = "leaked-source"
        leaked["partition_id"] = "dev"
        leaked["media_fingerprints"] = [
            "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        ]
        cases.append(leaked)
        with self.assertRaisesRegex(CorpusError, "source_bundle_id leaks"):
            validate_corpus(source_leak)

        family_leak = document()
        family_cases = cast(list[dict[str, object]], family_leak["cases"])
        family_copy = copy.deepcopy(family_cases[0])
        family_copy["case_id"] = "leaked-family"
        family_copy["partition_id"] = "test"
        family_copy["source_bundle_id"] = "bundle-new"
        family_copy["media_fingerprints"] = [
            "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        ]
        family_cases.append(family_copy)
        with self.assertRaisesRegex(CorpusError, "derivation_family_id leaks"):
            validate_corpus(family_leak)

        fingerprint_leak = document()
        fingerprint_cases = cast(list[dict[str, object]], fingerprint_leak["cases"])
        fingerprint_copy = copy.deepcopy(fingerprint_cases[0])
        fingerprint_copy["case_id"] = "leaked-fingerprint"
        fingerprint_copy["partition_id"] = "test"
        fingerprint_copy["source_bundle_id"] = "bundle-new-2"
        fingerprint_copy["derivation_family_id"] = "family-new-2"
        fingerprint_cases.append(fingerprint_copy)
        with self.assertRaisesRegex(CorpusError, "media_fingerprints leaks"):
            validate_corpus(fingerprint_leak)

    def test_restricted_oracle_partition_is_private_and_not_review_eligible(self) -> None:
        invalid = document()
        case = cast(list[dict[str, object]], invalid["cases"])[-1]
        case["review_eligible"] = True
        with self.assertRaisesRegex(CorpusError, "review_eligible must be false"):
            validate_corpus(invalid)

        invalid = document()
        case = cast(list[dict[str, object]], invalid["cases"])[-1]
        case["privacy_class"] = "synthetic"
        with self.assertRaisesRegex(CorpusError, "lacks restricted provenance"):
            validate_corpus(invalid)

    def test_annotation_provenance_uncertainty_agreement_and_exact_text_are_required(self) -> None:
        invalid = document()
        annotation = cast(
            dict[str, object], cast(list[dict[str, object]], invalid["cases"])[0]["annotation"]
        )
        del annotation["uncertainty"]
        with self.assertRaisesRegex(CorpusError, "missing required fields: uncertainty"):
            validate_corpus(invalid)

        invalid = document()
        exact = cast(
            dict[str, object],
            cast(
                dict[str, object], cast(list[dict[str, object]], invalid["cases"])[0]["annotation"]
            )["exact_text_ownership"],
        )
        exact["owner"] = "not_applicable"
        exact["preserve_verbatim"] = True
        with self.assertRaisesRegex(CorpusError, "cannot preserve text"):
            validate_corpus(invalid)

        invalid = document()
        exact = cast(
            dict[str, object],
            cast(
                dict[str, object], cast(list[dict[str, object]], invalid["cases"])[0]["annotation"]
            )["exact_text_ownership"],
        )
        exact["language_class"] = "english"
        with self.assertRaisesRegex(CorpusError, "language_class must match case language"):
            validate_corpus(invalid)

    def test_language_modality_risk_timing_and_sampling_rationale_are_required(self) -> None:
        invalid = document()
        coverage = cast(dict[str, object], invalid["coverage_policy"])
        coverage["quota_policy"] = "fixed_30_cases"
        with self.assertRaisesRegex(CorpusError, "reject arbitrary fixed quotas"):
            validate_corpus(invalid)

        invalid = document()
        case = cast(list[dict[str, object]], invalid["cases"])[0]
        case["timing"] = "unsupported_timing"
        with self.assertRaisesRegex(CorpusError, r"cases\[0\]\.timing must be one of"):
            validate_corpus(invalid)

    def test_license_privacy_and_blind_review_controls_fail_closed(self) -> None:
        invalid = document()
        policy = cast(dict[str, object], invalid["privacy_policy"])
        policy["forbidden_fields"] = ["raw_media"]
        with self.assertRaisesRegex(CorpusError, "omits forbidden fields"):
            validate_corpus(invalid)

        invalid = document()
        review = cast(dict[str, object], invalid["review_protocol"])
        review["order_swap_required"] = False
        with self.assertRaisesRegex(CorpusError, "must be true"):
            validate_corpus(invalid)

        invalid = document()
        provenance = cast(list[dict[str, object]], invalid["provenance"])[0]
        provenance["rights_status"] = "pending"
        with self.assertRaisesRegex(CorpusError, "non-oracle provenance must have reviewed rights"):
            validate_corpus(invalid)

    def test_unknown_private_fields_and_unsafe_source_urls_are_rejected(self) -> None:
        invalid = document()
        cast(list[dict[str, object]], invalid["cases"])[0]["raw_media"] = "forbidden"
        with self.assertRaisesRegex(CorpusError, "unsupported fields"):
            validate_corpus(invalid)

        invalid = document()
        source = cast(list[dict[str, object]], invalid["provenance"])[0]
        source["source_url"] = (
            "https://user:password@example.com/source"  # pragma: allowlist secret
        )
        with self.assertRaisesRegex(CorpusError, "userinfo"):
            validate_corpus(invalid)

    def test_deterministic_report_and_cli_are_offline(self) -> None:
        with tempfile.TemporaryDirectory(prefix="h3-corpus-test-") as directory:
            root = Path(directory)
            path = root / "corpus.json"
            path.write_text(json.dumps(document(), sort_keys=True), encoding="utf-8")
            before = path.read_bytes()
            first = inspect_corpus(path)
            second = inspect_corpus(path)
            self.assertEqual(first.status, "PASS")
            self.assertEqual(render_json(first), render_json(second))
            self.assertEqual(path.read_bytes(), before)
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "corpus_governance.py"),
                    "--corpus",
                    str(path),
                    "--json",
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(cast(dict[str, object], json.loads(result.stdout))["status"], "PASS")
            self.assertEqual(result.stderr, "")

    def test_oversized_or_symlink_corpus_is_not_read(self) -> None:
        with tempfile.TemporaryDirectory(prefix="h3-corpus-test-") as directory:
            root = Path(directory)
            oversized = root / "oversized.json"
            oversized.write_bytes(b"x" * (MAX_CORPUS_BYTES + 1))
            self.assertEqual(inspect_corpus(oversized).status, "INVALID")
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            link = root / "link.json"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable in this environment")
            self.assertEqual(inspect_corpus(link).status, "INVALID")


if __name__ == "__main__":
    unittest.main()
