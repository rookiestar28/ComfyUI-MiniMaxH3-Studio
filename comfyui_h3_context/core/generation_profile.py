"""Backend-owned qualification of a directly generation-capable App Mode workflow.

`M17-20` `D1`: the browser must not decide whether generation is possible. It
receives a content-free capability projection produced here, and gates on it; it
never derives eligibility, because deriving it in the browser is how a shell ends
up queueing a graph the host cannot run.

The module is pure. It discovers nothing, opens no file, contacts no network and
executes no model. An adapter reads the host and hands the observation in; the
functions below decide what that observation means. That separation is what lets
the decision be tested without a host, and it is why the observation type below
is a contract rather than a convenience.

Privacy is a design constraint, not a review note. A projection names **slots**
-- `video_unet`, `text_encoder` and so on -- and never a filename, a path or the
host's model inventory. `M17-20` `D2` requires exactly this: when a template's
published default weight is absent, the user is told which slot is unsatisfied
and resolves it on the canvas widget, and no part of that exchange needs to name
what else the host happens to have installed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .canonical import canonical_fingerprint

GENERATION_PROFILE_SCHEMA = "h3.context.generation_profile.v1"

#: Historical template revision retained in the V1 wire for provenance and
#: persisted-workflow compatibility. It is not a capability gate.
PINNED_TEMPLATE_REVISION = "5097de61ef09fe75466716ac0b200515f5ea078f"  # pragma: allowlist secret

MAX_SLOT_COUNT = 16
MAX_FAMILY_COUNT = 8
MAX_TEMPLATE_CAPABILITY_COUNT = 8
MAX_TEMPLATE_INPUT_COUNT = 64

_SLUG = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")


class GenerationProfileError(ValueError):
    """A qualification observation or projection that is not a valid contract."""


class AssetSlot(str, Enum):
    """The model roles a materialized H3 workflow needs, named by role only."""

    VIDEO_UNET = "video_unet"
    REFERENCE_UNET = "reference_unet"
    TEXT_ENCODER = "text_encoder"
    VIDEO_VAE = "video_vae"
    AUDIO_VAE = "audio_vae"


class ModeFamily(str, Enum):
    """The two native anchors, and therefore the two sets of slots.

    `M17-20` section 8.3: the four frame-driven task modes instantiate the same
    subgraph and the same `MiniMaxH3ImageToVideo` anchor, so they share a family;
    only `ref2va` is separate. A family is **not** a materialization basis --
    `t2va` and `i2va` share this family and load different template bytes -- so
    qualification iterates `materialization_bases()`, not this enum.
    """

    IMAGE_TO_VIDEO = "image_to_video"
    REFERENCE_TO_VIDEO = "reference_to_video"


class SlotDisposition(str, Enum):
    """What one required model role looks like on the host.

    `M17-28` split the old two-state answer. "The weight is installed" and "the
    weight the materialized workflow will name is installed" are different facts,
    and collapsing them is what let a fully provisioned host be told its weights
    were missing.
    """

    #: The template's own published default resolves; the materialized graph runs as written.
    PRESENT = "present"
    #: An accepted official variant resolves, but not the default the template names --
    #: another official quantization, or the same file under a user-chosen subdirectory.
    RELOCATED = "relocated"
    #: Nothing the official distribution publishes for this slot resolves.
    ABSENT = "absent"


class FamilyDisposition(str, Enum):
    AVAILABLE = "available"
    #: `M17-28`. Every required role is installed, but at least one is not the name
    #: the template materializes with. Not a missing weight and not a ready graph:
    #: the user picks the installed official file on the canvas widget.
    ASSET_RELOCATED = "asset_relocated"
    MISSING_ASSET = "missing_asset"
    TEMPLATE_DRIFT = "template_drift"
    UNSUPPORTED_HOST = "unsupported_host"


class Remediation(str, Enum):
    NONE = "none"
    SELECT_INSTALLED_ASSET_ON_CANVAS = "select_installed_asset_on_canvas"
    REQUALIFY_TEMPLATE = "requalify_template"
    UPGRADE_HOST = "upgrade_host"


#: Which slots each family needs. Taken from the observed host schemas recorded in
#: plan section 8.1: the image-to-video anchor takes one `vae`, the
#: reference-to-video anchor takes `vae` and `audio_vae` both.
FAMILY_SLOTS: dict[ModeFamily, tuple[AssetSlot, ...]] = {
    ModeFamily.IMAGE_TO_VIDEO: (
        AssetSlot.VIDEO_UNET,
        AssetSlot.TEXT_ENCODER,
        AssetSlot.VIDEO_VAE,
        AssetSlot.AUDIO_VAE,
    ),
    ModeFamily.REFERENCE_TO_VIDEO: (
        AssetSlot.REFERENCE_UNET,
        AssetSlot.TEXT_ENCODER,
        AssetSlot.VIDEO_VAE,
        AssetSlot.AUDIO_VAE,
    ),
}

#: The task mode each family materializes. `fl2va` and `l2va` are the same
#: subgraph with a promoted input bound, not separate topologies.
TASK_MODE_FAMILY: dict[str, ModeFamily] = {
    "t2va": ModeFamily.IMAGE_TO_VIDEO,
    "i2va": ModeFamily.IMAGE_TO_VIDEO,
    "fl2va": ModeFamily.IMAGE_TO_VIDEO,
    "l2va": ModeFamily.IMAGE_TO_VIDEO,
    "ref2va": ModeFamily.REFERENCE_TO_VIDEO,
}

#: The native conditioning node each family is anchored on. Splice, census and
#: qualification all resolve on these, never on a template node title.
FAMILY_ANCHOR: dict[ModeFamily, str] = {
    ModeFamily.IMAGE_TO_VIDEO: "MiniMaxH3ImageToVideo",
    ModeFamily.REFERENCE_TO_VIDEO: "MiniMaxH3ReferenceToVideo",
}

#: The template file each **task mode** is materialized from.
#:
#: Keyed by mode rather than by family, because a family is not a materialization
#: basis: `t2va` and `i2va` share an anchor and a set of slots but not a template.
#: `video_minimax_h3_t2v` is the same subgraph with no `LoadImage` chain reaching
#: the promoted `first_frame`, so qualifying `t2va` against the `i2v` bytes would
#: admit a mode whose actual basis this repository never checked. This mirrors
#: `MODE_TEMPLATE` in the shell exactly; the two must not drift apart.
MODE_TEMPLATE: dict[str, str] = {
    "t2va": "video_minimax_h3_t2v",
    "i2va": "video_minimax_h3_i2v",
    "fl2va": "video_minimax_h3_i2v",
    "l2va": "video_minimax_h3_i2v",
    "ref2va": "video_minimax_h3_r2v",
}


def materialization_bases() -> tuple[tuple[str, ModeFamily, tuple[str, ...]], ...]:
    """The distinct bases to qualify, each with its family and the modes it serves.

    Derived from the two tables above rather than restated, so a mode can never
    be added to one and forgotten in the other. Order follows first appearance in
    `MODE_TEMPLATE`, which makes the projection deterministic.
    """

    bases: dict[str, tuple[ModeFamily, list[str]]] = {}
    for mode, template in MODE_TEMPLATE.items():
        family = TASK_MODE_FAMILY[mode]
        entry = bases.get(template)
        if entry is None:
            bases[template] = (family, [mode])
            continue
        if entry[0] is not family:
            raise GenerationProfileError(f"ambiguous_basis_family:{template}")
        entry[1].append(mode)
    return tuple((template, family, tuple(modes)) for template, (family, modes) in bases.items())


#: Historical SHA-256 provenance for `PINNED_TEMPLATE_REVISION`. M23-01 keeps
#: these exported for persisted consumers but never compares them for admission.
# Template content digests, not credentials.
PINNED_TEMPLATE_DIGESTS: tuple[tuple[str, str], ...] = (  # pragma: allowlist secret
    (
        "video_minimax_h3_i2v",
        # pragma: allowlist nextline secret
        "bb71aecdd3c0b62e56eafe03acb14d1cfeabec7072eaed9cbdf473c2aaf73009",
    ),
    (
        "video_minimax_h3_r2v",
        # pragma: allowlist nextline secret
        "099d24eda6263854818975c7209db6f29ebfd0339936c928f12293d5ab029ffb",
    ),
    (
        "video_minimax_h3_t2v",
        # pragma: allowlist nextline secret
        "31ab33fdb053a7834cc866bd7aa08b887518fc656e4a796c89779c6b5e1786e6",
    ),
)


def _slug(value: object, field: str) -> str:
    if type(value) is not str or not _SLUG.fullmatch(value):
        raise GenerationProfileError(f"invalid_slug:{field}")
    return value


def _digest(value: object, field: str) -> str:
    if type(value) is not str or not _SHA256_HEX.fullmatch(value):
        raise GenerationProfileError(f"invalid_digest:{field}")
    return value


def _flag(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise GenerationProfileError(f"invalid_flag:{field}")
    return value


def _bounded_name(value: object, field: str) -> str:
    if type(value) is not str or not value or len(value) > 128 or any(ord(ch) < 32 for ch in value):
        raise GenerationProfileError(f"invalid_name:{field}")
    return value


@dataclass(frozen=True, slots=True)
class SlotObservation:
    """How one required model role resolves on the host.

    `M17-28` widened this from a single boolean. The adapter now reports which of
    the three `SlotDisposition` states it observed; what the repository still never
    does is *choose* a weight. A relocated role is reported and remediated on the
    canvas, never silently substituted.
    """

    slot: AssetSlot
    disposition: SlotDisposition

    def __post_init__(self) -> None:
        if type(self.slot) is not AssetSlot:
            raise GenerationProfileError("invalid_slot")
        if type(self.disposition) is not SlotDisposition:
            raise GenerationProfileError("invalid_slot_disposition")

    @property
    def present(self) -> bool:
        """Whether an accepted official weight is installed for this role at all."""

        return self.disposition is not SlotDisposition.ABSENT

    @property
    def default_resolves(self) -> bool:
        """Whether the name the materialized workflow will carry is the installed one."""

        return self.disposition is SlotDisposition.PRESENT

    def to_wire(self) -> dict[str, object]:
        return {"slot": self.slot.value, "disposition": self.disposition.value}


@dataclass(frozen=True, slots=True)
class TemplateCapability:
    """Content-free structure observed in one host-served workflow template.

    A template is admitted by the splice point it exposes, not by a package
    version or a byte-for-byte identity. Input values, widget values, paths and
    prompt text are deliberately absent from this contract.
    """

    template_name: str
    anchor_node_type: str
    input_names: frozenset[str]

    def __post_init__(self) -> None:
        _slug(self.template_name, "template_capability.name")
        _bounded_name(self.anchor_node_type, "template_capability.anchor")
        if (
            type(self.input_names) is not frozenset
            or len(self.input_names) > MAX_TEMPLATE_INPUT_COUNT
        ):
            raise GenerationProfileError("invalid_template_capability_inputs")
        for name in self.input_names:
            _bounded_name(name, "template_capability.input")


@dataclass(frozen=True, slots=True)
class HostObservation:
    """What an adapter read from the explicitly supplied host.

    Content-free by construction: node-class presence, structural template
    capabilities, template provenance digests, slot states and the host's own
    version string. There is no field a filename or a
    path could travel in, which is a stronger guarantee than a rule saying not to
    put one there.

    `host_version` is here rather than a browser capability flag because the
    backend can actually observe it. Whether the browser exposes a graph-load API
    is the shell's own gate and stays in the shell; mixing the two would either
    make the browser an input to canonical truth or make this contract claim an
    observation it cannot make.
    """

    anchor_node_types: frozenset[str]
    template_digests: tuple[tuple[str, str], ...]
    slots: tuple[SlotObservation, ...]
    host_version: str
    template_capabilities: tuple[TemplateCapability, ...] = ()

    def __post_init__(self) -> None:
        if type(self.anchor_node_types) is not frozenset:
            raise GenerationProfileError("invalid_anchor_node_types")
        for name in self.anchor_node_types:
            if type(name) is not str or not name:
                raise GenerationProfileError("invalid_anchor_node_type")
        if type(self.template_digests) is not tuple:
            raise GenerationProfileError("invalid_template_digests")
        seen: set[str] = set()
        for name, digest in self.template_digests:
            _slug(name, "template.name")
            _digest(digest, "template.digest")
            if name in seen:
                raise GenerationProfileError(f"duplicate_template:{name}")
            seen.add(name)
        if type(self.slots) is not tuple or len(self.slots) > MAX_SLOT_COUNT:
            raise GenerationProfileError("invalid_slots")
        observed: set[AssetSlot] = set()
        for slot in self.slots:
            if type(slot) is not SlotObservation:
                raise GenerationProfileError("invalid_slot_observation")
            if slot.slot in observed:
                raise GenerationProfileError(f"duplicate_slot:{slot.slot.value}")
            observed.add(slot.slot)
        if (
            type(self.template_capabilities) is not tuple
            or len(self.template_capabilities) > MAX_TEMPLATE_CAPABILITY_COUNT
        ):
            raise GenerationProfileError("invalid_template_capabilities")
        observed_templates: set[str] = set()
        for capability in self.template_capabilities:
            if type(capability) is not TemplateCapability:
                raise GenerationProfileError("invalid_template_capability")
            if capability.template_name in observed_templates:
                raise GenerationProfileError(
                    f"duplicate_template_capability:{capability.template_name}"
                )
            observed_templates.add(capability.template_name)
        # An empty version is a valid observation, not a malformed one: it is what
        # "no ComfyUI is present" looks like, and it must reach qualification so
        # the answer becomes an unqualified host rather than an exception the
        # caller has to interpret.
        if type(self.host_version) is not str or len(self.host_version) > 64:
            raise GenerationProfileError("invalid_host_version")

    def slot_map(self) -> dict[AssetSlot, SlotDisposition]:
        return {item.slot: item.disposition for item in self.slots}

    def digest_map(self) -> dict[str, str]:
        return dict(self.template_digests)

    def capability_map(self) -> dict[str, TemplateCapability]:
        return {item.template_name: item for item in self.template_capabilities}


@dataclass(frozen=True, slots=True)
class FamilyProfile:
    """The qualification answer for one materialization basis."""

    family: ModeFamily
    disposition: FamilyDisposition
    remediation: Remediation
    anchor_node_type: str
    template_name: str
    unsatisfied_slots: tuple[AssetSlot, ...]
    task_modes: tuple[str, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "family": self.family.value,
            "disposition": self.disposition.value,
            "remediation": self.remediation.value,
            "anchor_node_type": self.anchor_node_type,
            "template_name": self.template_name,
            "unsatisfied_slots": [slot.value for slot in self.unsatisfied_slots],
            "task_modes": list(self.task_modes),
        }

    @property
    def available(self) -> bool:
        return self.disposition is FamilyDisposition.AVAILABLE


@dataclass(frozen=True, slots=True)
class GenerationProfile:
    """The content-free capability projection the browser gates on."""

    families: tuple[FamilyProfile, ...]
    template_revision: str
    schema: str = GENERATION_PROFILE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != GENERATION_PROFILE_SCHEMA:
            raise GenerationProfileError("invalid_schema")
        if type(self.families) is not tuple or not 0 < len(self.families) <= MAX_FAMILY_COUNT:
            raise GenerationProfileError("invalid_families")
        # The identity of an entry is its template, not its family: two modes can
        # share an anchor and its slots while materializing from different
        # template structures, so collapsing them would qualify one mode against
        # another mode's observed capability.
        seen: set[str] = set()
        claimed: set[str] = set()
        for profile in self.families:
            if type(profile) is not FamilyProfile:
                raise GenerationProfileError("invalid_family_profile")
            if profile.template_name in seen:
                raise GenerationProfileError(f"duplicate_basis:{profile.template_name}")
            seen.add(profile.template_name)
            for mode in profile.task_modes:
                if mode in claimed:
                    raise GenerationProfileError(f"duplicate_task_mode:{mode}")
                claimed.add(mode)

    def basis(self, template_name: str) -> FamilyProfile:
        for profile in self.families:
            if profile.template_name == template_name:
                return profile
        raise GenerationProfileError(f"unknown_basis:{template_name}")

    def for_task_mode(self, task_mode: str) -> FamilyProfile:
        if task_mode not in TASK_MODE_FAMILY:
            raise GenerationProfileError(f"unsupported_task_mode:{task_mode}")
        for profile in self.families:
            if task_mode in profile.task_modes:
                return profile
        raise GenerationProfileError(f"unqualified_task_mode:{task_mode}")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "template_revision": self.template_revision,
            "families": [profile.to_wire() for profile in self.families],
        }

    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())


def qualify_generation_profile(
    observation: HostObservation,
    *,
    expected_template_digests: tuple[tuple[str, str], ...] = (),
) -> GenerationProfile:
    """Decide what a host observation means for each materialization basis.

    The order of the checks is the order of severity, and it is deliberate. A
    host that cannot load a graph fails before anything else is considered,
    because on `D11` option (b) there is no materialization route at all without
    it. A missing template splice capability outranks a missing asset because a
    weight cannot restore an absent native anchor or prompt socket.
    """

    if type(observation) is not HostObservation:
        raise GenerationProfileError("invalid_observation")
    # Compatibility-only provenance input. M23-01 deliberately validates but
    # does not compare these identities: admission is based on the observed
    # anchor and prompt socket below.
    for name, digest in expected_template_digests:
        _slug(name, "expected.name")
        _digest(digest, "expected.digest")

    capabilities = observation.capability_map()
    slots = observation.slot_map()
    profiles: list[FamilyProfile] = []
    for template_name, family, task_modes in materialization_bases():
        anchor = FAMILY_ANCHOR[family]
        required = FAMILY_SLOTS[family]
        absent = tuple(
            slot
            for slot in required
            if slots.get(slot, SlotDisposition.ABSENT) is SlotDisposition.ABSENT
        )
        relocated = tuple(
            slot
            for slot in required
            if slots.get(slot, SlotDisposition.ABSENT) is SlotDisposition.RELOCATED
        )
        unsatisfied: tuple[AssetSlot, ...] = ()

        capability = capabilities.get(template_name)
        if anchor not in observation.anchor_node_types:
            disposition = FamilyDisposition.UNSUPPORTED_HOST
            remediation = Remediation.UPGRADE_HOST
            unsatisfied = ()
        elif (
            capability is None
            or capability.anchor_node_type != anchor
            or "prompt" not in capability.input_names
        ):
            disposition = FamilyDisposition.TEMPLATE_DRIFT
            remediation = Remediation.REQUALIFY_TEMPLATE
            unsatisfied = ()
        elif absent:
            disposition = FamilyDisposition.MISSING_ASSET
            remediation = Remediation.SELECT_INSTALLED_ASSET_ON_CANVAS
            # M17-29: one remediation round names every role the materialized
            # template has to resolve. Dropping relocated roles merely because
            # another role is absent made the notice incomplete and left those
            # widgets carrying unusable defaults.
            unsatisfied = tuple(
                slot
                for slot in required
                if slots.get(slot, SlotDisposition.ABSENT) is not SlotDisposition.PRESENT
            )
        elif relocated:
            # M17-28. Everything is installed; the materialized graph would just
            # name the wrong file. Reported apart from `missing_asset` because the
            # two send the user to different actions, and because telling someone
            # with a provisioned host that a weight is missing is what this item
            # exists to stop.
            disposition = FamilyDisposition.ASSET_RELOCATED
            remediation = Remediation.SELECT_INSTALLED_ASSET_ON_CANVAS
            unsatisfied = relocated
        else:
            disposition = FamilyDisposition.AVAILABLE
            remediation = Remediation.NONE

        profiles.append(
            FamilyProfile(
                family=family,
                disposition=disposition,
                remediation=remediation,
                anchor_node_type=anchor,
                template_name=template_name,
                unsatisfied_slots=unsatisfied,
                task_modes=task_modes,
            )
        )
    return GenerationProfile(families=tuple(profiles), template_revision=PINNED_TEMPLATE_REVISION)


__all__ = [
    "AssetSlot",
    "FAMILY_ANCHOR",
    "FAMILY_SLOTS",
    "MODE_TEMPLATE",
    "FamilyDisposition",
    "FamilyProfile",
    "GENERATION_PROFILE_SCHEMA",
    "GenerationProfile",
    "GenerationProfileError",
    "HostObservation",
    "ModeFamily",
    "PINNED_TEMPLATE_DIGESTS",
    "PINNED_TEMPLATE_REVISION",
    "Remediation",
    "SlotDisposition",
    "SlotObservation",
    "TemplateCapability",
    "TASK_MODE_FAMILY",
    "materialization_bases",
    "qualify_generation_profile",
]
