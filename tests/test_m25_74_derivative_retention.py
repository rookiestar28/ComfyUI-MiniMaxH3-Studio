"""How long a derivative body outlives its leases, and what ends it.

An accepted edit ends every lease of the superseded snapshot, and the next snapshot asks for the
same bytes. The product therefore keeps a body after its last lease. These tests fix the bounds of
that: what keeps a body, what drops it, and that an idle body is never handed to a claim that has
not just proven its source.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from test_m25_45_lease_cross_boundary import (
    SizedClaim,
    lease_request,
    open_command,
    release_command,
)
from test_m25_media_source_leases import command

from comfyui_h3_context.adapters.authoring_media_leases import MediaLeaseAuthority
from comfyui_h3_context.adapters.comfyui_authoring_media_leases import MediaLeaseRouteService
from comfyui_h3_context.core.authoring_media import (
    DERIVATIVE_PROFILE_ID,
    LEASE_TTL_SECONDS,
    MAX_CACHE_BYTES,
    RETAINED_DERIVATIVE_IDLE_SECONDS,
    CreateLeaseRequest,
    DerivativeKind,
    MediaLeaseError,
    VerifiedDerivative,
    derivative_byte_limit,
)
from comfyui_h3_context.core.composition_contract import (
    DERIVATIVE_MANIFEST_SCHEMA,
    PRIVATE_SOURCE_MANIFEST_SCHEMA,
    DerivativeManifest,
    PrivateSourceManifest,
)

EMPTY = {"leases": 0, "cacheEntries": 0, "cacheBytes": 0, "activeReads": 0}
NOTHING_IDLE = {"entries": 0, "bytes": 0}


class Source:
    """What bodies derive from: alive until its owner ends it; its content can be replaced."""

    def __init__(self) -> None:
        self.alive = True
        self.replaced = False
        self.builds = 0
        self.asked = 0
        self.answer: Callable[[], bool] | None = None

    def current(self) -> bool:
        self.asked += 1
        return self.alive if self.answer is None else self.answer()


class Workspace:
    """Each accepted edit is a new revision; a claim is issued for exactly one.

    Every owner derives from the one asset unless `assets` names another for it, as two clips
    over one asset share a body and clips over different assets do not.
    """

    def __init__(self) -> None:
        self.revision = 0
        self.source = Source()
        self.assets: dict[str, tuple[str, int]] = {}
        self.before_confirm: Callable[[], None] | None = None

    def claim(self, request: CreateLeaseRequest) -> SnapshotClaim:
        asset, size = self.assets.get(request.owner_id, ("asset", 64))
        return SnapshotClaim(self, request.derivative_kind, asset, size)


class SnapshotClaim:
    """The product claim's shape: current for one revision, retained by its source's liveness."""

    def __init__(self, workspace: Workspace, kind: DerivativeKind, asset: str, size: int) -> None:
        self._workspace = workspace
        self._revision = workspace.revision
        self._kind = kind
        self._size = size
        self._disproved = False
        self.cache_key = f"{kind}-{asset}"

    def current(self) -> bool:
        return self._workspace.source.alive and self._workspace.revision == self._revision

    def confirm(self, _deadline: float) -> None:
        hook = self._workspace.before_confirm
        if hook is not None:
            hook()
        if not self.current():
            raise MediaLeaseError("stale")
        if self._workspace.source.replaced:
            self._disproved = True
            raise MediaLeaseError("stale")

    def retention_probe(self) -> Callable[[], bool] | None:
        return None if self._disproved else self._workspace.source.current

    def generate(
        self, _deadline: float, _cancellation: object
    ) -> tuple[bytearray, VerifiedDerivative]:
        self._workspace.source.builds += 1
        body = bytearray(b"\x7f" * self._size)
        fingerprint = "sha256:" + "1" * 64
        facts = VerifiedDerivative(
            PrivateSourceManifest(
                PRIVATE_SOURCE_MANIFEST_SCHEMA, "asset-1", 1, fingerprint, "authority-1"
            ),
            DerivativeManifest(
                DERIVATIVE_MANIFEST_SCHEMA,
                "asset-1",
                fingerprint,
                DERIVATIVE_PROFILE_ID,
                "sha256:" + hashlib.sha256(body).hexdigest(),
                1,
            ),
            self._kind,
            "sha256:" + "2" * 64,
            "sha256:" + "3" * 64,
            len(body),
            "absent",
        )
        return body, facts


Setup = tuple[MediaLeaseAuthority, Workspace, list[float]]


@pytest.fixture
def retaining() -> Iterator[Setup]:
    now = [100.0]
    workspace = Workspace()
    store = MediaLeaseAuthority(
        workspace.claim,
        clock=lambda: now[0],
        start_reaper=False,
        retention_seconds=RETAINED_DERIVATIVE_IDLE_SECONDS,
    )
    yield store, workspace, now
    store.close()
    assert store.resources() == EMPTY
    assert store.retained() == NOTHING_IDLE


def _release(store: MediaLeaseAuthority, receipt: dict[str, object], capability: str) -> None:
    store.release(release_command(receipt), capability)


def _move(workspace: Workspace) -> Callable[[], None]:
    def accepted_edit() -> None:
        workspace.revision += 1

    return accepted_edit


def _only_body(store: MediaLeaseAuthority) -> bytearray:
    (cached,) = store._cache.values()
    return cached.body


def test_the_class_default_ends_a_body_with_its_last_lease() -> None:
    workspace = Workspace()
    store = MediaLeaseAuthority(workspace.claim, start_reaper=False)
    try:
        assert store.retention_seconds == 0
        receipt, capability = store.create(lease_request("video_proxy"))
        body = _only_body(store)
        _release(store, receipt, capability)
        assert store.resources() == EMPTY
        assert len(body) == 0
        # Nothing asked the source whether a body could stay.
        assert workspace.source.asked == 0
    finally:
        store.close()


def test_the_product_route_service_retains_for_the_published_bound() -> None:
    service = MediaLeaseRouteService()
    try:
        assert service._authority.retention_seconds == RETAINED_DERIVATIVE_IDLE_SECONDS == 900
    finally:
        service.close()


def test_a_released_body_is_kept_and_reused_without_generation(retaining: Setup) -> None:
    store, workspace, _ = retaining
    first, capability = store.create(lease_request("video_proxy"))
    _release(store, first, capability)
    assert store.resources() == {"leases": 0, "cacheEntries": 1, "cacheBytes": 64, "activeReads": 0}
    assert store.retained() == {"entries": 1, "bytes": 64}

    workspace.revision += 1
    second, capability = store.create(lease_request("video_proxy", "clip-2"))
    assert workspace.source.builds == 1
    assert second["derivativeFingerprint"] == first["derivativeFingerprint"]
    assert second["leaseId"] != first["leaseId"]
    assert store.retained() == NOTHING_IDLE
    read = store.open(open_command(second), capability)
    assert read.read_chunk() == b"\x7f" * 64
    read.discard()


def test_an_edit_ends_the_lease_and_keeps_the_body(retaining: Setup) -> None:
    store, workspace, _ = retaining
    store.create(lease_request("video_proxy"))
    workspace.revision += 1
    store.reap()
    assert store.resources()["leases"] == 0
    assert store.retained() == {"entries": 1, "bytes": 64}


def test_an_expired_lease_keeps_the_body_until_the_idle_bound(retaining: Setup) -> None:
    store, _, now = retaining
    store.create(lease_request("video_proxy"))
    now[0] += LEASE_TTL_SECONDS
    store.reap()
    assert store.resources()["leases"] == 0
    assert store.retained()["entries"] == 1
    # The bound runs from the moment the last lease ended, not from generation.
    now[0] += RETAINED_DERIVATIVE_IDLE_SECONDS - 1
    store.reap()
    assert store.retained()["entries"] == 1
    now[0] += 1
    store.reap()
    assert store.resources() == EMPTY


def test_an_idle_body_ends_when_its_source_ends(retaining: Setup) -> None:
    store, workspace, _ = retaining
    receipt, capability = store.create(lease_request("video_proxy"))
    _release(store, receipt, capability)
    workspace.source.alive = False
    store.reap()
    assert store.resources() == EMPTY


def test_a_source_that_ends_under_a_live_lease_takes_the_body_in_the_same_pass(
    retaining: Setup,
) -> None:
    store, workspace, _ = retaining
    store.create(lease_request("video_proxy"))
    # A released workspace: the claim is stale and the source is gone, both at once. One pass
    # ends the lease and the body it just made idle; nothing waits for a second pass.
    workspace.source.alive = False
    store.reap()
    assert store.resources() == EMPTY


def test_a_body_leased_again_while_the_same_pass_evaluates_is_not_dropped(
    retaining: Setup,
) -> None:
    store, workspace, _ = retaining
    store.create(lease_request("video_proxy"))
    leased: list[tuple[dict[str, object], str]] = []

    # An edit ends the lease. While the pass that found it asks the source about the body it just
    # made idle, the next snapshot leases the body; the (now outdated) answer says the source is
    # gone.
    def leased_meanwhile() -> bool:
        workspace.source.answer = None
        leased.append(store.create(lease_request("video_proxy", "clip-2")))
        return False

    workspace.revision += 1
    workspace.source.answer = leased_meanwhile
    store.reap()
    assert leased and store.resources()["leases"] == 1
    assert store.resources()["cacheEntries"] == 1
    assert workspace.source.builds == 1


def test_a_probe_that_raises_ends_the_body(retaining: Setup) -> None:
    store, workspace, _ = retaining
    receipt, capability = store.create(lease_request("video_proxy"))
    _release(store, receipt, capability)

    def broken() -> bool:
        raise OSError("source unavailable")

    workspace.source.answer = broken
    store.reap()
    assert store.resources() == EMPTY


def test_close_drops_every_idle_body() -> None:
    workspace = Workspace()
    store = MediaLeaseAuthority(workspace.claim, start_reaper=False, retention_seconds=900)
    receipt, capability = store.create(lease_request("video_proxy"))
    _release(store, receipt, capability)
    assert store.retained()["entries"] == 1
    store.close()
    assert store.resources() == EMPTY


End = Callable[[MediaLeaseAuthority, Workspace, list[float]], None]


def _reaches_its_idle_bound(
    store: MediaLeaseAuthority, _workspace: Workspace, now: list[float]
) -> None:
    now[0] += RETAINED_DERIVATIVE_IDLE_SECONDS
    store.reap()


def _loses_its_source(store: MediaLeaseAuthority, workspace: Workspace, _now: list[float]) -> None:
    workspace.source.alive = False
    store.reap()


def _has_its_content_disproved(
    store: MediaLeaseAuthority, workspace: Workspace, _now: list[float]
) -> None:
    workspace.source.replaced = True
    with pytest.raises(MediaLeaseError, match="stale"):
        store.create(lease_request("video_proxy", "clip-2"))


def _is_closed_over(store: MediaLeaseAuthority, _workspace: Workspace, _now: list[float]) -> None:
    store.close()


@pytest.mark.parametrize(
    "end",
    [_reaches_its_idle_bound, _loses_its_source, _has_its_content_disproved, _is_closed_over],
)
def test_an_idle_body_that_ends_is_cleared(retaining: Setup, end: End) -> None:
    store, workspace, now = retaining
    _release(store, *store.create(lease_request("video_proxy")))
    body = _only_body(store)
    assert len(body) == 64
    end(store, workspace, now)
    assert store.resources() == EMPTY
    # The authority owns the private bytes. When a body ends they are wiped, whoever may still
    # hold the object; only the bounded chunk copies a reader took remain.
    assert len(body) == 0


def test_a_claim_without_a_probe_is_never_retained() -> None:
    claim = SizedClaim("video_proxy", 64)
    store = MediaLeaseAuthority(lambda _request: claim, start_reaper=False, retention_seconds=900)
    try:
        receipt, capability = store.create(lease_request("video_proxy"))
        _release(store, receipt, capability)
        assert store.resources() == EMPTY
    finally:
        store.close()


def test_a_claim_that_is_not_current_never_receives_an_idle_body(retaining: Setup) -> None:
    store, workspace, _ = retaining
    receipt, capability = store.create(lease_request("video_proxy"))
    _release(store, receipt, capability)

    # The workspace moves on while the new claim confirms: the claim is refused, no lease exists
    # and the body is neither served nor lost.
    workspace.before_confirm = _move(workspace)
    with pytest.raises(MediaLeaseError, match="stale"):
        store.create(lease_request("video_proxy", "clip-2"))
    workspace.before_confirm = None
    assert store.resources()["leases"] == 0
    assert store.retained() == {"entries": 1, "bytes": 64}

    store.create(lease_request("video_proxy", "clip-3"))
    assert workspace.source.builds == 1


def test_a_confirm_that_fails_because_the_workspace_moved_keeps_the_body(
    retaining: Setup,
) -> None:
    store, workspace, _ = retaining
    receipt, capability = store.create(lease_request("video_proxy"))

    # The edit lands between the lookup and the confirmation of `open`.
    workspace.before_confirm = _move(workspace)
    with pytest.raises(MediaLeaseError, match="stale"):
        store.open(open_command(receipt), capability)
    workspace.before_confirm = None
    assert store.resources()["leases"] == 0
    assert store.retained() == {"entries": 1, "bytes": 64}


def test_disproved_content_drops_an_idle_body_at_once(retaining: Setup) -> None:
    store, workspace, _ = retaining
    receipt, capability = store.create(lease_request("video_proxy"))
    _release(store, receipt, capability)
    workspace.source.replaced = True
    with pytest.raises(MediaLeaseError, match="stale"):
        store.create(lease_request("video_proxy", "clip-2"))
    assert store.resources() == EMPTY


def test_content_disproved_by_the_second_confirmation_drops_an_idle_body_at_once(
    retaining: Setup,
) -> None:
    store, workspace, _ = retaining
    receipt, capability = store.create(lease_request("video_proxy"))
    _release(store, receipt, capability)
    confirmations: list[int] = []

    # The source is replaced after the create's first confirmation. The second one, which stands
    # between the idle body and a new lease, is the one that finds it.
    def replaced_after_the_first() -> None:
        confirmations.append(1)
        workspace.source.replaced = len(confirmations) == 2

    workspace.before_confirm = replaced_after_the_first
    with pytest.raises(MediaLeaseError, match="stale"):
        store.create(lease_request("video_proxy", "clip-2"))
    workspace.before_confirm = None
    assert confirmations == [1, 1]
    assert store.resources() == EMPTY


def test_disproved_content_that_has_no_body_is_refused_as_stale(retaining: Setup) -> None:
    store, workspace, _ = retaining
    workspace.source.replaced = True
    # Nothing was ever built from this source, so there is nothing to withdraw; the refusal is
    # the claim's own and not a failure of the authority.
    with pytest.raises(MediaLeaseError, match="stale"):
        store.create(lease_request("video_proxy"))
    assert store.resources() == EMPTY


def test_disproved_content_ends_a_shared_body_with_its_last_lease(retaining: Setup) -> None:
    store, workspace, _ = retaining
    first, first_capability = store.create(lease_request("video_proxy", "clip-1"))
    second, second_capability = store.create(lease_request("video_proxy", "clip-2"))
    assert workspace.source.builds == 1

    workspace.source.replaced = True
    with pytest.raises(MediaLeaseError, match="stale"):
        store.renew(command(first, "renew"), first_capability)
    # The other lease has not confirmed since and keeps the bytes it was granted ...
    assert store.resources()["leases"] == 1
    assert store.resources()["cacheEntries"] == 1
    # ... but the body is not kept once that lease ends, although its own claim never failed.
    _release(store, second, second_capability)
    assert store.resources() == EMPTY


def test_content_proven_again_makes_the_body_retainable_again(retaining: Setup) -> None:
    store, workspace, _ = retaining
    first, first_capability = store.create(lease_request("video_proxy", "clip-1"))
    second, second_capability = store.create(lease_request("video_proxy", "clip-2"))
    workspace.source.replaced = True
    with pytest.raises(MediaLeaseError, match="stale"):
        store.renew(command(first, "renew"), first_capability)
    workspace.source.replaced = False
    third, third_capability = store.create(lease_request("video_proxy", "clip-3"))
    _release(store, second, second_capability)
    _release(store, third, third_capability)
    assert store.retained()["entries"] == 1
    assert workspace.source.builds == 1


def test_a_body_is_kept_by_the_source_of_its_latest_lease(retaining: Setup) -> None:
    store, workspace, _ = retaining
    earlier = workspace.source
    first, first_capability = store.create(lease_request("video_proxy", "clip-1"))
    # The same content admitted again as another source object; its lease shares the body.
    workspace.source = latest = Source()
    second, second_capability = store.create(lease_request("video_proxy", "clip-2"))
    assert (earlier.builds, latest.builds) == (1, 0)
    _release(store, first, first_capability)
    _release(store, second, second_capability)

    # The retainer is registered with the lease, not with the body: the object that last took a
    # lease is the one whose end ends the body.
    earlier.alive = False
    store.reap()
    assert store.retained() == {"entries": 1, "bytes": 64}
    latest.alive = False
    store.reap()
    assert store.resources() == EMPTY


def test_idle_bodies_make_room_longest_idle_first(retaining: Setup) -> None:
    store, workspace, now = retaining
    limit = derivative_byte_limit("video_proxy")
    assert 5 * limit <= MAX_CACHE_BYTES < 6 * limit
    workspace.assets = {f"clip-{index}": (f"asset-{index}", limit) for index in range(1, 7)}
    workspace.assets["again-1"] = workspace.assets["clip-1"]
    workspace.assets["again-3"] = workspace.assets["clip-3"]
    # The third body is leased and released first: the order in which the bodies became idle is
    # neither the order of their keys nor the order in which a map happens to hold them.
    for index in (3, 1, 2, 4, 5):
        receipt, capability = store.create(lease_request("video_proxy", f"clip-{index}"))
        _release(store, receipt, capability)
        now[0] += 1
    assert store.retained() == {"entries": 5, "bytes": 5 * limit}
    longest_idle = store._cache["video_proxy-asset-3"].body

    # A sixth body does not fit beside five idle ones. The longest idle one is dropped; a create
    # that fits without idle bodies is never refused because of them.
    store.create(lease_request("video_proxy", "clip-6"))
    assert store.resources()["cacheBytes"] == 5 * limit
    assert len(longest_idle) == 0
    assert store.retained() == {"entries": 4, "bytes": 4 * limit}
    assert workspace.source.builds == 6

    # The first body is still there; the third, idle the longest, is the one that went.
    receipt, capability = store.create(lease_request("video_proxy", "again-1"))
    _release(store, receipt, capability)
    assert workspace.source.builds == 6
    store.create(lease_request("video_proxy", "again-3"))
    assert workspace.source.builds == 7


def test_leased_bodies_still_refuse_at_the_byte_budget(retaining: Setup) -> None:
    store, workspace, _ = retaining
    video = derivative_byte_limit("video_proxy")
    image = derivative_byte_limit("image_proxy")
    plan: list[tuple[str, DerivativeKind, int]] = [
        ("clip-video-1", "video_proxy", video),
        ("clip-video-2", "video_proxy", video),
        ("clip-video-3", "video_proxy", video),
        ("clip-image-1", "image_proxy", image),
        ("clip-image-2", "image_proxy", image),
        ("clip-image-3", "image_proxy", image),
        ("clip-image-4", "image_proxy", image),
        ("clip-image-5", "image_proxy", image),
    ]
    workspace.assets = {owner: (owner, size) for owner, _kind, size in plan}
    held = [store.create(lease_request(kind, owner)) for owner, kind, _size in plan[:6]]
    assert store.resources()["cacheBytes"] == 3 * video + 3 * image == 120 * 1024 * 1024
    _release(store, *held.pop())
    assert store.retained() == {"entries": 1, "bytes": image}

    # The idle body is what makes room; once every body is leased the budget refuses as before.
    store.create(lease_request("image_proxy", "clip-image-4"))
    assert store.retained() == NOTHING_IDLE
    with pytest.raises(MediaLeaseError, match="resource_limit"):
        store.create(lease_request("image_proxy", "clip-image-5"))
    assert store.resources()["cacheBytes"] == 3 * video + 3 * image


def test_a_create_that_reaches_the_byte_budget_exactly_ends_no_idle_body(retaining: Setup) -> None:
    store, workspace, _ = retaining
    video = derivative_byte_limit("video_proxy")
    image = derivative_byte_limit("image_proxy")
    assert 2 * video + 5 * image == MAX_CACHE_BYTES
    plan: list[tuple[str, DerivativeKind, int]] = [
        ("clip-video-1", "video_proxy", video),
        ("clip-video-2", "video_proxy", video),
        ("clip-image-1", "image_proxy", image),
        ("clip-image-2", "image_proxy", image),
        ("clip-image-3", "image_proxy", image),
        ("clip-image-4", "image_proxy", image),
        ("clip-image-5", "image_proxy", image),
    ]
    workspace.assets = {owner: (owner, size) for owner, _kind, size in plan}
    held = [store.create(lease_request(kind, owner)) for owner, kind, _size in plan[:6]]
    _release(store, *held.pop())
    assert store.retained() == {"entries": 1, "bytes": image}

    # The seventh body fills the budget to the byte and does not pass it. Room is made only for
    # what does not fit: the idle body stays.
    store.create(lease_request("image_proxy", "clip-image-5"))
    assert store.resources()["cacheBytes"] == MAX_CACHE_BYTES
    assert store.retained() == {"entries": 1, "bytes": image}
    assert workspace.source.builds == 7


def test_a_body_leased_again_while_the_reaper_evaluates_is_not_dropped(retaining: Setup) -> None:
    store, workspace, _ = retaining
    receipt, capability = store.create(lease_request("video_proxy"))
    _release(store, receipt, capability)
    leased: list[tuple[dict[str, object], str]] = []

    # The reaper evaluates a probe outside the lock. While it does, the body is leased again; the
    # verdict of that (now outdated) evaluation says the source is gone.
    def leased_meanwhile() -> bool:
        workspace.source.answer = None
        leased.append(store.create(lease_request("video_proxy", "clip-2")))
        return False

    workspace.source.answer = leased_meanwhile
    store.reap()
    assert leased and store.resources()["leases"] == 1
    assert store.resources()["cacheEntries"] == 1
    assert workspace.source.builds == 1


def test_the_end_of_one_source_does_not_end_a_body_another_source_keeps_now(
    retaining: Setup,
) -> None:
    store, workspace, _ = retaining
    earlier = workspace.source
    receipt, capability = store.create(lease_request("video_proxy"))
    _release(store, receipt, capability)

    # While the pass asks the source that kept the body, the same content is leased and released
    # through another source object, within one tick of the clock. What comes back is the end of
    # a source that no longer keeps this body.
    def leased_and_released_meanwhile() -> bool:
        earlier.answer = None
        workspace.source = Source()
        _release(store, *store.create(lease_request("video_proxy", "clip-2")))
        return False

    earlier.answer = leased_and_released_meanwhile
    store.reap()
    assert store.retained() == {"entries": 1, "bytes": 64}
    assert (earlier.builds, workspace.source.builds) == (1, 0)


class _ProbeOf(SizedClaim):
    """A claim that offers whatever a test's supplier returns as its probe."""

    def __init__(self, supplier: Callable[[], object]) -> None:
        super().__init__("video_proxy", 64)
        self._supplier = supplier

    def retention_probe(self) -> object:
        return self._supplier()


