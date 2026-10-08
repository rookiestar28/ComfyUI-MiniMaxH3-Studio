from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from comfyui_h3_context.adapters.authoring_fonts import (
    FONT_MANIFEST_PROFILE_ID,
    FONT_MANIFEST_SCHEMA,
    AuthoringFontError,
    _load_packaged_font_manifest_from_root,
    load_packaged_font_manifest,
    require_text_coverage,
    resolve_packaged_font,
)

EXPECTED_FACE_KEYS = {
    (400, "normal"),
    (400, "italic"),
    (700, "normal"),
    (700, "italic"),
}


@pytest.mark.parametrize("crlf", ["license", "manifest", "both"])
def test_checkout_newlines_preserve_verified_font_package_identity(
    tmp_path: Path, crlf: str
) -> None:
    source = Path(__file__).resolve().parents[1] / "comfyui_h3_context"
    baseline = load_packaged_font_manifest()
    shutil.copytree(source / "fonts", tmp_path / "fonts")
    for name, selected in (
        ("LICENSE-OFL-1.1.txt", crlf in {"license", "both"}),
        ("font_manifest_v1.json", crlf in {"manifest", "both"}),
    ):
        if selected:
            path = tmp_path / "fonts" / name
            original = path.read_bytes()
            assert b"\r" not in original and b"\n" in original
            path.write_bytes(original.replace(b"\n", b"\r\n"))
    manifest = _load_packaged_font_manifest_from_root(tmp_path)
    assert manifest == baseline
    for face, original_face in zip(manifest.faces, baseline.faces, strict=True):
        assert face.read_verified_bytes() == original_face.read_verified_bytes()


@pytest.mark.parametrize("name", ["LICENSE-OFL-1.1.txt", "font_manifest_v1.json"])
def test_raw_checkout_size_is_bounded_before_newline_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    from comfyui_h3_context.adapters import authoring_fonts

    source = Path(__file__).resolve().parents[1] / "comfyui_h3_context" / "fonts"
    shutil.copytree(source, tmp_path / "fonts")
    path = tmp_path / "fonts" / name
    original = path.read_bytes()
    path.write_bytes(original.replace(b"\n", b"\r\n"))
    assert path.stat().st_size > len(original)
    limit = "_MAX_LICENSE_BYTES" if name.endswith(".txt") else "_MAX_MANIFEST_BYTES"
    monkeypatch.setattr(authoring_fonts, limit, len(original))
    with pytest.raises(AuthoringFontError):
        _load_packaged_font_manifest_from_root(tmp_path)


@pytest.mark.parametrize("mutation", ["lone_cr", "space", "content", "extra_lf"])
def test_license_accepts_only_checkout_crlf_equivalence(tmp_path: Path, mutation: str) -> None:
    source = Path(__file__).resolve().parents[1] / "comfyui_h3_context" / "fonts"
    shutil.copytree(source, tmp_path / "fonts")
    path = tmp_path / "fonts" / "LICENSE-OFL-1.1.txt"
    body = path.read_bytes()
    if mutation == "lone_cr":
        body = body.replace(b"\n", b"\r", 1)
    elif mutation == "space":
        body = body.replace(b"\n", b" \n", 1)
    elif mutation == "content":
        body = body.replace(b"Copyright", b"copyright", 1)
    else:
        body += b"\n"
    path.write_bytes(body)
    with pytest.raises(AuthoringFontError) as raised:
        _load_packaged_font_manifest_from_root(tmp_path)
    assert raised.value.code == "font_license_hash_mismatch"


