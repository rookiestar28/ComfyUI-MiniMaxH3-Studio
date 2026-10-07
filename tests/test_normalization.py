"""M1-02 deterministic request normalization and H3 duration-grid tests."""

from __future__ import annotations

import math
import unittest
from pathlib import Path

from comfyui_h3_context.core import (
    CURRENT_SCHEMA_VERSION,
    AssetDescriptor,
    AssetRole,
    MediaKind,
    ModelVariant,
    ProfileIdentity,
    PromptProfile,
    TaskMode,
    ValidationSeverity,
)
from comfyui_h3_context.core.normalization import (
    DEFAULT_FRAME_COUNT,
    FPS,
    MAX_FRAME_COUNT,
    MIN_FRAME_COUNT,
    DurationSource,
    NormalizationResult,
    RawContextRequest,
    normalize_request,
)

ROOT = Path(__file__).resolve().parents[1]


def image_asset(asset_id: str, role: AssetRole) -> AssetDescriptor:
    return AssetDescriptor(asset_id=asset_id, kind=MediaKind.IMAGE, role=role)


class NormalizationTests(unittest.TestCase):
    def assert_error(self, result: NormalizationResult, code: str) -> None:
        self.assertIsNone(result.request)
        self.assertIn(code, {diagnostic.code for diagnostic in result.diagnostics})
        self.assertTrue(
            any(
                diagnostic.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
                for diagnostic in result.diagnostics
            )
        )

    def test_default_seconds_and_explicit_frames_are_source_explicit(self) -> None:
        default = normalize_request(RawContextRequest(mode=TaskMode.T2VA, user_intent="a scene"))
        self.assertIsNotNone(default.request)
        assert default.request is not None
        self.assertEqual(default.request.duration_source, DurationSource.DEFAULT)
        self.assertEqual(default.request.effective_frame_count, DEFAULT_FRAME_COUNT)
        self.assertAlmostEqual(default.request.effective_duration_seconds, 124 / FPS)

        seconds = normalize_request(
            RawContextRequest(
                mode=TaskMode.I2VA,
                user_intent="a scene",
                duration_seconds=5.0,
                assets=(image_asset("first", AssetRole.FIRST_FRAME),),
            )
        )
        self.assertIsNotNone(seconds.request)
        assert seconds.request is not None
        self.assertEqual(seconds.request.duration_source, DurationSource.SECONDS)
        self.assertEqual(seconds.request.effective_frame_count, 124)
        # M17-25 / AC-M17-25-05: one neutral movement code carrying the requested
        # value, the delivered value and the direction, compared against the
        # request rather than against the intermediate estimate.
        movement = [d for d in seconds.diagnostics if d.code == "duration_snapped"]
        self.assertEqual(len(movement), 1)
        self.assertEqual(
            movement[0].message,
            "requested 5000 ms delivers 5167 ms (124 frames), longer than requested",
        )

        exact = normalize_request(
            RawContextRequest(
                mode=TaskMode.FL2VA,
                user_intent="a scene",
                duration_seconds=5.167,
                assets=(
                    image_asset("first", AssetRole.FIRST_FRAME),
                    image_asset("last", AssetRole.LAST_FRAME),
                ),
            )
        )
        self.assertIsNotNone(exact.request)
        assert exact.request is not None
        self.assertEqual(exact.request.duration_source, DurationSource.SECONDS)
        self.assertEqual(exact.request.effective_frame_count, 124)
        self.assertNotIn("duration_snapped", {d.code for d in exact.diagnostics})

    def test_all_modes_route_to_declared_profile_and_variant(self) -> None:
        cases = (
            (TaskMode.T2VA, (), PromptProfile.BASE, ModelVariant.BASE_FL2VA),
            (
                TaskMode.I2VA,
                (image_asset("first", AssetRole.FIRST_FRAME),),
                PromptProfile.BASE,
                ModelVariant.BASE_FL2VA,
            ),
            (
                TaskMode.FL2VA,
                (
                    image_asset("first", AssetRole.FIRST_FRAME),
                    image_asset("last", AssetRole.LAST_FRAME),
                ),
                PromptProfile.BASE,
                ModelVariant.BASE_FL2VA,
            ),
            (
                TaskMode.L2VA,
                (image_asset("last", AssetRole.LAST_FRAME),),
                PromptProfile.BASE,
                ModelVariant.BASE_FL2VA,
            ),
            (
                TaskMode.REF2VA,
                (image_asset("reference", AssetRole.REFERENCE),),
                PromptProfile.FULL_REFERENCE,
                ModelVariant.BASE_REF2VA,
            ),
        )
        for mode, assets, profile, variant in cases:
            with self.subTest(mode=mode):
                result = normalize_request(
                    RawContextRequest(mode=mode, user_intent="intent", assets=assets)
                )
                self.assertIsNotNone(result.request)
                assert result.request is not None
                self.assertEqual(result.request.schema_version, CURRENT_SCHEMA_VERSION)
                self.assertEqual(
                    result.request.profile, ProfileIdentity(profile, result.request.profile.version)
                )
                self.assertEqual(result.request.model_variant, variant)
                self.assertEqual(result.request.task_mode, mode)
                self.assertEqual(result.request.user_intent, "intent")

    def test_grid_alignment_is_deterministic_across_a_bounded_sweep(self) -> None:
        for milliseconds in range(250, 40_000, 7):
            with self.subTest(milliseconds=milliseconds):
                result = normalize_request(
                    RawContextRequest(
                        mode=TaskMode.T2VA,
                        user_intent="x",
                        duration_seconds=milliseconds / 1000,
                    )
                )
                self.assertIsNotNone(result.request)
                assert result.request is not None
                self.assertEqual(result.request.effective_frame_count % 17, 5)
                self.assertGreaterEqual(result.request.effective_frame_count, MIN_FRAME_COUNT)
                self.assertLessEqual(result.request.effective_frame_count, MAX_FRAME_COUNT)
                # The delivered length never differs silently: every request whose
                # delivered duration is not the requested one carries the movement
                # code, and every one that matches carries none.
                delivered = round(result.request.effective_frame_count * 1000 / 24)
                snapped = {d.code for d in result.diagnostics} & {"duration_snapped"}
                self.assertEqual(bool(snapped), delivered != milliseconds)

    def test_duration_bounds_and_invalid_values_fail_closed(self) -> None:
        for request in (
            RawContextRequest(mode=TaskMode.T2VA, user_intent="x", duration_seconds=0),
            RawContextRequest(mode=TaskMode.T2VA, user_intent="x", duration_seconds=-1),
            RawContextRequest(mode=TaskMode.T2VA, user_intent="x", duration_seconds=math.inf),
            RawContextRequest(mode=TaskMode.T2VA, user_intent="x", duration_seconds=math.nan),
            # Below the accepted minimum and above the host maximum both fail
            # closed. The official template clamps a sub-minimum request up to five
            # frames; this repository refuses rather than deliver a length nobody
            # asked for.
            RawContextRequest(mode=TaskMode.T2VA, user_intent="x", duration_seconds=0.15),
            RawContextRequest(mode=TaskMode.T2VA, user_intent="x", duration_seconds=200),
            # 3600 is inside the accepted frame range but is not on the lattice,
            # and aligning it lands above the host maximum.
            RawContextRequest(mode=TaskMode.T2VA, user_intent="x", duration_seconds=150.0),
        ):
            with self.subTest(request=request):
                self.assert_error(normalize_request(request), "duration_out_of_bounds")

    def test_intent_is_preserved_without_trimming_or_unicode_rewrite(self) -> None:
        intent = "  café\u0301\nkeep this exact text  "
        result = normalize_request(RawContextRequest(mode=TaskMode.T2VA, user_intent=intent))
        self.assertIsNotNone(result.request)
        assert result.request is not None
        self.assertEqual(result.request.user_intent, intent)

        self.assert_error(
            normalize_request(RawContextRequest(mode=TaskMode.T2VA, user_intent="   ")),
            "missing_user_intent",
        )
        self.assert_error(
            normalize_request(RawContextRequest(mode=TaskMode.T2VA, user_intent="bad\x00text")),
            "invalid_user_intent",
        )
        self.assert_error(
            normalize_request(RawContextRequest(mode=TaskMode.T2VA, user_intent="x" * 65_537)),
            "user_intent_too_long",
        )

    def test_unknown_mode_and_bad_asset_container_fail_with_diagnostics(self) -> None:
        self.assert_error(
            normalize_request(RawContextRequest(mode="unsupported", user_intent="x")),
            "unsupported_task_mode",
        )
        self.assert_error(
            normalize_request(RawContextRequest(mode=TaskMode.T2VA, user_intent="x", assets=[])),  # type: ignore[arg-type]
            "invalid_assets",
        )

    def test_base_mode_anchor_cardinality_and_kind_are_strict(self) -> None:
        cases = (
            (TaskMode.T2VA, (image_asset("first", AssetRole.FIRST_FRAME),), "unexpected_assets"),
            (TaskMode.I2VA, (), "missing_first_frame"),
            (TaskMode.I2VA, (image_asset("last", AssetRole.LAST_FRAME),), "missing_first_frame"),
            (
                TaskMode.I2VA,
                (AssetDescriptor("first", MediaKind.VIDEO, AssetRole.FIRST_FRAME),),
                "frame_asset_must_be_image",
            ),
            (TaskMode.FL2VA, (image_asset("first", AssetRole.FIRST_FRAME),), "missing_last_frame"),
            (TaskMode.L2VA, (image_asset("first", AssetRole.FIRST_FRAME),), "missing_last_frame"),
        )
        for mode, assets, code in cases:
            with self.subTest(mode=mode, code=code):
                self.assert_error(
                    normalize_request(RawContextRequest(mode=mode, user_intent="x", assets=assets)),
                    code,
                )

    def test_reference_counts_and_duplicate_ids_are_bounded(self) -> None:
        self.assert_error(
            normalize_request(RawContextRequest(mode=TaskMode.REF2VA, user_intent="x")),
            "missing_reference_asset",
        )
        over_images = tuple(image_asset(f"image_{i}", AssetRole.REFERENCE) for i in range(10))
        self.assert_error(
            normalize_request(
                RawContextRequest(mode=TaskMode.REF2VA, user_intent="x", assets=over_images)
            ),
            "too_many_images",
        )
        duplicate = (
            image_asset("same", AssetRole.REFERENCE),
            image_asset("same", AssetRole.REFERENCE),
        )
        self.assert_error(
            normalize_request(
                RawContextRequest(mode=TaskMode.REF2VA, user_intent="x", assets=duplicate)
            ),
            "duplicate_asset_id",
        )
        over_videos = tuple(
            AssetDescriptor(f"video_{i}", MediaKind.VIDEO, AssetRole.REFERENCE) for i in range(4)
        )
        self.assert_error(
            normalize_request(
                RawContextRequest(mode=TaskMode.REF2VA, user_intent="x", assets=over_videos)
            ),
            "too_many_videos",
        )
        over_audio = tuple(
            AssetDescriptor(f"audio_{i}", MediaKind.AUDIO, AssetRole.AUDIO_SOURCE) for i in range(7)
        )
        self.assert_error(
            normalize_request(
                RawContextRequest(mode=TaskMode.REF2VA, user_intent="x", assets=over_audio)
            ),
            "too_many_audio",
        )

    def test_result_has_no_fake_request_when_any_error_exists(self) -> None:
        result = normalize_request(
            RawContextRequest(
                mode=TaskMode.I2VA,
                user_intent="",
                duration_seconds=5,
                assets=(),
            )
        )
        self.assertIsNone(result.request)
        self.assertFalse(result.is_valid)
        self.assertTrue(result.has_errors)

    def test_normalization_module_is_pure_source(self) -> None:
        source = (ROOT / "comfyui_h3_context" / "core" / "normalization.py").read_text(
            encoding="utf-8"
        )
        for forbidden in (
            "comfy",
            "cv2",
            "diffusers",
            "httpx",
            "requests",
            "torch",
            "transformers",
        ):
            self.assertNotIn(f"import {forbidden}", source)
            self.assertNotIn(f"from {forbidden}", source)


if __name__ == "__main__":
    unittest.main()