def _retaining(claim: SizedClaim, now: list[float] | None = None) -> MediaLeaseAuthority:
    clock = [100.0] if now is None else now
    return MediaLeaseAuthority(
        lambda _request: claim,
        clock=lambda: clock[0],
        start_reaper=False,
        retention_seconds=RETAINED_DERIVATIVE_IDLE_SECONDS,
    )


class _OneProbe:
    """One probe object for every lease, as a packaged font's is.

    The authority cannot tell by the probe which lease registered it, so only the body's own
    state tells a pass that the body it listed was used since. A test acts while it is asked.
    """

    def __init__(self) -> None:
        self.meanwhile: list[Callable[[], None]] = []

    def __call__(self) -> bool:
        while self.meanwhile:
            self.meanwhile.pop()()
        return True


def test_a_bound_reached_by_an_earlier_idle_period_does_not_end_a_body_idle_again() -> None:
    now = [100.0]
    probe = _OneProbe()
    store = _retaining(_ProbeOf(lambda: probe), now)
    try:
        _release(store, *store.create(lease_request("video_proxy")))
        now[0] = 500.0

        # While the pass asks the probe, the body is leased and released again; only then does the
        # clock reach the bound of the idle period the pass listed. Nothing but the idle stamp
        # tells the body's second idle period from its first.
        def used_again_meanwhile() -> None:
            _release(store, *store.create(lease_request("video_proxy", "clip-2")))
            now[0] = 100.0 + RETAINED_DERIVATIVE_IDLE_SECONDS

        probe.meanwhile.append(used_again_meanwhile)
        store.reap()
        assert store.retained() == {"entries": 1, "bytes": 64}
        # The second idle period has its own bound.
        now[0] = 500.0 + RETAINED_DERIVATIVE_IDLE_SECONDS
        store.reap()
        assert store.resources() == EMPTY
    finally:
        store.close()


