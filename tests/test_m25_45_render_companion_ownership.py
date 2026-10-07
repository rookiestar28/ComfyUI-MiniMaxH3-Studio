"""B-M2545-44: the render companion measures its own children, all of them, and nobody else's.

Read this first, because an earlier version of this file said otherwise. These cases pin the
ownership rule's BEHAVIOUR. They are **not** evidence about the run that prompted the rule --
`g10-hardening-acceptance`, which failed a 1 GiB child ceiling at 2,661,179,392 bytes. That failure
was never reproduced and its cause is not established; the register entry is open.

Two hazards are pinned here, and the evidence behind them is very different.

*Over-counting a stranger* is a hazard in principle. Windows records `th32ParentProcessID` at
creation and never clears it while reissuing PIDs aggressively, so a parent-pointer walk can climb
from a stranger, through a dead ancestor whose PID was handed to one of our own children, and land
on us -- and the counter this feeds, `PeakWorkingSetSize`, is a LIFETIME high-water mark, so one bad
sample would import a stranger's whole peak. Four instrumented reproductions over 11,230 samples
observed this **zero** times. The guard is kept because the hazard is real, not because it was seen.

*Under-counting our own* was observed, repeatedly, and was caused by the first attempt at that
guard. `ffprobe.exe` and its `conhost.exe` exit within milliseconds, so their creation time is gone
before it can be read; requiring a readable timestamp dropped 40 real children across those same
four runs, silently, in the direction that hides a breach.

The cases run the rule as a pure function over a synthetic process table, which is what makes them
fast and deterministic -- and also what makes them unable to say anything about a real run.
"""

from __future__ import annotations

import importlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


def companion() -> ModuleType:
    return importlib.import_module("scripts.nle_stress_render_companion")


