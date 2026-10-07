"""Run the pinned real CLI pack command with workspace-owned configuration."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-root", type=Path, required=True)
    args = parser.parse_args(argv)
    config = args.config_root.resolve()
    workspace = Path(__file__).resolve().parents[1]
    if not config.is_relative_to(workspace) or not config.is_dir():
        raise ValueError("CLI configuration must be inside the tooling workspace")
    if importlib.metadata.version("comfy-cli") != "1.16.0":
        raise ValueError("CLI version does not match the publication lock")
    os.environ["COMFY_NO_TELEMETRY"] = "1"
    os.environ["DO_NOT_TRACK"] = "1"
    constants = importlib.import_module("comfy_cli.constants")
    # CRITICAL: CLI initialization creates legacy config/tmp even with XDG overrides. Redirect
    # its exact closed config map before importing the entry point; never write operator config.
    locations = vars(constants)["DEFAULT_CONFIG"]
    if not isinstance(locations, dict) or not locations:
        raise ValueError("pinned CLI configuration contract changed")
    vars(constants)["DEFAULT_CONFIG"] = dict.fromkeys(locations, str(config))
    (config / "config.ini").write_text("[DEFAULT]\nenable_tracking = False\n", encoding="utf-8")
    sys.argv = ["comfy", "node", "pack"]
    importlib.import_module("comfy_cli.__main__").main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
