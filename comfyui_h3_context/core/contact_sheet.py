"""Deterministic ordered contact-sheet sampling and composition contracts for M21-02.

A contact sheet is the *internal* visual representation of one video: a bounded, ordered set of
frames sampled from an already-decoded document and composed into a single numbered image.  This
module plans and describes that representation.  It never opens media, imports a decoder or a
compositor, assigns a canonical reference label, or renders an H3 prompt.

Two properties are load-bearing and are enforced here rather than left to a caller.  Sampling is a
pure function of the decoded frames and the declared parameters, so the same inputs always select
the same frames and record the same source timestamps.  And a plan is an immutable value: a
regeneration produces a new plan or raises, so a failed second look cannot damage the first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .canonical import canonical_fingerprint
from .errors import ContactSheetError
from .video_decode import (
    MAX_DECODE_FRAMES,
    DecodedFrame,
    FrameReference,
    VideoDecodeDocument,
    VideoDecodeStatus,
)

CONTACT_SHEET_SCHEMA = "h3.contact.sheet.v1"
CONTACT_SHEET_PLAN_SCHEMA = "h3.contact.sheet.plan.v1"
#: The closed set of sheet sizes.  A count outside it is refused rather than rounded.
CONTACT_SHEET_FRAME_COUNTS = (4, 6, 8)
#: Column count per sheet size; every sheet is two rows, so a wide cell stays readable.
_GRID_COLUMNS = {4: 2, 6: 3, 8: 4}
MAX_CONTACT_SHEET_RESAMPLE_INDEX = 63
MAX_CONTACT_SHEET_CELL_DIMENSION = 1_024
MAX_CONTACT_SHEET_DIMENSION = 4_096
MAX_CONTACT_SHEET_BYTES = 8_000_000
MAX_CONTACT_SHEET_LABEL_LENGTH = 64

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FORMAT_LABEL = re.compile(r"[a-z][a-z0-9]{0,15}\Z")
#: The canonical video label this sheet may declare itself the representation of.  This is the
#: Reference Registry's own label text, angle brackets included, and is never rebuilt here.
_VIDEO_LABEL = re.compile(r"<Video (?:[1-9][0-9]{0,2})>\Z")


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ContactSheetError(f"{field} must be a bounded identifier")
    return value


def _fingerprint(value: object, field: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise ContactSheetError(f"{field} must be a lowercase SHA-256 fingerprint")
    return value


def _bounded_int(value: object, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ContactSheetError(f"{field} is outside the bounded contact-sheet limit")
    return value


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ContactSheetError(f"{field} must be a boolean")
    return value


@dataclass(frozen=True, slots=True)
class ContactSheetSampling:
    """The complete declared sampling parameters; nothing else influences frame selection."""

    frame_count: int
    include_endpoints: bool = True
    resample_index: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.frame_count, bool) or self.frame_count not in CONTACT_SHEET_FRAME_COUNTS:
            raise ContactSheetError("frame_count must be one of the declared sheet sizes")
        _boolean(self.include_endpoints, "include_endpoints")
        _bounded_int(self.resample_index, "resample_index", 0, MAX_CONTACT_SHEET_RESAMPLE_INDEX)

    def to_wire(self) -> dict[str, object]:
        return {
            "frame_count": self.frame_count,
            "include_endpoints": self.include_endpoints,
            "resample_index": self.resample_index,
        }


@dataclass(frozen=True, slots=True)
class ContactSheetPlan:
    """One immutable ordered frame selection with its exact source timestamps."""

    asset_id: str
    source_id: str
    sampling: ContactSheetSampling
    frames: tuple[FrameReference, ...]
    source_frame_count: int
    #: False when every bucket held exactly one frame, so a resample could not differ.  Recorded
    #: because "look again" silently returning the same frames is a result, not a failure.
    offset_effective: bool
    schema: str = CONTACT_SHEET_PLAN_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CONTACT_SHEET_PLAN_SCHEMA:
            raise ContactSheetError("unsupported contact sheet plan schema")
        _identifier(self.asset_id, "plan asset_id")
        _identifier(self.source_id, "plan source_id")
        if not isinstance(self.sampling, ContactSheetSampling):
            raise ContactSheetError("plan sampling must be ContactSheetSampling")
        if not isinstance(self.frames, tuple) or len(self.frames) != self.sampling.frame_count:
            raise ContactSheetError("plan frames must match the declared frame count")
        if not all(isinstance(item, FrameReference) for item in self.frames):
            raise ContactSheetError("plan frames must be FrameReference values")
        frame_ids = [item.frame_id for item in self.frames]
        if len(frame_ids) != len(set(frame_ids)):
            raise ContactSheetError("plan frames must be unique")
        for previous, current in zip(self.frames, self.frames[1:], strict=False):
            if current.source_pts.timestamp.seconds <= previous.source_pts.timestamp.seconds:
                raise ContactSheetError("plan frames must be strictly increasing in source time")
        _bounded_int(
            self.source_frame_count,
            "source_frame_count",
            self.sampling.frame_count,
            MAX_DECODE_FRAMES,
        )
        _boolean(self.offset_effective, "offset_effective")

    @property
    def timestamps(self) -> tuple[str, ...]:
        """The exact source spelling of every sampled instant, in sheet order."""

        return tuple(item.source_pts.timestamp.raw for item in self.frames)

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "sampling": self.sampling.to_wire(),
            "frames": [item.to_wire() for item in self.frames],
            "source_frame_count": self.source_frame_count,
            "offset_effective": self.offset_effective,
        }

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "asset_id": self.asset_id,
            "sampling": self.sampling.to_wire(),
            "timestamps": list(self.timestamps),
            "source_frame_count": self.source_frame_count,
            "offset_effective": self.offset_effective,
        }


@dataclass(frozen=True, slots=True)
class ContactSheetDocument:
    """One composed sheet image described without a locator, a path, or its bytes."""

    plan: ContactSheetPlan
    columns: int
    rows: int
    cell_width: int
    cell_height: int
    byte_length: int
    content_fingerprint: str
    format_label: str = "png"
    schema: str = CONTACT_SHEET_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CONTACT_SHEET_SCHEMA:
            raise ContactSheetError("unsupported contact sheet schema")
        if not isinstance(self.plan, ContactSheetPlan):
            raise ContactSheetError("sheet plan must be ContactSheetPlan")
        expected_columns, expected_rows = sheet_grid(self.plan.sampling.frame_count)
        if self.columns != expected_columns or self.rows != expected_rows:
            raise ContactSheetError("sheet grid does not match the declared frame count")
        for value, field in (
            (self.cell_width, "cell_width"),
            (self.cell_height, "cell_height"),
        ):
            _bounded_int(value, field, 1, MAX_CONTACT_SHEET_CELL_DIMENSION)
        for value, field in (
            (self.columns * self.cell_width, "sheet width"),
            (self.rows * self.cell_height, "sheet height"),
        ):
            _bounded_int(value, field, 1, MAX_CONTACT_SHEET_DIMENSION)
        _bounded_int(self.byte_length, "byte_length", 1, MAX_CONTACT_SHEET_BYTES)
        _fingerprint(self.content_fingerprint, "sheet content_fingerprint")
        if (
            not isinstance(self.format_label, str)
            or _FORMAT_LABEL.fullmatch(self.format_label) is None
        ):
            raise ContactSheetError("sheet format_label must be a bounded lower-case token")

    @property
    def width(self) -> int:
        return self.columns * self.cell_width

    @property
    def height(self) -> int:
        return self.rows * self.cell_height

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "plan": self.plan.to_wire(),
            "columns": self.columns,
            "rows": self.rows,
            "cell_width": self.cell_width,
            "cell_height": self.cell_height,
            "width": self.width,
            "height": self.height,
            "byte_length": self.byte_length,
            "content_fingerprint": self.content_fingerprint,
            "format_label": self.format_label,
        }

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "plan": self.plan.to_public_dict(),
            "columns": self.columns,
            "rows": self.rows,
            "width": self.width,
            "height": self.height,
            "byte_length": self.byte_length,
            "format_label": self.format_label,
        }


def sheet_grid(frame_count: int) -> tuple[int, int]:
    """Return the deterministic (columns, rows) grid for a declared sheet size."""

    columns = _GRID_COLUMNS.get(frame_count)
    if columns is None:
        raise ContactSheetError("frame_count has no declared contact-sheet grid")
    return columns, frame_count // columns


def _asset_frames(document: VideoDecodeDocument, asset_id: str) -> tuple[DecodedFrame, ...]:
    if not isinstance(document, VideoDecodeDocument):
        raise ContactSheetError("document must be VideoDecodeDocument")
    if document.status is not VideoDecodeStatus.COMPLETE:
        raise ContactSheetError("a contact sheet requires a complete decode document")
    _identifier(asset_id, "asset_id")
    if asset_id not in document.selected_asset_ids:
        raise ContactSheetError("asset was not part of the decode document")
    frames = tuple(item for item in document.frames if item.asset_id == asset_id)
    if not frames:
        raise ContactSheetError("decode document carries no frame for this asset")
    # One source per asset is already a decode-document invariant, so it is not re-checked here.
    ordered = tuple(
        sorted(frames, key=lambda item: (item.source_pts.timestamp.seconds, item.frame_id))
    )
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current.source_pts.timestamp.seconds == previous.source_pts.timestamp.seconds:
            raise ContactSheetError("decoded frames carry a duplicate source timestamp")
    return ordered


def _selected_indices(total: int, sampling: ContactSheetSampling) -> tuple[tuple[int, ...], bool]:
    """Select `frame_count` strictly increasing indices from `total` frames, deterministically.

    The sampled range is split into `frame_count` contiguous buckets of near-equal width and one
    frame is taken from each, so the selection is monotonic and duplicate-free by construction; the
    resample index rotates the choice *within* a bucket, so a second look at the same video differs
    wherever a bucket holds more than one frame.

    With endpoints included the first and last decoded frames are pinned, because "does it start and
    end where the caller thinks" is the question a sheet is most often asked.  With endpoints
    excluded the sampled range is the strict interior, so neither endpoint can be selected by
    accident -- which means excluding them costs two frames of headroom, and a video without that
    headroom abstains rather than quietly returning an endpoint the caller asked not to see.
    """

    count = sampling.frame_count
    lower, upper = (0, total) if sampling.include_endpoints else (1, total - 1)
    span = upper - lower
    if span < count:
        raise ContactSheetError("too few frames remain in the sampled range for this sheet size")
    bounds = [lower + (index * span) // count for index in range(count + 1)]
    widths = [bounds[index + 1] - bounds[index] for index in range(count)]
    rotated = range(1, count - 1) if sampling.include_endpoints else range(count)
    indices: list[int] = []
    for index in range(count):
        start, width = bounds[index], widths[index]
        if sampling.include_endpoints and index == 0:
            indices.append(0)
        elif sampling.include_endpoints and index == count - 1:
            indices.append(total - 1)
        elif sampling.include_endpoints:
            indices.append(start + (sampling.resample_index % width))
        else:
            indices.append(start + ((width // 2 + sampling.resample_index) % width))
    effective = any(widths[index] > 1 for index in rotated)
    if len(set(indices)) != count or list(indices) != sorted(indices):
        # Unreachable for span >= count with disjoint buckets; refused rather than trusted.
        raise ContactSheetError("contact sheet sampling did not produce a monotonic selection")
    return tuple(indices), effective


def plan_contact_sheet(
    document: VideoDecodeDocument, asset_id: str, sampling: ContactSheetSampling
) -> ContactSheetPlan:
    """Plan one ordered sheet for one asset; raise rather than repeat or invent a frame."""

    if not isinstance(sampling, ContactSheetSampling):
        raise ContactSheetError("sampling must be ContactSheetSampling")
    frames = _asset_frames(document, asset_id)
    if len(frames) < sampling.frame_count:
        raise ContactSheetError("decoded frame count is below the declared sheet size")
    indices, effective = _selected_indices(len(frames), sampling)
    return ContactSheetPlan(
        asset_id=asset_id,
        source_id=frames[0].source_id,
        sampling=sampling,
        frames=tuple(
            FrameReference(frame_id=frames[index].frame_id, source_pts=frames[index].source_pts)
            for index in indices
        ),
        source_frame_count=len(frames),
        offset_effective=effective,
    )


def contact_sheet_declaration(plan: ContactSheetPlan, video_label: str) -> str:
    """State exactly what the sheet is, and what it is not, in the request that carries it.

    The vocabulary here is the vocabulary `M21-01` refuses in a rendered prompt.  That is the
    mechanical guarantee that this declaration cannot leak into product output: it is not a
    stylistic choice, it is the reason the audit rule was delivered first.
    """

    if not isinstance(plan, ContactSheetPlan):
        raise ContactSheetError("declaration requires a ContactSheetPlan")
    if (
        not isinstance(video_label, str)
        or len(video_label) > MAX_CONTACT_SHEET_LABEL_LENGTH
        or _VIDEO_LABEL.fullmatch(video_label) is None
    ):
        raise ContactSheetError("a contact sheet may only declare a canonical video label")
    columns, rows = sheet_grid(plan.sampling.frame_count)
    timestamps = ", ".join(plan.timestamps)
    return (
        f"one ordered contact sheet sampled from {video_label}: "
        f"{plan.sampling.frame_count} frames in a {columns} by {rows} grid, "
        f"numbered left to right then top to bottom, at source times {timestamps}. "
        f"This sheet is only the internal visual representation of {video_label}; "
        f"it is not a <Picture> reference and must never change or renumber an external "
        f"reference label."
    )


__all__ = [
    "CONTACT_SHEET_SCHEMA",
    "CONTACT_SHEET_PLAN_SCHEMA",
    "CONTACT_SHEET_FRAME_COUNTS",
    "MAX_CONTACT_SHEET_BYTES",
    "MAX_CONTACT_SHEET_CELL_DIMENSION",
    "MAX_CONTACT_SHEET_DIMENSION",
    "MAX_CONTACT_SHEET_RESAMPLE_INDEX",
    "ContactSheetDocument",
    "ContactSheetPlan",
    "ContactSheetSampling",
    "contact_sheet_declaration",
    "plan_contact_sheet",
    "sheet_grid",
]