def test_a_bound_reached_while_idle_does_not_end_a_body_leased_since() -> None:
    now = [100.0]
    probe = _OneProbe()
    store = _retaining(_ProbeOf(lambda: probe), now)
    try:
        _release(store, *store.create(lease_request("video_proxy")))
        now[0] = 500.0

        # While the pass asks the probe, the body is leased and stays leased; then the clock
        # reaches the bound of the idle period the pass listed.
        def leased_meanwhile() -> None:
            store.create(lease_request("video_proxy", "clip-2"))
            now[0] = 100.0 + RETAINED_DERIVATIVE_IDLE_SECONDS

        probe.meanwhile.append(leased_meanwhile)
        store.reap()
        assert store.resources() == {
            "leases": 1,
            "cacheEntries": 1,
            "cacheBytes": 64,
            "activeReads": 0,
        }
    finally:
        store.close()


def test_a_pass_asks_the_source_of_an_idle_body_once(retaining: Setup) -> None:
    store, workspace, _ = retaining
    receipt, capability = store.create(lease_request("video_proxy"))
    _release(store, receipt, capability)
    asked = workspace.source.asked
    store.reap()
    assert workspace.source.asked == asked + 1
    assert store.retained() == {"entries": 1, "bytes": 64}


def test_a_pass_neither_asks_about_nor_ends_a_body_that_is_still_leased() -> None:
    alive = {"ending": True, "kept": True}
    asked: list[str] = []

    def probe_of(name: str) -> Callable[[], bool]:
        def probe() -> bool:
            asked.append(name)
            return alive[name]

        return probe

    ending_probe, kept_probe = probe_of("ending"), probe_of("kept")
    ending = _ProbeOf(lambda: ending_probe)
    kept = _ProbeOf(lambda: kept_probe)
    kept.cache_key += "-kept"
    claims = {"clip-1": ending, "clip-2": kept}
    store = MediaLeaseAuthority(
        lambda request: claims[request.owner_id],
        start_reaper=False,
        retention_seconds=RETAINED_DERIVATIVE_IDLE_SECONDS,
    )
    try:
        store.create(lease_request("video_proxy", "clip-1"))
        store.create(lease_request("video_proxy", "clip-2"))
        leased = store._cache[kept.cache_key].body
        # One lease goes stale and its source ends, both at once. The pass ends that lease and
        # the body it leaves. Only that body is the pass's business: a leased body has a claim of
        # its own to answer for it, and a verdict about it would be a verdict on live bytes.
        ending.live = False
        alive["ending"] = False
        store.reap()
        assert asked == ["ending"]
        assert len(leased) == 64
        assert store.resources() == {
            "leases": 1,
            "cacheEntries": 1,
            "cacheBytes": 64,
            "activeReads": 0,
        }
    finally:
        store.close()


