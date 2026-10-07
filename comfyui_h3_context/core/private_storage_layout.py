"""Private storage ownership declarations; resolving a subtree creates no files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

MIB = 1024 * 1024
MANAGED_ARTIFACT_SUBTREE = "managed-artifacts"


@dataclass(frozen=True, slots=True)
class PrivateStorageSubtree:
    name: str
    owner: str
    lifetime: str
    limit_bytes: int | None
    limit_scope: str
    reserved: bool = False


PRIVATE_STORAGE_SUBTREES = (
    PrivateStorageSubtree(
        MANAGED_ARTIFACT_SUBTREE, "managed_generation", "process", 1024 * MIB, "per_live_process"
    ),
    PrivateStorageSubtree(
        "media-runtime", "media_runtime", "profile", None, "separate_runtime_policy"
    ),
    PrivateStorageSubtree("workspace-state", "workspace_state", "durable", 16 * MIB, "global"),
    PrivateStorageSubtree("recovery-assets", "retained_assets", "durable", 1024 * MIB, "global"),
    PrivateStorageSubtree(
        "project-recovery", "editor_recovery", "durable", 640 * MIB, "global", True
    ),
)
RESERVED_DURABLE_BYTES = sum(
    row.limit_bytes or 0 for row in PRIVATE_STORAGE_SUBTREES if row.reserved
)
DURABLE_BYTES = sum(
    row.limit_bytes or 0 for row in PRIVATE_STORAGE_SUBTREES if row.lifetime == "durable"
)


def private_subtree_path(root: Path, name: str) -> Path:
    if (
        not isinstance(root, Path)
        or not root.is_absolute()
        or name not in {row.name for row in PRIVATE_STORAGE_SUBTREES}
    ):
        raise ValueError("private_storage_layout")
    return root / name
