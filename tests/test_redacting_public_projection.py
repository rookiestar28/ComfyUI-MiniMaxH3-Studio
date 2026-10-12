"""Exact transformed Git blobs, immutable tool authority and paired source digests."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from test_public_release_controller import _commit, _fixture

from scripts import build_provenance, public_projection, public_redaction, supply_chain_manifest

ROOT = Path(__file__).resolve().parents[1]
TOOL_INPUTS = (
    "scripts/public_redaction.py",
    "scripts/public_redaction_frontend.mjs",
    "scripts/public_source_policy.py",
    "governance/contracts/public_redaction_allowlist_v1.json",
    "frontend/package.json",
    "frontend/pnpm-lock.yaml",
)


@pytest.fixture
def repository() -> Any:
    (ROOT / ".tmp").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="redacting-projection-", dir=ROOT / ".tmp"
    ) as directory:
        root = Path(directory)
        parent, _ = _fixture(root)
        body = b'# M25-88: stable guard\r\nvalue = "M25-88"\r\n# HC-13: stable boundary\r\n'
        (root / "scripts/redaction_fixture.py").write_bytes(body)
        yield root, parent, _commit(root), body


def test_projection_emits_public_blobs_and_content_free_v2_receipt(repository: Any) -> None:
    root, parent, source, body = repository
    before = public_projection.snapshot(root)
    result = public_projection.project(
        root, source=source, parent=parent, output=root / ".tmp/projection", local_ref="safe-public"
    )
    path = "scripts/redaction_fixture.py"
    public = public_projection.git(root, "show", str(result["public_commit"]) + ":" + path)
    assert public == public_redaction.redact_blob(path, body)
    assert public != body and b'value = "M25-88"' in public
    assert result["schema"] == "h3-context-public-projection/2"
    assert result["transform"] == public_redaction.TRANSFORM_ID
    binding = cast(dict[str, dict[str, Any]], result["paths"])[path]
    assert binding["source_sha256"] == hashlib.sha256(body).hexdigest()
    assert binding["public_sha256"] == binding["sha256"] == hashlib.sha256(public).hexdigest()
    assert binding["changed_lines"] == 2
    assert public_projection.snapshot(root) == before


@pytest.mark.parametrize("path", TOOL_INPUTS)
def test_projection_refuses_candidate_tool_substitution_before_ref(
    repository: Any, path: str
) -> None:
    root, parent, _, _ = repository
    target = root / path
    target.write_bytes(target.read_bytes() + b"\n")
    source = _commit(root)
    before = public_projection.snapshot(root)
    with pytest.raises(public_projection.ProjectionError, match="tool authority"):
        public_projection.project(
            root,
            source=source,
            parent=parent,
            output=root / ".tmp/unsafe",
            local_ref="unsafe-public",
        )
    assert public_projection.snapshot(root) == before
    assert public_projection.git(root, "for-each-ref", "refs/heads/unsafe-public") == b""


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
def test_changed_lines_count_physical_lines_once_and_preserve_unicode_literals(
    newline: bytes,
) -> None:
    source = newline.join([b"# M25-88 HC-13: guard", 'value = "a\u2028b\u2029c"'.encode(), b""])
    public = newline.join([b"# guard", 'value = "a\u2028b\u2029c"'.encode(), b""])
    assert public_projection._changed_lines(source, public) == 1
    with pytest.raises(public_projection.ProjectionError, match="line breaks"):
        public_projection._changed_lines(source, public + newline)


def test_projection_refuses_changed_tool_after_transform_before_ref(repository: Any) -> None:
    root, parent, source, _ = repository
    actual_read = Path.read_bytes
    actual_redact = public_redaction.redact_blobs
    changed = False

    def read(path: Path) -> bytes:
        body = actual_read(path)
        return body + b"\n" if changed and path == ROOT / TOOL_INPUTS[3] else body

    def transform(*args: Any, **kwargs: Any) -> Any:
        nonlocal changed
        result = actual_redact(*args, **kwargs)
        changed = True
        return result

    with (
        patch.object(Path, "read_bytes", read),
        patch.object(public_projection, "redact_blobs", transform),
    ):
        with pytest.raises(public_projection.ProjectionError, match="tool authority"):
            public_projection.project(
                root,
                source=source,
                parent=parent,
                output=root / ".tmp/moving-tool",
                local_ref="moving-tool",
            )
    assert public_projection.git(root, "for-each-ref", "refs/heads/moving-tool") == b""


def test_projection_refuses_wrong_transformed_git_blob_before_ref(repository: Any) -> None:
    root, parent, source, body = repository
    actual_git = public_projection.git
    original = public_projection.entries(root, source)["scripts/redaction_fixture.py"][2]
    expected = public_redaction.redact_blob("scripts/redaction_fixture.py", body)

    def wrong_blob(cwd: Path, *args: str, **kwargs: Any) -> bytes:
        if args == ("hash-object", "-w", "--stdin") and kwargs.get("data") == expected:
            return original.encode() + b"\n"
        return actual_git(cwd, *args, **kwargs)

    with patch.object(public_projection, "git", wrong_blob):
        with pytest.raises(public_projection.ProjectionError, match="blob bytes"):
            public_projection.project(
                root,
                source=source,
                parent=parent,
                output=root / ".tmp/wrong-blob",
                local_ref="wrong-blob",
            )
    assert public_projection.git(root, "for-each-ref", "refs/heads/wrong-blob") == b""


@pytest.mark.parametrize("module", [build_provenance, supply_chain_manifest])
def test_source_binding_uses_public_form_bytes_and_size_and_still_binds_code(
    module: Any, tmp_path: Path
) -> None:
    path = tmp_path / "scripts/input.py"
    path.parent.mkdir()
    path.write_bytes(b"# M25-88: stable guard\nvalue = 1\n")
    allowlist = tmp_path / public_redaction.ALLOWLIST_PATH
    allowlist.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / public_redaction.ALLOWLIST_PATH, allowlist)
    inventory = "_source_input_paths" if module is build_provenance else "_build_input_paths"
    with (
        patch.object(module, "ROOT", tmp_path),
        patch.object(module, inventory, return_value=(path,)),
    ):
        if module is build_provenance:
            read = module._source_inputs_digest
        else:
            read = module._build_inputs
        original = read()
        public = public_redaction.redact_blob("scripts/input.py", path.read_bytes())
        path.write_bytes(public)
        assert read() == original
        if module is supply_chain_manifest:
            assert original[0]["size"] == len(public)
            assert original[0]["sha256"] == "sha256:" + hashlib.sha256(public).hexdigest()
        path.write_bytes(public.replace(b"value = 1", b"value = 2"))
        assert read() != original
