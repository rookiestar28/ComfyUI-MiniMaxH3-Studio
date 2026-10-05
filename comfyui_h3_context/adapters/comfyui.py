"""Optional ComfyUI host boundary.

This module is intentionally explicit: importing it requires the host-provided ``comfy_api``
module. The package root and pure core never import this module automatically.
"""

from __future__ import annotations

from comfyui_h3_context.core.errors import OptionalDependencyError

try:
    import comfy_api as _comfy_api
except ImportError as exc:
    raise OptionalDependencyError(
        "The ComfyUI adapter requires the host-provided 'comfy_api' module; "
        "run this adapter inside a supported ComfyUI installation after M0-07 host setup."
    ) from exc


HOST_API = _comfy_api

__all__ = ["HOST_API"]
