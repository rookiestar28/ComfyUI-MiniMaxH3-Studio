"""M19-07 pre-M20 temporal and model capability qualification harness.

This package is qualification-only. It is deliberately not part of the importable
`comfyui_h3_context` product package, because it is not a product runtime authority and not a
portable workflow contract, and because keeping it here leaves the product diff untouched while the
concurrent `M17-20` chain edits `core/generation_profile.py`, `core/product_shell.py` and
`core/native_h3.py`.

Nothing here may become a product import. `M20-01` and `M20-04` own the constants this harness
measures, and only if their qualification rows come back `supported`.
"""

from __future__ import annotations

QUALIFICATION_ITEM = "M19-07"
EVIDENCE_SCHEMA = "h3.m19_07_qualification_evidence.v1"
EVIDENCE_VERSION = "1.0.0"

__all__ = ["EVIDENCE_SCHEMA", "EVIDENCE_VERSION", "QUALIFICATION_ITEM"]
