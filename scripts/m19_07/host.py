"""Read-only inventory of the explicitly supplied host.

Nothing here queues a prompt or loads a weight. The two endpoints used are informational, and the
node inventory is cached to disk after the first read because `/object_info` is large and the
supplied host is shared with the concurrent `M17-20` supported-host lane — re-reading it on every
run would tax a host that is busy generating.

The host is never started, discovered or restarted. A supplied endpoint that does not answer is an
explicit `NOT_RUN`, not an invitation to go looking for another one.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The pinned native module every H3 node must come from. A node claiming an H3 identity from some
#: other module is a custom pack, not the exact subject.
NATIVE_MODULE = "comfy_extras.nodes_minimax_h3"


class HostUnavailable(RuntimeError):
    """The supplied host did not answer. Never a reason to look for a different one."""


@dataclass(frozen=True)
class HostSnapshot:
    comfyui_version: str
    frontend_version: str
    templates_version: str
    python_version: str
    torch_version: str
    device_class: str
    argv: tuple[str, ...]

    def as_evidence(self) -> dict[str, Any]:
        return {
            "comfyui_version": self.comfyui_version,
            "frontend_version": self.frontend_version,
            "templates_version": self.templates_version,
            "python_version": self.python_version,
            "torch_version": self.torch_version,
            "device_class": self.device_class,
            "argv": list(self.argv),
        }


def opaque_identity(value: str, *, prefix: str) -> str:
    """A bounded, stable, non-reversing identity for something that must not be named in full."""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def _get_json(base_url: str, route: str, *, timeout: float) -> Any:
    url = base_url.rstrip("/") + route
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - loopback only
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise HostUnavailable(
            f"supplied host did not answer {route}: {error.__class__.__name__}"
        ) from error


def system_snapshot(base_url: str, *, timeout: float = 30.0) -> HostSnapshot:
    payload = _get_json(base_url, "/system_stats", timeout=timeout)
    system = payload.get("system", {})
    packages = {
        item.get("name"): item.get("installed") for item in system.get("comfy_package_versions", [])
    }
    devices = payload.get("devices") or [{}]
    first = devices[0]
    python_version = str(system.get("python_version", "")).split(" ", 1)[0]
    return HostSnapshot(
        comfyui_version=str(system.get("comfyui_version", "")),
        frontend_version=str(packages.get("comfyui-frontend-package", "")),
        templates_version=str(packages.get("comfyui-workflow-templates", "")),
        python_version=python_version,
        torch_version=str(system.get("pytorch_version", "")),
        device_class=str(first.get("type", "unknown")),
        argv=tuple(str(item) for item in system.get("argv", [])),
    )


def node_inventory(base_url: str, *, cache: Path, timeout: float = 120.0) -> Mapping[str, Any]:
    """Fetch `/object_info` once and reuse it.

    The cache is keyed by nothing but its own presence on purpose: the subject is frozen for the
    duration of the item, and a stale cache across a subject change is caught by the identity
    precondition rather than by re-fetching megabytes on every invocation.
    """
    if cache.exists():
        cached: dict[str, Any] = json.loads(cache.read_text(encoding="utf-8"))
        return cached
    payload: dict[str, Any] = _get_json(base_url, "/object_info", timeout=timeout)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def native_node_ids(object_info: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        sorted(
            node_id
            for node_id, spec in object_info.items()
            if spec.get("python_module") == NATIVE_MODULE
        )
    )


def input_names(object_info: Mapping[str, Any], node_id: str) -> tuple[str, ...]:
    spec = object_info.get(node_id, {}).get("input", {})
    names: list[str] = []
    for kind in ("required", "optional"):
        names.extend((spec.get(kind) or {}).keys())
    return tuple(names)


def search_node_ids(object_info: Mapping[str, Any], *, needles: tuple[str, ...]) -> tuple[str, ...]:
    """Every node class whose identity mentions one of the given terms, case-insensitively."""
    lowered = tuple(needle.lower() for needle in needles)
    return tuple(
        sorted(
            node_id
            for node_id in object_info
            if any(needle in node_id.lower() for needle in lowered)
        )
    )
