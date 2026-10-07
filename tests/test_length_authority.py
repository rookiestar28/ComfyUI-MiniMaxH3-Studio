"""M17-25 focused tests for `core.length`, the single H3 alignment authority.

These tests own the arithmetic contract: the lattice, the accepted and trained
ranges, the round-to-nearest seconds-to-frames convention adopted from the
official ComfyUI templates, the unreachability of the rounding tie, the
requested/delivered/snapped triple, and the migration of a legacy authored frame
count. Anything that needs a frame count derives it from here; nothing else in
the repository is allowed a second copy, which `SingleAuthorityTests` enforces by
scanning the source rather than by convention.
"""

from __future__ import annotations

import re
import unittest
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from official_temporal_parity import official_expression

from comfyui_h3_context.core.length import (
    DEFAULT_FRAME_COUNT,
    FPS,
    LATTICE_OFFSET,
    LATTICE_STEP,
    MAX_ACCEPTED_MILLISECONDS,
    MAX_FRAME_COUNT,
    MAX_PRODUCIBLE_FRAME_COUNT,
    MIN_ACCEPTED_MILLISECONDS,
    MIN_FRAME_COUNT,
    TRAINED_MAX_FRAME_COUNT,
    TRAINED_MIN_FRAME_COUNT,
    LengthError,
    align_frame_count,
    frames_from_milliseconds,
    is_producible,
    milliseconds_from_frames,
    official_template_frames,
    resolve_frames,
    resolve_milliseconds,
)

ROOT = Path(__file__).resolve().parents[1]


class LatticeTests(unittest.TestCase):
    def test_producibility_is_the_lattice_inside_the_accepted_range(self) -> None:
        producible = [n for n in range(0, MAX_FRAME_COUNT + 200) if is_producible(n)]
        self.assertEqual(len(producible), 212)
        self.assertEqual(producible[0], MIN_FRAME_COUNT)
        self.assertEqual(producible[-1], MAX_PRODUCIBLE_FRAME_COUNT)
        self.assertEqual(MAX_PRODUCIBLE_FRAME_COUNT, 3592)
        for value in producible:
            self.assertEqual((value - LATTICE_OFFSET) % LATTICE_STEP, 0)
        self.assertFalse(is_producible(True))
        # 3600 is the accepted maximum and is not itself producible, which is why
        # a request that aligns above 3592 fails instead of being clamped down.
        self.assertFalse(is_producible(MAX_FRAME_COUNT))

    def test_alignment_only_ever_moves_upward_and_is_idempotent(self) -> None:
        for value in range(-5, MAX_FRAME_COUNT + 1):
            aligned = align_frame_count(value)
            floor = max(value, MIN_FRAME_COUNT)
            self.assertGreaterEqual(aligned, floor)
            self.assertLess(aligned - floor, LATTICE_STEP)
            self.assertEqual((aligned - LATTICE_OFFSET) % LATTICE_STEP, 0)
            self.assertEqual(align_frame_count(aligned), aligned)

    def test_declared_ranges_are_the_documented_ones(self) -> None:
        self.assertEqual((FPS, MIN_FRAME_COUNT, MAX_FRAME_COUNT), (24, 5, 3600))
        self.assertEqual((TRAINED_MIN_FRAME_COUNT, TRAINED_MAX_FRAME_COUNT), (124, 362))
        self.assertEqual(DEFAULT_FRAME_COUNT, 124)
        self.assertTrue(is_producible(DEFAULT_FRAME_COUNT))
        self.assertTrue(resolve_frames(DEFAULT_FRAME_COUNT).within_trained_range)
        self.assertFalse(resolve_frames(MAX_PRODUCIBLE_FRAME_COUNT).within_trained_range)


