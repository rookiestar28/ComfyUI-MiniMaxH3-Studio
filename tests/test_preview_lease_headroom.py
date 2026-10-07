"""The preview's lease-headroom numbers are the authority's.

While a clip plays, the browser preview opens the next clip's source ahead of the cut. It does
that only when the leases the prepared owner holds are certain to fit, and it decides from two
numbers it cannot ask the authority for: the size of the workspace lease pool and the part of that
pool the authority keeps free of decorations so that playback can always acquire. A change to
either number on one side only would either starve the bin and timeline decorations or let a
prepared owner take a lease a visible layer needs. These tests read both ends and hold them equal.

No fixture here carries a prompt, a media value, a private path or a credential.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from comfyui_h3_context.adapters.authoring_media_leases import MediaLeaseAuthority
from comfyui_h3_context.core.authoring_media import MAX_WORKSPACE_LEASES

REPO_ROOT = Path(__file__).resolve().parents[1]
LOOKAHEAD = REPO_ROOT / "frontend" / "src" / "runtime" / "previewLookahead.ts"


def _exported_integer(name: str) -> int:
    source = LOOKAHEAD.read_text(encoding="utf-8")
    found = re.findall(rf"^export const {name} = (\d+);$", source, flags=re.MULTILINE)
    assert len(found) == 1, f"{name} must be declared exactly once as an integer literal"
    return int(found[0])


def _decoration_reserve() -> int:
    source = inspect.getsource(MediaLeaseAuthority._capacity)
    found = re.findall(r"len\(workspace\) >= MAX_WORKSPACE_LEASES - (\d+)", source)
    assert len(found) == 1, "the decoration admission must reserve one literal slot count"
    return int(found[0])


def test_the_preview_pool_is_the_authority_workspace_pool() -> None:
    assert _exported_integer("WORKSPACE_LEASE_POOL") == MAX_WORKSPACE_LEASES


def test_the_preview_reserve_is_the_authority_decoration_reserve() -> None:
    assert _exported_integer("WORKSPACE_PLAYBACK_RESERVE") == _decoration_reserve()


def test_the_headroom_rule_leaves_a_decoration_admissible() -> None:
    # The preview prepares only while `held <= pool - reserve - 1`; the authority admits a
    # decoration only while `held < pool - reserve`. The two are the same boundary.
    pool = _exported_integer("WORKSPACE_LEASE_POOL")
    reserve = _exported_integer("WORKSPACE_PLAYBACK_RESERVE")
    source = LOOKAHEAD.read_text(encoding="utf-8")
    assert "WORKSPACE_LEASE_POOL - WORKSPACE_PLAYBACK_RESERVE - 1" in source
    assert pool - reserve - 1 < MAX_WORKSPACE_LEASES - _decoration_reserve()