class RenderCompanionOwnershipTests(unittest.TestCase):
    OWNER = 1000

    @staticmethod
    def _table(
        processes: dict[int, tuple[int, int]],
    ) -> tuple[dict[int, int], object]:
        """Split `{pid: (parent, created)}` into the two arguments the rule takes."""

        parents = {pid: link[0] for pid, link in processes.items()}
        return parents, lambda pid: (processes.get(pid) or (0, None))[1]

    @staticmethod
    def _owned(result: tuple[frozenset[int], frozenset[int]]) -> frozenset[int]:
        return result[0]

    def test_real_descendants_are_owned_to_the_depth_limit(self) -> None:
        resolve = companion().resolve_owned_descendants
        processes = {
            4: (0, 0),
            self.OWNER: (4, 10),
            2001: (self.OWNER, 20),  # ffmpeg, spawned by us
            2002: (2001, 30),  # its own helper, one level deeper
            3001: (4, 20),  # a sibling of ours, never owned
        }
        parents, created = self._table(processes)
        self.assertEqual(
            self._owned(resolve(parents, self.OWNER, created)), frozenset({2001, 2002})
        )

    def test_a_recycled_ancestor_pid_does_not_graft_a_foreign_tree(self) -> None:
        """The regression proper: without the creation-time rule this returns the stranger."""

        resolve = companion().resolve_owned_descendants
        # 2001 is ours, created at t=20. 9001 is a stranger created LONG BEFORE, at t=5, whose
        # recorded parent is 2001 only because the PID 2001 previously belonged to a process that
        # has since exited. A parent cannot be younger than its child, so the link is not real.
        processes = {
            self.OWNER: (4, 10),
            2001: (self.OWNER, 20),
            9001: (2001, 5),
            9002: (9001, 6),
        }
        parents, created = self._table(processes)
        owned = self._owned(resolve(parents, self.OWNER, created))
        self.assertEqual(owned, frozenset({2001}))
        self.assertNotIn(9001, owned)
        self.assertNotIn(9002, owned)

    def test_the_bare_parent_walk_this_replaced_would_have_accepted_it(self) -> None:
        """The proof that the case above is not vacuous: the previous rule fails it.

        Kept as an executable statement of the defect rather than a comment, so that a future
        simplification back to a parent-only walk is refused by a test that already knows why.
        """

        processes = {
            self.OWNER: (4, 10),
            2001: (self.OWNER, 20),
            9001: (2001, 5),
            9002: (9001, 6),
        }

        def bare_parent_walk(owner: int) -> frozenset[int]:
            owned: set[int] = set()
            for pid, (parent, _created) in processes.items():
                walker, depth = parent, 0
                while walker and depth < 8:
                    if walker == owner:
                        owned.add(pid)
                        break
                    walker = processes.get(walker, (0, 0))[0]
                    depth += 1
            return frozenset(owned)

        parents, created = self._table(processes)
        self.assertEqual(bare_parent_walk(self.OWNER), frozenset({2001, 9001, 9002}))
        self.assertNotEqual(
            bare_parent_walk(self.OWNER),
            self._owned(companion().resolve_owned_descendants(parents, self.OWNER, created)),
        )

    def test_a_cycle_and_an_unknown_parent_terminate(self) -> None:
        resolve = companion().resolve_owned_descendants
        processes = {
            self.OWNER: (4, 10),
            5001: (5002, 20),
            5002: (5001, 20),  # a cycle, which only equal timestamps can express
            6001: (7777, 20),  # a parent that is not in the table at all
        }
        parents, created = self._table(processes)
        self.assertEqual(self._owned(resolve(parents, self.OWNER, created)), frozenset())

    def test_the_owner_never_counts_itself(self) -> None:
        resolve = companion().resolve_owned_descendants
        parents, created = self._table({self.OWNER: (self.OWNER, 10)})
        self.assertEqual(self._owned(resolve(parents, self.OWNER, created)), frozenset())

    def test_a_chain_longer_than_the_depth_limit_stops(self) -> None:
        resolve = companion().resolve_owned_descendants
        processes: dict[int, tuple[int, int]] = {self.OWNER: (4, 0)}
        previous = self.OWNER
        for step in range(1, 12):
            processes[8000 + step] = (previous, step)
            previous = 8000 + step
        parents, created = self._table(processes)
        owned = self._owned(resolve(parents, self.OWNER, created))
        self.assertIn(8001, owned)
        self.assertIn(8008, owned)
        self.assertNotIn(8011, owned)

    def test_a_child_whose_creation_time_cannot_be_read_is_kept_and_flagged(self) -> None:
        """The one thing the instrumented reproduction actually proved, pinned.

        `ffprobe.exe` and its `conhost.exe` exit within milliseconds of being spawned, so by the
        time the sampler asks for a creation time `OpenProcess` answers ERROR_INVALID_PARAMETER.
        An earlier version of this rule required a readable timestamp and skipped the process --
        40 such records across four instrumented runs, every one of them a real child of the
        companion. Under-counting is the direction that hides a breach, and doing it silently is
        worse, so the process is kept and named in `unverified`.
        """

        resolve = companion().resolve_owned_descendants
        parents = {self.OWNER: 4, 2001: self.OWNER, 2002: 2001}
        times: dict[int, int | None] = {self.OWNER: 10, 2001: None, 2002: None}
        owned, unverified = resolve(parents, self.OWNER, times.get)
        self.assertIn(2001, owned)
        self.assertIn(2001, unverified)
        # And the descent CONTINUES past it. `ffprobe.exe` spawns a `conhost.exe`, so stopping at
        # the unreadable process drops a real grandchild: an earlier form of this repair kept the
        # `ffprobe` and still excluded three `conhost` processes per instrumented run.
        self.assertIn(2002, owned)
        self.assertIn(2002, unverified)

    def test_an_unreadable_child_is_still_refused_when_the_parent_link_is_contradicted(
        self,
    ) -> None:
        """Keeping unreadable children must not become a way back in for a recycled PID."""

        resolve = companion().resolve_owned_descendants
        parents = {self.OWNER: 4, 2001: self.OWNER, 9001: 2001}
        times: dict[int, int | None] = {self.OWNER: 10, 2001: 20, 9001: 5}
        owned, unverified = resolve(parents, self.OWNER, times.get)
        self.assertEqual(owned, frozenset({2001}))
        self.assertEqual(unverified, frozenset())


