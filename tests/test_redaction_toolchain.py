"""A candidate seed uses only an explicitly bound, unchanged parser authority."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import public_redaction as redaction

ROOT = Path(__file__).resolve().parents[1]
INPUTS = (
    "scripts/public_redaction.py",
    "scripts/public_redaction_frontend.mjs",
    "scripts/public_source_policy.py",
    "governance/contracts/public_redaction_allowlist_v1.json",
    "frontend/package.json",
    "frontend/pnpm-lock.yaml",
)
SOURCE = {"frontend/src/seed.ts": b"// M25-88: preserve guard\nexport const value = 1;\n"}


def seed(root: Path) -> Path:
    root.mkdir()
    for relative in INPUTS:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    return root


def test_real_parser_is_explicitly_bound_without_dependencies_in_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = seed(tmp_path / "seed")
    binding = redaction.make_toolchain_binding(ROOT, candidate)
    monkeypatch.setenv(redaction.TOOLCHAIN_ENV, binding)
    actual = redaction.redact_bound_blobs(SOURCE, candidate_root=candidate)
    assert actual == redaction.redact_blobs(SOURCE, toolchain_root=ROOT)
    assert actual != SOURCE
    assert not (candidate / "frontend/node_modules").exists()


def test_without_binding_standalone_seed_still_refuses_missing_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = seed(tmp_path / "seed")
    monkeypatch.delenv(redaction.TOOLCHAIN_ENV, raising=False)
    with pytest.raises(redaction.RedactionRefusal, match="P2_TOOLCHAIN_UNAVAILABLE"):
        redaction.redact_bound_blobs(SOURCE, candidate_root=candidate)


@pytest.mark.parametrize("relative", INPUTS)
def test_any_substituted_seed_tool_refuses_before_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    candidate = seed(tmp_path / "seed")
    binding = redaction.make_toolchain_binding(ROOT, candidate)
    path = candidate / relative
    path.write_bytes(path.read_bytes() + b"\n")
    monkeypatch.setenv(redaction.TOOLCHAIN_ENV, binding)
    with patch.object(redaction, "redact_blobs", side_effect=AssertionError("parser called")):
        with pytest.raises(redaction.RedactionRefusal, match="TOOLCHAIN_BINDING_REFUSED"):
            redaction.redact_bound_blobs(SOURCE, candidate_root=candidate)


def test_wrong_candidate_or_incomplete_binding_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = seed(tmp_path / "seed")
    other = seed(tmp_path / "other")
    binding = redaction.make_toolchain_binding(ROOT, candidate)
    monkeypatch.setenv(redaction.TOOLCHAIN_ENV, binding)
    with pytest.raises(redaction.RedactionRefusal, match="TOOLCHAIN_BINDING_REFUSED"):
        redaction.redact_bound_blobs(SOURCE, candidate_root=other)
    value = json.loads(binding)
    value["inputs"].pop(INPUTS[0])
    monkeypatch.setenv(redaction.TOOLCHAIN_ENV, json.dumps(value))
    with pytest.raises(redaction.RedactionRefusal, match="TOOLCHAIN_BINDING_REFUSED"):
        redaction.redact_bound_blobs(SOURCE, candidate_root=candidate)


def test_mutation_during_parser_refuses_after_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = seed(tmp_path / "seed")
    binding = redaction.make_toolchain_binding(ROOT, candidate)
    monkeypatch.setenv(redaction.TOOLCHAIN_ENV, binding)

    def mutate(blobs: Mapping[str, bytes], *, toolchain_root: Path) -> dict[str, bytes]:
        assert toolchain_root == ROOT
        path = candidate / INPUTS[1]
        path.write_bytes(path.read_bytes() + b"\n")
        return dict(blobs)

    with patch.object(redaction, "redact_blobs", side_effect=mutate):
        with pytest.raises(redaction.RedactionRefusal, match="TOOLCHAIN_BINDING_REFUSED"):
            redaction.redact_bound_blobs(SOURCE, candidate_root=candidate)


def test_cached_parse_cannot_hide_subsequent_authority_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = seed(tmp_path / "seed")
    binding = redaction.make_toolchain_binding(ROOT, candidate)
    monkeypatch.setenv(redaction.TOOLCHAIN_ENV, binding)
    redaction.redact_bound_blobs(SOURCE, candidate_root=candidate)
    (candidate / INPUTS[1]).unlink()
    with pytest.raises(redaction.RedactionRefusal, match="TOOLCHAIN_BINDING_REFUSED"):
        redaction.redact_bound_blobs(SOURCE, candidate_root=candidate)


def test_direct_orchestrator_cli_can_import_shared_binding_from_another_cwd(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/derived_artifacts.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "dry-run" in result.stdout


@pytest.mark.parametrize("mutation", ["duplicate", "unknown", "empty", "oversized", "relative"])
def test_closed_bounded_binding_refuses_invalid_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    candidate = seed(tmp_path / "seed")
    binding = redaction.make_toolchain_binding(ROOT, candidate)
    value = json.loads(binding)
    if mutation == "duplicate":
        binding = binding[:-1] + ',"schema":"h3-context-redaction-toolchain-binding/1"}'
    elif mutation == "unknown":
        value["unexpected"] = True
        binding = json.dumps(value)
    elif mutation == "relative":
        value["root"] = "."
        binding = json.dumps(value)
    elif mutation == "empty":
        binding = ""
    else:
        binding = " " * (16 * 1024 + 1)
    monkeypatch.setenv(redaction.TOOLCHAIN_ENV, binding)
    with pytest.raises(redaction.RedactionRefusal, match="TOOLCHAIN_BINDING_REFUSED"):
        redaction.redact_bound_blobs(SOURCE, candidate_root=candidate)


def test_orchestrator_clears_ambient_authority_for_unselected_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import derived_artifacts

    monkeypatch.setenv(redaction.TOOLCHAIN_ENV, "untrusted ambient value")
    command = [
        sys.executable,
        "-c",
        f"import os; print(os.environ.get({redaction.TOOLCHAIN_ENV!r}))",
    ]
    result = derived_artifacts._run(command, cwd=tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == "None"
    result = derived_artifacts._run(command, cwd=tmp_path, toolchain_binding="explicit")
    assert result.returncode == 0
    assert result.stdout.strip() == "explicit"


def test_loaded_transform_identity_is_not_replaced_by_a_new_binding(tmp_path: Path) -> None:
    candidate = seed(tmp_path / "seed")
    with patch.object(redaction, "_RUNNING_BINDING_INPUTS", {}):
        with pytest.raises(redaction.RedactionRefusal, match="TOOLCHAIN_BINDING_REFUSED"):
            redaction.make_toolchain_binding(ROOT, candidate)


@pytest.mark.parametrize("generator", ["build_provenance", "supply_chain_manifest"])
def test_generators_keep_their_typed_binding_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, generator: str
) -> None:
    from scripts import build_provenance, supply_chain_manifest

    candidate = seed(tmp_path / "seed")
    source = candidate / "frontend/src/seed.ts"
    source.parent.mkdir()
    source.write_bytes(SOURCE["frontend/src/seed.ts"])
    monkeypatch.setenv(redaction.TOOLCHAIN_ENV, "invalid")
    if generator == "build_provenance":
        monkeypatch.setattr(build_provenance, "ROOT", candidate)
        monkeypatch.setattr(build_provenance, "_source_input_paths", lambda: (source,))
        with pytest.raises(build_provenance.ProvenanceEmissionError, match="public form"):
            build_provenance._source_inputs_digest()
    else:
        monkeypatch.setattr(supply_chain_manifest, "ROOT", candidate)
        monkeypatch.setattr(supply_chain_manifest, "_build_input_paths", lambda: (source,))
        with pytest.raises(supply_chain_manifest.SupplyChainError, match="public form"):
            supply_chain_manifest._build_inputs()
