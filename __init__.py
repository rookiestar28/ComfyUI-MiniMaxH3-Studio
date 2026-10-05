"""ComfyUI custom-node loader entrypoint.

ComfyUI executes this file when the repository is copied as one directory under ``custom_nodes``.
The implementation and contracts remain in the nested ``comfyui_h3_context`` package; this shim
only exposes the V1 mappings expected by the host loader.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ComfyUI executes a directory's ``__init__.py`` under a synthetic module name derived from the
# absolute path. That name is not a valid Python package anchor for relative imports, so make this
# repository root importable before resolving the canonical nested package.
_PACKAGE_ROOT = str(Path(__file__).resolve().parent)
if _PACKAGE_ROOT not in sys.path:
    sys.path.insert(0, _PACKAGE_ROOT)

from comfyui_h3_context import (  # noqa: E402
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
)
from comfyui_h3_context import (  # noqa: E402
    WEB_DIRECTORY as _PACKAGE_WEB_DIRECTORY,
)

WEB_DIRECTORY = f"./comfyui_h3_context/{_PACKAGE_WEB_DIRECTORY}"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
