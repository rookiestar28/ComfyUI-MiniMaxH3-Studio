"""M17-20 phase 1: the backend-owned generation-profile qualification contract.

These rows are written against the evidence recorded in plan section 8.1, which
was measured on the pinned host on 2026-08-18 rather than assumed. The host that
evidence came from is a host on which three of the five template default weights
do **not** resolve, so the "missing asset" path below is the real state of a real
machine and not a hypothetical.
"""

from __future__ import annotations

import unittest
from typing import cast

from comfyui_h3_context.core.continuity_handoff import QUALIFIED_COMFYUI_HOST_VERSION
from comfyui_h3_context.core.generation_profile import (
    FAMILY_SLOTS,
    GENERATION_PROFILE_SCHEMA,
    MODE_TEMPLATE,
    PINNED_TEMPLATE_REVISION,
    TASK_MODE_FAMILY,
    AssetSlot,
    FamilyDisposition,
    GenerationProfile,
    GenerationProfileError,
    HostObservation,
    ModeFamily,
    Remediation,
    SlotDisposition,
    SlotObservation,
    TemplateCapability,
    materialization_bases,
    qualify_generation_profile,
)

ANCHORS = frozenset({"MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo"})
I2V_DIGEST = "b" * 64
R2V_DIGEST = "c" * 64
T2V_DIGEST = "e" * 64
#: All three pinned bases. `t2va` loads `video_minimax_h3_t2v`, not the `i2v`
#: bytes, so a projection that omitted it would leave one materialization basis
#: unqualified.
EXPECTED = (
    ("video_minimax_h3_t2v", T2V_DIGEST),
    ("video_minimax_h3_i2v", I2V_DIGEST),
    ("video_minimax_h3_r2v", R2V_DIGEST),
)
CAPABILITIES = (
    TemplateCapability(
        template_name="video_minimax_h3_t2v",
        anchor_node_type="MiniMaxH3ImageToVideo",
        input_names=frozenset({"prompt"}),
    ),
    TemplateCapability(
        template_name="video_minimax_h3_i2v",
        anchor_node_type="MiniMaxH3ImageToVideo",
        input_names=frozenset({"prompt"}),
    ),
    TemplateCapability(
        template_name="video_minimax_h3_r2v",
        anchor_node_type="MiniMaxH3ReferenceToVideo",
        input_names=frozenset({"prompt"}),
    ),
)


def observation(
    *,
    present: set[AssetSlot] | None = None,
    anchors: frozenset[str] = ANCHORS,
    digests: tuple[tuple[str, str], ...] = EXPECTED,
    host_version: str = QUALIFIED_COMFYUI_HOST_VERSION,
    capabilities: tuple[TemplateCapability, ...] = CAPABILITIES,
) -> HostObservation:
    satisfied = set(AssetSlot) if present is None else present
    return HostObservation(
        anchor_node_types=anchors,
        template_digests=digests,
        slots=tuple(
            SlotObservation(
                slot=slot,
                disposition=(
                    SlotDisposition.PRESENT if slot in satisfied else SlotDisposition.ABSENT
                ),
            )
            for slot in AssetSlot
        ),
        host_version=host_version,
        template_capabilities=capabilities,
    )