def test_a_pass_forgets_the_tombstone_of_a_lease_that_ended_a_lifetime_ago(
    retaining: Setup,
) -> None:
    store, _, now = retaining
    receipt, capability = store.create(lease_request("video_proxy"))
    _release(store, receipt, capability)
    assert len(store._tombstones) == 1
    now[0] += LEASE_TTL_SECONDS - 1
    store.reap()
    assert len(store._tombstones) == 1
    # A release repeated after this is answered from the tombstone's own expiry, so the pass only
    # keeps the table from holding what nothing can ask for again.
    now[0] += 1
    store.reap()
    assert len(store._tombstones) == 0


def test_any_bound_above_zero_retains_and_withdraws() -> None:
    now = [100.0]
    workspace = Workspace()
    store = MediaLeaseAuthority(
        workspace.claim, clock=lambda: now[0], start_reaper=False, retention_seconds=0.5
    )
    try:
        _release(store, *store.create(lease_request("video_proxy")))
        assert store.retained() == {"entries": 1, "bytes": 64}
        # Retention is on or off, never "on from one second": the withdrawal of a body whose
        # content was disproved is as immediate under half a second as under the product's bound.
        workspace.source.replaced = True
        with pytest.raises(MediaLeaseError, match="stale"):
            store.create(lease_request("video_proxy", "clip-2"))
        assert store.resources() == EMPTY
    finally:
        store.close()


