"""Optional ComfyUI execution-context correlation seam.

The pure core never imports host internals.  The terminal Preview node may use this narrow public
execution-context helper when it is actually running inside the reviewed ComfyUI host; injected
tests can provide the same two identifiers explicitly.
"""

from __future__ import annotations

from comfyui_h3_context.core.errors import OptionalDependencyError

try:
    # IMPORTANT: availability differs between the project venv and host/pre-commit environments.
    from comfy_execution.utils import (  # type: ignore[import-not-found,unused-ignore]
        get_executing_context,
    )
except ImportError as exc:
    raise OptionalDependencyError(
        "The ComfyUI execution correlation seam requires the host-provided "
        "'comfy_execution.utils' module."
    ) from exc


def current_execution_correlation() -> tuple[str, str] | None:
    """Return the current host prompt/node IDs, or ``None`` outside node execution."""

    context = get_executing_context()
    if context is None:
        return None
    return context.prompt_id, context.node_id


__all__ = ["current_execution_correlation"]
