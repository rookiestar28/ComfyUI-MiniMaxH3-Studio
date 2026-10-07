"""Every module under `scripts/` can still be imported.

B-M2545-21: `scripts/nle_stress_render_companion.py` imported a name the render stage had stopped
exporting, so the module raised `ImportError` before its first statement ran. Nothing noticed,
because nothing imports it except the stress lane, and the stress lane runs late -- after a
measurement run has been set up. A consumer of a removed name is invisible to every test that does
not import it, which is a poor thing to rely on when the consumer is a lane's entry point.

This is the cheapest complete guard: import all of them. It catches what `mypy` cannot see as well
as what it can -- a circular import, a module-level file read whose fixture moved, a package that
stopped being installed.

The import runs in a subprocess. `scripts/m25_16_service_loopback.py` exists to install a
`sys.audit` hook, and an audit hook cannot be removed once installed; importing the whole tree
inside the shared pytest process would be one refactor away from installing one there for every
later test.
"""

from __future__ import annotations

import json
import subprocess  # noqa: S404 -- this interpreter, fixed argv, no shell
import sys
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]
SCRIPTS: Final = ROOT / "scripts"

#: Modules that refuse to import somewhere other than where they are meant to run.
#:
#: GUARD: this is an exemption list, not a quarantine. An entry needs a reason that is a deliberate
#: contract of the module, and a module that merely happens to be broken does not belong here --
#: that is the failure this suite exists to report.
REFUSES_TO_IMPORT: Final[dict[str, str]] = {
    # Asserts the supplied ComfyUI host root at import (`comfy_api/latest` under the working
    # directory) because it is only ever run from inside one, and refusing early is what keeps it
    # from being run against this repository by mistake.
    "m17_10_supported_host_smoke": "supplied_host_root_missing",
}

PROBE: Final = """
import importlib, json, sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
failures = {}
names = sorted(p.stem for p in (Path(sys.argv[1]) / "scripts").glob("*.py") if p.stem != "__init__")
for name in names:
    try:
        importlib.import_module("scripts." + name)
    except BaseException as exc:  # a module may raise anything at import; all of it is a failure
        failures[name] = f"{type(exc).__name__}: {exc}"[:200]
print(json.dumps({"probed": names, "failures": failures}))
"""


def test_every_script_module_imports() -> None:
    completed = subprocess.run(  # noqa: S603 -- this interpreter, fixed argv, no shell
        [sys.executable, "-c", PROBE, str(ROOT)],
        check=True,
        capture_output=True,
        text=True,
        timeout=1800,
        cwd=ROOT,
    )
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    probed = result["probed"]
    failures = dict(result["failures"])

    # The tree is large enough that an empty or tiny probe would pass vacuously.
    assert len(probed) >= 100, probed
    assert {item.stem for item in SCRIPTS.glob("*.py")} - {"__init__"} == set(probed)

    for name, reason in REFUSES_TO_IMPORT.items():
        assert name in probed, f"{name} is exempted but no longer exists"
        assert name in failures, f"{name} is exempted but imports cleanly; drop the exemption"
        assert reason in failures.pop(name), (name, failures.get(name))

    assert failures == {}, failures
