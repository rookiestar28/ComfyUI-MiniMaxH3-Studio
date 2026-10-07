"""The sidebar's style bound, checked against the stylesheet that actually ships.

`createMountController` refuses to mount when the sidebar stylesheet exceeds its bound, and that
refusal is a `throw` during module evaluation -- it does not degrade the sidebar, it destroys the
extension before `registerExtension` is reached. On 2026-08-18 the shipped stylesheet crossed the
bound by 43 characters and every build after it shipped a sidebar that could not mount on any real
host. Nothing caught it: `frontend/e2e/main.tsx` renders with `createRoot` directly and never
constructs a `MountController`, `frontend/tests/mountController.test.tsx` never passes `styles` so
it only ever measures the 193-character default, and the build succeeds either way.

The bound's only real input is the stylesheet the bundle ships, so this pairs the two, and reads
both out of the artifacts that ship rather than out of the sources they are built from. Raising the
bound without rebuilding fails here; growing the stylesheet past the bound fails here.

Everything in this module **fails closed**. A locator that quietly finds nothing would put back
exactly the blindness this test exists to remove, so "I could not find it" is a failure, never a
skip and never a pass.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "frontend" / "src" / "lifecycle" / "mountController.tsx"
BUNDLE = ROOT / "comfyui_h3_context" / "web" / "h3-context-sidebar.js"

#: Present in the sidebar stylesheet and in nothing else the bundle contains.
STYLE_MARKER = "[data-h3-context-mount]"

#: `export const MAX_SHELL_STYLE_LENGTH = 65_536;`
BOUND_PATTERN = re.compile(
    r"export\s+const\s+MAX_SHELL_STYLE_LENGTH\s*=\s*([0-9_]+)\s*;",
)


def declared_bound(source: str) -> int:
    """The bound as `mountController.tsx` declares it, or an error naming what was not found."""

    match = BOUND_PATTERN.search(source)
    if match is None:
        raise AssertionError(
            "MAX_SHELL_STYLE_LENGTH is not declared in mountController.tsx; if it was renamed, "
            "update BOUND_PATTERN here rather than deleting this check"
        )
    return int(match.group(1).replace("_", ""))


def shipped_stylesheets(bundle: str) -> list[str]:
    """Every string literal in the bundle that carries the sidebar's mount marker.

    Two are expected: the small `defaultShellStyle` fallback and the compiled `tokens.css`. The
    literal is delimited by whichever quote most recently opened before the marker; CSS contains no
    backticks or `${`, so the enclosing literal ends at the next unescaped delimiter.
    """

    found: list[str] = []
    for match in re.finditer(re.escape(STYLE_MARKER), bundle):
        opening = max(bundle.rfind(quote, 0, match.start()) for quote in ("`", '"', "'"))
        if opening < 0:
            continue
        delimiter = bundle[opening]
        cursor = opening + 1
        while cursor < len(bundle):
            if bundle[cursor] == "\\":
                cursor += 2
                continue
            if bundle[cursor] == delimiter:
                break
            cursor += 1
        else:  # pragma: no cover - an unterminated literal means the bundle is corrupt
            raise AssertionError("unterminated string literal around the sidebar stylesheet")
        literal = bundle[opening + 1 : cursor]
        # The marker occurs several times inside each sheet, so the same literal is reached from
        # several offsets. Deduplicate, or "at least two stylesheets" is satisfied by one of them.
        if STYLE_MARKER in literal and literal not in found:
            found.append(literal)
    return found


class SidebarStyleBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(CONTROLLER.is_file(), f"missing {CONTROLLER}")
        self.assertTrue(BUNDLE.is_file(), f"missing {BUNDLE}")
        self.controller = CONTROLLER.read_text(encoding="utf-8")
        self.bundle = BUNDLE.read_text(encoding="utf-8")

    def test_the_bound_is_declared_where_this_test_can_read_it(self) -> None:
        self.assertGreater(declared_bound(self.controller), 0)

    def test_the_stylesheet_is_locatable_in_the_shipped_bundle(self) -> None:
        """If this fails the bundle changed shape, and the budget check below proves nothing."""

        sheets = shipped_stylesheets(self.bundle)
        self.assertGreaterEqual(
            len(sheets),
            2,
            "expected the defaultShellStyle fallback and the compiled tokens.css in the bundle; "
            f"found {len(sheets)}",
        )

    def test_the_shipped_stylesheet_fits_the_bound_the_host_enforces(self) -> None:
        bound = declared_bound(self.controller)
        sheets = shipped_stylesheets(self.bundle)
        largest = max(len(sheet) for sheet in sheets)
        self.assertLessEqual(
            largest,
            bound,
            f"the shipped sidebar stylesheet is {largest} characters against a bound of {bound}; "
            "createMountController throws during module evaluation, so this ships an extension "
            "that cannot register its sidebar on any host",
        )

    def test_the_fallback_stylesheet_also_fits(self) -> None:
        """`defaultShellStyle` is used whenever no styles are supplied, and shares the bound."""

        bound = declared_bound(self.controller)
        for sheet in shipped_stylesheets(self.bundle):
            self.assertGreaterEqual(len(sheet), 1)
            self.assertLessEqual(len(sheet), bound)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