class RoundingConventionTests(unittest.TestCase):
    def test_matches_the_official_template_over_a_contiguous_range(self) -> None:
        """AC-M17-25-02. Parity over every integer millisecond the range admits."""

        divergences = []
        for milliseconds in range(188, 30_001):
            expected = official_expression(milliseconds / 1000)
            self.assertEqual(official_template_frames(milliseconds), expected)
            if frames_from_milliseconds(milliseconds) != expected:
                divergences.append(milliseconds)
        self.assertEqual(divergences, [])

    def test_the_one_deliberate_divergence_is_the_sub_minimum_clamp(self) -> None:
        """Below the accepted minimum the official expression clamps to five
        frames. This repository fails closed instead: delivering a length nobody
        asked for is a silent substitution, and the whole contract here is that
        movement gets reported."""

        for milliseconds in range(1, 188):
            self.assertEqual(official_expression(milliseconds / 1000), MIN_FRAME_COUNT)
            with self.assertRaises(LengthError) as caught:
                frames_from_milliseconds(milliseconds)
            self.assertEqual(caught.exception.code, "duration_below_minimum")
        self.assertEqual(frames_from_milliseconds(188), MIN_FRAME_COUNT)

    def test_the_ceiling_convention_this_item_replaced_disagrees_and_is_longer(self) -> None:
        """The pre-M17-25 convention rounded seconds up. Recomputed here rather
        than quoted from the plan, so the headline number is evidence."""

        divergences = 0
        for milliseconds in range(188, 30_001):
            ceiling = align_frame_count(
                max(
                    MIN_FRAME_COUNT,
                    int(
                        (Decimal(milliseconds) * FPS / 1000).to_integral_value(
                            rounding=ROUND_CEILING
                        )
                    ),
                )
            )
            official = official_expression(milliseconds / 1000)
            if ceiling != official:
                divergences += 1
                self.assertGreater(ceiling, official)
        self.assertEqual(divergences, 889)

    def test_an_exact_rounding_tie_is_unreachable_on_integer_milliseconds(self) -> None:
        """Scope item 3. A tie needs `6 * ms` (even) to equal `250x + 125` (odd),
        so no integer millisecond can produce one. That is what makes the choice
        of rounding mode unobservable, and therefore what makes adopting
        ROUND_HALF_EVEN safe rather than merely convenient."""

        half = Decimal("0.5")
        for milliseconds in range(1, 300_001):
            if (Decimal(milliseconds) * FPS / 1000) % 1 == half:
                self.fail(f"reachable rounding tie at {milliseconds} ms")


class ResolutionTests(unittest.TestCase):
    def test_requested_delivered_and_direction_are_reported_together(self) -> None:
        rows = {
            1000: (39, 1625, True, "longer"),
            2000: (56, 2333, True, "longer"),
            4460: (107, 4458, True, "shorter"),
            5000: (124, 5167, True, "longer"),
            5167: (124, 5167, False, "exact"),
            7500: (192, 8000, True, "longer"),
            8000: (192, 8000, False, "exact"),
            12500: (311, 12958, True, "longer"),
        }
        for milliseconds, expected in rows.items():
            with self.subTest(milliseconds=milliseconds):
                resolved = resolve_milliseconds(milliseconds)
                self.assertEqual(
                    (
                        resolved.frame_count,
                        resolved.delivered_milliseconds,
                        resolved.snapped,
                        resolved.direction,
                    ),
                    expected,
                )
                self.assertEqual(resolved.requested_milliseconds, milliseconds)

    def test_a_delivered_duration_shorter_than_the_request_is_reachable(self) -> None:
        """The reason the movement diagnostic cannot be called `rounded_up`. At
        4.46 s the raw and aligned lengths are both 107 frames, so a diagnostic
        comparing aligned against raw stays silent while the caller receives
        less than was asked for."""

        resolved = resolve_milliseconds(4460)
        self.assertEqual(resolved.direction, "shorter")
        self.assertTrue(resolved.snapped)
        self.assertEqual(official_template_frames(4460), resolved.frame_count)

    def test_movement_is_judged_at_wire_resolution_not_on_the_exact_rational(self) -> None:
        """124 frames is 5166.67 ms. Comparing rationals would call an ordinary
        5167 ms request snapped by a third of a millisecond, a difference no wire
        value can carry and no reader can act on."""

        resolved = resolve_milliseconds(5167)
        self.assertFalse(resolved.snapped)
        self.assertEqual(resolved.direction, "exact")

    def test_out_of_range_durations_fail_closed_with_distinct_codes(self) -> None:
        with self.assertRaises(LengthError) as low:
            resolve_milliseconds(100)
        self.assertEqual(low.exception.code, "duration_below_minimum")
        with self.assertRaises(LengthError) as high:
            resolve_milliseconds(200_000)
        self.assertEqual(high.exception.code, "duration_above_maximum")
        with self.assertRaises(LengthError) as aligned_over:
            resolve_milliseconds(milliseconds_from_frames(MAX_FRAME_COUNT))
        self.assertEqual(aligned_over.exception.code, "duration_above_maximum")
        for bad in (0, -1, True, 1.5, "5"):
            with self.subTest(bad=bad):
                with self.assertRaises(LengthError):
                    resolve_milliseconds(bad)  # type: ignore[arg-type]