class GenerationProfileTests(unittest.TestCase):
    def test_a_fully_provisioned_host_makes_every_basis_available(self) -> None:
        profile = qualify_generation_profile(observation(), expected_template_digests=EXPECTED)
        self.assertEqual(profile.schema, GENERATION_PROFILE_SCHEMA)
        self.assertEqual(profile.template_revision, PINNED_TEMPLATE_REVISION)
        self.assertEqual(len(profile.families), 3)
        for entry in profile.families:
            with self.subTest(basis=entry.template_name):
                self.assertIs(entry.disposition, FamilyDisposition.AVAILABLE)
                self.assertIs(entry.remediation, Remediation.NONE)
                self.assertEqual(entry.unsatisfied_slots, ())
                self.assertTrue(entry.available)

    def test_every_frame_driven_task_mode_resolves_to_one_shared_family(self) -> None:
        """Plan section 8.3. `t2v` and `i2v` instantiate the same subgraph, so
        `fl2va` and `l2va` are that subgraph with a promoted input bound rather
        than topologies of their own."""

        profile = qualify_generation_profile(observation(), expected_template_digests=EXPECTED)
        for mode in ("t2va", "i2va", "fl2va", "l2va"):
            with self.subTest(mode=mode):
                self.assertIs(profile.for_task_mode(mode).family, ModeFamily.IMAGE_TO_VIDEO)
        self.assertIs(profile.for_task_mode("ref2va").family, ModeFamily.REFERENCE_TO_VIDEO)
        # A shared family is not a shared basis: `t2va` loads the `t2v` bytes and
        # `i2va` the `i2v` bytes, and qualification follows the bytes.
        self.assertEqual(profile.for_task_mode("t2va").template_name, "video_minimax_h3_t2v")
        for mode in ("i2va", "fl2va", "l2va"):
            with self.subTest(mode=mode):
                self.assertEqual(profile.for_task_mode(mode).template_name, "video_minimax_h3_i2v")
        with self.assertRaises(GenerationProfileError):
            profile.for_task_mode("nonsense")

    def test_the_measured_pinned_host_reports_missing_assets_not_availability(self) -> None:
        """The state of the supplied host on 2026-08-18: both VAEs resolve, the
        two H3 UNET defaults and the text-encoder default do not. Generation must
        be unavailable with remediation, and the remediation must be the one D2
        froze -- select an installed asset on the canvas -- not an automatic
        substitution."""

        profile = qualify_generation_profile(
            observation(present={AssetSlot.VIDEO_VAE, AssetSlot.AUDIO_VAE}),
            expected_template_digests=EXPECTED,
        )
        image = profile.for_task_mode("i2va")
        reference = profile.for_task_mode("ref2va")
        self.assertIs(image.disposition, FamilyDisposition.MISSING_ASSET)
        self.assertIs(image.remediation, Remediation.SELECT_INSTALLED_ASSET_ON_CANVAS)
        self.assertEqual(image.unsatisfied_slots, (AssetSlot.VIDEO_UNET, AssetSlot.TEXT_ENCODER))
        self.assertEqual(
            reference.unsatisfied_slots,
            (AssetSlot.REFERENCE_UNET, AssetSlot.TEXT_ENCODER),
        )
        self.assertFalse(image.available)
        self.assertFalse(reference.available)

    def test_one_family_can_be_available_while_the_other_is_not(self) -> None:
        """The two families need different UNET slots, so a host with only the
        reference weight must not report the frame-driven modes as generable."""

        profile = qualify_generation_profile(
            observation(
                present={
                    AssetSlot.REFERENCE_UNET,
                    AssetSlot.TEXT_ENCODER,
                    AssetSlot.VIDEO_VAE,
                    AssetSlot.AUDIO_VAE,
                }
            ),
            expected_template_digests=EXPECTED,
        )
        self.assertTrue(profile.for_task_mode("ref2va").available)
        for mode in ("t2va", "i2va"):
            with self.subTest(mode=mode):
                self.assertFalse(profile.for_task_mode(mode).available)
                self.assertEqual(
                    profile.for_task_mode(mode).unsatisfied_slots,
                    (AssetSlot.VIDEO_UNET,),
                )

    def test_mixed_absent_and_relocated_roles_share_one_ordered_remediation_round(self) -> None:
        """M17-29: severity stays missing, but relocated roles must not disappear."""

        dispositions = {
            AssetSlot.VIDEO_UNET: SlotDisposition.ABSENT,
            AssetSlot.TEXT_ENCODER: SlotDisposition.RELOCATED,
            AssetSlot.VIDEO_VAE: SlotDisposition.PRESENT,
            AssetSlot.AUDIO_VAE: SlotDisposition.RELOCATED,
            AssetSlot.REFERENCE_UNET: SlotDisposition.PRESENT,
        }
        profile = qualify_generation_profile(
            HostObservation(
                anchor_node_types=ANCHORS,
                template_digests=EXPECTED,
                slots=tuple(
                    SlotObservation(slot=slot, disposition=dispositions[slot]) for slot in AssetSlot
                ),
                host_version=QUALIFIED_COMFYUI_HOST_VERSION,
                template_capabilities=CAPABILITIES,
            ),
            expected_template_digests=EXPECTED,
        )
        image = profile.for_task_mode("i2va")
        self.assertIs(image.disposition, FamilyDisposition.MISSING_ASSET)
        self.assertEqual(
            image.unsatisfied_slots,
            (
                AssetSlot.VIDEO_UNET,
                AssetSlot.TEXT_ENCODER,
                AssetSlot.AUDIO_VAE,
            ),
        )

    def test_missing_template_prompt_capability_outranks_a_missing_asset(self) -> None:
        """A template without the splice socket cannot be repaired by a weight."""

        drifted = qualify_generation_profile(
            observation(
                present=set(),
                digests=(
                    ("video_minimax_h3_t2v", T2V_DIGEST),
                    ("video_minimax_h3_i2v", "d" * 64),
                    ("video_minimax_h3_r2v", R2V_DIGEST),
                ),
                capabilities=tuple(
                    TemplateCapability(
                        template_name=item.template_name,
                        anchor_node_type=item.anchor_node_type,
                        input_names=(
                            frozenset()
                            if item.template_name == "video_minimax_h3_i2v"
                            else item.input_names
                        ),
                    )
                    for item in CAPABILITIES
                ),
            ),
            expected_template_digests=EXPECTED,
        )
        image = drifted.for_task_mode("i2va")
        self.assertIs(image.disposition, FamilyDisposition.TEMPLATE_DRIFT)
        self.assertIs(image.remediation, Remediation.REQUALIFY_TEMPLATE)
        self.assertEqual(image.unsatisfied_slots, ())
        # The undrifted family is still judged on its own merits.
        self.assertIs(
            drifted.for_task_mode("ref2va").disposition,
            FamilyDisposition.MISSING_ASSET,
        )

    def test_an_absent_template_capability_is_drift_rather_than_availability(self) -> None:
        profile = qualify_generation_profile(
            observation(
                digests=(("video_minimax_h3_r2v", R2V_DIGEST),),
                capabilities=(CAPABILITIES[2],),
            ),
            expected_template_digests=EXPECTED,
        )
        for mode in ("t2va", "i2va"):
            with self.subTest(mode=mode):
                self.assertIs(
                    profile.for_task_mode(mode).disposition,
                    FamilyDisposition.TEMPLATE_DRIFT,
                )

    def test_a_missing_t2v_prompt_socket_does_not_travel_to_other_modes(self) -> None:
        """`t2va` and `i2va` share an anchor and a slot set but not a basis. A
        family-keyed qualification reported the `i2v` answer for both, so a host
        serving drifted `t2v` bytes was told `t2va` was available and would have
        materialized from bytes this repository never qualified."""

        profile = qualify_generation_profile(
            observation(
                digests=(
                    ("video_minimax_h3_t2v", "f" * 64),
                    ("video_minimax_h3_i2v", I2V_DIGEST),
                    ("video_minimax_h3_r2v", R2V_DIGEST),
                ),
                capabilities=(
                    TemplateCapability(
                        template_name="video_minimax_h3_t2v",
                        anchor_node_type="MiniMaxH3ImageToVideo",
                        input_names=frozenset(),
                    ),
                    CAPABILITIES[1],
                    CAPABILITIES[2],
                ),
            ),
            expected_template_digests=EXPECTED,
        )
        self.assertIs(profile.for_task_mode("t2va").disposition, FamilyDisposition.TEMPLATE_DRIFT)
        self.assertIs(profile.for_task_mode("t2va").remediation, Remediation.REQUALIFY_TEMPLATE)
        for mode in ("i2va", "fl2va", "l2va", "ref2va"):
            with self.subTest(mode=mode):
                self.assertTrue(profile.for_task_mode(mode).available)

    def test_host_version_is_provenance_and_does_not_override_capability(self) -> None:

        profile = qualify_generation_profile(
            observation(host_version="0.31.0"), expected_template_digests=EXPECTED
        )
        for entry in profile.families:
            with self.subTest(basis=entry.template_name):
                self.assertIs(entry.disposition, FamilyDisposition.AVAILABLE)
                self.assertIs(entry.remediation, Remediation.NONE)
                self.assertEqual(entry.unsatisfied_slots, ())

    def test_a_missing_native_anchor_is_an_unsupported_host(self) -> None:
        profile = qualify_generation_profile(
            observation(anchors=frozenset({"MiniMaxH3ImageToVideo"})),
            expected_template_digests=EXPECTED,
        )
        self.assertTrue(profile.for_task_mode("i2va").available)
        self.assertTrue(profile.for_task_mode("t2va").available)
        self.assertIs(
            profile.for_task_mode("ref2va").disposition,
            FamilyDisposition.UNSUPPORTED_HOST,
        )

    def test_the_projection_carries_no_host_filename_or_path(self) -> None:
        """The privacy guarantee is structural: there is no field a filename
        could travel in. This asserts it on the serialized wire rather than
        trusting the dataclass shape."""

        wire = qualify_generation_profile(
            observation(present={AssetSlot.VIDEO_VAE}), expected_template_digests=EXPECTED
        ).to_wire()

        def strings(value: object) -> list[str]:
            if isinstance(value, str):
                return [value]
            if isinstance(value, dict):
                return [item for entry in value.values() for item in strings(entry)]
            if isinstance(value, list):
                return [item for entry in value for item in strings(entry)]
            return []

        # Every string the wire carries is checked, not the repr, because a
        # repr contains its own punctuation and would make the assertion pass or
        # fail for reasons that have nothing to do with the payload.
        for text in strings(wire):
            with self.subTest(value=text):
                for marker in (".safetensors", ".ckpt", ".pt", "/", "\\", ":", " "):
                    self.assertNotIn(marker, text)
        families = cast(list[dict[str, object]], wire["families"])
        self.assertEqual(
            {entry["family"] for entry in families},
            {family.value for family in ModeFamily},
        )

    def test_the_projection_is_fingerprintable_and_stable(self) -> None:
        first = qualify_generation_profile(observation(), expected_template_digests=EXPECTED)
        second = qualify_generation_profile(observation(), expected_template_digests=EXPECTED)
        self.assertEqual(first.fingerprint(), second.fingerprint())
        changed = qualify_generation_profile(
            observation(present={AssetSlot.VIDEO_VAE}), expected_template_digests=EXPECTED
        )
        self.assertNotEqual(first.fingerprint(), changed.fingerprint())

    def test_observations_and_projections_reject_malformed_contracts(self) -> None:
        with self.assertRaises(GenerationProfileError):
            SlotObservation(slot=AssetSlot.VIDEO_VAE, disposition="present")  # type: ignore[arg-type]
        with self.assertRaises(GenerationProfileError):
            HostObservation(
                anchor_node_types=ANCHORS,
                template_digests=EXPECTED,
                slots=(
                    SlotObservation(slot=AssetSlot.VIDEO_VAE, disposition=SlotDisposition.PRESENT),
                    SlotObservation(slot=AssetSlot.VIDEO_VAE, disposition=SlotDisposition.ABSENT),
                ),
                host_version=QUALIFIED_COMFYUI_HOST_VERSION,
                template_capabilities=CAPABILITIES,
            )
        with self.assertRaises(GenerationProfileError):
            HostObservation(
                anchor_node_types=ANCHORS,
                template_digests=(("video_minimax_h3_i2v", "not-a-digest"),),
                slots=(),
                host_version=QUALIFIED_COMFYUI_HOST_VERSION,
                template_capabilities=CAPABILITIES,
            )
        with self.assertRaises(GenerationProfileError):
            GenerationProfile(families=(), template_revision=PINNED_TEMPLATE_REVISION)
        with self.assertRaises(GenerationProfileError):
            qualify_generation_profile(
                observation(), expected_template_digests=(("bad name", I2V_DIGEST),)
            )

    def test_the_family_slot_and_template_tables_agree_with_the_pinned_anchors(self) -> None:
        """A table that drifts from the host schemas would qualify the wrong
        thing silently. Section 8.1 records that only the reference anchor takes
        an `audio_vae` of its own, and that both families need a text encoder."""

        self.assertEqual(set(FAMILY_SLOTS), set(ModeFamily))
        # Every task mode has a basis, and every basis is reachable from a mode.
        self.assertEqual(set(MODE_TEMPLATE), set(TASK_MODE_FAMILY))
        bases = materialization_bases()
        self.assertEqual(
            [name for name, _, _ in bases],
            ["video_minimax_h3_t2v", "video_minimax_h3_i2v", "video_minimax_h3_r2v"],
        )
        self.assertEqual(
            sorted(mode for _, _, modes in bases for mode in modes),
            sorted(TASK_MODE_FAMILY),
        )
        self.assertIn(AssetSlot.VIDEO_UNET, FAMILY_SLOTS[ModeFamily.IMAGE_TO_VIDEO])
        self.assertIn(AssetSlot.REFERENCE_UNET, FAMILY_SLOTS[ModeFamily.REFERENCE_TO_VIDEO])
        self.assertNotIn(AssetSlot.REFERENCE_UNET, FAMILY_SLOTS[ModeFamily.IMAGE_TO_VIDEO])
        for family, slots in FAMILY_SLOTS.items():
            with self.subTest(family=family.value):
                self.assertEqual(len(slots), len(set(slots)))
                self.assertIn(AssetSlot.TEXT_ENCODER, slots)
                self.assertIn(AssetSlot.AUDIO_VAE, slots)


if __name__ == "__main__":
    unittest.main()
