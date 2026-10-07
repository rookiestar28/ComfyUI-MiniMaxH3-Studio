"""M17-17 Ref2VA soundtrack canonical-ownership reproduction row R1.

This module pins what the repository's own typed report claims about a reference video's
soundtrack, so the browser rows can be compared against an executable oracle instead of
against a hand-written expectation. The pure core is already correct here: an explicit
``paired_audios`` asset is the only route to a native ``ref_video_audio_*`` binding, and a
registry that never receives one declares, everywhere it speaks, that no soundtrack was
submitted. What M17-17 reproduces is that App Mode materializes a graph which submits one
anyway.
"""

from __future__ import annotations

import unittest

from comfyui_h3_context.core import (
    AssetRole,
    ContextReport,
    ExecutionCorrelation,
    MediaKind,
    NativeH3Wiring,
    PromptRenderStatus,
    RawContextRequest,
    ReferenceRegistry,
    ReferenceRegistryError,
    TaskMode,
    build_native_h3_wiring,
    build_reference_registry,
    build_sidebar_workspace_projection,
)
from comfyui_h3_context.core.registry import ReferenceAsset
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextValidatorNode,
    H3ReferenceRegistryNode,
    ReferenceRegistryNodeError,
)

WORKSPACE_ID = "ws_m1717_ref2va_soundtrack_ownership"
INTENT = "Preserve the exact declared intent."


class OpaqueMedia:
    """A host-like media value that fails loudly if anything tries to read it."""

    def __iter__(self) -> object:
        raise AssertionError("the reference registry must not iterate opaque media")


def _registry(**sockets: list[object]) -> ReferenceRegistry:
    """Build a registry exactly the way the node does when a canvas feeds those sockets."""

    return H3ReferenceRegistryNode().build_registry(**sockets)[0]


def _report(registry: ReferenceRegistry) -> ContextReport:
    raw = RawContextRequest(
        mode=TaskMode.REF2VA,
        user_intent=INTENT,
        duration_seconds=5.0,
        assets=registry.to_asset_descriptors(),
        reference_registry=registry,
    )
    plan = H3ContextPlanNode().build_plan(raw)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    assert document.status is PromptRenderStatus.RENDERED
    return H3ContextValidatorNode().validate(plan, document)[1]


def _paths(wiring: NativeH3Wiring) -> list[str]:
    return [binding.native_path for binding in wiring.bindings]


def _pairs(registry: ReferenceRegistry) -> list[tuple[str, str | None]]:
    return [(asset.asset_id, asset.paired_video_id) for asset in registry.assets]


def _candidates(report: ContextReport, wiring: NativeH3Wiring) -> list[tuple[str, str, str | None]]:
    projection = build_sidebar_workspace_projection(
        report,
        wiring,
        ExecutionCorrelation("prompt.m17.17", "17"),
        workspace_id=WORKSPACE_ID,
        base_prompt_fingerprint=wiring.prompt_fingerprint,
    )
    return [
        (candidate.label, candidate.kind, candidate.paired_with)
        for candidate in projection.reference_candidates
    ]