def test_non_eol_manifest_change_still_changes_the_package_identity(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "comfyui_h3_context" / "fonts"
    baseline = load_packaged_font_manifest()
    shutil.copytree(source, tmp_path / "fonts")
    path = tmp_path / "fonts" / "font_manifest_v1.json"
    path.write_bytes(path.read_bytes() + b" ")
    manifest = _load_packaged_font_manifest_from_root(tmp_path)
    assert manifest.manifest_fingerprint == baseline.manifest_fingerprint
    assert manifest.package_fingerprint != baseline.package_fingerprint


def test_default_windows_git_filters_preserve_both_pinned_font_texts(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    names = (
        "comfyui_h3_context/fonts/LICENSE-OFL-1.1.txt",
        "comfyui_h3_context/fonts/font_manifest_v1.json",
    )
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}

    def git(*args: str) -> bytes:
        return subprocess.check_output(
            ["git", "-C", str(tmp_path), *args], env=env, stderr=subprocess.PIPE, timeout=30
        )

    git("init", "--quiet")
    shutil.copyfile(root / ".gitattributes", tmp_path / ".gitattributes")
    for name in names:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((root / name).read_bytes())
    git("-c", "core.autocrlf=false", "add", "--", ".gitattributes", *names)
    tree = git("write-tree").decode().strip()
    for name in names:
        exported = git(
            "-c", "core.autocrlf=true", "cat-file", "--filters", f"--path={name}", f"{tree}:{name}"
        )
        assert exported == (root / name).read_bytes()
        assert b"\r" not in exported


def test_real_packaged_manifest_verifies_artifacts_license_build_and_cmaps() -> None:
    manifest = load_packaged_font_manifest()

    assert manifest.schema_version == FONT_MANIFEST_SCHEMA
    assert manifest.profile_id == FONT_MANIFEST_PROFILE_ID
    assert manifest.fallback_order == ("h3.font.noto_sans.v1",)
    assert set(manifest.faces_by_style) == EXPECTED_FACE_KEYS
    assert manifest.package_fingerprint.startswith("sha256:")
    assert manifest.manifest_fingerprint.startswith("sha256:")

    for key in sorted(EXPECTED_FACE_KEYS):
        face = resolve_packaged_font(manifest, "h3.font.noto_sans.v1", *key)
        assert face.family_name == "Noto Sans"
        assert face.build_version == "Version 2.015; ttfautohint (v1.8.4.7-5d5b)"
        assert face.file_fingerprint.startswith("sha256:")
        assert len(face.upstream_git_blob_sha1) == 40
        assert face.cmap_fingerprint.startswith("sha256:")
        assert face.covered_codepoint_count > 2_000
        assert len(face.read_verified_bytes()) == face.size_bytes


def test_packaged_font_covers_declared_latin_greek_cyrillic_text() -> None:
    manifest = load_packaged_font_manifest()

    face = require_text_coverage(
        manifest,
        "h3.font.noto_sans.v1",
        400,
        "normal",
        "H3 Context — Café Ω Привет\n",
    )

    assert face.artifact_id == "h3.font.noto_sans.regular.v1"


def test_unknown_font_or_style_is_a_typed_blocker() -> None:
    manifest = load_packaged_font_manifest()
    cases = (
        ("h3.font.unknown.v1", 400, "normal", "font_asset_unsupported"),
        ("h3.font.noto_sans.v1", 500, "normal", "font_style_unsupported"),
        ("h3.font.noto_sans.v1", 400, "oblique", "font_style_unsupported"),
    )

    for font_asset_id, weight, style, code in cases:
        with pytest.raises(AuthoringFontError) as raised:
            resolve_packaged_font(manifest, font_asset_id, weight, style)

        assert raised.value.code == code


def test_uncovered_cjk_is_a_typed_blocker_without_system_font_fallback() -> None:
    manifest = load_packaged_font_manifest()

    with pytest.raises(AuthoringFontError) as raised:
        require_text_coverage(
            manifest,
            "h3.font.noto_sans.v1",
            700,
            "normal",
            "中文",
        )

    assert raised.value.code == "font_glyph_unsupported"
    assert "U+4E2D" in str(raised.value)
    assert manifest.fallback_order == ("h3.font.noto_sans.v1",)


def test_resolved_font_repr_never_contains_the_private_install_path() -> None:
    manifest = load_packaged_font_manifest()
    face = resolve_packaged_font(manifest, "h3.font.noto_sans.v1", 400, "normal")

    representation = repr(face)
    package_root = str(Path(__file__).resolve().parents[1])
    assert package_root not in representation
    assert "_filesystem_path" not in representation
    assert face.package_path == "fonts/NotoSans-Regular.ttf"


def test_reader_rejects_a_tampered_packaged_font_before_resolution(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "comfyui_h3_context" / "fonts"
    package_root = tmp_path / "comfyui_h3_context"
    shutil.copytree(source, package_root / "fonts")
    font_path = package_root / "fonts" / "NotoSans-Regular.ttf"
    payload = bytearray(font_path.read_bytes())
    payload[-1] ^= 0x01
    font_path.write_bytes(payload)

    with pytest.raises(AuthoringFontError) as raised:
        _load_packaged_font_manifest_from_root(package_root)

    assert raised.value.code == "font_artifact_hash_mismatch"
    assert str(font_path) not in str(raised.value)


def test_reader_rejects_a_tampered_font_license(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "comfyui_h3_context" / "fonts"
    package_root = tmp_path / "comfyui_h3_context"
    shutil.copytree(source, package_root / "fonts")
    license_path = package_root / "fonts" / "LICENSE-OFL-1.1.txt"
    license_path.write_bytes(license_path.read_bytes() + b"tampered")

    with pytest.raises(AuthoringFontError) as raised:
        _load_packaged_font_manifest_from_root(package_root)

    assert raised.value.code == "font_license_hash_mismatch"
    assert str(license_path) not in str(raised.value)


def test_manifest_cannot_redirect_a_face_to_a_system_font(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "comfyui_h3_context" / "fonts"
    package_root = tmp_path / "comfyui_h3_context"
    shutil.copytree(source, package_root / "fonts")
    manifest_path = package_root / "fonts" / "font_manifest_v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["font_assets"][0]["faces"][0]["package_path"] = "/system/fonts/example.ttf"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(AuthoringFontError) as raised:
        _load_packaged_font_manifest_from_root(package_root)

    assert raised.value.code == "font_manifest_invalid"
    assert "/system/fonts/example.ttf" not in str(raised.value)
