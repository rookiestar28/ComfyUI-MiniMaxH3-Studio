"""Only a fresh authenticated census can cover the gate's duplicate repository check."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts import gate_link_receipt as receipt
from scripts.workspace_link_guard import find_links


@pytest.fixture
def attested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    if os.name != "nt":
        pytest.skip("Windows gate receipts deliberately refuse other process identity platforms")
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".gitignore").write_text(".tmp/\n", encoding="utf-8")
    (root / "source.py").write_text("value = 1\n", encoding="utf-8")
    for args in (
        ["init", "--quiet", "--template="],
        ["add", ".gitignore", "source.py"],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    path = root / ".tmp/gate-link-receipts/owned-run/receipt.json"
    monkeypatch.setenv(receipt.RECEIPT_ENV, str(path))
    monkeypatch.setenv(receipt.KEY_ENV, secrets.token_hex(32))
    monkeypatch.setenv(receipt.PID_ENV, str(os.getpid()))
    receipt.issue(root, find_links(root, repo_root=root, strict=True))
    return root, path


def resign(path: Path, **changes: Any) -> None:
    envelope = json.loads(path.read_bytes())
    envelope["payload"].update(changes)
    envelope["signature"] = hmac.new(
        bytes.fromhex(os.environ[receipt.KEY_ENV]),
        receipt._canonical(envelope["payload"]),
        hashlib.sha256,
    ).hexdigest()
    path.write_bytes(receipt._canonical(envelope))


def test_missing_or_plain_flag_never_authorizes_a_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (receipt.RECEIPT_ENV, receipt.KEY_ENV, receipt.PID_ENV):
        monkeypatch.delenv(name, raising=False)
    assert not receipt.authentic_current_receipt(tmp_path)
    monkeypatch.setenv(receipt.RECEIPT_ENV, "true")
    assert not receipt.authentic_current_receipt(tmp_path)


def test_an_actual_current_receipt_is_valid_and_cleanup_removes_only_it(
    attested: tuple[Path, Path],
) -> None:
    root, path = attested
    assert receipt.authentic_current_receipt(root)
    before = (root / "source.py").read_bytes()
    receipt.cleanup(root, str(path))
    assert not path.parent.exists()
    assert (root / "source.py").read_bytes() == before
    assert not receipt.authentic_current_receipt(root)


@pytest.mark.parametrize(
    "field,value",
    [
        ("issued", 0),
        ("issued", 10**20),
        ("pid", -1),
        ("pid", True),
        ("process", "different-process-creation"),
        ("run", "different-run"),
        ("root", "different-root"),
        ("schema", "different-schema"),
        ("source", "different-source"),
        ("topology", "different-topology"),
    ],
)
def test_even_a_signed_receipt_must_match_every_current_authority(
    attested: tuple[Path, Path], field: str, value: Any
) -> None:
    root, path = attested
    resign(path, **{field: value})
    assert not receipt.authentic_current_receipt(root)


@pytest.mark.parametrize(
    "defect", ["bad-signature", "unknown-key", "duplicate-key", "oversized", "malformed"]
)
def test_untrusted_receipt_bytes_fail_closed(attested: tuple[Path, Path], defect: str) -> None:
    root, path = attested
    if defect == "malformed":
        path.write_text("{", encoding="utf-8")
    elif defect == "oversized":
        path.write_bytes(b" " * (receipt.MAX_BYTES + 1))
    elif defect == "duplicate-key":
        path.write_text('{"payload":{},"payload":{},"signature":"x"}', encoding="utf-8")
    else:
        envelope = json.loads(path.read_bytes())
        if defect == "unknown-key":
            envelope["unknown"] = True
        else:
            envelope["signature"] = "0" * 64
        path.write_bytes(receipt._canonical(envelope))
    assert not receipt.authentic_current_receipt(root)


@pytest.mark.parametrize("kind", ["tracked", "untracked", "index"])
def test_source_and_index_changes_invalidate_a_receipt(
    attested: tuple[Path, Path], kind: str
) -> None:
    root, _ = attested
    if kind == "untracked":
        (root / "new_source.py").write_text("new = True\n", encoding="utf-8")
    else:
        (root / "source.py").write_text("value = 2\n", encoding="utf-8")
        if kind == "index":
            subprocess.run(["git", "add", "source.py"], cwd=root, check=True, capture_output=True)
            (root / "source.py").write_text("value = 1\n", encoding="utf-8")
    assert not receipt.authentic_current_receipt(root)


def test_new_ignored_junction_is_not_hidden_by_an_authenticated_receipt(
    attested: tuple[Path, Path],
) -> None:
    root, _ = attested
    unit = root / ".tmp/unit"
    target = unit / "target"
    target.mkdir(parents=True)
    link = unit / "link"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True
    )
    try:
        assert not receipt.authentic_current_receipt(root)
    finally:
        os.rmdir(link)
    assert receipt.authentic_current_receipt(root)


def test_a_dangling_target_becoming_live_invalidates_the_receipt(
    attested: tuple[Path, Path],
) -> None:
    root, path = attested
    receipt.cleanup(root, str(path))
    unit = root / ".tmp/unit"
    unit.mkdir()
    target = root / ".tmp/other"
    target.mkdir()
    link = unit / "link"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True
    )
    try:
        target.rmdir()
        receipt.issue(root, find_links(root, repo_root=root, strict=True))
        assert receipt.authentic_current_receipt(root)
        target.mkdir()
        assert not receipt.authentic_current_receipt(root)
    finally:
        os.rmdir(link)


def test_uninspectable_topology_cannot_authorize_a_skip(
    attested: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = attested

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("controlled unreadable census")

    monkeypatch.setattr(os, "scandir", unavailable)
    assert not receipt.authentic_current_receipt(root)


def test_cleanup_refuses_a_foreign_location(tmp_path: Path) -> None:
    target = tmp_path / "keep.json"
    target.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError):
        receipt.cleanup(tmp_path, str(target))
    assert target.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("key", [" " * 64, "0" * 62, "g" * 64])
def test_invalid_key_shape_cannot_authenticate_even_a_matching_signature(
    attested: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    root, path = attested
    monkeypatch.setenv(receipt.KEY_ENV, key)
    if key.isspace():
        resign(path)
    assert not receipt.authentic_current_receipt(root)


def test_only_the_exact_repository_duplicate_uses_the_pinned_skip_rule(
    attested: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_workspace_link_guard as repository_guard

    root, path = attested
    monkeypatch.setattr(repository_guard, "ROOT", root)
    called: list[bool] = []

    def ordinary_check(*args: Any, **kwargs: Any) -> list[Any]:
        called.append(True)
        return []

    monkeypatch.setattr(repository_guard, "find_links", ordinary_check)
    with pytest.raises(
        pytest.skip.Exception,
        match="fresh authenticated Full Gate stage-1 census covers this exact repository",
    ):
        repository_guard.test_the_repository_holds_no_escaping_link()
    assert called == []
    path.write_text("{}", encoding="utf-8")
    repository_guard.test_the_repository_holds_no_escaping_link()
    assert called == [True]