def test_a_probe_supplier_that_raises_offers_no_probe() -> None:
    def broken() -> object:
        raise OSError("no probe")

    claim = _ProbeOf(broken)
    store = _retaining(claim)
    try:
        receipt, capability = store.create(lease_request("video_proxy"))
        _release(store, receipt, capability)
        assert store.resources() == EMPTY
        # The supplier's failure answers nothing else: a claim that is not current is refused as
        # stale, not as whatever its supplier raised.
        claim.live = False
        with pytest.raises(MediaLeaseError, match="stale"):
            store.create(lease_request("video_proxy", "clip-2"))
    finally:
        store.close()


def test_a_probe_that_cannot_be_called_is_no_probe() -> None:
    store = _retaining(_ProbeOf(lambda: True))
    try:
        receipt, capability = store.create(lease_request("video_proxy"))
        _release(store, receipt, capability)
        assert store.resources() == EMPTY
    finally:
        store.close()


@pytest.mark.parametrize("answer", [1, "alive"])
def test_only_true_from_its_probe_keeps_an_idle_body(answer: object) -> None:
    store = _retaining(_ProbeOf(lambda: lambda: answer))
    try:
        receipt, capability = store.create(lease_request("video_proxy"))
        _release(store, receipt, capability)
        assert store.retained() == {"entries": 1, "bytes": 64}
        store.reap()
        assert store.resources() == EMPTY
    finally:
        store.close()


@pytest.mark.parametrize("value", [-1, -0.5, math.nan, math.inf, True, "900", None])
def test_retention_seconds_is_validated(value: Any) -> None:
    with pytest.raises(ValueError, match="retention_seconds"):
        MediaLeaseAuthority(Workspace().claim, start_reaper=False, retention_seconds=value)
