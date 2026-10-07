"""Normalize and compare a bounded content-free HC-09 live shape fixture.

The tool never contacts or discovers a host. It accepts only a closed fixture-shaped
observation document, validates it against the tracked census, emits deterministic bytes,
and can compare those bytes with the tracked fixture without rewriting either input.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TypedDict

from comfyui_h3_context.core.host_seam_contract import (
    HostSeamShape,
    parse_host_seam_contract,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CENSUS = Path("comfyui_h3_context/contracts/host_seam_census_v1.json")
DEFAULT_FIXTURE = Path("comfyui_h3_context/contracts/host_seam_shape_fixture_v1.json")
MAX_OBSERVED_BYTES = 256_000


class HostSeamFixtureError(ValueError):
    """Raised when observed shape facts cannot produce the tracked fixture."""


class HostSeamFixtureSummary(TypedDict):
    status: str
    seams: int
    bytes: int


class NormalizedHostSeamFixture(TypedDict):
    schema: str
    profile: str
    subject: dict[str, object]
    observations: list[dict[str, object]]


def _read_json(path: Path, label: str) -> object:
    try:
        if path.is_symlink() or not path.is_file():
            raise HostSeamFixtureError(f"{label} is not a regular file")
        size = path.stat().st_size
        if not 1 <= size <= MAX_OBSERVED_BYTES:
            raise HostSeamFixtureError(f"{label} exceeds its closed byte bound")
        return json.loads(path.read_text(encoding="utf-8"))
    except HostSeamFixtureError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HostSeamFixtureError(f"{label} is not bounded strict JSON") from error


def _shape_wire(shape: HostSeamShape) -> dict[str, object]:
    return {
        "seam_id": shape.seam_id,
        "presence": shape.presence.value,
        "kind": shape.kind.value,
        "key_shape": shape.key_shape.value,
        "element_kind": shape.element_kind.value,
        "readiness_state": shape.readiness_state.value,
        "count_bucket": shape.count_bucket.value,
        "byte_bucket": shape.byte_bucket.value,
        "latency_bucket": shape.latency_bucket.value,
    }


def normalized_fixture_wire(census: object, observed: object) -> NormalizedHostSeamFixture:
    """Return one closed, sorted fixture projection after an exact contract join."""

    contract = parse_host_seam_contract(census, observed)
    if not isinstance(observed, Mapping) or not isinstance(observed.get("subject"), Mapping):
        raise HostSeamFixtureError("observed fixture has no closed subject")
    subject = observed["subject"]
    return {
        "schema": "h3.context.host_seam_shape_fixture.v1",
        "profile": contract.profile,
        "subject": {
            "comfyui_version": subject["comfyui_version"],
            "comfyui_revision": subject["comfyui_revision"],
            "frontend_version": subject["frontend_version"],
            "fixture_version": subject["fixture_version"],
        },
        "observations": [_shape_wire(row.shape) for row in contract.rows],
    }


def deterministic_fixture_bytes(value: Mapping[str, object]) -> bytes:
    """Render the stable compact-row fixture format used by the tracked artifact."""

    subject = value["subject"]
    observations = value["observations"]
    if not isinstance(subject, Mapping) or not isinstance(observations, Sequence):
        raise HostSeamFixtureError("normalized fixture has an invalid closed shape")
    rows = ",\n".join("    " + json.dumps(row, ensure_ascii=False) for row in observations)
    text = (
        "{\n"
        '  "schema": "h3.context.host_seam_shape_fixture.v1",\n'
        '  "profile": "comfyui_host_seams_v1",\n'
        '  "subject": {\n'
        f'    "comfyui_version": {json.dumps(subject["comfyui_version"])},\n'
        f'    "comfyui_revision": {json.dumps(subject["comfyui_revision"])},\n'
        f'    "frontend_version": {json.dumps(subject["frontend_version"])},\n'
        f'    "fixture_version": {subject["fixture_version"]}\n'
        "  },\n"
        '  "observations": [\n'
        f"{rows}\n"
        "  ]\n"
        "}\n"
    )
    return text.encode("utf-8")


def check_observed_fixture(
    census_path: Path, observed_path: Path, tracked_fixture_path: Path
) -> HostSeamFixtureSummary:
    census = _read_json(census_path, "census")
    observed_root = _read_json(observed_path, "observed fixture")
    if isinstance(observed_root, Mapping) and "observed_fixture" in observed_root:
        observed_root = observed_root["observed_fixture"]
    normalized = normalized_fixture_wire(census, observed_root)
    normalized_bytes = deterministic_fixture_bytes(normalized)
    tracked = _read_json(tracked_fixture_path, "tracked fixture")
    tracked_bytes = deterministic_fixture_bytes(normalized_fixture_wire(census, tracked))
    if tracked_fixture_path.read_bytes() != tracked_bytes:
        raise HostSeamFixtureError("tracked fixture bytes are not deterministic")
    if normalized_bytes != tracked_bytes:
        raise HostSeamFixtureError("observed host seam fixture is DRIFTED")
    return {
        "status": "PASS",
        "seams": len(normalized["observations"]),
        "bytes": len(normalized_bytes),
    }


def _rooted(root: Path, value: Path) -> Path:
    return value if value.is_absolute() else root / value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--census", type=Path, default=DEFAULT_CENSUS)
    parser.add_argument("--observed", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    args = parser.parse_args(argv)
    if not args.check:
        parser.error("only the read-only --check operation is supported")
    try:
        summary = check_observed_fixture(
            _rooted(args.root, args.census),
            _rooted(args.root, args.observed),
            _rooted(args.root, args.fixture),
        )
    except (HostSeamFixtureError, ValueError, OSError) as error:
        print(json.dumps({"status": "DRIFTED", "detail": str(error)}, sort_keys=True))
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
