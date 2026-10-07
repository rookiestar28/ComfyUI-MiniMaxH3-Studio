"""Run the exact M15-03 backend and real-browser supported-host contract."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.m3_08_host_e2e import main as host_main  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--mode" in raw or "--browser-e2e" in raw:
        raise SystemExit("M15-03 owns the exact product-shell modes and browser lane")
    return host_main(
        [
            *raw,
            "--mode",
            "product_shell_base",
            "--mode",
            "product_shell_reference",
            "--browser-e2e",
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
