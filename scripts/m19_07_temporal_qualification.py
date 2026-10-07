"""M19-07 pre-M20 temporal and model capability qualification.

Produces one deterministic, content-free matrix of the five successor capability rows against one
explicitly supplied, frozen subject. It never starts, discovers, upgrades or patches a host, never
searches for weights or media, and never substitutes a different backend. A subject that cannot be
reached or does not match the frozen identity is reported `BLOCKED`, never PASS.

Usage:

    python scripts/m19_07_temporal_qualification.py \
        --source <supplied host root>/comfy_extras/nodes_minimax_h3.py \
        --host http://127.0.0.1:8188/ \
        --output .planning/260818-M19-07_QUALIFICATION_MATRIX.json

The source path is supplied, never discovered: this item qualifies the subject the maintainer named
and nothing else.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from comfyui_h3_context.core import native_h3  # noqa: E402
from scripts.m19_07 import host as host_probe  # noqa: E402
from scripts.m19_07.evidence import build_matrix  # noqa: E402
from scripts.m19_07.rows import CANDIDATE_NODE_TERMS, build_rows  # noqa: E402
from scripts.m19_07.source_facts import derive, load_source  # noqa: E402

SOURCE_LABEL = native_h3.NATIVE_H3_SOURCE
EXIT_BLOCKED = 3


class SubjectMismatch(RuntimeError):
    """The supplied subject is not the frozen one. Never qualify a different subject."""


def _project_commit(repo_root: Path) -> str:
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return "unavailable"
    return result.stdout.strip()


def _verify_subject(source_identity: Any) -> None:
    """The frozen product constants are a precondition of the run, not one of its results."""
    expected = f"gitblob:{native_h3.NATIVE_H3_SOURCE_BLOB}"
    if source_identity.blob != expected:
        raise SubjectMismatch(
            "supplied native source does not match the frozen subject: "
            f"{source_identity.blob} is not {expected}"
        )
    if source_identity.label != SOURCE_LABEL:
        raise SubjectMismatch(
            f"supplied source label {source_identity.label!r} is not {SOURCE_LABEL!r}"
        )


def _subject_descriptor(
    *,
    source_identity: Any,
    snapshot: host_probe.HostSnapshot | None,
    project_commit: str,
    weights: Sequence[str],
) -> dict[str, Any]:
    descriptor: dict[str, Any] = {
        "item": "M19-07",
        "project_commit": project_commit,
        "native_source": source_identity.as_evidence(),
        "frozen_host_version": native_h3.NATIVE_H3_HOST_VERSION,
        "frozen_host_revision": native_h3.NATIVE_H3_HOST_REVISION,
        "model_subject": [
            host_probe.opaque_identity(name, prefix="weight") for name in sorted(weights)
        ],
    }
    if snapshot is None:
        descriptor["host"] = None
        descriptor["host_state"] = "not_probed"
    else:
        descriptor["host"] = snapshot.as_evidence()
        descriptor["host_state"] = "probed_read_only"
    return descriptor


def _inventory_candidates(object_info: Mapping[str, Any] | None) -> dict[str, list[str]]:
    if object_info is None:
        return {row: [] for row in CANDIDATE_NODE_TERMS}
    return {
        row: list(host_probe.search_node_ids(object_info, needles=terms))
        for row, terms in CANDIDATE_NODE_TERMS.items()
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="supplied native H3 source file")
    parser.add_argument("--host", default=None, help="supplied already-running loopback endpoint")
    parser.add_argument(
        "--object-info-cache",
        type=Path,
        default=Path(".tmp/m19-07/object_info.json"),
        help="where the live node inventory is cached between runs",
    )
    parser.add_argument("--output", type=Path, default=None, help="matrix destination")
    parser.add_argument(
        "--live-evidence",
        type=Path,
        default=None,
        help="observations captured from the supplied host; absent means the offline stage only",
    )
    parser.add_argument(
        "--weight",
        action="append",
        default=[],
        help="model subject member; recorded as a bounded opaque identity, never in full",
    )
    args = parser.parse_args(argv)

    try:
        text, identity = load_source(args.source, label=SOURCE_LABEL)
    except OSError as error:
        print(f"BLOCKED: supplied source could not be read ({error.__class__.__name__})")
        return EXIT_BLOCKED

    try:
        _verify_subject(identity)
    except SubjectMismatch as error:
        print(f"BLOCKED: {error}")
        return EXIT_BLOCKED

    facts = derive(text, identity)

    snapshot: host_probe.HostSnapshot | None = None
    object_info: Mapping[str, Any] | None = None
    limitations: list[str] = []
    if args.host:
        try:
            snapshot = host_probe.system_snapshot(args.host)
            object_info = host_probe.node_inventory(args.host, cache=args.object_info_cache)
        except host_probe.HostUnavailable as error:
            limitations.append(
                f"live node inventory unavailable ({error}); successor rows stay unqualified "
                "rather than claiming an exhaustive absence"
            )
        else:
            if snapshot.comfyui_version != native_h3.NATIVE_H3_HOST_VERSION:
                print(
                    "BLOCKED: supplied host reports "
                    f"{snapshot.comfyui_version}, frozen subject is "
                    f"{native_h3.NATIVE_H3_HOST_VERSION}"
                )
                return EXIT_BLOCKED
            native_ids = host_probe.native_node_ids(object_info)
            if set(native_ids) != set(facts.node_ids()):
                print(
                    "BLOCKED: live native node inventory does not match the pinned source "
                    f"({sorted(native_ids)} vs {sorted(facts.node_ids())})"
                )
                return EXIT_BLOCKED
    else:
        limitations.append(
            "run without a supplied host: successor rows carry no live inventory and remain "
            "unqualified by construction"
        )

    live: Mapping[str, Any] | None = None
    if args.live_evidence:
        live = json.loads(args.live_evidence.read_text(encoding="utf-8"))
        if live.get("resource_block"):
            limitations.append(live["resource_block"])
    else:
        limitations.append(
            "offline stage only: no weight was loaded and no prompt was queued, so every row whose "
            "mechanism depends on model or sampler behaviour is unqualified pending the live "
            "canaries"
        )

    rows = build_rows(
        facts,
        inventory_candidates=_inventory_candidates(object_info),
        inventory_available=object_info is not None,
        live=live,
    )
    matrix = build_matrix(
        subject=_subject_descriptor(
            source_identity=identity,
            snapshot=snapshot,
            project_commit=_project_commit(_REPO_ROOT),
            weights=args.weight,
        ),
        rows=rows,
        limitations=limitations,
    )

    rendered = json.dumps(matrix, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")

    for row in matrix["rows"]:
        print(f"{row['row']:30} {row['status']:12} -> {row['consumer']}  ({row['reason_code']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