class MigrationTests(unittest.TestCase):
    def test_the_lattice_round_trip_is_lossless(self) -> None:
        """AC-M17-25-07, first half."""

        failures = [
            n
            for n in range(MIN_FRAME_COUNT, MAX_FRAME_COUNT + 1)
            if is_producible(n) and frames_from_milliseconds(milliseconds_from_frames(n)) != n
        ]
        self.assertEqual(failures, [])

    def test_off_lattice_migration_equals_alignment_and_is_marked_snapped(self) -> None:
        """AC-M17-25-07, second half. A legacy non-producible value lands on
        exactly the length alignment would have given it, so the migration never
        introduces a third answer, and it is never silent.

        The domain is `MIN_FRAME_COUNT .. MAX_PRODUCIBLE_FRAME_COUNT`, and that is
        the correction M17-25's distinct review forced. This test used to sweep to
        `MAX_FRAME_COUNT` and carry an `if aligned > MAX_FRAME_COUNT: assertRaises;
        continue` escape hatch, which let its own name promise "equals alignment"
        while it asserted a raise for eight of the values it covered. The eight are
        now a separate, explicit test below, so neither claim hides the other.
        """

        mismatches = []
        for n in range(MIN_FRAME_COUNT, MAX_PRODUCIBLE_FRAME_COUNT + 1):
            resolved = resolve_frames(n)
            if resolved.frame_count != align_frame_count(n):
                mismatches.append(n)
            self.assertEqual(
                resolved.snapped,
                resolved.delivered_milliseconds != resolved.requested_milliseconds,
            )
            if not is_producible(n):
                self.assertTrue(resolved.snapped)
        self.assertEqual(mismatches, [])

    def test_frame_counts_inside_the_host_range_but_above_the_lattice_fail_closed(self) -> None:
        """The gap between the host's stated ceiling and the longest producible
        video is real and was mis-stated before this review. Every count from
        `MAX_PRODUCIBLE_FRAME_COUNT + 1` to `MAX_FRAME_COUNT` passes a naive
        `<= MAX_FRAME_COUNT` guard and aligns to 3609, above the ceiling, so none
        of them can be delivered. They are refused by name rather than reported as
        a duration that came out too long."""

        dead_zone = list(range(MAX_PRODUCIBLE_FRAME_COUNT + 1, MAX_FRAME_COUNT + 1))
        self.assertEqual(dead_zone, list(range(3593, 3601)))
        for n in dead_zone:
            with self.subTest(frame_count=n):
                self.assertGreater(align_frame_count(n), MAX_FRAME_COUNT)
                with self.assertRaises(LengthError) as caught:
                    resolve_frames(n)
                self.assertEqual(caught.exception.code, "frame_count_out_of_bounds")

    def test_the_published_duration_domain_is_exactly_what_resolves(self) -> None:
        """Surfaces bound their controls with these two constants, so a literal
        that drifted from the arithmetic would offer durations that always fail.
        They are recomputed here by sweep rather than restated."""

        accepted = [ms for ms in range(1, MAX_ACCEPTED_MILLISECONDS + 400) if _resolves(ms)]
        self.assertEqual(
            (min(accepted), max(accepted)),
            (MIN_ACCEPTED_MILLISECONDS, MAX_ACCEPTED_MILLISECONDS),
        )
        # Contiguous: every millisecond between the two bounds resolves, so the
        # domain is an interval a control can express.
        self.assertEqual(len(accepted), MAX_ACCEPTED_MILLISECONDS - MIN_ACCEPTED_MILLISECONDS + 1)
        self.assertEqual(resolve_milliseconds(MIN_ACCEPTED_MILLISECONDS).frame_count, 5)
        self.assertEqual(
            resolve_milliseconds(MAX_ACCEPTED_MILLISECONDS).frame_count,
            MAX_PRODUCIBLE_FRAME_COUNT,
        )

    def test_the_repository_fixture_values_migrate_as_recorded(self) -> None:
        self.assertEqual(
            [
                (n, resolve_frames(n).requested_milliseconds, resolve_frames(n).frame_count)
                for n in (100, 300, 180, 120)
            ],
            [(100, 4167, 107), (300, 12500, 311), (180, 7500, 192), (120, 5000, 124)],
        )
        exact = resolve_frames(124)
        self.assertEqual(
            (exact.requested_milliseconds, exact.frame_count, exact.snapped),
            (5167, 124, False),
        )

    def test_migration_fails_closed_outside_the_accepted_range(self) -> None:
        # `MAX_PRODUCIBLE_FRAME_COUNT + 1` is the first count the migration cannot
        # deliver; `MAX_FRAME_COUNT + 1` is past the host ceiling as well. Both
        # fail, and the reason is the same one: there is no producible length to
        # migrate to. Every member is an `int` -- `True` included, which is the
        # point of listing it -- so no type-ignore is needed here.
        for bad in (0, 4, MAX_PRODUCIBLE_FRAME_COUNT + 1, MAX_FRAME_COUNT + 1, True, -10):
            with self.subTest(bad=bad):
                with self.assertRaises(LengthError):
                    resolve_frames(bad)


