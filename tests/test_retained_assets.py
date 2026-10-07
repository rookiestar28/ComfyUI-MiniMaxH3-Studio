"""Closed metadata references never reconstruct media or execution permission."""

from __future__ import annotations

import importlib
import importlib.util
import json
import unittest
from types import ModuleType

from comfyui_h3_context.core.retained_assets import RetainedCatalog

MODULE = "comfyui_h3_context.core.retained_assets"
OWNER = "owner_" + "a" * 32
SCHEMA = "h3.context.retained_asset_catalog.v1"


def asset_wire(number: int = 1) -> dict[str, object]:
    return {
        "asset_id": "asset_" + f"{number:032x}",
        "content_fingerprint": "sha256:" + "b" * 64,
        "byte_length": 64,
        "media_profile": "h3.authoring.video_source_facts.v2",
        "width": 512,
        "height": 512,
        "frame_count": 362,
        "duration_ms": 15084,
        "facts_fingerprint": "sha256:" + "c" * 64,
        "created_at_ms": 1000,
        "closed_at_ms": 1000,
        "project_references": [],
    }


def catalog_wire(*rows: dict[str, object]) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "owner_id": OWNER,
        "revision": 1,
        "enabled": True,
        "assets": list(rows),
    }


class RetainedAssetsTests(unittest.TestCase):
    def module(self) -> ModuleType:
        self.assertIsNotNone(
            importlib.util.find_spec(MODULE),
            "missing closed retained-asset metadata and reference contract",
        )
        return importlib.import_module(MODULE)

    def decode(self, value: object, owner: str = OWNER) -> RetainedCatalog:
        module = self.module()
        decoded: object = module.decode_catalog(json.dumps(value).encode(), expected_owner=owner)
        assert isinstance(decoded, RetainedCatalog)
        return decoded

    def test_default_off_and_closed_round_trip(self) -> None:
        module = self.module()
        empty = module.RetainedCatalog(OWNER)
        self.assertFalse(empty.enabled)
        self.assertEqual(empty.revision, 0)
        self.assertEqual(empty.assets, ())
        wire = catalog_wire(asset_wire())
        restored = self.decode(wire)
        self.assertEqual(restored.to_wire(), wire)
        self.assertEqual(
            module.decode_catalog(module.encode_catalog(restored), expected_owner=OWNER), restored
        )
        self.assertEqual(restored.assets[0].asset_id, asset_wire()["asset_id"])

    def test_prose_locators_receipts_grants_and_unknown_fields_refuse(self) -> None:
        module = self.module()
        forbidden = (
            "prompt",
            "path",
            "url",
            "owner",
            "receipt",
            "production_callback",
            "source_handle",
            "grant",
            "provider_payload",
            "model_fingerprint",
            "landmarks",
        )
        for name in forbidden:
            with self.subTest(field=name), self.assertRaises(module.RetainedAssetError):
                self.decode(catalog_wire({**asset_wire(), name: "untrusted"}))
        for name in forbidden:
            with self.subTest(root=name), self.assertRaises(module.RetainedAssetError):
                self.decode({**catalog_wire(), name: "untrusted"})
        with self.assertRaises(module.RetainedAssetError):
            self.decode({**catalog_wire(), "schema": "h3.context.retained_asset_catalog.v2"})

    def test_json_owner_and_size_boundaries_refuse_before_construction(self) -> None:
        module = self.module()
        for payload in (
            b'{"revision":1,"revision":2}',
            b'{"revision":NaN}',
            b"\xff",
            b"{" * 100,
            b" " * (module.MAX_CATALOG_BYTES + 1),
        ):
            with (
                self.subTest(payload_length=len(payload)),
                self.assertRaises(module.RetainedAssetError),
            ):
                module.decode_catalog(payload, expected_owner=OWNER)
        with self.assertRaises(module.RetainedAssetError):
            self.decode(catalog_wire(asset_wire()), "owner_" + "d" * 32)
        with self.assertRaises(module.RetainedAssetError):
            self.decode(catalog_wire(), "../owner")

    def test_media_and_scalar_facts_have_finite_current_profile_bounds(self) -> None:
        module = self.module()
        changes = {
            "asset_id": ("../asset", "asset_" + "A" * 32, "record_" + "a" * 32),
            "content_fingerprint": ("c" * 64, "sha256:" + "A" * 64),
            "facts_fingerprint": ("url:content", None),
            "byte_length": (0, True, 64 * 1024 * 1024 + 1),
            "media_profile": ("unknown", "h3.authoring.video_source_facts.v1"),
            "width": (0, True, 2049),
            "height": (0, 2049),
            "frame_count": (0, True, 513),
            "duration_ms": (0, True, 30001),
            "created_at_ms": (0, True, 10_000_000_000_000),
            "closed_at_ms": (999, False, None),
            "project_references": ("project", ["../project"], ["pw_live"]),
        }
        for name, values in changes.items():
            for value in values:
                with (
                    self.subTest(field=name, value=value),
                    self.assertRaises(module.RetainedAssetError),
                ):
                    self.decode(catalog_wire({**asset_wire(), name: value}))
        for field, values in {"revision": (True, -1, 1 << 53), "enabled": (0, "true")}.items():
            for value in values:
                with (
                    self.subTest(root=field, value=value),
                    self.assertRaises(module.RetainedAssetError),
                ):
                    self.decode({**catalog_wire(), field: value})

    def test_project_reference_protection_is_closed_and_correlated(self) -> None:
        module = self.module()
        project = "project_" + "1" * 32
        valid = {**asset_wire(), "project_references": [project], "closed_at_ms": None}
        self.assertEqual(self.decode(catalog_wire(valid)).assets[0].project_references, (project,))
        for refs, closed in (
            ([project], 1000),
            ([project, project], None),
            (["project_" + f"{index:032x}" for index in range(9)], None),
        ):
            with (
                self.subTest(refs=len(refs), closed=closed),
                self.assertRaises(module.RetainedAssetError),
            ):
                self.decode(
                    catalog_wire(
                        {**asset_wire(), "project_references": refs, "closed_at_ms": closed}
                    )
                )

    def test_duplicate_assets_and_owner_count_overflow_refuse(self) -> None:
        module = self.module()
        for rows in ((asset_wire(), asset_wire()), tuple(asset_wire(index) for index in range(17))):
            with self.subTest(count=len(rows)), self.assertRaises(module.RetainedAssetError):
                self.decode(catalog_wire(*rows))

    def test_maximum_valid_catalog_fits_without_relaxing_media_profile(self) -> None:
        module = self.module()
        projects = ["project_" + f"{index:032x}" for index in range(8)]
        rows = tuple(
            {
                **asset_wire(index),
                "project_references": projects,
                "closed_at_ms": None,
                "byte_length": 64 * 1024 * 1024,
                "width": 2048,
                "height": 2048,
                "frame_count": 512,
                "duration_ms": 30000,
            }
            for index in range(16)
        )
        catalog = self.decode(catalog_wire(*rows))
        encoded = module.encode_catalog(catalog)
        self.assertLessEqual(len(encoded), module.MAX_CATALOG_BYTES)
        self.assertEqual(module.decode_catalog(encoded, expected_owner=OWNER), catalog)
        self.assertEqual(len(catalog.assets), 16)


if __name__ == "__main__":
    unittest.main()