class Ref2VASoundtrackOwnershipTests(unittest.TestCase):
    """R1: what the registry, the native adapter and the sidebar say about a soundtrack."""

    def test_the_app_mode_registry_shape_declares_that_no_soundtrack_was_submitted(self) -> None:
        # This is the exact socket set App Mode wires today: the video and any standalone audio,
        # and nothing on `paired_audios`. Every authority the repository owns then agrees that
        # this run submits no soundtrack for the video -- which is the claim the materialized
        # canvas contradicts by binding `ref_video_audio_0` from the video's own audio track.
        registry = _registry(videos=[OpaqueMedia()], audios=[OpaqueMedia()])
        report = _report(registry)
        wiring = build_native_h3_wiring(report)

        self.assertEqual(_pairs(registry), [("video_1", None), ("audio_1", None)])
        self.assertEqual(_paths(wiring), ["ref_videos.ref_video_0", "ref_audios.ref_audio_0"])
        self.assertNotIn(
            "ref_video_audios",
            {binding.native_input for binding in wiring.bindings},
        )
        self.assertEqual(
            _candidates(report, wiring),
            [("<Video 1>", "video", None), ("<Audio 1>", "audio", None)],
        )

    def test_the_paired_socket_is_the_only_route_to_a_native_soundtrack(self) -> None:
        registry = _registry(videos=[OpaqueMedia()], paired_audios=[OpaqueMedia()])
        report = _report(registry)
        wiring = build_native_h3_wiring(report)

        # Canonical order places the soundtrack immediately before the video it belongs to, and
        # the native slot index is derived from the video's ordinal rather than from the audio's
        # position in any list. That derivation is what makes the ownership exact.
        self.assertEqual(_pairs(registry), [("audio_pair_1", "video_1"), ("video_1", None)])
        self.assertEqual(
            _paths(wiring),
            ["ref_video_audios.ref_video_audio_0", "ref_videos.ref_video_0"],
        )
        self.assertEqual(
            _candidates(report, wiring),
            [("<Audio 1>", "audio", "<Video 1>"), ("<Video 1>", "video", None)],
        )

    def test_an_excluded_soundtrack_creates_no_asset_label_or_native_edge(self) -> None:
        registry = _registry(videos=[OpaqueMedia()])
        report = _report(registry)
        wiring = build_native_h3_wiring(report)

        self.assertEqual(_paths(wiring), ["ref_videos.ref_video_0"])
        self.assertEqual(_candidates(report, wiring), [("<Video 1>", "video", None)])

    def test_each_soundtrack_owns_exactly_one_video_when_several_are_submitted(self) -> None:
        registry = _registry(
            videos=[OpaqueMedia(), OpaqueMedia()],
            paired_audios=[OpaqueMedia(), OpaqueMedia()],
        )
        wiring = build_native_h3_wiring(_report(registry))

        self.assertEqual(
            _paths(wiring),
            [
                "ref_video_audios.ref_video_audio_0",
                "ref_videos.ref_video_0",
                "ref_video_audios.ref_video_audio_1",
                "ref_videos.ref_video_1",
            ],
        )
        self.assertEqual(
            _pairs(registry),
            [
                ("audio_pair_1", "video_1"),
                ("video_1", None),
                ("audio_pair_2", "video_2"),
                ("video_2", None),
            ],
        )

    def test_a_soundtrack_without_a_target_video_fails_closed(self) -> None:
        with self.assertRaises(ReferenceRegistryNodeError) as context:
            H3ReferenceRegistryNode().build_registry(paired_audios=[OpaqueMedia()])
        self.assertIn("paired_audio_without_video", str(context.exception))

    def test_a_second_soundtrack_for_one_video_fails_closed(self) -> None:
        # The node cannot express this shape, so the invariant is asserted against the core the
        # node delegates to: two soundtracks naming one video is a contradiction, not a merge.
        # The core carries two guards for it. The ordering guard is the one that fires, because
        # two soundtracks cannot both immediately precede the same video; the duplicate-count
        # guard behind it is defensive and unreachable through this arrangement. What matters
        # for M17-17 is that the shape is refused rather than silently reduced to one owner.
        with self.assertRaises(ReferenceRegistryError) as context:
            build_reference_registry(
                (
                    ReferenceAsset(
                        "audio_pair_1",
                        MediaKind.AUDIO,
                        AssetRole.AUDIO_SOURCE,
                        1,
                        paired_video_id="video_1",
                    ),
                    ReferenceAsset(
                        "audio_pair_2",
                        MediaKind.AUDIO,
                        AssetRole.AUDIO_SOURCE,
                        2,
                        paired_video_id="video_1",
                    ),
                    ReferenceAsset("video_1", MediaKind.VIDEO, AssetRole.REFERENCE, 3),
                )
            )
        self.assertIn("must appear immediately before its video", str(context.exception))


if __name__ == "__main__":  # pragma: no cover - direct execution convenience
    unittest.main()
