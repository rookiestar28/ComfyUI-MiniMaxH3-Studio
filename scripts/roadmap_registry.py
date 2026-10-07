"""Bounded active roadmap index, deterministic root view, and immutable history archive.

Roadmap and plan documents are inert ignored UTF-8 data. This tool does not import product code,
execute document content, contact a network, or follow symlink/reparse components.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn, cast

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_INDEX_SCHEMA = "h3-context-active-roadmap-index/2"
ARCHIVE_MANIFEST_SCHEMA = "h3-context-roadmap-archive/1"
RENDER_SCHEMA = "h3-context-active-roadmap-render/2"
HISTORY_SCHEMA = "h3-context-roadmap-history/1"
RESTORE_SCHEMA = "h3-context-roadmap-restore/2"
TERMINAL_ITEMS_SCHEMA = "h3-context-roadmap-terminal-items/1"
DEFAULT_INDEX_PATH = Path(".planning/roadmap/roadmap.json")
DEFAULT_ARCHIVE_PATH = Path(".planning/roadmap/archive/260813_PRE_RM17_ACTIVE_INDEX")

ACTIVE_STATUSES = frozenset(
    {"PENDING", "READY", "IN_PROGRESS", "BLOCKED", "IMPLEMENTED_UNVERIFIED"}
)
ITEM_ID_PATTERN = re.compile(r"^(?:M\d+|HC|RM)-\d+$")
OID_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ROOT_MEMBERS = frozenset({"schema", "latest_accepted", "items", "archives"})
CHECKPOINT_MEMBERS = frozenset({"id", "title", "commit", "tree", "record_path"})
ITEM_MEMBERS = frozenset(
    {
        "id",
        "title",
        "status",
        "depends_on",
        "owner",
        "plan_path",
        "plan_state",
        "summary",
        "activation_condition",
        "live_record_path",
    }
)
ARCHIVE_MEMBERS = frozenset({"path", "manifest_sha256", "item_count", "description"})
TERMINAL_ROOT_MEMBERS = frozenset({"schema", "items"})
TERMINAL_ITEM_MEMBERS = frozenset(
    {"id", "title", "status", "depends_on", "owner", "plan_path", "commit", "tree", "record_path"}
)
MAX_INDEX_BYTES = 128_000
MAX_ITEMS = 256
PLAN_STATES = frozenset({"NOT_REQUIRED", "DRAFT", "FINALIZED", "STALE"})
MAX_TEXT = 2_000
MAX_ITEM_TEXT = 240
MAX_ROOT_BYTES = 12 * 1024
MAX_ROOT_LINE = 320
VIEW_DIRECTORY = Path(".planning/roadmap/views")
VIEW_TRACKS = (
    "LEGACY_RELEASE",
    "M17",
    "M18",
    "M19",
    "M20",
    "M21",
    "M22",
    "M23",
    "M24",
    "M25",
    "M26",
)
COORDINATION_PLAN_PATTERN = re.compile(
    r"(?:^|\s)Coordination:\s+([0-9]{6}-[A-Za-z0-9_-]+_PLAN\.md)\."
)
REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class RoadmapIndexError(RuntimeError):
    """Raised when active roadmap or archive validation fails closed."""


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _fail(message: str) -> NoReturn:
    raise RoadmapIndexError(message)


def _duplicate_guard(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        _fail(f"{field} must be an object")
    return cast(dict[str, object], value)


def _list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        _fail(f"{field} must be an array")
    return cast(list[object], value)


def _string(value: object, field: str, *, maximum: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        _fail(f"{field} must be a bounded non-empty string")
    if any(marker in value for marker in ("\r", "\n", "|")):
        _fail(f"{field} must be a single-line markdown-safe string")
    return value


def _closed(value: dict[str, object], expected: frozenset[str], field: str) -> None:
    if set(value) != expected:
        _fail(f"{field} members are not closed")


def _safe_relative(value: str | Path, field: str) -> Path:
    text = str(value)
    path = Path(text)
    if (
        not text
        or "\x00" in text
        or path.is_absolute()
        or path.drive
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        _fail(f"{field} must be a safe repo-relative path")
    return path


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        metadata = path.lstat()
        return bool(int(getattr(metadata, "st_file_attributes", 0)) & REPARSE_ATTRIBUTE)
    except AttributeError:
        return False


def _checked_path(root: Path, relative: Path, field: str, *, regular: bool) -> Path:
    root = root.resolve()
    candidate = root / relative
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() and _is_link_or_reparse(current):
            _fail(f"{field} contains a symlink or reparse component")
    if regular and (not candidate.is_file() or _is_link_or_reparse(candidate)):
        _fail(f"{field} must be a regular nonlink file")
    return candidate


def _read_bytes(root: Path, relative: Path, field: str, *, maximum: int = 2_000_000) -> bytes:
    path = _checked_path(root, relative, field, regular=True)
    raw = path.read_bytes()
    if not raw or len(raw) > maximum:
        _fail(f"{field} has invalid byte length")
    return raw


def _read_json(root: Path, relative: Path, field: str, *, maximum: int) -> dict[str, object]:
    raw = _read_bytes(root, relative, field, maximum=maximum)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RoadmapIndexError(f"{field} is not strict UTF-8") from exc
    try:
        value = json.loads(text, object_pairs_hook=_duplicate_guard)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise RoadmapIndexError(f"{field} is invalid JSON") from exc
    return _mapping(value, field)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _atomic_write(root: Path, relative: Path, raw: bytes, field: str) -> None:
    root = root.resolve()
    if relative.parent != Path("."):
        _checked_path(root, relative.parent, f"{field} parent", regular=False)
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if _is_link_or_reparse(target.parent) or (target.exists() and _is_link_or_reparse(target)):
        _fail(f"{field} destination contains a symlink or reparse component")
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_plan(root: Path, value: object, status: str, field: str) -> str | None:
    if value is None:
        if status != "PENDING":
            _fail(f"only PENDING placeholders may omit {field}")
        return None
    text = _string(value, field, maximum=240)
    relative = _safe_relative(text, field)
    if relative.suffix != ".md" or not relative.name.endswith("_PLAN.md"):
        _fail(f"{field} must name an item plan")
    _checked_path(root, relative, "plan file", regular=True)
    return relative.as_posix()


def _validate_optional_internal_path(root: Path, value: object, field: str) -> str | None:
    if value is None:
        return None
    text = _string(value, field, maximum=260)
    relative = _safe_relative(text, field)
    if relative.suffix != ".md" or not relative.as_posix().startswith(".planning/"):
        _fail(f"{field} must name an internal markdown file")
    _checked_path(root, relative, field, regular=True)
    return relative.as_posix()


def _manifest(root: Path, archive: Path) -> tuple[dict[str, object], bytes]:
    relative = archive / "manifest.json"
    raw = _read_bytes(root, relative, "archive manifest", maximum=256_000)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicate_guard)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RoadmapIndexError("archive manifest is invalid strict UTF-8 JSON") from exc
    manifest = _mapping(value, "archive manifest")
    _closed(manifest, frozenset({"schema", "entries"}), "archive manifest")
    if manifest["schema"] != ARCHIVE_MANIFEST_SCHEMA:
        _fail("archive manifest schema is unknown")
    entries = _list(manifest["entries"], "archive entries")
    if not entries or len(entries) > 64:
        _fail("archive entry count is invalid")
    seen: set[str] = set()
    for index, raw_entry in enumerate(entries):
        entry = _mapping(raw_entry, f"archive entry[{index}]")
        _closed(entry, frozenset({"path", "byte_length", "sha256"}), f"archive entry[{index}]")
        path_text = _string(entry["path"], f"archive entry[{index}].path", maximum=260)
        if path_text in seen:
            _fail("archive contains a duplicate path")
        seen.add(path_text)
        path = _safe_relative(path_text, f"archive entry[{index}].path")
        byte_length = entry["byte_length"]
        digest = entry["sha256"]
        if not isinstance(byte_length, int) or isinstance(byte_length, bool) or byte_length < 1:
            _fail(f"archive entry[{index}] byte length is invalid")
        if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None:
            _fail(f"archive entry[{index}] hash is invalid")
        archived = _read_bytes(root, archive / path, f"archive entry[{index}]")
        if len(archived) != byte_length or _sha256(archived) != digest:
            _fail(f"archive entry[{index}] bytes do not match manifest")
    return manifest, raw


def load_index(root: Path, index_path: Path | str = DEFAULT_INDEX_PATH) -> dict[str, object]:
    """Load and fail-closed validate the sole active roadmap authority."""

    root = root.resolve()
    relative = _safe_relative(index_path, "index path")
    index = _read_json(root, relative, "active roadmap index", maximum=MAX_INDEX_BYTES)
    _closed(index, ROOT_MEMBERS, "active roadmap index")
    if index["schema"] != ACTIVE_INDEX_SCHEMA:
        _fail("active roadmap index schema is unknown")

    checkpoint = _mapping(index["latest_accepted"], "latest accepted checkpoint")
    _closed(checkpoint, CHECKPOINT_MEMBERS, "latest accepted checkpoint")
    checkpoint_id = _string(checkpoint["id"], "latest accepted id", maximum=32)
    if ITEM_ID_PATTERN.fullmatch(checkpoint_id) is None:
        _fail("latest accepted id is invalid")
    for member in ("commit", "tree"):
        oid = _string(checkpoint[member], f"latest accepted {member}", maximum=40)
        if OID_PATTERN.fullmatch(oid) is None:
            _fail(f"latest accepted {member} is invalid")
    _string(checkpoint["title"], "latest accepted title")
    _safe_relative(
        _string(checkpoint["record_path"], "latest accepted record path", maximum=260),
        "latest accepted record path",
    )

    raw_items = _list(index["items"], "active items")
    if not raw_items or len(raw_items) > MAX_ITEMS:
        _fail("active item count is invalid")
    items: list[dict[str, object]] = []
    ids: list[str] = []
    for item_index, raw_item in enumerate(raw_items):
        item = _mapping(raw_item, f"item[{item_index}]")
        _closed(item, ITEM_MEMBERS, f"item[{item_index}]")
        item_id = _string(item["id"], f"item[{item_index}].id", maximum=32)
        if ITEM_ID_PATTERN.fullmatch(item_id) is None:
            _fail(f"item[{item_index}] id is invalid")
        status = _string(item["status"], f"item[{item_index}].status", maximum=32)
        if status not in ACTIVE_STATUSES:
            _fail(f"item[{item_index}] must have an active status")
        dependencies = _list(item["depends_on"], f"item[{item_index}].depends_on")
        if any(
            not isinstance(value, str) or ITEM_ID_PATTERN.fullmatch(value) is None
            for value in dependencies
        ):
            _fail(f"item[{item_index}] dependencies are invalid")
        if len(dependencies) != len(set(dependencies)):
            _fail(f"item[{item_index}] dependencies contain duplicates")
        _string(item["title"], f"item[{item_index}].title")
        _string(item["owner"], f"item[{item_index}].owner", maximum=100)
        plan = _validate_plan(root, item["plan_path"], status, f"item[{item_index}].plan_path")
        plan_state = _string(item["plan_state"], f"item[{item_index}].plan_state", maximum=32)
        if plan_state not in PLAN_STATES:
            _fail(f"item[{item_index}].plan_state is invalid")
        if plan is None and plan_state not in {"DRAFT", "NOT_REQUIRED"}:
            _fail(f"item[{item_index}] without a plan has invalid plan state")
        if plan is not None and plan_state == "NOT_REQUIRED":
            _fail(f"item[{item_index}] with a plan cannot be NOT_REQUIRED")
        _string(item["summary"], f"item[{item_index}].summary", maximum=MAX_ITEM_TEXT)
        _string(
            item["activation_condition"],
            f"item[{item_index}].activation_condition",
            maximum=MAX_ITEM_TEXT,
        )
        live_record = _validate_optional_internal_path(
            root, item["live_record_path"], f"item[{item_index}].live_record_path"
        )
        if live_record is not None and status not in {"IN_PROGRESS", "IMPLEMENTED_UNVERIFIED"}:
            _fail(f"item[{item_index}] status cannot own a live record")
        ids.append(item_id)
        items.append(item)
    if len(ids) != len(set(ids)):
        _fail("active index contains a duplicate item ID")
    raw_archives = _list(index["archives"], "archives")
    # IMPORTANT: the archive catalog has no count ceiling, on purpose. MAX_INDEX_BYTES already
    # bounds how many entries the index can name, and every archive named here is read under its
    # own byte bound, so a count limit measures nothing those bounds do not. A fixed count
    # ceiling stops the catalog from growing while archive_authorities still refuses to rewrite
    # an existing archive, and the only way left to advance the checkpoint is to drop satisfied
    # dependency edges. Keep the non-empty requirement: an index without history cannot resolve
    # archived dependencies.
    if not raw_archives:
        _fail("archive count is invalid")
    seen_archives: set[str] = set()
    archived_ids: set[str] = set()
    for archive_index, raw_archive in enumerate(raw_archives):
        archive = _mapping(raw_archive, f"archive[{archive_index}]")
        _closed(archive, ARCHIVE_MEMBERS, f"archive[{archive_index}]")
        archive_text = _string(archive["path"], f"archive[{archive_index}].path", maximum=260)
        if archive_text in seen_archives:
            _fail("active index contains a duplicate archive")
        seen_archives.add(archive_text)
        archive_path = _safe_relative(archive_text, f"archive[{archive_index}].path")
        digest = archive["manifest_sha256"]
        if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None:
            _fail(f"archive[{archive_index}] manifest hash is invalid")
        item_count = archive["item_count"]
        if not isinstance(item_count, int) or isinstance(item_count, bool) or item_count < 1:
            _fail(f"archive[{archive_index}] item count is invalid")
        _string(archive["description"], f"archive[{archive_index}].description")
        _manifest_value, manifest_raw = _manifest(root, archive_path)
        if _sha256(manifest_raw) != digest:
            _fail(f"archive[{archive_index}] manifest hash does not match")
        archived_ids.update(_archived_item_ids(root, archive_path))
    allowed_dependencies = set(ids) | archived_ids | {checkpoint_id}
    for item in items:
        for dependency in cast(list[str], item["depends_on"]):
            if dependency not in allowed_dependencies:
                _fail(f"active item {item['id']} has dangling dependency {dependency}")
    _validate_graph(items)
    return index


def _validate_graph(items: list[dict[str, object]]) -> None:
    by_id = {str(item["id"]): item for item in items}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(item_id: str) -> None:
        if item_id in visiting:
            _fail("active roadmap dependency cycle detected")
        if item_id in visited:
            return
        visiting.add(item_id)
        for dependency in cast(list[str], by_id[item_id]["depends_on"]):
            if dependency in by_id:
                visit(dependency)
        visiting.remove(item_id)
        visited.add(item_id)

    for item_id in by_id:
        visit(item_id)


def archive_authorities(
    root: Path, authorities: Sequence[Path | str], archive_path: Path | str
) -> dict[str, object]:
    """Copy an exact bounded authority set into an immutable dated archive."""

    root = root.resolve()
    archive = _safe_relative(archive_path, "archive path")
    if not authorities or len(authorities) > 64:
        _fail("archive authority count is invalid")
    entries: list[dict[str, object]] = []
    source_bytes: dict[Path, bytes] = {}
    for index, authority in enumerate(authorities):
        relative = _safe_relative(authority, f"authority[{index}]")
        if relative in source_bytes:
            _fail("archive authorities contain a duplicate path")
        raw = _read_bytes(root, relative, f"authority[{index}]")
        source_bytes[relative] = raw
        entries.append(
            {"path": relative.as_posix(), "byte_length": len(raw), "sha256": _sha256(raw)}
        )
    entries.sort(key=lambda entry: str(entry["path"]))
    manifest: dict[str, object] = {"schema": ARCHIVE_MANIFEST_SCHEMA, "entries": entries}
    manifest_raw = _json_bytes(manifest)
    manifest_path = root / archive / "manifest.json"
    if manifest_path.exists():
        current_manifest, current_raw = _manifest(root, archive)
        if current_manifest != manifest:
            _fail("existing archive differs from requested authority bytes")
        return {
            **current_manifest,
            "entry_count": len(entries),
            "manifest_sha256": _sha256(current_raw),
        }
    archive_root = root / archive
    if archive_root.exists() and any(archive_root.iterdir()):
        _fail("existing archive is partial or lacks a valid manifest")
    for relative, raw in source_bytes.items():
        _atomic_write(root, archive / relative, raw, f"archive {relative.as_posix()}")
    _atomic_write(root, archive / "manifest.json", manifest_raw, "archive manifest")
    verified, verified_raw = _manifest(root, archive)
    return {
        **verified,
        "entry_count": len(entries),
        "manifest_sha256": _sha256(verified_raw),
    }


def _archived_items(root: Path, archive: Path) -> list[dict[str, object]]:
    manifest, _raw = _manifest(root, archive)
    entries = _list(manifest["entries"], "archive entries")
    full_registries = [
        Path(str(_mapping(entry, "archive entry")["path"]))
        for entry in entries
        if Path(str(_mapping(entry, "archive entry")["path"])).name == "roadmap.json"
    ]
    terminal_sources = [
        Path(str(_mapping(entry, "archive entry")["path"]))
        for entry in entries
        if Path(str(_mapping(entry, "archive entry")["path"])).name == "terminal_items.json"
    ]
    if len(full_registries) + len(terminal_sources) != 1:
        _fail("archive must contain exactly one roadmap or terminal-item authority")
    if full_registries:
        registry = _read_json(
            root, archive / full_registries[0], "archived roadmap", maximum=1_000_000
        )
        raw_items = _list(registry.get("items"), "archived roadmap items")
        return [_mapping(item, f"archived item[{index}]") for index, item in enumerate(raw_items)]

    terminal = _read_json(
        root, archive / terminal_sources[0], "archived terminal items", maximum=128_000
    )
    _closed(terminal, TERMINAL_ROOT_MEMBERS, "archived terminal items")
    if terminal["schema"] != TERMINAL_ITEMS_SCHEMA:
        _fail("archived terminal-item schema is unknown")
    raw_items = _list(terminal["items"], "archived terminal items")
    if not raw_items or len(raw_items) > MAX_ITEMS:
        _fail("archived terminal-item count is invalid")
    items: list[dict[str, object]] = []
    for index, raw_item in enumerate(raw_items):
        item = _mapping(raw_item, f"archived terminal item[{index}]")
        _closed(item, TERMINAL_ITEM_MEMBERS, f"archived terminal item[{index}]")
        item_id = _string(item["id"], f"archived terminal item[{index}].id", maximum=32)
        if ITEM_ID_PATTERN.fullmatch(item_id) is None:
            _fail(f"archived terminal item[{index}] id is invalid")
        if item["status"] not in {"DONE", "SUPERSEDED"}:
            _fail(f"archived terminal item[{index}] status is not terminal")
        dependencies = _list(item["depends_on"], f"archived terminal item[{index}].depends_on")
        if any(
            not isinstance(value, str) or ITEM_ID_PATTERN.fullmatch(value) is None
            for value in dependencies
        ) or len(dependencies) != len(set(dependencies)):
            _fail(f"archived terminal item[{index}] dependencies are invalid")
        _string(item["title"], f"archived terminal item[{index}].title")
        _string(item["owner"], f"archived terminal item[{index}].owner", maximum=100)
        for member in ("commit", "tree"):
            oid = _string(item[member], f"archived terminal item[{index}].{member}", maximum=40)
            if OID_PATTERN.fullmatch(oid) is None:
                _fail(f"archived terminal item[{index}].{member} is invalid")
        plan = _safe_relative(
            _string(item["plan_path"], f"archived terminal item[{index}].plan_path", maximum=260),
            f"archived terminal item[{index}].plan_path",
        )
        record = _safe_relative(
            _string(
                item["record_path"],
                f"archived terminal item[{index}].record_path",
                maximum=260,
            ),
            f"archived terminal item[{index}].record_path",
        )
        if not plan.name.endswith("_PLAN.md") or not record.name.endswith(
            "_IMPLEMENTATION_RECORD.md"
        ):
            _fail(f"archived terminal item[{index}] plan or record path is invalid")
        items.append(item)
    return items


def history_lookup(root: Path, archive_path: Path | str, item_id: str) -> dict[str, object]:
    """Return a bounded item projection from an immutable roadmap archive."""

    root = root.resolve()
    archive = _safe_relative(archive_path, "archive path")
    raw_items = _archived_items(root, archive)
    matches = [item for item in raw_items if item.get("id") == item_id]
    if len(matches) != 1:
        _fail(f"historical roadmap item {item_id} was not found")
    item = matches[0]
    projection = {
        "id": item.get("id"),
        "title": item.get("title"),
        "status": item.get("status"),
        "depends_on": item.get("depends_on"),
    }
    if not isinstance(projection["depends_on"], list):
        _fail("historical roadmap item is malformed")
    return {"schema": HISTORY_SCHEMA, "status": "PASS", "item": projection}


def _archived_item_ids(root: Path, archive: Path) -> set[str]:
    raw_items = _archived_items(root, archive)
    item_ids = {str(item["id"]) for item in raw_items if isinstance(item.get("id"), str)}
    if len(item_ids) != len(raw_items):
        _fail("archived roadmap item IDs are invalid or duplicated")
    return item_ids


def _track(item_id: str) -> str:
    match = re.match(r"^(M\d+)-", item_id)
    if match and match.group(1) in VIEW_TRACKS:
        return match.group(1)
    return "LEGACY_RELEASE"


def _eligible_items(items: list[dict[str, object]]) -> list[dict[str, object]]:
    active_ids = {str(item["id"]) for item in items}
    return [
        item
        for item in items
        if item["status"] in {"PENDING", "READY"}
        and all(dependency not in active_ids for dependency in cast(list[str], item["depends_on"]))
    ]


def _dependency_waves(items: list[dict[str, object]], *, target_id: str) -> list[list[str]]:
    """Return earliest HARD-dependency waves for one active target and its active ancestors."""

    by_id = {str(item["id"]): item for item in items}
    if target_id not in by_id:
        return []

    closure: set[str] = set()

    def collect(item_id: str) -> None:
        if item_id in closure:
            return
        closure.add(item_id)
        for dependency in cast(list[str], by_id[item_id]["depends_on"]):
            if dependency in by_id:
                collect(dependency)

    collect(target_id)
    levels: dict[str, int] = {}

    def level(item_id: str) -> int:
        if item_id in levels:
            return levels[item_id]
        active_dependencies = [
            dependency
            for dependency in cast(list[str], by_id[item_id]["depends_on"])
            if dependency in closure
        ]
        levels[item_id] = (
            0
            if not active_dependencies
            else 1 + max(level(dependency) for dependency in active_dependencies)
        )
        return levels[item_id]

    for item_id in closure:
        level(item_id)
    return [
        sorted(item_id for item_id in closure if levels[item_id] == wave)
        for wave in range(max(levels.values()) + 1)
    ]


def _coordination_plan_paths(items: list[dict[str, object]]) -> list[str]:
    """Extract the bounded current coordination authorities declared by active item metadata."""

    paths: set[str] = set()
    for item in items:
        match = COORDINATION_PLAN_PATTERN.search(str(item["activation_condition"]))
        if match:
            paths.add(f".planning/{match.group(1)}")
    return sorted(paths)


def _root_excerpt(value: object, maximum: int = 120) -> str:
    text = str(value)
    return text if len(text) <= maximum else f"{text[: maximum - 3]}..."


def _render_text(index: dict[str, object]) -> str:
    checkpoint = _mapping(index["latest_accepted"], "latest accepted checkpoint")
    items = [_mapping(item, "active item") for item in _list(index["items"], "active items")]
    archives = [_mapping(item, "archive") for item in _list(index["archives"], "archives")]
    lines = [
        "# ComfyUI-MiniMaxH3-Context Roadmap",
        "",
        "Generated compact dashboard; `.planning/roadmap/roadmap.json` is the sole active "
        "authority.",
        "",
        "## Current Handoff",
        "",
        f"- Latest accepted item: `{checkpoint['id']}` — {checkpoint['title']}",
        f"- Accepted commit: `{checkpoint['commit']}`",
        f"- Accepted tree: `{checkpoint['tree']}`",
        f"- Record: `{checkpoint['record_path']}`",
        "",
        "## Active Execution",
        "",
        "| Item | Status | Record | Activation condition |",
        "| --- | --- | --- | --- |",
    ]
    active = [
        item
        for item in items
        if item["status"] in {"READY", "IN_PROGRESS", "BLOCKED", "IMPLEMENTED_UNVERIFIED"}
    ]
    for item in active:
        record = str(item["live_record_path"] or item["plan_path"] or "not recorded")
        lines.append(
            f"| `{item['id']}` | `{item['status']}` | `{record}` | "
            f"{_root_excerpt(item['activation_condition'])} |"
        )
    lines.extend(
        [
            "",
            "## Chain Summary",
            "",
            "| Chain | Pending | Active | Blocked |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for track in VIEW_TRACKS:
        cohort = [item for item in items if _track(str(item["id"])) == track]
        pending = sum(item["status"] == "PENDING" for item in cohort)
        running = sum(
            item["status"] in {"READY", "IN_PROGRESS", "IMPLEMENTED_UNVERIFIED"} for item in cohort
        )
        blocked = sum(item["status"] == "BLOCKED" for item in cohort)
        lines.append(f"| `{track}` | {pending} | {running} | {blocked} |")
    waves = _dependency_waves(items, target_id="M16-05")
    if waves:
        by_id = {str(item["id"]): item for item in items}
        lines.extend(
            [
                "",
                "## Pre-release HARD Dependency Waves",
                "",
                "Earliest graph eligibility derived from `depends_on`; "
                "it does not grant activation. "
                "`ORDER_ONLY` remains an execution/path-serialization rule in the coordination "
                "authority.",
                "",
                "| Wave | Active items |",
                "| --- | --- |",
            ]
        )
        for wave_number, wave in enumerate(waves):
            entries = ", ".join(f"`{item_id}` (`{by_id[item_id]['status']}`)" for item_id in wave)
            lines.append(f"| H{wave_number} | {entries} |")
    coordination_paths = _coordination_plan_paths(items)
    if coordination_paths:
        lines.extend(["", "## Coordination Authorities", ""])
        lines.extend(f"- [`{path}`]({path})" for path in coordination_paths)
    lines.extend(["", "## Eligible Frontier", ""])
    eligible = _eligible_items(items)
    lines.extend(
        f"- `{item['id']}` — {_root_excerpt(item['activation_condition'])}" for item in eligible
    )
    if not eligible:
        lines.append("- None; dependency eligibility grants no activation authority.")
    lines.extend(
        [
            "",
            "## Recent Archives",
            "",
            f"- Catalog: {len(archives)} archives; complete generated view: "
            "`.planning/roadmap/views/ARCHIVES.md`.",
        ]
    )
    for archive in archives[-5:]:
        lines.append(
            f"- `{archive['path']}` — {archive['item_count']} items — {archive['description']}"
        )
    lines.extend(
        [
            "",
            "Render/check: `.venv\\Scripts\\python.exe scripts\\roadmap_registry.py render "
            "--index .planning/roadmap/roadmap.json --output ROADMAP.md [--check]`.",
            "",
        ]
    )
    text = "\n".join(lines)
    if len(text.encode("utf-8")) > MAX_ROOT_BYTES or any(
        len(line) > MAX_ROOT_LINE for line in text.splitlines()
    ):
        _fail("root roadmap readability budget exceeded")
    return text


def _render_chain(track: str, items: list[dict[str, object]]) -> str:
    lines = [
        f"# {track} active roadmap view",
        "",
        "Generated projection; edit `.planning/roadmap/roadmap.json`, not this file.",
        "",
    ]
    for item in items:
        dependencies = ", ".join(cast(list[str], item["depends_on"])) or "None"
        lines.extend(
            [
                f"## {item['id']} — {item['title']}",
                "",
                f"- Status: `{item['status']}`; plan state: `{item['plan_state']}`; "
                f"owner: `{item['owner']}`",
                f"- Depends on: {dependencies}",
                f"- Plan: `{item['plan_path'] or 'plan required'}`",
                f"- Summary: {item['summary']}",
                f"- Activation: {item['activation_condition']}",
                f"- Live record: `{item['live_record_path'] or 'none'}`",
                "",
            ]
        )
    return "\n".join(lines)


def _render_archives(archives: list[dict[str, object]]) -> str:
    lines = [
        "# Roadmap archive catalog",
        "",
        "Generated projection; edit `.planning/roadmap/roadmap.json`, not this file.",
        "",
    ]
    for archive in archives:
        lines.extend(
            [
                f"## {archive['path']}",
                "",
                f"- Items: {archive['item_count']}",
                f"- Manifest SHA-256: `{archive['manifest_sha256']}`",
                f"- Description: {archive['description']}",
                "",
            ]
        )
    return "\n".join(lines)


def _render_outputs(index: dict[str, object]) -> dict[Path, bytes]:
    items = [_mapping(item, "active item") for item in _list(index["items"], "active items")]
    archives = [_mapping(item, "archive") for item in _list(index["archives"], "archives")]
    outputs = {Path("ROADMAP.md"): _render_text(index).encode("utf-8")}
    for track in VIEW_TRACKS:
        cohort = [item for item in items if _track(str(item["id"])) == track]
        if cohort:
            outputs[VIEW_DIRECTORY / f"{track}.md"] = _render_chain(track, cohort).encode("utf-8")
    outputs[VIEW_DIRECTORY / "ARCHIVES.md"] = _render_archives(archives).encode("utf-8")
    return outputs


def render_roadmap(
    root: Path, index_path: Path | str, output: Path | str, *, check: bool
) -> dict[str, object]:
    """Render or verify the closed compact roadmap projection set."""

    root = root.resolve()
    output_path = _safe_relative(output, "render output")
    if output_path != Path("ROADMAP.md"):
        _fail("the only live roadmap projection is ROADMAP.md")
    index = load_index(root, index_path)
    outputs = _render_outputs(index)
    expected_paths = set(outputs)
    view_root = root / VIEW_DIRECTORY
    current_views = (
        {path.relative_to(root) for path in view_root.glob("*.md")} if view_root.is_dir() else set()
    )
    unexpected = current_views - {path for path in expected_paths if path.parent == VIEW_DIRECTORY}
    if unexpected:
        _fail("unexpected owned roadmap projection detected")
    if check:
        for relative, expected in outputs.items():
            current = _read_bytes(root, relative, "roadmap projection", maximum=128_000)
            if current != expected:
                _fail("roadmap render drift detected; --check made no changes")
    else:
        staging_parent = root / ".tmp"
        staging_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="rm19-render-", dir=staging_parent) as temporary:
            staged = Path(temporary)
            for relative, expected in outputs.items():
                target = staged / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(expected)
                if target.read_bytes() != expected:
                    _fail("staged roadmap projection verification failed")
            for relative, expected in outputs.items():
                _atomic_write(root, relative, expected, "roadmap projection")
    return {
        "schema": RENDER_SCHEMA,
        "status": "PASS",
        "item_count": len(_list(index["items"], "active items")),
        "output": output_path.as_posix(),
        "projection_count": len(outputs),
    }


def restore_check(root: Path, archive_path: Path | str) -> dict[str, object]:
    """Verify every archived byte through a disposable workspace-owned reconstruction."""

    root = root.resolve()
    archive = _safe_relative(archive_path, "archive path")
    manifest, _raw = _manifest(root, archive)
    entries = _list(manifest["entries"], "archive entries")
    temp_parent = root / ".tmp"
    temp_parent.mkdir(parents=True, exist_ok=True)
    if _is_link_or_reparse(temp_parent):
        _fail("restore temporary parent is a symlink or reparse point")
    with tempfile.TemporaryDirectory(prefix="rm17-restore-", dir=temp_parent) as temporary_text:
        temporary = Path(temporary_text)
        for index, raw_entry in enumerate(entries):
            entry = _mapping(raw_entry, f"archive entry[{index}]")
            relative = _safe_relative(str(entry["path"]), f"archive entry[{index}].path")
            source = _read_bytes(root, archive / relative, f"archive entry[{index}]")
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source)
            restored = target.read_bytes()
            if len(restored) != entry["byte_length"] or _sha256(restored) != entry["sha256"]:
                _fail(f"restored archive entry[{index}] differs")
    return {
        "schema": RESTORE_SCHEMA,
        "status": "PASS",
        "entry_count": len(entries),
        "cleanup": "PASS",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    archive = commands.add_parser("archive")
    archive.add_argument("--output", required=True)
    archive.add_argument("--path", action="append", required=True)
    render = commands.add_parser("render")
    render.add_argument("--index", required=True)
    render.add_argument("--output", required=True)
    render.add_argument("--check", action="store_true")
    history = commands.add_parser("history")
    history.add_argument("--archive", required=True)
    history.add_argument("--item", required=True)
    restore = commands.add_parser("restore-check")
    restore.add_argument("--archive", required=True)
    return parser


def main(argv: Sequence[str] | None = None, *, root: Path = ROOT) -> int:
    args = _parser().parse_args(argv)
    if args.command == "archive":
        result = archive_authorities(root, args.path, args.output)
        result = {
            "schema": result["schema"],
            "status": "PASS",
            "entry_count": result["entry_count"],
            "manifest_sha256": result["manifest_sha256"],
        }
    elif args.command == "render":
        result = render_roadmap(root, args.index, args.output, check=args.check)
    elif args.command == "history":
        result = history_lookup(root, args.archive, args.item)
    else:
        result = restore_check(root, args.archive)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RoadmapIndexError as exc:
        print(f"ROADMAP INDEX: FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