def _resolves(milliseconds: int) -> bool:
    """Return whether an authored duration resolves at all, without saying how."""

    try:
        resolve_milliseconds(milliseconds)
    except LengthError:
        return False
    return True


class SingleAuthorityTests(unittest.TestCase):
    """AC-M17-25-01. A fitness check, not a style rule: two authorities for one
    contract is the defect this item exists to remove, and the only thing that
    keeps it removed is a test that fails when a second one appears."""

    LATTICE = re.compile(r"%\s*17|17\s*\*|/\s*17\b")
    RANGES = re.compile(
        r"^\s*(FPS|MIN_FRAME_COUNT|MAX_FRAME_COUNT|DEFAULT_FRAME_COUNT"
        r"|TRAINED_MIN_FRAME_COUNT|TRAINED_MAX_FRAME_COUNT|LATTICE_OFFSET|LATTICE_STEP)\s*=",
        re.MULTILINE,
    )

    def _sources(self) -> list[Path]:
        return [
            path
            for path in (ROOT / "comfyui_h3_context").rglob("*.py")
            if "__pycache__" not in path.parts and path.name != "length.py"
        ]

    def test_only_length_py_implements_the_lattice(self) -> None:
        offenders = []
        for path in self._sources():
            body = "\n".join(
                line
                for line in path.read_text(encoding="utf-8").splitlines()
                if not line.lstrip().startswith("#")
            )
            if self.LATTICE.search(body):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])

    def test_only_length_py_defines_the_accepted_and_trained_ranges(self) -> None:
        offenders = [
            str(path.relative_to(ROOT))
            for path in self._sources()
            if self.RANGES.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])

    def test_no_shipped_artifact_authors_a_frame_count(self) -> None:
        """AC-M17-25-03. The derived frame count stays; the ability to author one
        does not. Checked on the shipped artifacts, which is where the previous
        contract non-producible values actually lived."""

        for folder in ("workflows", "subgraphs"):
            for path in sorted((ROOT / folder).glob("*.json")):
                with self.subTest(path=path.name):
                    self.assertNotIn('"frame_count"', path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
