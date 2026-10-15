"""Host-module access for the harness-only sequence continuation canaries.

The canaries need ``torch``, ``comfy.nested_tensor``, ``node_helpers`` and the H3 model module.
Each is imported here, lazily, through :func:`importlib.import_module` inside a small accessor, so
that the canary module itself imports nothing host-side and the security audit can confine dynamic
imports to this one reviewed seam. Nothing here runs at package import, and no accessor accepts a
module name from its caller.
"""

from __future__ import annotations

import importlib
from typing import Any


def torch_module() -> Any:
    """The host's ``torch`` module."""

    return importlib.import_module("torch")


def nested_tensor(tensors: tuple[Any, Any]) -> Any:
    """A host ``NestedTensor`` over the video and audio domains, in that order."""

    return importlib.import_module("comfy.nested_tensor").NestedTensor(tensors)


def conditioning_set_values(conditioning: Any, values: dict[str, Any]) -> Any:
    """The host's own conditioning update, so no conditioning structure is rebuilt here."""

    return importlib.import_module("node_helpers").conditioning_set_values(conditioning, values)


def minimax_model_module() -> Any:
    """The host's H3 model module, read only by the layout probe."""

    return importlib.import_module("comfy.ldm.minimax.model")
