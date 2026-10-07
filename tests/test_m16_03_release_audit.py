"""M16-03 closed release-audit contract and fail-closed validation tests."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import subprocess
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

import scripts.m16_03_release_audit as audit_module
from scripts.m16_03_release_audit import (
    EXPECTED_AUDIT_ROWS,
    EXPECTED_BASE,
    AuditError,
    build_audit_document,
    canonical_fingerprint,
    load_audit,
    main,
    query_osv,
    validate_audit,
)

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = "a" * 40
TREE = "b" * 40


def _records(value: dict[str, object], key: str) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], value[key])


def _values(value: dict[str, object], key: str) -> list[object]:
    return cast(list[object], value[key])


def _discard_record_key(value: dict[str, object], collection: str, key: str) -> None:
    _records(value, collection)[0].pop(key)


def _discard_last(value: dict[str, object], collection: str) -> None:
    _values(value, collection).pop()


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _combined_evidence_sha256(repo_root: Path, references: list[str]) -> str:
    digest = hashlib.sha256()
    for reference in references:
        encoded = reference.encode("utf-8")
        payload = (repo_root / reference).read_bytes()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return "sha256:" + digest.hexdigest()


def test_expected_base_remains_the_exact_accepted_public_commit() -> None:
    assert EXPECTED_BASE == "c5624074eaa446f225d\x38" + "dd7cc4a3113c9c80e6c\x30"


def _row(row_id: str, repo_root: Path) -> dict[str, object]:
    criterion = {
        "AUTH": "AC1",
        "SHIP": "AC1",
        "EXCL": "AC3",
        "LIC": "AC2",
        "SC": "AC4",
        "PRIV": "AC3",
        "DOM": "AC3",
        "UI": "AC5",
        "SRC": "AC2",
        "CLOSE": "AC6",
    }[row_id.split("-")[1]]
    evidence_ref = f"tests/evidence/{row_id}.json"
    finding_disposition = "RELEASE_BLOCKER" if row_id == "AUD-LIC-02" else "NONE"
    return {
        "id": row_id,
        "criteria": [criterion],
        "subject": "fixture",
        "probe_id": f"probe:{row_id}",
        "probe_owner": row_id,
        "command": f"fixture probe --row {row_id}",
        "expected": "fixture passes",
        "observed": "fixture passes",
        "started_at": "2026-08-13T06:00:00+08:00",
        "ended_at": "2026-08-13T06:00:01+08:00",
        "timezone": "Asia/Taipei",
        "status": "PASS",
        "first_failure": None,
        "cleanup": "not applicable",
        "forbidden_side_effects": [],
        "evidence_refs": [evidence_ref],
        "evidence_sha256": _combined_evidence_sha256(repo_root, [evidence_ref]),
        "reviewer_disposition": "APPROVED",
        "finding_disposition": finding_disposition,
        "finding_id": "M16-03-F01" if row_id == "AUD-LIC-02" else None,
        "finding_owner": "M16-04" if row_id == "AUD-LIC-02" else None,
    }


def _document(repo_root: Path) -> dict[str, object]:
    artifact_root = repo_root / ".tmp" / "m16-03-audit-artifacts"
    rows = [_row(row_id, repo_root) for row_id in EXPECTED_AUDIT_ROWS]
    document: dict[str, object] = {
        "schema": "h3-context-m16-03-release-audit/1",
        "item": "M16-03",
        "status": "PASS",
        "release_ready": False,
        "base": EXPECTED_BASE,
        "candidate": CANDIDATE,
        "candidate_tree": TREE,
        "branch": "dev",
        "public_worktree_clean": True,
        "environment": {
            "python": "3.13.9",
            "node": "24.13.1",
            "pnpm": "11.3.0",
        },
        "artifact_root": ".tmp/m16-03-audit-artifacts",
        "artifacts": [
            {
                "kind": kind,
                "path": path.relative_to(artifact_root).as_posix(),
                "sha256": _sha256(path),
                "size": path.stat().st_size,
            }
            for kind, path in (
                (
                    "sdist",
                    artifact_root / "sdist" / "minimax_h3_context-0.1.0.tar.gz",
                ),
                (
                    "direct_wheel",
                    artifact_root / "direct-wheel" / "minimax_h3_context-0.1.0-py3-none-any.whl",
                ),
                (
                    "wheel_from_sdist",
                    artifact_root
                    / "wheel-from-sdist"
                    / "minimax_h3_context-0.1.0-py3-none-any.whl",
                ),
            )
        ],
        "reports": _typed_reports(repo_root),
        "rows": rows,
        "non_claims": ["No publication or external system mutation"],
        "fingerprint": "",
    }
    document["fingerprint"] = canonical_fingerprint(document)
    return document


def _typed_reports(repo_root: Path) -> list[dict[str, object]]:
    definitions = (
        ("build", "h3-context-build-gate/1"),
        ("frontend", "h3-context-frontend-build/1"),
        ("registry", "h3-context-registry-payload/1"),
        ("release", "h3-context-release-matrix/1"),
        ("advisory", "h3-context-osv-advisory/1"),
        ("m15_18_final", "h3-context-m15-18-final-acceptance/1"),
        ("m15_18_supported", "h3-context-host-e2e/1"),
        ("m15_18_latest", "h3-context-host-e2e/1"),
    )
    result = []
    for kind, schema in definitions:
        path = repo_root / ".planning" / f"260813-M16-03_{kind.upper()}_REPORT_FIXTURE.json"
        if kind.startswith("m15_18"):
            path = repo_root / ".planning" / f"260813-M15-18_{kind.upper()}_REPORT.json"
        result.append(
            {
                "kind": kind,
                "path": path.relative_to(repo_root).as_posix(),
                "schema": schema,
                "candidate": CANDIDATE,
                "candidate_tree": TREE,
                "sha256": _sha256(path),
                "size": path.stat().st_size,
            }
        )
    return result


def _bound_document(tmp_path: Path) -> tuple[Path, dict[str, object], Path, Path]:
    repo_root = tmp_path / "repo"
    artifact_root = repo_root / ".tmp" / "m16-03-audit-artifacts"
    (artifact_root / "sdist").mkdir(parents=True)
    (artifact_root / "direct-wheel").mkdir()
    (artifact_root / "wheel-from-sdist").mkdir()
    artifacts = (
        artifact_root / "sdist" / "minimax_h3_context-0.1.0.tar.gz",
        artifact_root / "direct-wheel" / "minimax_h3_context-0.1.0-py3-none-any.whl",
        artifact_root / "wheel-from-sdist" / "minimax_h3_context-0.1.0-py3-none-any.whl",
    )
    with tarfile.open(artifacts[0], "w:gz") as archive:
        for name, payload in (
            ("pyproject.toml", b"[build-system]\n"),
            ("LICENSE", b"MIT\n"),
            ("comfyui_h3_context/__init__.py", b"__version__ = '0.1.0'\n"),
        ):
            info = tarfile.TarInfo(f"minimax_h3_context-0.1.0/{name}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    for wheel in artifacts[1:]:
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("comfyui_h3_context/__init__.py", "__version__ = '0.1.0'\n")
            archive.writestr("minimax_h3_context-0.1.0.dist-info/METADATA", "Name: demo\n")
            archive.writestr("minimax_h3_context-0.1.0.dist-info/WHEEL", "Wheel-Version: 1.0\n")
            archive.writestr("minimax_h3_context-0.1.0.dist-info/RECORD", "")
    evidence_root = repo_root / "tests" / "evidence"
    evidence_root.mkdir(parents=True)
    for row_id in EXPECTED_AUDIT_ROWS:
        (evidence_root / f"{row_id}.json").write_text(
            json.dumps({"schema": "h3-context-audit-probe/1", "row": row_id, "status": "PASS"}),
            encoding="utf-8",
        )
    planning = repo_root / ".planning"
    planning.mkdir()
    fixture_sbom = repo_root / "governance" / "contracts" / "sbom.spdx.json"
    fixture_sbom.parent.mkdir(parents=True)
    fixture_sbom.write_text(
        json.dumps(
            {
                "packages": [
                    {
                        "name": "demo",
                        "versionInfo": "1.2.3",
                        "externalRefs": [
                            {
                                "referenceType": "purl",
                                "referenceLocator": "pkg:pypi/demo@1.2.3",
                            }
                        ],
                    },
                    {
                        "name": "ComfyUI",
                        "versionInfo": "0.30.0+c44dea18809e3ca0e12e25cbe6938c2c45a29c9d",
                        "comment": "host_owned_not_bundled",
                        "primaryPackagePurpose": "FRAMEWORK",
                        "externalRefs": [
                            {
                                "referenceType": "purl",
                                "referenceLocator": (
                                    "pkg:github/Comfy-Org/ComfyUI@"
                                    "c44dea18809e3ca0e12e25cbe6938c2c45a29c9d"
                                ),
                            }
                        ],
                    },
                    {
                        "name": "Ollama",
                        "versionInfo": "user-selected-qualified-version",
                        "comment": "external_user_managed_not_bundled",
                        "primaryPackagePurpose": "APPLICATION",
                        "externalRefs": [
                            {
                                "referenceType": "purl",
                                "referenceLocator": "pkg:github/ollama/ollama",
                            }
                        ],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    advisory = query_osv(
        fixture_sbom,
        transport=lambda *_args: json.dumps({"results": [{"vulns": []}, {"vulns": []}]}).encode(),
    )
    report_values: dict[str, dict[str, object]] = {
        "build": {
            "schema": "h3-context-build-gate/1",
            "status": "PASS",
            "artifacts": [
                {
                    "kind": kind,
                    "filename": path.name,
                    "sha256": _sha256(path).removeprefix("sha256:"),
                    "bytes": path.stat().st_size,
                }
                for kind, path in zip(
                    ("sdist", "direct_wheel", "wheel_from_sdist"), artifacts, strict=True
                )
            ],
        },
        "frontend": {
            "schema": "h3-context-frontend-build/1",
            "status": "PASS",
            "bundle_sha256": hashlib.sha256(b"bundle").hexdigest(),
            "bundle_bytes": 6,
        },
        "registry": {
            "schema": "h3-context-registry-payload/1",
            "status": "PASS",
            "entries": [
                {
                    "path": "comfyui_h3_context/web/h3-context-sidebar.js",
                    "sha256": "sha256:" + hashlib.sha256(b"bundle").hexdigest(),
                    "size": 6,
                }
            ],
        },
        "release": {
            "schema": "h3-context-release-matrix/1",
            "status": "PASS",
            "artifact_lanes": [
                {
                    "artifact": "sdist",
                    "sha256": _sha256(artifacts[0]).removeprefix("sha256:"),
                },
                {
                    "artifact": "wheel",
                    "sha256": _sha256(artifacts[1]).removeprefix("sha256:"),
                },
            ],
        },
        "advisory": advisory,
        "m15_18_supported": {"schema": "h3-context-host-e2e/1", "status": "PASS"},
        "m15_18_latest": {"schema": "h3-context-host-e2e/1", "status": "PASS"},
    }
    for kind, value in report_values.items():
        name = f"260813-M16-03_{kind.upper()}_REPORT_FIXTURE.json"
        if kind.startswith("m15_18"):
            name = f"260813-M15-18_{kind.upper()}_REPORT.json"
        (planning / name).write_text(json.dumps(value), encoding="utf-8")
    supported_path = planning / "260813-M15-18_M15_18_SUPPORTED_REPORT.json"
    latest_path = planning / "260813-M15-18_M15_18_LATEST_REPORT.json"
    final_value = {
        "schema": "h3-context-m15-18-final-acceptance/1",
        "status": "PASS",
        "candidate": EXPECTED_BASE,
        "evidence": {
            "supported_report": {
                "path": supported_path.relative_to(repo_root).as_posix(),
                "sha256": _sha256(supported_path).removeprefix("sha256:"),
            },
            "latest_coinstall_report": {
                "path": latest_path.relative_to(repo_root).as_posix(),
                "sha256": _sha256(latest_path).removeprefix("sha256:"),
            },
        },
    }
    (planning / "260813-M15-18_M15_18_FINAL_REPORT.json").write_text(
        json.dumps(final_value), encoding="utf-8"
    )

    document = _document(repo_root)
    return repo_root, document, artifacts[0], evidence_root / "AUD-AUTH-01.json"


def _git(repo_root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_expected_rows_cover_the_finalized_checklist() -> None:
    assert len(EXPECTED_AUDIT_ROWS) == 73
    assert EXPECTED_AUDIT_ROWS[0] == "AUD-AUTH-01"
    assert EXPECTED_AUDIT_ROWS[-1] == "AUD-CLOSE-06"
    assert len(set(EXPECTED_AUDIT_ROWS)) == len(EXPECTED_AUDIT_ROWS)


def test_closed_document_validates_and_fingerprint_is_deterministic(tmp_path: Path) -> None:
    repo_root, document, _, _ = _bound_document(tmp_path)
    assert (
        validate_audit(document, repo_root=repo_root, expected_candidate=CANDIDATE)
        == document["fingerprint"]
    )
    assert canonical_fingerprint(document) == document["fingerprint"]


def test_loader_rejects_duplicate_members_and_nonfinite_numbers(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema":"h3-context-m16-03-release-audit/1","schema":"again"}',
        encoding="utf-8",
    )
    with pytest.raises(AuditError, match="duplicate JSON member"):
        load_audit(duplicate, ROOT)

    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text(
        '{"schema":"h3-context-m16-03-release-audit/1","value":NaN}',
        encoding="utf-8",
    )
    with pytest.raises(AuditError, match="non-finite JSON"):
        load_audit(nonfinite, ROOT)


def test_closed_schema_rejects_unknown_top_level_or_row_fields(tmp_path: Path) -> None:
    repo_root, document, _, _ = _bound_document(tmp_path)
    mutations: tuple[tuple[Callable[[dict[str, object]], None], str], ...] = (
        (lambda value: value.update({"private_path": "C:/secret"}), "unknown/private"),
        (
            lambda value: _records(value, "rows")[0].update({"extra": True}),
            "unknown/private",
        ),
        (
            lambda value: _discard_record_key(value, "rows", "started_at"),
            "started_at",
        ),
    )
    for mutation, expected in mutations:
        mutated = copy.deepcopy(document)
        mutation(mutated)
        with pytest.raises(AuditError, match=expected):
            validate_audit(mutated, repo_root=repo_root, expected_candidate=CANDIDATE)


def test_candidate_base_clean_identity_and_fingerprint_are_bound(tmp_path: Path) -> None:
    repo_root, document, _, _ = _bound_document(tmp_path)
    mutations: tuple[Callable[[dict[str, object]], None], ...] = (
        lambda value: value.update({"base": "b" * 40}),
        lambda value: value.update({"candidate": "not-a-commit"}),
        lambda value: value.update({"public_worktree_clean": False}),
        lambda value: value.update({"fingerprint": "sha256:" + "f" * 64}),
    )
    for mutation in mutations:
        mutated = copy.deepcopy(document)
        mutation(mutated)
        with pytest.raises(AuditError, match="base|candidate|clean|fingerprint"):
            validate_audit(mutated, repo_root=repo_root, expected_candidate=CANDIDATE)


def test_artifact_license_privacy_and_open_box_joins_fail_closed(tmp_path: Path) -> None:
    repo_root, document, _, _ = _bound_document(tmp_path)
    mutations: tuple[tuple[Callable[[dict[str, object]], None], str], ...] = (
        (
            lambda value: _records(value, "artifacts")[0].update({"sha256": "not-a-hash"}),
            "artifact",
        ),
        (
            lambda value: _records(value, "rows")[0].update({"evidence_refs": []}),
            "evidence",
        ),
        (
            lambda value: _records(value, "rows")[0].update({"observed": "raw prompt content"}),
            "forbidden",
        ),
        (
            lambda value: _records(value, "rows")[-1].update({"criteria": []}),
            "criteria",
        ),
        (
            lambda value: _records(value, "rows")[-1].update({"criteria": ["AC1"]}),
            "criteria",
        ),
        (
            lambda value: _records(value, "rows")[0].update(
                {"started_at": "2026-08-13T06:00:02+08:00"}
            ),
            "time",
        ),
    )
    for mutation, expected in mutations:
        mutated = copy.deepcopy(document)
        mutation(mutated)
        with pytest.raises(AuditError, match=expected):
            validate_audit(mutated, repo_root=repo_root, expected_candidate=CANDIDATE)


def test_forbidden_membership_and_private_values_are_rejected(tmp_path: Path) -> None:
    repo_root, document, _, _ = _bound_document(tmp_path)
    mutations: tuple[Callable[[dict[str, object]], None], ...] = (
        lambda value: _records(value, "artifacts")[0].update({"path": ".planning/private.json"}),
        lambda value: _records(value, "rows")[0].update({"subject": "C:/Users/private/media.mp4"}),
        lambda value: _records(value, "rows")[0].update(
            {"command": "curl https://provider.invalid/upload?token=secret"}
        ),
    )
    for mutation in mutations:
        mutated = copy.deepcopy(document)
        mutation(mutated)
        with pytest.raises(AuditError, match="forbidden|artifact"):
            validate_audit(mutated, repo_root=repo_root, expected_candidate=CANDIDATE)


def test_top_level_pass_cannot_override_failed_or_missing_rows(tmp_path: Path) -> None:
    repo_root, document, _, _ = _bound_document(tmp_path)
    mutations: tuple[tuple[Callable[[dict[str, object]], None], str], ...] = (
        (
            lambda value: _records(value, "rows")[0].update({"status": "FAIL"}),
            "status",
        ),
        (lambda value: value.update({"rows": []}), "rows"),
        (
            lambda value: _records(value, "rows")[0].update({"reviewer_disposition": "BLOCKED"}),
            "reviewer",
        ),
        (
            lambda value: _records(value, "rows")[0].update({"reviewer_disposition": "PENDING"}),
            "reviewer",
        ),
        (
            lambda value: _records(value, "rows")[0].update(
                {"command": "Explicit candidate-bound probe required"}
            ),
            "skeleton",
        ),
    )
    for mutation, expected in mutations:
        mutated = copy.deepcopy(document)
        mutation(mutated)
        with pytest.raises(AuditError, match=expected):
            validate_audit(mutated, repo_root=repo_root, expected_candidate=CANDIDATE)


def test_builder_binds_current_candidate_and_fail_closed_artifact_state(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    artifact_root = repo_root / ".tmp" / "m16-03-audit-artifacts"
    artifact_root.mkdir(parents=True)
    (repo_root / "LICENSE").write_text("fixture license", encoding="utf-8")
    document = build_audit_document(
        repo_root=repo_root,
        candidate=CANDIDATE,
        base=EXPECTED_BASE,
        artifact_root=artifact_root,
    )
    assert document["schema"] == "h3-context-m16-03-release-audit/1"
    assert document["candidate"] == CANDIDATE
    assert document["base"] == EXPECTED_BASE
    assert document["status"] in {"PASS", "FAIL", "BLOCKED"}
    assert isinstance(document["rows"], list)
    assert len(document["rows"]) == len(EXPECTED_AUDIT_ROWS)
    assert all(
        row["status"] == "BLOCKED" and row["reviewer_disposition"] == "PENDING"
        for row in cast(list[dict[str, object]], document["rows"])
    )
    with pytest.raises(AuditError, match="artifact root.*repository"):
        build_audit_document(
            repo_root=repo_root,
            candidate=CANDIDATE,
            base=EXPECTED_BASE,
            artifact_root=tmp_path / "outside",
        )


def test_true_builder_uses_typed_evidence_matrix_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, source_document, _, _ = _bound_document(tmp_path)
    artifact_root = repo_root / ".tmp" / "m16-03-audit-artifacts"
    matrix_path = repo_root / ".tmp" / "m16-03-evidence-matrix.json"
    matrix_path.write_text(
        json.dumps(
            {
                "schema": "h3-context-m16-03-evidence-matrix/1",
                "base": EXPECTED_BASE,
                "candidate": CANDIDATE,
                "candidate_tree": TREE,
                "rows": source_document["rows"],
                "non_claims": source_document["non_claims"],
            }
        ),
        encoding="utf-8",
    )
    report_paths: dict[str, Path] = {
        cast(str, report["kind"]): repo_root / cast(str, report["path"])
        for report in cast(list[dict[str, object]], source_document["reports"])
    }

    def git_text(_repo_root: Path, *arguments: str) -> str:
        if arguments == ("rev-parse", "HEAD"):
            return CANDIDATE
        if arguments == ("rev-parse", f"{CANDIDATE}^{{tree}}"):
            return TREE
        if arguments == ("branch", "--show-current"):
            return "dev"
        if arguments == ("status", "--porcelain=v1", "--untracked-files=all"):
            return ""
        raise AssertionError(arguments)

    monkeypatch.setattr(audit_module, "_git_text", git_text)
    document = build_audit_document(
        repo_root=repo_root,
        candidate=CANDIDATE,
        base=EXPECTED_BASE,
        artifact_root=artifact_root,
        evidence_matrix=matrix_path,
        report_paths=report_paths,
    )
    assert document["status"] == "PASS"
    assert document["release_ready"] is False
    assert len(cast(list[object], document["reports"])) == 8

    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    cast(list[object], matrix["rows"]).pop()
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    blocked = build_audit_document(
        repo_root=repo_root,
        candidate=CANDIDATE,
        base=EXPECTED_BASE,
        artifact_root=artifact_root,
        evidence_matrix=matrix_path,
        report_paths=report_paths,
    )
    assert blocked["status"] == "BLOCKED"
    assert all(row["status"] == "BLOCKED" for row in cast(list[dict[str, object]], blocked["rows"]))

    matrix["rows"] = copy.deepcopy(source_document["rows"])
    cast(list[dict[str, object]], matrix["rows"])[0]["evidence_refs"] = [
        "tests/evidence/missing.json"
    ]
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    blocked = build_audit_document(
        repo_root=repo_root,
        candidate=CANDIDATE,
        base=EXPECTED_BASE,
        artifact_root=artifact_root,
        evidence_matrix=matrix_path,
        report_paths=report_paths,
    )
    assert blocked["status"] == "BLOCKED"
    assert all(
        row["command"] == "Explicit candidate-bound probe required"
        for row in cast(list[dict[str, object]], blocked["rows"])
    )


def test_passing_document_binds_exact_owned_artifact_and_evidence_bytes(tmp_path: Path) -> None:
    repo_root, document, _, _ = _bound_document(tmp_path)
    assert (
        validate_audit(document, repo_root=repo_root, expected_candidate=CANDIDATE)
        == document["fingerprint"]
    )


def test_passing_document_rejects_changed_or_extra_artifact_bytes(tmp_path: Path) -> None:
    repo_root, document, artifact, _ = _bound_document(tmp_path)
    artifact.write_bytes(b"changed artifact bytes")
    with pytest.raises(AuditError, match="artifact.*(?:sha256|bytes|size)"):
        validate_audit(document, repo_root=repo_root, expected_candidate=CANDIDATE)

    repo_root, document, _, _ = _bound_document(tmp_path / "extra")
    extra = repo_root / ".tmp" / "m16-03-audit-artifacts" / "extra.bin"
    extra.write_bytes(b"unlisted")
    with pytest.raises(AuditError, match="artifact.*(?:extra|unlisted|membership)"):
        validate_audit(document, repo_root=repo_root, expected_candidate=CANDIDATE)

    repo_root, document, artifact, _ = _bound_document(tmp_path / "fake")
    artifact.write_bytes(b"arbitrary artifact bytes")
    artifact_row = cast(list[dict[str, object]], document["artifacts"])[0]
    artifact_row["sha256"] = _sha256(artifact)
    artifact_row["size"] = artifact.stat().st_size
    build_path = next(
        repo_root / cast(str, report["path"])
        for report in cast(list[dict[str, object]], document["reports"])
        if report["kind"] == "build"
    )
    build = json.loads(build_path.read_text(encoding="utf-8"))
    build["artifacts"][0]["sha256"] = _sha256(artifact).removeprefix("sha256:")
    build["artifacts"][0]["bytes"] = artifact.stat().st_size
    build_path.write_text(json.dumps(build), encoding="utf-8")
    build_ref = next(
        report
        for report in cast(list[dict[str, object]], document["reports"])
        if report["kind"] == "build"
    )
    build_ref["sha256"] = _sha256(build_path)
    build_ref["size"] = build_path.stat().st_size
    document["fingerprint"] = canonical_fingerprint(document)
    with pytest.raises(AuditError, match="valid required archive"):
        validate_audit(document, repo_root=repo_root, expected_candidate=CANDIDATE)


def test_passing_document_rejects_changed_or_missing_evidence_bytes(tmp_path: Path) -> None:
    repo_root, document, _, evidence = _bound_document(tmp_path)
    evidence.write_bytes(b"changed evidence")
    with pytest.raises(AuditError, match="evidence.*(?:sha256|bytes)"):
        validate_audit(document, repo_root=repo_root, expected_candidate=CANDIDATE)

    repo_root, document, _, evidence = _bound_document(tmp_path / "missing")
    evidence.unlink()
    with pytest.raises(AuditError, match="evidence.*(?:missing|regular|file)"):
        validate_audit(document, repo_root=repo_root, expected_candidate=CANDIDATE)


def test_candidate_tree_is_part_of_the_closed_schema(tmp_path: Path) -> None:
    repo_root, document, _, _ = _bound_document(tmp_path)
    assert (
        validate_audit(
            document,
            repo_root=repo_root,
            expected_candidate=CANDIDATE,
            expected_candidate_tree=TREE,
        )
        == document["fingerprint"]
    )

    missing = copy.deepcopy(document)
    missing.pop("candidate_tree")
    missing["fingerprint"] = canonical_fingerprint(missing)
    with pytest.raises(AuditError, match="candidate_tree|candidate tree"):
        validate_audit(missing, repo_root=repo_root, expected_candidate=CANDIDATE)

    mismatch = copy.deepcopy(document)
    mismatch["candidate_tree"] = "c" * 40
    mismatch["fingerprint"] = canonical_fingerprint(mismatch)
    with pytest.raises(AuditError, match="candidate tree"):
        validate_audit(
            mismatch,
            repo_root=repo_root,
            expected_candidate=CANDIDATE,
            expected_candidate_tree=TREE,
        )


def test_release_finding_and_release_ready_are_fail_closed(tmp_path: Path) -> None:
    repo_root, document, _, _ = _bound_document(tmp_path)
    lic_row = _records(document, "rows")[21]
    assert lic_row["id"] == "AUD-LIC-02"
    assert lic_row["finding_disposition"] == "RELEASE_BLOCKER"
    assert lic_row["finding_id"] == "M16-03-F01"
    assert lic_row["finding_owner"] == "M16-04"
    assert document["release_ready"] is False

    mutations: tuple[tuple[Callable[[dict[str, object]], None], str], ...] = (
        (lambda value: value.update({"release_ready": True}), "release_ready"),
        (
            lambda value: _records(value, "rows")[21].update(
                {"finding_disposition": "NONE", "finding_id": None, "finding_owner": None}
            ),
            "M16-03-F01",
        ),
        (
            lambda value: _records(value, "rows")[0].update({"reviewer_disposition": "PENDING"}),
            "reviewer",
        ),
    )
    for mutation, expected in mutations:
        mutated = copy.deepcopy(document)
        mutation(mutated)
        mutated["fingerprint"] = canonical_fingerprint(mutated)
        with pytest.raises(AuditError, match=expected):
            validate_audit(mutated, repo_root=repo_root, expected_candidate=CANDIDATE)


def test_exact_artifact_and_typed_report_inventory_is_required(tmp_path: Path) -> None:
    repo_root, document, _, _ = _bound_document(tmp_path)
    assert {item["kind"] for item in _records(document, "artifacts")} == {
        "sdist",
        "direct_wheel",
        "wheel_from_sdist",
    }
    mutations: tuple[tuple[Callable[[dict[str, object]], None], str], ...] = (
        (
            lambda value: _records(value, "artifacts")[0].update({"kind": "fixture"}),
            "artifact kind",
        ),
        (lambda value: _discard_last(value, "artifacts"), "artifact kind"),
        (lambda value: _discard_last(value, "reports"), "report kind"),
        (
            lambda value: _records(value, "reports")[0].update({"schema": "arbitrary/1"}),
            "schema",
        ),
    )
    for mutation, expected in mutations:
        mutated = copy.deepcopy(document)
        mutation(mutated)
        mutated["fingerprint"] = canonical_fingerprint(mutated)
        with pytest.raises(AuditError, match=expected):
            validate_audit(mutated, repo_root=repo_root, expected_candidate=CANDIDATE)


def test_evidence_allowlist_privacy_and_unique_probe_ownership(tmp_path: Path) -> None:
    repo_root, document, _, _ = _bound_document(tmp_path)
    rows = _records(document, "rows")
    outside = repo_root / "random.txt"
    outside.write_text("not approved evidence", encoding="utf-8")
    private = repo_root / "tests" / "evidence" / "private.json"
    sensitive_name = "api_" + "key"
    private.write_text(json.dumps({sensitive_name: "secret"}), encoding="utf-8")
    mutated = copy.deepcopy(document)
    _records(mutated, "rows")[0].update(
        {
            "evidence_refs": ["random.txt"],
            "evidence_sha256": _combined_evidence_sha256(repo_root, ["random.txt"]),
        }
    )
    mutated["fingerprint"] = canonical_fingerprint(mutated)
    with pytest.raises(AuditError, match="allowlist"):
        validate_audit(mutated, repo_root=repo_root, expected_candidate=CANDIDATE)

    mutated = copy.deepcopy(document)
    _records(mutated, "rows")[0].update(
        {
            "evidence_refs": ["tests/evidence/private.json"],
            "evidence_sha256": _combined_evidence_sha256(
                repo_root, ["tests/evidence/private.json"]
            ),
        }
    )
    mutated["fingerprint"] = canonical_fingerprint(mutated)
    with pytest.raises(AuditError, match="sensitive|secret|private"):
        validate_audit(mutated, repo_root=repo_root, expected_candidate=CANDIDATE)

    mutated = copy.deepcopy(document)
    mutated_rows = _records(mutated, "rows")
    mutated_rows[1].update(
        {
            "probe_id": rows[0]["probe_id"],
            "probe_owner": rows[0]["probe_owner"],
            "command": rows[0]["command"],
            "evidence_refs": rows[0]["evidence_refs"],
            "evidence_sha256": rows[0]["evidence_sha256"],
        }
    )
    mutated["fingerprint"] = canonical_fingerprint(mutated)
    with pytest.raises(AuditError, match="unique|ownership|shared"):
        validate_audit(mutated, repo_root=repo_root, expected_candidate=CANDIDATE)


def test_json_depth_and_aggregate_limits_fail_closed(tmp_path: Path) -> None:
    nested: object = "leaf"
    for _ in range(80):
        nested = {"node": nested}
    path = tmp_path / "deep.json"
    path.write_text(json.dumps(nested), encoding="utf-8")
    with pytest.raises(AuditError, match="depth|aggregate"):
        load_audit(path)

    repo_root, document, _, _ = _bound_document(tmp_path / "aggregate")
    cast(list[dict[str, object]], document["rows"])[0]["observed"] = "x" * 5000
    document["fingerprint"] = canonical_fingerprint(document)
    with pytest.raises(AuditError, match="bounded|aggregate"):
        validate_audit(document, repo_root=repo_root, expected_candidate=CANDIDATE)


def test_osv_querybatch_is_one_bounded_ordered_post_with_content_free_summary(
    tmp_path: Path,
) -> None:
    sbom = tmp_path / "sbom.json"
    sbom.write_text(
        json.dumps(
            {
                "packages": [
                    {
                        "name": "demo",
                        "versionInfo": "1.2.3",
                        "externalRefs": [
                            {
                                "referenceType": "purl",
                                "referenceLocator": "pkg:pypi/demo@1.2.3",
                            }
                        ],
                    },
                    {
                        "name": "widget",
                        "versionInfo": "4.5.6",
                        "externalRefs": [
                            {
                                "referenceType": "purl",
                                "referenceLocator": "pkg:npm/widget@4.5.6",
                            }
                        ],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    calls: list[tuple[str, bytes, float, int]] = []

    def transport(url: str, body: bytes, timeout: float, maximum: int) -> bytes:
        calls.append((url, body, timeout, maximum))
        request = json.loads(body)
        assert request == {
            "queries": [
                {"package": {"ecosystem": "PyPI", "name": "demo"}, "version": "1.2.3"},
                {"package": {"ecosystem": "npm", "name": "widget"}, "version": "4.5.6"},
            ]
        }
        return json.dumps({"results": [{"vulns": []}, {"vulns": [{"id": "OSV-2026-1"}]}]}).encode()

    result = query_osv(sbom, transport=transport)
    assert len(calls) == 1
    assert calls[0][0] == "https://api.osv.dev/v1/querybatch"
    assert result["schema"] == "h3-context-osv-advisory/1"
    assert result["source"] == "OSV_QUERYBATCH"
    assert [entry["package"] for entry in cast(list[dict[str, object]], result["results"])] == [
        "demo",
        "widget",
    ]
    assert result["raw_response_retained"] is False


def test_osv_querybatch_blocks_pagination_cardinality_and_sensitive_response(
    tmp_path: Path,
) -> None:
    sbom = tmp_path / "sbom.json"
    sbom.write_text(
        json.dumps(
            {
                "packages": [
                    {
                        "name": "demo",
                        "versionInfo": "1.2.3",
                        "externalRefs": [
                            {
                                "referenceType": "purl",
                                "referenceLocator": "pkg:pypi/demo@1.2.3",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    responses: tuple[dict[str, object], ...] = (
        {"results": [{"vulns": [], "next_page_token": "more"}]},
        {"results": []},
        {"results": [{"vulns": [{"id": "api_" + "key=secret"}]}]},
    )
    for response in responses:
        result = query_osv(
            sbom, transport=lambda *_args, value=response: json.dumps(value).encode()
        )
        assert result["status"] == "BLOCKED"
        assert result["raw_response_retained"] is False
        assert result["results"] == []
        assert result["first_error"] in {
            "OSV_RESPONSE_INCOMPLETE_OR_PAGINATED",
            "OSV_RESPONSE_PRIVATE_OR_MALFORMED",
        }


def test_current_sbom_queries_exact_comfyui_and_noto_commits_and_retains_ollama_nonclaim() -> None:
    calls: list[dict[str, object]] = []

    def transport(_url: str, body: bytes, _timeout: float, _maximum: int) -> bytes:
        request = cast(dict[str, object], json.loads(body))
        calls.append(request)
        queries = cast(list[dict[str, object]], request["queries"])
        assert {"commit": "b323a345bbbfb2f3a95b5b73b68eb7919a26515e"} in queries
        assert {"commit": "c4a321e123-e4d4ff315f-57f4e0adf2-94fe3a95be".replace("-", "")} in queries
        assert not any("ollama" in json.dumps(query).casefold() for query in queries)
        return json.dumps({"results": [{"vulns": []} for _ in queries]}).encode()

    result = query_osv(
        ROOT / "governance" / "contracts" / "sbom.spdx.json",
        transport=transport,
    )
    assert result["status"] == "PASS"
    assert len(calls) == 1
    assert result["request_count"] == result["query_count"]
    assert cast(str, result["query_set_sha256"]).startswith("sha256:")
    assert cast(str, result["sbom_sha256"]).startswith("sha256:")
    assert result["nonclaims"] == [
        {
            "identity": "pkg:github/ollama/ollama",
            "package": "Ollama",
            "version": "user-selected-qualified-version",
            "reason": "EXTERNAL_USER_MANAGED_NOT_BUNDLED",
            "finding_disposition": "ACCEPTED_NONCLAIM",
            "finding_owner": "M16-04",
            "claim_cap": "NO_CURRENT_ADVISORY_ASSERTION_FOR_USER_SELECTED_VERSION",
        }
    ]


def test_osv_rejects_arbitrary_github_or_generic_unversioned_exclusions(
    tmp_path: Path,
) -> None:
    accepted_commit = "c44dea18809e3ca0e12e25cbe6938c2c45a29c9d"
    current_commit = "b323a345bbbfb2f3a95b5b73b68eb7919a26515e"
    invalid_packages = (
        {
            "name": "Other",
            "versionInfo": accepted_commit,
            "comment": "host_owned_not_bundled",
            "primaryPackagePurpose": "FRAMEWORK",
            "externalRefs": [
                {
                    "referenceType": "purl",
                    "referenceLocator": f"pkg:github/example/other@{accepted_commit}",
                }
            ],
        },
        {
            "name": "ComfyUI",
            "versionInfo": "0.30.0+not-a-commit",
            "comment": "host_owned_not_bundled",
            "primaryPackagePurpose": "FRAMEWORK",
            "externalRefs": [
                {
                    "referenceType": "purl",
                    "referenceLocator": "pkg:github/Comfy-Org/ComfyUI@not-a-commit",
                }
            ],
        },
        {
            "name": "ComfyUI",
            "versionInfo": "0.30.0+aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "comment": "host_owned_not_bundled",
            "primaryPackagePurpose": "FRAMEWORK",
            "externalRefs": [
                {
                    "referenceType": "purl",
                    "referenceLocator": (
                        "pkg:github/Comfy-Org/ComfyUI@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                    ),
                }
            ],
        },
        {
            "name": "ComfyUI",
            "versionInfo": f"0.30.0+{accepted_commit}",
            "comment": "host_owned_not_bundled",
            "primaryPackagePurpose": "FRAMEWORK",
            "externalRefs": [
                {
                    "referenceType": "purl",
                    "referenceLocator": f"pkg:github/Comfy-Org/ComfyUI@{current_commit}",
                }
            ],
        },
        {
            "name": "ComfyUI",
            "versionInfo": f"0.32.0+{current_commit}",
            "comment": "host_owned_not_bundled",
            "primaryPackagePurpose": "FRAMEWORK",
            "externalRefs": [
                {
                    "referenceType": "purl",
                    "referenceLocator": f"pkg:github/Comfy-Org/ComfyUI@{accepted_commit}",
                }
            ],
        },
        {
            "name": "Other",
            "versionInfo": "user-selected-qualified-version",
            "comment": "external_user_managed_not_bundled",
            "primaryPackagePurpose": "APPLICATION",
            "externalRefs": [
                {
                    "referenceType": "purl",
                    "referenceLocator": "pkg:github/example/other",
                }
            ],
        },
        {
            "name": "Ollama",
            "versionInfo": "user-selected-qualified-version",
            "comment": "host_owned_not_bundled",
            "primaryPackagePurpose": "APPLICATION",
            "externalRefs": [
                {
                    "referenceType": "purl",
                    "referenceLocator": "pkg:github/ollama/ollama",
                }
            ],
        },
        {
            "name": "Noto Sans",
            "versionInfo": "NotoSans-v2.015",
            "comment": "bundled_font_runtime",
            "primaryPackagePurpose": "LIBRARY",
            "externalRefs": [
                {
                    "referenceType": "purl",
                    "referenceLocator": (
                        "pkg:github/notofonts/latin-greek-cyrillic@"
                        + "aaaaaaaaaa-aaaaaaaaaa-aaaaaaaaaa-aaaaaaaaaa".replace("-", "")
                    ),
                }
            ],
        },
        {
            "name": "Foreign Font",
            "versionInfo": "NotoSans-v2.015",
            "comment": "bundled_font_runtime",
            "primaryPackagePurpose": "LIBRARY",
            "externalRefs": [
                {
                    "referenceType": "purl",
                    "referenceLocator": (
                        "pkg:github/notofonts/latin-greek-cyrillic@"
                        + "c4a321e123-e4d4ff315f-57f4e0adf2-94fe3a95be".replace("-", "")
                    ),
                }
            ],
        },
    )
    for index, package in enumerate(invalid_packages):
        sbom = tmp_path / f"invalid-{index}.json"
        sbom.write_text(json.dumps({"packages": [package]}), encoding="utf-8")
        called = False

        def transport(*_args: object) -> bytes:
            nonlocal called
            called = True
            return b'{"results": []}'

        result = query_osv(sbom, transport=transport)
        assert result["status"] == "BLOCKED"
        assert result["first_error"] == "SBOM_ECOSYSTEM_OR_IDENTITY_UNSUPPORTED"
        assert result["request_count"] == 0
        assert called is False


def test_typed_advisory_report_binds_query_set_sbom_and_ollama_nonclaim(tmp_path: Path) -> None:
    mutations: tuple[Callable[[dict[str, object]], None], ...] = (
        lambda value: value.update({"query_set_sha256": "sha256:" + "0" * 64}),
        lambda value: _records(value, "results")[0].update(
            {"package": "not-the-requested-package"}
        ),
        lambda value: value.update({"nonclaims": []}),
    )
    for index, mutation in enumerate(mutations):
        repo_root, document, _, _ = _bound_document(tmp_path / str(index))
        advisory_ref = next(
            report for report in _records(document, "reports") if report["kind"] == "advisory"
        )
        advisory_path = repo_root / cast(str, advisory_ref["path"])
        advisory = cast(dict[str, object], json.loads(advisory_path.read_text(encoding="utf-8")))
        mutation(advisory)
        advisory_path.write_text(json.dumps(advisory), encoding="utf-8")
        advisory_ref["sha256"] = _sha256(advisory_path)
        advisory_ref["size"] = advisory_path.stat().st_size
        document["fingerprint"] = canonical_fingerprint(document)
        with pytest.raises(AuditError, match="advisory|OSV|query|nonclaim|SBOM"):
            validate_audit(document, repo_root=repo_root, expected_candidate=CANDIDATE)


def test_cli_binds_git_ancestry_exact_scope_report_ignore_and_clean_dev(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, document, _, _ = _bound_document(tmp_path)
    (repo_root / ".gitignore").write_text(".planning/\n.tmp/\n", encoding="utf-8")
    _git(repo_root, "init", "-b", "dev")
    _git(repo_root, "config", "user.email", "audit@example.invalid")
    _git(repo_root, "config", "user.name", "M16 Audit Test")
    _git(
        repo_root,
        "add",
        ".gitignore",
        "tests/evidence",
        "governance/contracts/sbom.spdx.json",
    )
    _git(repo_root, "commit", "-m", "accepted base")
    base = _git(repo_root, "rev-parse", "HEAD")
    monkeypatch.setattr(audit_module, "EXPECTED_BASE", base)
    (repo_root / "scripts").mkdir()
    (repo_root / "scripts" / "m16_03_release_audit.py").write_text("tool", encoding="utf-8")
    (repo_root / "tests" / "test_m16_03_release_audit.py").write_text("tests", encoding="utf-8")
    _git(
        repo_root,
        "add",
        "scripts/m16_03_release_audit.py",
        "tests/test_m16_03_release_audit.py",
    )
    _git(repo_root, "commit", "-m", "candidate")
    candidate = _git(repo_root, "rev-parse", "HEAD")
    candidate_tree = _git(repo_root, "rev-parse", "HEAD^{tree}")
    document["base"] = base
    document["candidate"] = candidate
    document["candidate_tree"] = candidate_tree
    for typed_report in cast(list[dict[str, object]], document["reports"]):
        typed_report["candidate"] = candidate
        typed_report["candidate_tree"] = candidate_tree
        if typed_report["kind"] == "m15_18_final":
            final_path = repo_root / cast(str, typed_report["path"])
            final_value = json.loads(final_path.read_text(encoding="utf-8"))
            final_value["candidate"] = base
            final_path.write_text(json.dumps(final_value), encoding="utf-8")
            typed_report["sha256"] = _sha256(final_path)
            typed_report["size"] = final_path.stat().st_size
    document["fingerprint"] = canonical_fingerprint(document)
    report = repo_root / ".planning" / "m16-03-result.json"
    report.parent.mkdir(exist_ok=True)
    report.write_text(json.dumps(document), encoding="utf-8")
    arguments = [
        "--candidate",
        candidate,
        "--base",
        base,
        "--artifact-root",
        str(repo_root / ".tmp" / "m16-03-audit-artifacts"),
        "--report",
        str(report),
        "--repo-root",
        str(repo_root),
    ]
    assert main(arguments) == 0
    capsys.readouterr()

    public_report = repo_root / "public-result.json"
    arguments[arguments.index(str(report))] = str(public_report)
    assert main(arguments) == 1
    assert "ignored" in capsys.readouterr().err
    arguments[arguments.index(str(public_report))] = str(report)

    document["candidate_tree"] = "d" * 40
    document["fingerprint"] = canonical_fingerprint(document)
    report.write_text(json.dumps(document), encoding="utf-8")
    assert main(arguments) == 1
    tree_error = capsys.readouterr().err
    assert "candidate/tree" in tree_error or "candidate tree" in tree_error

    document["candidate_tree"] = candidate_tree
    document["fingerprint"] = canonical_fingerprint(document)
    report.write_text(json.dumps(document), encoding="utf-8")
    (repo_root / "dirty.txt").write_text("dirty", encoding="utf-8")
    assert main(arguments) == 1
    assert "clean dev" in capsys.readouterr().err
    (repo_root / "dirty.txt").unlink()

    _git(repo_root, "checkout", "-b", "other")
    assert main(arguments) == 1
    assert "clean dev" in capsys.readouterr().err

    _git(repo_root, "checkout", "dev")
    (repo_root / "extra.py").write_text("scope drift", encoding="utf-8")
    _git(repo_root, "add", "extra.py")
    _git(repo_root, "commit", "-m", "scope drift")
    drift_candidate = _git(repo_root, "rev-parse", "HEAD")
    drift_tree = _git(repo_root, "rev-parse", "HEAD^{tree}")
    document["candidate"] = drift_candidate
    document["candidate_tree"] = drift_tree
    for typed_report in cast(list[dict[str, object]], document["reports"]):
        typed_report["candidate"] = drift_candidate
        typed_report["candidate_tree"] = drift_tree
    document["fingerprint"] = canonical_fingerprint(document)
    report.write_text(json.dumps(document), encoding="utf-8")
    arguments[arguments.index(candidate)] = drift_candidate
    assert main(arguments) == 1
    assert "exact frozen two paths" in capsys.readouterr().err
