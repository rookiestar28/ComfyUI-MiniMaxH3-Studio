"""M20-03 focused tests for the authoring-workspace adapter.

The rows here hold apart what the adapter must keep apart:

* the domains stay canonical -- every wire mutation maps onto one M20-00/M20-02 command and a
  domain rejection returns 409 with the untouched projection and the machine-readable code;
* seeding is honest -- accepted registry assets admit in connection order with canonical
  labels, pairings seed as included-but-blocked-unknown, and a source the domain cannot hold
  is listed as not-admissible with its exact reason, never dropped;
* availability has one producer -- the frozen producer identity is enforced at the wire, the
  producer revision is monotonic, and no reference or timeline action can move availability;
* the transport is bounded and replayable -- closed vocabulary, byte/depth/node/array bounds,
  a replaying request ledger, TTL tombstones and workspace capacity.

No fixture carries a prompt, media value, path, URL, filename or credential.
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from comfyui_h3_context.adapters import comfyui_authoring_workspace as authoring_route
from comfyui_h3_context.adapters.authoring_source_binding import (
    AuthoringSourceBindingReceipt,
    RuntimeVideoCapability,
)
from comfyui_h3_context.adapters.comfyui_authoring_workspace import (
    AUTHORING_ACTION_SCHEMA,
    AUTHORING_AVAILABILITY_PRODUCER,
    AUTHORING_PROJECTION_SCHEMA,
    AUTHORING_SNAP_SCHEMA,
    MAX_AUTHORING_ACTION_BYTES,
    TIMELINE_HISTORY_PROJECTION_SCHEMA,
    AuthoringWorkbenchError,
    AuthoringWorkspaceRegistry,
    decode_authoring_action_json,
    ensure_authoring_route_registered,
)
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import (
    SidebarAuthoringSeed,
    SidebarAuthoringSource,
    SidebarAuthoringWorkspaceClaim,
)
from comfyui_h3_context.core.authoring_preview_protocol import AuthoringPreviewRequest
from comfyui_h3_context.core.canonical import canonical_fingerprint
from comfyui_h3_context.core.composition_contract import (
    PublicCompositionSnapshot,
    decode_public_snapshot,
    public_snapshot_fingerprint,
)
from comfyui_h3_context.core.contracts import MediaKind, TaskMode
from comfyui_h3_context.core.nle_authoring_contract import (
    NLE_AUTHORING_PROFILE_ID,
    NLE_AUTHORING_SCHEMA,
    NLE_OPERATION_PROFILE_ID,
    TIMELINE_TRANSACTION_SCHEMA_V2,
)
from comfyui_h3_context.core.registry import build_reference_registry
from comfyui_h3_context.core.timeline_history import (
    MAX_TRANSACTION_BYTES,
    TIMELINE_RECEIPT_SCHEMA,
    TIMELINE_TRANSACTION_SCHEMA,
)

FP = "sha256:" + "0" * 64
M25_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "m25_10_composition_contract_v1.json"


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _CurrentSource:
    def current(self) -> bool:
        return True


class _SourceBindingReceipt(AuthoringSourceBindingReceipt):
    def __init__(self) -> None:
        super().__init__(exact_registry=build_reference_registry(()), generation=1)
        self.release_count = 0
        self.source = _CurrentSource()
        self.claim_count = 0

    def _capability_for(self, source_id: str) -> RuntimeVideoCapability:
        if source_id == "vid-1":
            return RuntimeVideoCapability.AVAILABLE
        return RuntimeVideoCapability.UNSUPPORTED

    def _claim_source(self, source_id: str) -> object:
        if source_id != "vid-1":
            raise AssertionError("only the admitted video can be claimed")
        self.claim_count += 1
        return self.source

    def _release_sources(self) -> None:
        self.release_count += 1


def row(
    asset_id: str,
    kind: MediaKind,
    duration: int | None,
    *,
    paired: str | None = None,
    order: int,
) -> SidebarAuthoringSource:
    return SidebarAuthoringSource(
        asset_id=asset_id,
        kind=kind,
        duration_milliseconds=duration,
        paired_video_id=paired,
        connection_order=order,
        identity_fingerprint=FP,
    )


def standard_seed() -> SidebarAuthoringSeed:
    return SidebarAuthoringSeed(
        source_id="report-1",
        task_mode=TaskMode.T2VA,
        registry_fingerprint=FP,
        sources=(
            row("img-1", MediaKind.IMAGE, None, order=1),
            row("vid-1", MediaKind.VIDEO, 5_000, order=2),
            row("aud-1", MediaKind.AUDIO, 5_000, paired="vid-1", order=3),
            row("aud-2", MediaKind.AUDIO, 5_000, order=4),
        ),
    )


def history_snapshot(workspace_handle: str) -> PublicCompositionSnapshot:
    document = json.loads(M25_FIXTURE_PATH.read_text(encoding="utf-8"))
    wire = copy.deepcopy(document["snapshot"])
    wire["workspace_handle"] = workspace_handle
    wire["workspace_fingerprint"] = canonical_fingerprint(
        {
            "project_id": wire["project_id"],
            "workspace_handle": workspace_handle,
            "workspace_revision": wire["workspace_revision"],
            "timeline_revision": wire["timeline_revision"],
            "timeline_fingerprint": wire["timeline_fingerprint"],
        }
    )
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    return decode_public_snapshot(wire)


def timeline_transaction(
    snapshot: PublicCompositionSnapshot,
    *,
    request_id: str,
    commands: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "schema": TIMELINE_TRANSACTION_SCHEMA,
        "request_id": request_id,
        "transaction_id": f"tx-{request_id}",
        "workspace_handle": snapshot.workspace_handle,
        "expected_workspace_revision": snapshot.workspace_revision,
        "expected_timeline_revision": snapshot.timeline_revision,
        "expected_timeline_fingerprint": snapshot.timeline_fingerprint,
        "commands": commands,
    }


class _Harness:
    def __init__(self, seed: SidebarAuthoringSeed | None = None, **kwargs: object) -> None:
        self.seed = seed if seed is not None else standard_seed()
        self.clock = _Clock()

        def claim(handle: str) -> SidebarAuthoringSeed:
            if handle != "ws-live":
                raise KeyError("workspace is unavailable")
            return self.seed

        self.registry = AuthoringWorkspaceRegistry(
            seed_claim=claim,
            clock=self.clock,
            **kwargs,  # type: ignore[arg-type]
        )
        self._counter = 0

    def act(self, action: str, **payload: object) -> dict[str, object]:
        self._counter += 1
        return {
            "schema": AUTHORING_ACTION_SCHEMA,
            "request_id": f"req-{self._counter}",
            "action": action,
            "payload": payload,
        }

    def create(self) -> tuple[str, dict[str, object]]:
        result = self.registry.dispatch(
            self.act("create_authoring_workspace", context_workspace_handle="ws-live")
        )
        assert result.status == 201 and result.body is not None
        return str(result.body["workspace_handle"]), result.body


def reference_of(body: dict[str, object]) -> dict[str, object]:
    part = body["reference"]
    assert type(part) is dict
    return part


def timeline_of(body: dict[str, object]) -> dict[str, object]:
    part = body["timeline"]
    assert type(part) is dict
    return part


def canonical_labels(body: dict[str, object]) -> list[tuple[str, str]]:
    rows = reference_of(body)["canonical"]
    assert type(rows) is list
    return [(str(item["source_id"]), str(item["label"])) for item in rows]


def rejection_code(body: dict[str, object] | None) -> str | None:
    assert body is not None
    value = body["rejection"]
    if value is None:
        return None
    assert type(value) is dict
    return str(value["code"])


class DecodeBoundaryTests(unittest.TestCase):
    """The one action wire is closed, bounded and producer-aware."""

    def _encode(self, value: dict[str, object]) -> bytes:
        import json

        return json.dumps(value).encode("utf-8")

    def _action(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        return {
            "schema": AUTHORING_ACTION_SCHEMA,
            "request_id": "req-1",
            "action": action,
            "payload": payload,
        }

    def test_root_schema_action_and_payload_are_closed(self) -> None:
        with self.assertRaises(ValueError):
            decode_authoring_action_json(b"")
        with self.assertRaises(ValueError):
            decode_authoring_action_json(self._encode({"schema": "x"}))
        with self.assertRaises(ValueError):
            decode_authoring_action_json(
                self._encode(self._action("not_an_action", {"workspace_handle": "h"}))
            )
        with self.assertRaises(ValueError):
            decode_authoring_action_json(
                self._encode(self._action("read_projection", {"workspace_handle": "h", "extra": 1}))
            )
        decoded = decode_authoring_action_json(
            self._encode(self._action("read_projection", {"workspace_handle": "h"}))
        )
        self.assertEqual(decoded["action"], "read_projection")

    def test_only_the_frozen_availability_producer_passes_the_wire(self) -> None:
        payload: dict[str, object] = {
            "workspace_handle": "h",
            "producer": "frontend.somewhere.else",
            "producer_revision": 1,
            "fingerprint": FP,
            "facts": [{"video_id": "vid-1", "availability": "available"}],
        }
        with self.assertRaises(ValueError):
            decode_authoring_action_json(self._encode(self._action("set_availability", payload)))
        payload["producer"] = AUTHORING_AVAILABILITY_PRODUCER
        decoded = decode_authoring_action_json(
            self._encode(self._action("set_availability", payload))
        )
        self.assertEqual(decoded["action"], "set_availability")
        payload["facts"] = [{"video_id": "vid-1", "availability": "probably"}]
        with self.assertRaises(ValueError):
            decode_authoring_action_json(self._encode(self._action("set_availability", payload)))

    def test_envelope_points_and_group_arrays_stay_bounded_and_closed(self) -> None:
        base: dict[str, object] = {
            "workspace_handle": "h",
            "expected_timeline_revision": 1,
            "clip_id": "clip-1",
        }
        with self.assertRaises(ValueError):
            decode_authoring_action_json(
                self._encode(
                    self._action(
                        "set_envelope",
                        {**base, "points": [{"offset_frames": 1, "extra": 2}]},
                    )
                )
            )
        with self.assertRaises(ValueError):
            decode_authoring_action_json(
                self._encode(
                    self._action(
                        "set_envelope",
                        {
                            **base,
                            "points": [{"offset_frames": 1, "strength_per_mille": 2_000}],
                        },
                    )
                )
            )

    def test_timeline_transaction_action_is_closed_and_shares_the_outer_request_id(self) -> None:
        snapshot = history_snapshot("authoring-fixture")
        payload = timeline_transaction(
            snapshot,
            request_id="req-history-1",
            commands=[{"kind": "select_clips", "payload": {"clip_ids": []}}],
        )
        decoded = decode_authoring_action_json(
            self._encode(
                {
                    "schema": AUTHORING_ACTION_SCHEMA,
                    "request_id": "req-history-1",
                    "action": "apply_timeline_transaction",
                    "payload": payload,
                }
            )
        )
        self.assertEqual(decoded["payload"], payload)

        with self.assertRaises(ValueError):
            decode_authoring_action_json(
                self._encode(
                    {
                        "schema": AUTHORING_ACTION_SCHEMA,
                        "request_id": "different-request",
                        "action": "apply_timeline_transaction",
                        "payload": payload,
                    }
                )
            )
        with self.assertRaises(ValueError):
            decode_authoring_action_json(
                self._encode(
                    {
                        "schema": AUTHORING_ACTION_SCHEMA,
                        "request_id": "req-history-1",
                        "action": "apply_timeline_transaction",
                        "payload": {**payload, "snapshot": snapshot.to_wire()},
                    }
                )
            )

    def test_v2_timeline_transaction_action_requires_its_exact_authoring_profile(self) -> None:
        payload: dict[str, object] = {
            "schema": TIMELINE_TRANSACTION_SCHEMA_V2,
            "authoring_schema": NLE_AUTHORING_SCHEMA,
            "profile_id": NLE_AUTHORING_PROFILE_ID,
            "operation_profile_id": NLE_OPERATION_PROFILE_ID,
            "request_id": "req-history-v2",
            "transaction_id": "tx-req-history-v2",
            "workspace_handle": "authoring-v2",
            "expected_workspace_revision": 1,
            "expected_timeline_revision": 1,
            "expected_timeline_fingerprint": FP,
            "expected_authoring_fingerprint": FP,
            "commands": [{"kind": "select_clips", "payload": {"clip_ids": []}}],
        }
        action = {
            "schema": AUTHORING_ACTION_SCHEMA,
            "request_id": "req-history-v2",
            "action": "apply_timeline_transaction",
            "payload": payload,
        }

        decoded = decode_authoring_action_json(self._encode(cast(dict[str, object], action)))
        self.assertEqual(decoded["payload"], payload)

        for changed in (
            {**payload, "profile_id": "h3.authoring.unknown"},
            {key: value for key, value in payload.items() if key != "authoring_schema"},
            {**payload, "unexpected": True},
        ):
            with self.subTest(changed=sorted(changed)):
                with self.assertRaises(ValueError):
                    decode_authoring_action_json(self._encode({**action, "payload": changed}))

    def test_read_timeline_history_payload_is_exact(self) -> None:
        decoded = decode_authoring_action_json(
            self._encode(
                self._action("read_timeline_history", {"workspace_handle": "authoring-fixture"})
            )
        )
        self.assertEqual(decoded["action"], "read_timeline_history")
        with self.assertRaises(ValueError):
            decode_authoring_action_json(
                self._encode(
                    self._action(
                        "read_timeline_history",
                        {"workspace_handle": "authoring-fixture", "snapshot": {}},
                    )
                )
            )

    def test_outer_transaction_route_retains_fixed_framing_headroom(self) -> None:
        self.assertEqual(MAX_AUTHORING_ACTION_BYTES, MAX_TRANSACTION_BYTES + 1_024)

    def test_registration_without_a_host_is_a_safe_no(self) -> None:
        self.assertFalse(ensure_authoring_route_registered())


class SeedingTests(unittest.TestCase):
    """AC-M20-03-06/07: canonical labels, explicit soundtrack state, honest inadmissibility."""

    def test_create_seeds_canonical_labels_pairing_and_blocked_unknown(self) -> None:
        harness = _Harness()
        _handle, body = harness.create()
        self.assertEqual(body["schema"], AUTHORING_PROJECTION_SCHEMA)
        self.assertEqual(body["task_mode"], "t2va")
        # Pairing is seeded as included intent, availability is unknown, so the bound audio
        # projects no label and the queue is blocked -- exactly the M20-00 derivation.
        self.assertEqual(
            canonical_labels(body),
            [("img-1", "<Picture 1>"), ("vid-1", "<Video 1>"), ("aud-2", "<Audio 1>")],
        )
        soundtracks = reference_of(body)["soundtracks"]
        assert type(soundtracks) is list
        self.assertEqual(
            [(item["video_id"], item["derived_state"]) for item in soundtracks],
            [("vid-1", "blocked_unknown")],
        )
        blockers = reference_of(body)["queue_blockers"]
        assert type(blockers) is list
        self.assertEqual(len(blockers), 1)

    def test_labels_agree_with_the_accepted_sidebar_candidate_shape(self) -> None:
        import re

        harness = _Harness()
        _handle, body = harness.create()
        label_shape = re.compile(r"^<(Picture|Video|Audio) [1-9][0-9]{0,2}>$")
        rows = reference_of(body)["canonical"]
        assert type(rows) is list
        for item in rows:
            self.assertRegex(str(item["label"]), label_shape)
            if item["paired_with"] is not None:
                self.assertEqual(item["kind"], "audio")
                self.assertRegex(str(item["paired_with"]), r"^<Video [1-9][0-9]{0,2}>$")

    def test_a_timed_source_without_duration_is_listed_not_admissible(self) -> None:
        seed = SidebarAuthoringSeed(
            source_id="report-2",
            task_mode=TaskMode.I2VA,
            registry_fingerprint=FP,
            sources=(
                row("vid-1", MediaKind.VIDEO, None, order=1),
                row("img-1", MediaKind.IMAGE, None, order=2),
            ),
        )
        harness = _Harness(seed)
        _handle, body = harness.create()
        sources = reference_of(body)["sources"]
        assert type(sources) is list
        by_id = {str(item["source_id"]): item for item in sources}
        self.assertFalse(by_id["vid-1"]["admitted"])
        self.assertFalse(by_id["vid-1"]["admissible"])
        self.assertEqual(by_id["vid-1"]["reason"], "missing_duration")
        self.assertTrue(by_id["img-1"]["admitted"])
        self.assertEqual(canonical_labels(body), [("img-1", "<Picture 1>")])

    def test_an_unknown_context_workspace_is_404(self) -> None:
        harness = _Harness()
        with self.assertRaises(AuthoringWorkbenchError) as caught:
            harness.registry.dispatch(
                harness.act("create_authoring_workspace", context_workspace_handle="ws-dead")
            )
        error = caught.exception
        self.assertEqual((error.status, error.code), (404, "context_unavailable"))


class AvailabilityAuthorityTests(unittest.TestCase):
    """AC-M20-03-08: the UI cannot set availability; the one producer moves it monotonically."""

    def test_producer_facts_unblock_and_stale_producer_revision_rejects(self) -> None:
        harness = _Harness()
        handle, _body = harness.create()
        result = harness.registry.dispatch(
            harness.act(
                "set_availability",
                workspace_handle=handle,
                producer=AUTHORING_AVAILABILITY_PRODUCER,
                producer_revision=1,
                fingerprint=FP,
                facts=[{"video_id": "vid-1", "availability": "available"}],
            )
        )
        self.assertEqual(result.status, 200)
        assert result.body is not None
        self.assertEqual(
            canonical_labels(result.body),
            [
                ("img-1", "<Picture 1>"),
                ("aud-1", "<Audio 1>"),
                ("vid-1", "<Video 1>"),
                ("aud-2", "<Audio 2>"),
            ],
        )
        self.assertEqual(reference_of(result.body)["queue_blockers"], [])
        stale = harness.registry.dispatch(
            harness.act(
                "set_availability",
                workspace_handle=handle,
                producer=AUTHORING_AVAILABILITY_PRODUCER,
                producer_revision=1,
                fingerprint=FP,
                facts=[{"video_id": "vid-1", "availability": "unavailable"}],
            )
        )
        self.assertEqual(stale.status, 409)
        self.assertEqual(rejection_code(stale.body), "stale_availability")

    def test_no_reference_action_can_express_availability(self) -> None:
        self.assertNotIn(
            "availability",
            {
                key
                for keys in ({"workspace_handle", "expected_reference_revision", "source_id"},)
                for key in keys
            },
        )
        # The wire itself is the guard: every reference/timeline payload key set excludes
        # availability fields, proven by the closed-payload decode test above.


class ReferenceCommandTests(unittest.TestCase):
    """AC-M20-03-02/06: backend revision canonical; bounded commands; visible state."""

    def test_stale_revision_returns_409_with_projection_and_code(self) -> None:
        harness = _Harness()
        handle, body = harness.create()
        revision = reference_of(body)["revision"]
        result = harness.registry.dispatch(
            harness.act(
                "remove_source",
                workspace_handle=handle,
                expected_reference_revision=999_999,
                source_id="img-1",
            )
        )
        self.assertEqual(result.status, 409)
        self.assertEqual(rejection_code(result.body), "stale_revision")
        assert result.body is not None
        self.assertEqual(reference_of(result.body)["revision"], revision)

    def test_exclude_remove_readd_and_reorder_round_trip(self) -> None:
        harness = _Harness()
        handle, body = harness.create()

        def revision() -> int:
            return int(str(reference_of(body)["revision"]))

        def run(action: str, **payload: object) -> dict[str, object]:
            result = harness.registry.dispatch(
                harness.act(action, workspace_handle=handle, **payload)
            )
            self.assertEqual(result.status, 200)
            assert result.body is not None
            return result.body

        body = run(
            "exclude_soundtrack",
            expected_reference_revision=revision(),
            video_id="vid-1",
        )
        # An explicit exclude visibly returns the audio to the standalone pool.
        self.assertIn(("aud-1", "<Audio 1>"), canonical_labels(body))
        body = run("remove_source", expected_reference_revision=revision(), source_id="aud-2")
        self.assertNotIn("aud-2", [asset_id for asset_id, _ in canonical_labels(body)])
        sources = reference_of(body)["sources"]
        assert type(sources) is list
        aud2 = next(item for item in sources if item["source_id"] == "aud-2")
        self.assertFalse(aud2["admitted"])
        self.assertTrue(aud2["admissible"])
        body = run("add_source", expected_reference_revision=revision(), source_id="aud-2")
        self.assertIn("aud-2", [asset_id for asset_id, _ in canonical_labels(body)])
        body = run(
            "reorder_source",
            expected_reference_revision=revision(),
            source_id="aud-2",
            new_index=0,
        )
        audio_rows = [
            asset_id for asset_id, label in canonical_labels(body) if label.startswith("<Audio")
        ]
        self.assertEqual(audio_rows[0], "aud-2")

    def test_adding_a_source_outside_the_universe_is_422(self) -> None:
        harness = _Harness()
        handle, _body = harness.create()
        with self.assertRaises(AuthoringWorkbenchError) as caught:
            harness.registry.dispatch(
                harness.act(
                    "add_source",
                    workspace_handle=handle,
                    expected_reference_revision=1,
                    source_id="vid-ghost",
                )
            )
        error = caught.exception
        self.assertEqual((error.status, error.code), (422, "unknown_source"))


class TimelineCommandTests(unittest.TestCase):
    """AC-M20-03-01/06: timeline commands map one-to-one; drift is a visible blocker."""

    def _created(self) -> tuple[_Harness, str, dict[str, object]]:
        harness = _Harness()
        handle, body = harness.create()
        return harness, handle, body

    def _run(
        self, harness: _Harness, handle: str, action: str, **payload: object
    ) -> tuple[int, dict[str, object] | None]:
        result = harness.registry.dispatch(harness.act(action, workspace_handle=handle, **payload))
        return result.status, result.body

    def test_add_clip_names_are_server_owned_and_revisions_guard(self) -> None:
        harness, handle, body = self._created()
        status, body2 = self._run(
            harness,
            handle,
            "add_clip",
            expected_timeline_revision=int(str(timeline_of(body)["revision"])),
            asset_id="vid-1",
            lane=0,
            start_frame=0,
            frames=12,
            source_start_frame=0,
        )
        self.assertEqual(status, 200)
        assert body2 is not None
        clips = timeline_of(body2)["clips"]
        assert type(clips) is list
        self.assertEqual([item["clip_id"] for item in clips], ["clip-1"])
        status, body3 = self._run(
            harness,
            handle,
            "add_clip",
            expected_timeline_revision=999,
            asset_id="vid-1",
            lane=1,
            start_frame=0,
            frames=12,
            source_start_frame=0,
        )
        self.assertEqual(status, 409)
        self.assertEqual(rejection_code(body3), "stale_revision")

    def test_link_split_merge_and_alignment_rules_reach_the_domain(self) -> None:
        harness, handle, body = self._created()
        revision = int(str(timeline_of(body)["revision"]))

        def step(action: str, expected_status: int, **payload: object) -> dict[str, object]:
            nonlocal revision
            status, out = self._run(
                harness,
                handle,
                action,
                expected_timeline_revision=revision,
                **payload,
            )
            self.assertEqual(status, expected_status)
            assert out is not None
            revision = int(str(timeline_of(out)["revision"]))
            return out

        step(
            "add_clip",
            200,
            asset_id="vid-1",
            lane=0,
            start_frame=0,
            frames=12,
            source_start_frame=0,
        )
        step(
            "add_clip",
            200,
            asset_id="aud-2",
            lane=0,
            start_frame=0,
            frames=12,
            source_start_frame=0,
        )
        out = step("link_clips", 200, video_clip_id="clip-1", audio_clip_id="clip-2")
        links = timeline_of(out)["links"]
        assert type(links) is list
        self.assertEqual(len(links), 1)
        out = step("move_clip", 409, clip_id="clip-1", delta_frames=1, delta_lanes=0)
        self.assertEqual(rejection_code(out), "audio_alignment")
        step("unlink_clips", 200, video_clip_id="clip-1")
        out = step("split_clip", 200, clip_id="clip-1", at_offset_frames=6)
        clips = timeline_of(out)["clips"]
        assert type(clips) is list
        self.assertEqual(len(clips), 3)
        out = step("merge_clips", 200, first_clip_id="clip-1", second_clip_id="clip-3")
        clips = timeline_of(out)["clips"]
        assert type(clips) is list
        self.assertEqual(len(clips), 2)

    def test_reference_drift_surfaces_as_timeline_blockers(self) -> None:
        harness, handle, body = self._created()
        step_status, body2 = self._run(
            harness,
            handle,
            "add_clip",
            expected_timeline_revision=int(str(timeline_of(body)["revision"])),
            asset_id="vid-1",
            lane=0,
            start_frame=0,
            frames=12,
            source_start_frame=0,
        )
        self.assertEqual(step_status, 200)
        assert body2 is not None
        status, body3 = self._run(
            harness,
            handle,
            "remove_source",
            expected_reference_revision=int(str(reference_of(body2)["revision"])),
            source_id="vid-1",
        )
        self.assertEqual(status, 200)
        assert body3 is not None
        blockers = timeline_of(body3)["blockers"]
        assert type(blockers) is list
        self.assertEqual(
            [(item["clip_id"], item["code"]) for item in blockers],
            [("clip-1", "asset_missing")],
        )

    def test_read_snap_is_bounded_and_schema_stable(self) -> None:
        harness, handle, _body = self._created()
        result = harness.registry.dispatch(
            harness.act(
                "read_snap",
                workspace_handle=handle,
                frame=50,
                playhead_frame=None,
                exclude_clip_id=None,
            )
        )
        self.assertEqual(result.status, 200)
        assert result.body is not None
        self.assertEqual(result.body["schema"], AUTHORING_SNAP_SCHEMA)
        candidates = result.body["candidates"]
        assert type(candidates) is list
        self.assertTrue(candidates)


class TimelineHistoryRouteTests(unittest.TestCase):
    """M25-11: trusted snapshot binding and the closed transaction route stay backend-owned."""

    def test_history_is_unavailable_until_a_backend_snapshot_is_bound(self) -> None:
        harness = _Harness()
        handle, _body = harness.create()
        with self.assertRaises(AuthoringWorkbenchError) as caught:
            harness.registry.dispatch(harness.act("read_timeline_history", workspace_handle=handle))
        self.assertEqual(
            (caught.exception.status, caught.exception.code),
            (422, "timeline_history_unavailable"),
        )

        with self.assertRaises(AuthoringWorkbenchError) as cross_workspace:
            harness.registry.bind_timeline_history_snapshot(
                history_snapshot("authoring-another-workspace")
            )
        self.assertEqual(
            (cross_workspace.exception.status, cross_workspace.exception.code),
            (404, "workspace_unavailable"),
        )

    def test_composition_root_can_bind_only_through_the_backend_registry(self) -> None:
        harness = _Harness()
        handle, _body = harness.create()
        snapshot = history_snapshot(handle)
        with patch.object(authoring_route, "_registry", return_value=harness.registry):
            authoring_route.bind_authoring_timeline_history_snapshot(snapshot)
        read = harness.registry.dispatch(
            harness.act("read_timeline_history", workspace_handle=handle)
        )
        self.assertEqual(read.status, 200)
        assert read.body is not None
        self.assertEqual(read.body["snapshot"], snapshot.to_wire())

    def test_read_apply_replay_and_conflict_return_only_accepted_history_state(self) -> None:
        harness = _Harness()
        handle, _body = harness.create()
        snapshot = history_snapshot(handle)
        harness.registry.bind_timeline_history_snapshot(snapshot)
        harness.registry.bind_timeline_history_snapshot(snapshot)

        read = harness.registry.dispatch(
            harness.act("read_timeline_history", workspace_handle=handle)
        )
        self.assertEqual(read.status, 200)
        assert read.body is not None
        self.assertEqual(read.body["schema"], TIMELINE_HISTORY_PROJECTION_SCHEMA)
        self.assertEqual(read.body["workspace_handle"], handle)
        self.assertEqual(read.body["snapshot"], snapshot.to_wire())
        self.assertEqual(read.body["selection"], [])
        self.assertIsNone(read.body["undo_cursor"])
        self.assertIsNone(read.body["redo_cursor"])
        self.assertIsNone(read.body["rejection"])

        transaction = timeline_transaction(
            snapshot,
            request_id="req-history-apply",
            commands=[
                {
                    "kind": "select_clips",
                    "payload": {"clip_ids": ["clip-main", "clip-image"]},
                }
            ],
        )
        action: dict[str, object] = {
            "schema": AUTHORING_ACTION_SCHEMA,
            "request_id": "req-history-apply",
            "action": "apply_timeline_transaction",
            "payload": transaction,
        }
        first = harness.registry.dispatch(action)
        replay = harness.registry.dispatch(action)
        self.assertEqual(first.status, 200)
        self.assertEqual(first.body, replay.body)
        assert first.body is not None
        self.assertEqual(first.body["schema"], TIMELINE_RECEIPT_SCHEMA)
        self.assertEqual(first.body["workspace_handle"], handle)
        self.assertEqual(first.body["selection"], ["clip-image", "clip-main"])
        accepted = first.body["snapshot"]
        assert type(accepted) is dict
        self.assertEqual(accepted["workspace_revision"], snapshot.workspace_revision + 1)

        after_apply = harness.registry.dispatch(
            harness.act("read_timeline_history", workspace_handle=handle)
        )
        assert after_apply.body is not None
        self.assertEqual(after_apply.body["undo_cursor"], first.body["history_cursor"])
        self.assertIsNone(after_apply.body["redo_cursor"])

        accepted_snapshot = decode_public_snapshot(accepted)
        undo = harness.registry.dispatch(
            {
                "schema": AUTHORING_ACTION_SCHEMA,
                "request_id": "req-history-undo",
                "action": "apply_timeline_transaction",
                "payload": timeline_transaction(
                    accepted_snapshot,
                    request_id="req-history-undo",
                    commands=[
                        {
                            "kind": "undo",
                            "payload": {"history_cursor": first.body["history_cursor"]},
                        }
                    ],
                ),
            }
        )
        self.assertEqual(undo.status, 200)
        assert undo.body is not None
        after_undo = harness.registry.dispatch(
            harness.act("read_timeline_history", workspace_handle=handle)
        )
        assert after_undo.body is not None
        self.assertIsNone(after_undo.body["undo_cursor"])
        self.assertEqual(after_undo.body["redo_cursor"], undo.body["history_cursor"])

        stale = timeline_transaction(
            snapshot,
            request_id="req-history-stale",
            commands=[{"kind": "select_clips", "payload": {"clip_ids": []}}],
        )
        conflict = harness.registry.dispatch(
            {
                "schema": AUTHORING_ACTION_SCHEMA,
                "request_id": "req-history-stale",
                "action": "apply_timeline_transaction",
                "payload": stale,
            }
        )
        self.assertEqual(conflict.status, 409)
        assert conflict.body is not None
        self.assertEqual(conflict.body["schema"], TIMELINE_HISTORY_PROJECTION_SCHEMA)
        self.assertEqual(conflict.body["snapshot"], after_undo.body["snapshot"])
        self.assertEqual(conflict.body["selection"], after_undo.body["selection"])
        self.assertEqual(conflict.body["undo_cursor"], after_undo.body["undo_cursor"])
        self.assertEqual(conflict.body["redo_cursor"], after_undo.body["redo_cursor"])
        self.assertEqual(conflict.body["rejection"], {"code": "stale_workspace_revision"})

    def test_timeline_route_replay_uses_the_core_canonical_transaction_identity(self) -> None:
        harness = _Harness()
        handle, _body = harness.create()
        snapshot = history_snapshot(handle)
        harness.registry.bind_timeline_history_snapshot(snapshot)
        transaction = timeline_transaction(
            snapshot,
            request_id="req-history-canonical-replay",
            commands=[
                {
                    "kind": "select_clips",
                    "payload": {"clip_ids": ["clip-main", "clip-image"]},
                }
            ],
        )
        action: dict[str, object] = {
            "schema": AUTHORING_ACTION_SCHEMA,
            "request_id": "req-history-canonical-replay",
            "action": "apply_timeline_transaction",
            "payload": transaction,
        }
        first = harness.registry.dispatch(action)
        replay_transaction = copy.deepcopy(transaction)
        replay_commands = cast(list[dict[str, object]], replay_transaction["commands"])
        replay_payload = cast(dict[str, object], replay_commands[0]["payload"])
        replay_payload["clip_ids"] = ["clip-image", "clip-main", "clip-image"]
        replay = harness.registry.dispatch({**action, "payload": replay_transaction})

        self.assertEqual((first.status, replay.status), (200, 200))
        self.assertEqual(first.body, replay.body)

    def test_timeline_route_does_not_resurrect_a_core_evicted_receipt(self) -> None:
        harness = _Harness()
        handle, _body = harness.create()
        snapshot = history_snapshot(handle)
        harness.registry.bind_timeline_history_snapshot(snapshot)
        first_action: dict[str, object] = {
            "schema": AUTHORING_ACTION_SCHEMA,
            "request_id": "req-history-evicted-first",
            "action": "apply_timeline_transaction",
            "payload": timeline_transaction(
                snapshot,
                request_id="req-history-evicted-first",
                commands=[{"kind": "select_clips", "payload": {"clip_ids": ["clip-main"]}}],
            ),
        }

        with patch(
            "comfyui_h3_context.core.timeline_history.MAX_IDEMPOTENCY_RECEIPTS",
            1,
        ):
            first = harness.registry.dispatch(first_action)
            assert first.body is not None
            accepted = decode_public_snapshot(cast(dict[str, object], first.body["snapshot"]))
            second = harness.registry.dispatch(
                {
                    "schema": AUTHORING_ACTION_SCHEMA,
                    "request_id": "req-history-evicted-second",
                    "action": "apply_timeline_transaction",
                    "payload": timeline_transaction(
                        accepted,
                        request_id="req-history-evicted-second",
                        commands=[{"kind": "select_clips", "payload": {"clip_ids": []}}],
                    ),
                }
            )
            replay = harness.registry.dispatch(first_action)

        self.assertEqual((first.status, second.status, replay.status), (200, 200, 409))
        assert second.body is not None and replay.body is not None
        self.assertEqual(replay.body["snapshot"], second.body["snapshot"])
        self.assertEqual(replay.body["rejection"], {"code": "stale_workspace_revision"})

    def test_timeline_route_does_not_cache_a_conflict_projection(self) -> None:
        harness = _Harness()
        handle, _body = harness.create()
        snapshot = history_snapshot(handle)
        harness.registry.bind_timeline_history_snapshot(snapshot)
        stale = timeline_transaction(
            snapshot,
            request_id="req-history-conflict-not-ledgered",
            commands=[{"kind": "select_clips", "payload": {"clip_ids": []}}],
        )
        stale["expected_workspace_revision"] = snapshot.workspace_revision - 1
        stale_action: dict[str, object] = {
            "schema": AUTHORING_ACTION_SCHEMA,
            "request_id": "req-history-conflict-not-ledgered",
            "action": "apply_timeline_transaction",
            "payload": stale,
        }
        first = harness.registry.dispatch(stale_action)
        accepted = harness.registry.dispatch(
            {
                "schema": AUTHORING_ACTION_SCHEMA,
                "request_id": "req-history-advance-after-conflict",
                "action": "apply_timeline_transaction",
                "payload": timeline_transaction(
                    snapshot,
                    request_id="req-history-advance-after-conflict",
                    commands=[
                        {
                            "kind": "select_clips",
                            "payload": {"clip_ids": ["clip-main"]},
                        }
                    ],
                ),
            }
        )
        replayed_conflict = harness.registry.dispatch(stale_action)

        self.assertEqual((first.status, accepted.status, replayed_conflict.status), (409, 200, 409))
        assert (
            first.body is not None
            and accepted.body is not None
            and replayed_conflict.body is not None
        )
        self.assertNotEqual(first.body["snapshot"], replayed_conflict.body["snapshot"])
        self.assertEqual(replayed_conflict.body["snapshot"], accepted.body["snapshot"])
        self.assertEqual(replayed_conflict.body["selection"], ["clip-main"])

    def test_timeline_route_maps_malformed_nested_clip_to_closed_conflict(self) -> None:
        harness = _Harness()
        handle, _body = harness.create()
        snapshot = history_snapshot(handle)
        harness.registry.bind_timeline_history_snapshot(snapshot)
        request_id = "req-history-malformed-nested-clip"
        action: dict[str, object] = {
            "schema": AUTHORING_ACTION_SCHEMA,
            "request_id": request_id,
            "action": "apply_timeline_transaction",
            "payload": timeline_transaction(
                snapshot,
                request_id=request_id,
                commands=[
                    {
                        "kind": "insert_range",
                        "payload": {
                            "clip": {
                                "clip_id": "clip-malformed",
                                "track_id": "track-image",
                            },
                            "scope_track_ids": ["track-image"],
                        },
                    }
                ],
            ),
        }

        rejected = harness.registry.dispatch(action)

        self.assertEqual(rejected.status, 409)
        assert rejected.body is not None
        self.assertEqual(rejected.body["snapshot"], snapshot.to_wire())
        self.assertEqual(rejected.body["rejection"], {"code": "invalid_command"})

    def test_route_request_and_transaction_request_id_cannot_diverge(self) -> None:
        harness = _Harness()
        handle, _body = harness.create()
        snapshot = history_snapshot(handle)
        harness.registry.bind_timeline_history_snapshot(snapshot)
        transaction = timeline_transaction(
            snapshot,
            request_id="transaction-request",
            commands=[{"kind": "select_clips", "payload": {"clip_ids": []}}],
        )
        with self.assertRaises(AuthoringWorkbenchError) as caught:
            harness.registry.dispatch(
                {
                    "schema": AUTHORING_ACTION_SCHEMA,
                    "request_id": "outer-request",
                    "action": "apply_timeline_transaction",
                    "payload": transaction,
                }
            )
        self.assertEqual(
            (caught.exception.status, caught.exception.code),
            (422, "timeline_request_mismatch"),
        )

    def _accepted_timeline_action(
        self, *, request_id: str
    ) -> tuple[_Harness, str, dict[str, object]]:
        harness = _Harness(ttl_seconds=1)
        handle, _body = harness.create()
        snapshot = history_snapshot(handle)
        harness.registry.bind_timeline_history_snapshot(snapshot)
        action: dict[str, object] = {
            "schema": AUTHORING_ACTION_SCHEMA,
            "request_id": request_id,
            "action": "apply_timeline_transaction",
            "payload": timeline_transaction(
                snapshot,
                request_id=request_id,
                commands=[
                    {
                        "kind": "select_clips",
                        "payload": {"clip_ids": ["clip-main"]},
                    }
                ],
            ),
        }
        self.assertEqual(harness.registry.dispatch(action).status, 200)
        return harness, handle, action

    def test_release_never_replays_an_old_timeline_receipt(self) -> None:
        harness, handle, action = self._accepted_timeline_action(request_id="req-history-release")
        release = harness.act("release_workspace", workspace_handle=handle)
        first_release = harness.registry.dispatch(release)
        replayed_release = harness.registry.dispatch(release)
        self.assertEqual((first_release.status, replayed_release.status), (204, 204))

        with self.assertRaises(AuthoringWorkbenchError) as caught:
            harness.registry.dispatch(action)
        self.assertEqual(
            (caught.exception.status, caught.exception.code),
            (410, "workspace_gone"),
        )

    def test_expiry_never_replays_an_old_timeline_receipt(self) -> None:
        harness, _handle, action = self._accepted_timeline_action(request_id="req-history-expiry")
        harness.clock.now += 2.0

        with self.assertRaises(AuthoringWorkbenchError) as caught:
            harness.registry.dispatch(action)
        self.assertEqual(
            (caught.exception.status, caught.exception.code),
            (410, "workspace_gone"),
        )


class TransportLifecycleTests(unittest.TestCase):
    """Replay, release, expiry and capacity stay exact and bounded."""

    def test_the_ledger_replays_a_mutation_without_reapplying_it(self) -> None:
        harness = _Harness()
        handle, body = harness.create()
        action = harness.act(
            "remove_source",
            workspace_handle=handle,
            expected_reference_revision=int(str(reference_of(body)["revision"])),
            source_id="img-1",
        )
        first = harness.registry.dispatch(action)
        second = harness.registry.dispatch(action)
        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        self.assertEqual(first.body, second.body)

    def test_a_reused_request_id_with_different_content_is_refused(self) -> None:
        harness = _Harness()
        handle, body = harness.create()
        revision = int(str(reference_of(body)["revision"]))
        action = harness.act(
            "remove_source",
            workspace_handle=handle,
            expected_reference_revision=revision,
            source_id="img-1",
        )
        harness.registry.dispatch(action)
        altered = dict(action)
        altered["payload"] = {
            "workspace_handle": handle,
            "expected_reference_revision": revision,
            "source_id": "vid-1",
        }
        with self.assertRaises(AuthoringWorkbenchError) as caught:
            harness.registry.dispatch(altered)
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(caught.exception.code, "request_replay_mismatch")

    def test_identifiers_keep_the_shared_character_class(self) -> None:
        import json

        for bad in ("bad id!", " lead", "", "a" * 129):
            with self.assertRaises(ValueError):
                decode_authoring_action_json(
                    json.dumps(
                        {
                            "schema": AUTHORING_ACTION_SCHEMA,
                            "request_id": "req-1",
                            "action": "remove_source",
                            "payload": {
                                "workspace_handle": "h",
                                "expected_reference_revision": 1,
                                "source_id": bad,
                            },
                        }
                    ).encode("utf-8")
                )

    def test_release_then_gone_and_expiry_then_gone(self) -> None:
        harness = _Harness()
        handle, _body = harness.create()
        released = harness.registry.dispatch(
            harness.act("release_workspace", workspace_handle=handle)
        )
        self.assertEqual(released.status, 204)
        with self.assertRaises(AuthoringWorkbenchError) as caught:
            harness.registry.dispatch(harness.act("read_projection", workspace_handle=handle))
        error = caught.exception
        self.assertEqual(error.status, 410)
        harness2 = _Harness()
        handle2, _body2 = harness2.create()
        harness2.clock.now += 100_000.0
        with self.assertRaises(AuthoringWorkbenchError) as caught2:
            harness2.registry.dispatch(harness2.act("read_projection", workspace_handle=handle2))
        error2 = caught2.exception
        self.assertEqual(error2.status, 410)

    def test_workspace_owns_projects_and_releases_opaque_source_binding(self) -> None:
        receipt = _SourceBindingReceipt()

        def claim(handle: str) -> SidebarAuthoringWorkspaceClaim:
            self.assertEqual(handle, "ws-live")
            return SidebarAuthoringWorkspaceClaim(standard_seed(), receipt)

        registry = AuthoringWorkspaceRegistry(workspace_claim=claim)
        created = registry.dispatch(
            {
                "schema": AUTHORING_ACTION_SCHEMA,
                "request_id": "req-source-binding-create",
                "action": "create_authoring_workspace",
                "payload": {"context_workspace_handle": "ws-live"},
            }
        )
        self.assertEqual(created.status, 201)
        assert created.body is not None
        rows = reference_of(created.body)["sources"]
        assert type(rows) is list
        by_id = {str(row["source_id"]): row for row in rows}
        self.assertEqual(
            by_id["vid-1"]["preview"],
            {
                "schema": "h3.context.authoring_source_preview.capability.v1",
                "available": True,
                "reason": None,
            },
        )
        self.assertEqual(by_id["aud-1"]["preview"]["reason"], "unsupported")
        self.assertFalse(receipt.released)

        released = registry.dispatch(
            {
                "schema": AUTHORING_ACTION_SCHEMA,
                "request_id": "req-source-binding-release",
                "action": "release_workspace",
                "payload": {"workspace_handle": created.body["workspace_handle"]},
            }
        )
        self.assertEqual(released.status, 204)
        self.assertTrue(receipt.released)
        self.assertEqual(receipt.release_count, 1)

        _handle, no_binding = _Harness().create()
        rows = reference_of(no_binding)["sources"]
        assert type(rows) is list
        self.assertTrue(all(row["preview"]["reason"] == "not_bound" for row in rows))

    def test_preview_admission_binds_exact_clip_source_and_late_cas(self) -> None:
        receipt = _SourceBindingReceipt()

        def claim(_handle: str) -> SidebarAuthoringWorkspaceClaim:
            return SidebarAuthoringWorkspaceClaim(standard_seed(), receipt)

        registry = AuthoringWorkspaceRegistry(workspace_claim=claim)
        created = registry.dispatch(
            {
                "schema": AUTHORING_ACTION_SCHEMA,
                "request_id": "req-preview-create",
                "action": "create_authoring_workspace",
                "payload": {"context_workspace_handle": "ws-live"},
            }
        )
        assert created.body is not None
        handle = str(created.body["workspace_handle"])
        added = registry.dispatch(
            {
                "schema": AUTHORING_ACTION_SCHEMA,
                "request_id": "req-preview-add",
                "action": "add_clip",
                "payload": {
                    "workspace_handle": handle,
                    "expected_timeline_revision": timeline_of(created.body)["revision"],
                    "asset_id": "vid-1",
                    "lane": 0,
                    "start_frame": 30,
                    "frames": 60,
                    "source_start_frame": 30,
                },
            }
        )
        self.assertEqual(added.status, 200)
        assert added.body is not None
        request = AuthoringPreviewRequest(
            request_id="preview-request-1",
            workspace_handle=handle,
            reference_revision=int(str(reference_of(added.body)["revision"])),
            timeline_revision=int(str(timeline_of(added.body)["revision"])),
            timeline_content_fingerprint=str(timeline_of(added.body)["content_fingerprint"]),
            clip_id="clip-1",
        )

        preview_claim = registry.admit_media_preview(request)
        self.assertEqual(receipt.claim_count, 1)
        self.assertNotIn("source", repr(preview_claim).casefold())
        self.assertEqual(
            (
                preview_claim.source_id,
                preview_claim.source_start_frame,
                preview_claim.frames,
                preview_claim.video_fps,
            ),
            ("vid-1", 30, 60, 24),
        )
        self.assertTrue(registry.media_preview_is_current(preview_claim))

        moved = registry.dispatch(
            {
                "schema": AUTHORING_ACTION_SCHEMA,
                "request_id": "req-preview-move",
                "action": "move_clip",
                "payload": {
                    "workspace_handle": handle,
                    "expected_timeline_revision": request.timeline_revision,
                    "clip_id": "clip-1",
                    "delta_frames": 1,
                    "delta_lanes": 0,
                },
            }
        )
        self.assertEqual(moved.status, 200)
        self.assertFalse(registry.media_preview_is_current(preview_claim))

    def test_workspace_capacity_is_429(self) -> None:
        harness = _Harness(max_entries=1)
        harness.create()
        with self.assertRaises(AuthoringWorkbenchError) as caught:
            harness.registry.dispatch(
                harness.act("create_authoring_workspace", context_workspace_handle="ws-live")
            )
        error = caught.exception
        self.assertEqual((error.status, error.code), (429, "workspace_capacity"))


class PrivacyTests(unittest.TestCase):
    """AC-M20-03: no media locator or private content in any wire value."""

    def test_projection_and_snap_wires_carry_no_content_tokens(self) -> None:
        harness = _Harness()
        handle, body = harness.create()
        snap = harness.registry.dispatch(
            harness.act(
                "read_snap",
                workspace_handle=handle,
                frame=10,
                playhead_frame=None,
                exclude_clip_id=None,
            )
        )
        dump = repr(body) + repr(snap.body)
        for token in ("\\", "://", "C:", "/" + "tmp", ".safetensors", ".mp4", ".png"):
            self.assertNotIn(token, dump)


if __name__ == "__main__":
    unittest.main()