class RenderCompanionSamplerValidityTests(unittest.TestCase):
    """B-M2545-50: instrumentation failure is invalid evidence, never an admissible zero."""

    def test_process_snapshot_failure_is_not_reported_as_an_empty_tree(self) -> None:
        module = companion()

        class Kernel:
            @staticmethod
            def CreateToolhelp32Snapshot(_kind: int, _pid: int) -> int:
                return int(module._INVALID_HANDLE)

        reader = module._WindowsMemory.__new__(module._WindowsMemory)
        reader._kernel = Kernel()
        with self.assertRaisesRegex(module.CompanionError, "process_snapshot_unavailable"):
            reader._snapshot()

    def test_unreadable_owner_identity_invalidates_the_sample(self) -> None:
        module = companion()
        parents = {1000: 4, 2001: 1000, 2002: 2001}
        with self.assertRaisesRegex(module.CompanionError, "owner_identity_unavailable"):
            module.resolve_owned_descendants(parents, 1000, lambda _pid: None)

    def test_background_reader_failure_reaches_sampler_stop(self) -> None:
        module = companion()
        failed = threading.Event()

        class Reader:
            @staticmethod
            def own() -> int:
                return 4096

            @staticmethod
            def children() -> tuple[int, int, str, int]:
                failed.set()
                raise module.CompanionError("process_snapshot_unavailable")

        sampler = module._Sampler(Reader())
        with patch.object(module, "SAMPLE_INTERVAL_S", 0.001):
            sampler.start(4096)
            self.assertTrue(failed.wait(1.0), "sampler did not execute")
            with self.assertRaisesRegex(module.CompanionError, "process_snapshot_unavailable"):
                sampler.stop()

    def test_completed_observed_and_unobserved_processes_remain_distinct(self) -> None:
        module = companion()
        process_module = importlib.import_module(
            "comfyui_h3_context.adapters.authoring_render_process"
        )
        observer = module._ProcessBoundaryObserver()
        common = {
            "session_id": "session-a",
            "executable_fingerprint": "sha256:" + "1" * 64,
            "job_membership_verified": True,
            "peak_committed_bytes": 8192,
            "limits_verified": True,
        }
        observer.observe(
            process_module.RenderProcessObservation(
                **common,
                run_id="run-observed",
                process_id=2001,
                working_set_valid=True,
                working_set_failure=None,
                working_set_sample_count=3,
                peak_working_set_bytes=4096,
            )
        )
        observer.observe(
            process_module.RenderProcessObservation(
                **common,
                run_id="run-unobserved",
                process_id=2002,
                working_set_valid=False,
                working_set_failure="working_set_unavailable",
                working_set_sample_count=0,
                peak_working_set_bytes=0,
            )
        )
        document = observer.document()
        self.assertFalse(document["sampling_valid"])
        self.assertEqual(document["completed_processes"], 2)
        self.assertEqual(document["measured_processes"], 1)
        self.assertEqual(document["unobserved_processes"], 1)
        self.assertEqual(document["sampling_failure_reasons"], ["working_set_unavailable"])

    def test_sampler_failure_writes_partial_document_with_prior_process_rows(self) -> None:
        module = companion()
        process_module = importlib.import_module(
            "comfyui_h3_context.adapters.authoring_render_process"
        )
        jobs_module = importlib.import_module("comfyui_h3_context.core.authoring_render_jobs")
        observer = module._ProcessBoundaryObserver()
        observer.observe(
            process_module.RenderProcessObservation(
                session_id="session-a",
                run_id="run-before-failure",
                process_id=2001,
                executable_fingerprint="sha256:" + "1" * 64,
                job_membership_verified=True,
                working_set_valid=True,
                working_set_failure=None,
                working_set_sample_count=3,
                peak_working_set_bytes=4096,
                peak_committed_bytes=8192,
                limits_verified=True,
            )
        )

        class Reader:
            pass

        sampler = module._Sampler(Reader())
        sampler.baseline = 2048
        sampler.own_peak = 4096
        sampler.tree_peak = 8192
        sampler.samples = 2
        sampler.failure_reasons.append("process_snapshot_unavailable")
        failure = module.CompanionError("process_snapshot_unavailable")
        document = module._companion_document(
            started=0.0,
            build_ms=1,
            jobs=[],
            maxima={"running": 0, "queued": 0},
            sampler=sampler,
            process_observer=observer,
            limits=jobs_module.RenderJobLimits(),
            snapshot=None,
            wire=None,
            failure=failure,
        )
        self.assertEqual(document["status"], "FAIL")
        self.assertFalse(document["backend_rss"]["sampling_valid"])
        self.assertEqual(
            document["backend_rss"]["sampling_failure_reasons"],
            ["process_snapshot_unavailable"],
        )
        self.assertEqual(document["child_process_tree"]["completed_processes"], 1)
        self.assertEqual(
            document["child_process_tree"]["processes"][0]["run_id"],
            "run-before-failure",
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ffmpeg = root / "ffmpeg.exe"
            ffprobe = root / "ffprobe.exe"
            output = root / "failure.json"
            ffmpeg.touch()
            ffprobe.touch()
            with patch.object(
                module,
                "run",
                side_effect=module.CompanionRunFailure("process_snapshot_unavailable", document),
            ):
                exit_code = module.main(
                    [
                        "--ffmpeg",
                        str(ffmpeg),
                        "--ffprobe",
                        str(ffprobe),
                        "--scratch-root",
                        str(root / "scratch"),
                        "--out",
                        str(output),
                    ]
                )
            self.assertEqual(exit_code, 2)
            retained = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(retained["failure"]["reason"], "process_snapshot_unavailable")
            self.assertEqual(
                retained["child_process_tree"]["processes"][0]["process_id"],
                2001,
            )


if __name__ == "__main__":
    unittest.main()
