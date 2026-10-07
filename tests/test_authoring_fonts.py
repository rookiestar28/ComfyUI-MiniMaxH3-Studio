from __future__ import annotations

import json
import shutil
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
