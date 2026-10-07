"""Focused tests for the uniform Registry publication identity/version guard."""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import yaml

from scripts.registry_publish_guard import (
    PublishGuardError,
    decide_publish,
    main,
    parse_project_version,
    read_previous_version_from_git,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "publish.yml"
LOCK = ROOT / "requirements" / "registry-publish-py310-linux-x86_64.txt"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
LOCK_ENTRY = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[A-Za-z0-9][A-Za-z0-9.!+_-]*) "
    r"--hash=sha256:(?P<digest>[0-9a-f]{64})$"
)
COMFY_CLI_WHEEL_SHA256 = bytes(
    (
        135,
        130,
        37,
        86,
        14,
        202,
        245,
        4,
        182,
        86,
        127,
        125,
        197,
        48,
        183,
        19,
        126,
        157,
        136,
        254,
        150,
        49,
        29,
        226,
        240,
        41,
        72,
        86,
        252,
        238,
        135,
        61,
    )
).hex()
EXCEPTIONGROUP_WHEEL_SHA256 = bytes(
    (
        167,
        163,
        154,
        59,
        210,
        118,
        120,
        30,
        152,
        57,
        73,
        135,
        211,
        165,
        112,
        29,
        12,
        78,
        223,
        251,
        99,
        59,
        183,
        165,
        20,
        69,
        119,
        248,
        44,
        119,
        53,
        152,
    )
).hex()


def _version(value: str) -> tuple[str, tuple[int, int, int]]:
    return parse_project_version(
        f'[project]\nname = "fixture"\nversion = "{value}"\n'.encode(),
        "fixture",
    )


def _manifest(value: str) -> str:
    return f'[project]\nname = "fixture"\nversion = "{value}"\n'


def _commit(root: Path, manifest: str | None, message: str) -> str:
    pyproject = root / "pyproject.toml"
    if manifest is None:
        pyproject.unlink(missing_ok=True)
    else:
        pyproject.write_text(manifest, encoding="utf-8")
    marker = root / "history.txt"
    marker.write_text(marker.read_text(encoding="utf-8") + message + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", message], cwd=root, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _release_fixture(
    root: Path,
    previous_manifest: str | None,
    current_version: str,
) -> tuple[str, str]:
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Registry Guard Test"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "registry-guard@example.invalid"],
        cwd=root,
        check=True,
    )
    (root / "history.txt").write_text("", encoding="utf-8")
    previous = _commit(root, previous_manifest, "previous")
    current = _commit(root, _manifest(current_version), "current")
    return previous, current


def _guard_args(
    *,
    event_name: str,
    previous_ref: str,
    current_ref: str,
    expected_candidate: str,
    output: Path,
    expected_version: str | None = None,
) -> list[str]:
    arguments = [
        "--event-name",
        event_name,
        "--previous-ref",
        previous_ref,
        "--current-ref",
        current_ref,
        "--expected-candidate",
        expected_candidate,
        "--github-output",
        str(output),
    ]
    if expected_version is not None:
        arguments.extend(("--expected-version", expected_version))
    return arguments


def _workflow() -> dict[str, Any]:
    loaded = cast(dict[Any, Any], yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))
    # PyYAML follows YAML 1.1 and parses the workflow key `on` as boolean true.
    if True in loaded and "on" not in loaded:
        loaded["on"] = loaded.pop(True)
    return cast(dict[str, Any], loaded)


class RegistryPublicationWorkflowTests(unittest.TestCase):
    def test_workflow_triggers_only_manual_and_metadata_pushes(self) -> None:
        events = _workflow()["on"]
        self.assertEqual(set(events), {"workflow_dispatch", "push"})
        self.assertEqual(
            events["push"], {"branches": ["main", "master"], "paths": ["pyproject.toml"]}
        )

    def test_workflow_never_uploads_a_private_publication_environment(self) -> None:
        workflow = _workflow()
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                self.assertFalse(
                    step.get("uses", "").startswith(
                        ("actions/upload-artifact@", "actions/download-artifact@")
                    )
                )
        preflight = str(workflow["jobs"]["preflight"])
        self.assertNotIn(".publish-venv", preflight)
        self.assertNotIn("publication-capsule", str(workflow))

    def test_workflow_has_uniform_guard_and_two_bounded_jobs(self) -> None:
        workflow = _workflow()
        inputs = workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(set(inputs), {"expected_version", "candidate_commit"})
        self.assertTrue(all(item["required"] is True for item in inputs.values()))
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertIs(workflow["concurrency"]["cancel-in-progress"], False)
        jobs = workflow["jobs"]
        self.assertEqual(set(jobs), {"preflight", "publish"})
        self.assertEqual(jobs["preflight"]["timeout-minutes"], 5)
        self.assertEqual(jobs["publish"]["timeout-minutes"], 20)
        self.assertEqual(jobs["publish"]["needs"], "preflight")
        self.assertNotIn("environment", jobs["preflight"])
        self.assertEqual(jobs["publish"]["environment"], "registry-production")
        self.assertIn("needs.preflight.outputs.should_publish == 'true'", jobs["publish"]["if"])
        for job in jobs.values():
            self.assertEqual(job["permissions"], {"contents": "read"})
        guards = [
            step
            for step in jobs["preflight"]["steps"]
            if "scripts/registry_publish_guard.py" in step.get("run", "")
        ]
        self.assertEqual(len(guards), 1)
        guard = guards[0]
        for argument in (
            "--event-name",
            "--previous-ref",
            "--current-ref",
            "--expected-candidate",
            "--expected-version",
        ):
            self.assertIn(argument, guard["run"])
        self.assertNotIn("github.event", guard["run"])
        self.assertTrue(
            {"EVENT_NAME", "EXPECTED_VERSION", "EXPECTED_CANDIDATE"} <= set(guard["env"])
        )
        steps = jobs["preflight"]["steps"]
        guard_python = next(
            step for step in steps if step.get("uses", "").startswith("actions/setup-python@")
        )
        self.assertEqual(guard_python["with"]["python-version"], "3.11")
        self.assertLess(steps.index(guard_python), steps.index(guard))

    def test_workflow_builds_one_hash_locked_cli_after_approval_and_directly_publishes(
        self,
    ) -> None:
        workflow = _workflow()
        jobs = workflow["jobs"]
        text = WORKFLOW.read_text(encoding="utf-8")
        uses = re.findall(r"(?m)^\s*(?:-\s*)?uses:\s*([^\s#]+)", text)
        self.assertEqual(
            set(uses),
            {
                "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0",
                "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
            },
        )
        self.assertTrue(all(FULL_SHA.fullmatch(item.rsplit("@", 1)[1]) for item in uses))
        self.assertEqual(text.count("python -m pip install"), 1)
        self.assertIn("--require-hashes", text)
        self.assertIn("requirements/registry-publish-py310-linux-x86_64.txt", text)
        self.assertIn("python -m venv --copies .publish-venv", text)
        self.assertNotIn("REGISTRY_ACCESS_TOKEN", str(jobs["preflight"]))
        self.assertNotIn("python -m pip install", str(jobs["preflight"]))
        self.assertEqual(set(jobs["preflight"]["outputs"]), {"should_publish", "candidate_commit"})
        steps = jobs["publish"]["steps"]
        publication_python = next(
            step for step in steps if step.get("uses", "").startswith("actions/setup-python@")
        )
        self.assertEqual(publication_python["with"]["python-version"], "3.10")
        environment = next(step for step in steps if "python -m pip install" in step.get("run", ""))
        audits = [step for step in steps if "scripts/registry_payload.py" in step.get("run", "")]
        self.assertEqual(len(audits), 1)
        audit = audits[0]
        self.assertLess(steps.index(environment), steps.index(audit))
        self.assertIn(".publish-venv/bin/comfy node pack", audit["run"])
        self.assertIn("--archive node.zip", audit["run"])
        self.assertIn("--source-root . --public-projection", audit["run"])
        self.assertLess(
            audit["run"].index("scripts/product_completeness.py --source-root ."),
            audit["run"].index("comfy node pack"),
        )
        self.assertIn('test "$(git rev-parse HEAD)" = "$CANDIDATE_COMMIT"', audit["run"])
        self.assertEqual(
            audit["env"]["CANDIDATE_COMMIT"], "${{ needs.preflight.outputs.candidate_commit }}"
        )
        self.assertIn(
            '.publish-venv/bin/comfy node publish --token "$REGISTRY_ACCESS_TOKEN"',
            steps[-1]["run"],
        )
        self.assertEqual(
            steps[-1]["env"], {"REGISTRY_ACCESS_TOKEN": "${{ secrets.REGISTRY_ACCESS_TOKEN }}"}
        )
        self.assertNotIn("REGISTRY_ACCESS_TOKEN", str(steps[:-1]))
        self.assertNotIn("Comfy-Org/publish-node-action", text)
        self.assertNotIn("skip_checkout", text)
        for job in jobs.values():
            checkout = next(
                step
                for step in job["steps"]
                if step.get("uses", "").startswith("actions/checkout@")
            )
            self.assertEqual(checkout["with"]["fetch-depth"], 0)
            self.assertIs(checkout["with"]["persist-credentials"], False)
        self.assertEqual(steps[0]["with"]["ref"], "${{ needs.preflight.outputs.candidate_commit }}")


class RegistryPublicationLockTests(unittest.TestCase):
    def test_linux_python310_lock_is_exact_closed_and_hashed(self) -> None:
        lines = [
            line
            for line in LOCK.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ]
        self.assertEqual(len(lines), 51)
        entries: dict[str, tuple[str, str]] = {}
        for line in lines:
            match = LOCK_ENTRY.fullmatch(line)
            self.assertIsNotNone(match, line)
            assert match is not None
            normalized = re.sub(r"[-_.]+", "-", match.group("name")).casefold()
            self.assertNotIn(normalized, entries)
            entries[normalized] = (match.group("version"), match.group("digest"))
            self.assertNotIn(";", line)
            self.assertNotIn("://", line)
        self.assertEqual(
            entries["comfy-cli"],
            (
                "1.16.0",
                COMFY_CLI_WHEEL_SHA256,
            ),
        )
        # CRITICAL: AnyIO requires this backport on the workflow's pinned Python 3.10 lane.
        self.assertEqual(entries["exceptiongroup"], ("1.3.1", EXCEPTIONGROUP_WHEEL_SHA256))


class RegistryPublishGuardTests(unittest.TestCase):
    def test_guard_help_runs_without_site_packages_under_stdlib_tomllib_python(self) -> None:
        self.assertGreaterEqual(sys.version_info, (3, 11))
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                str(ROOT / "scripts" / "registry_publish_guard.py"),
                "--help",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_same_version_skips_and_increased_version_publishes(self) -> None:
        self.assertEqual(
            decide_publish(_version("0.1.0"), _version("0.1.0")),
            (False, "version_unchanged"),
        )
        self.assertEqual(
            decide_publish(_version("0.2.0"), _version("0.1.9")),
            (True, "version_increased"),
        )

    def test_downgrade_and_invalid_versions_fail_closed(self) -> None:
        with self.assertRaisesRegex(PublishGuardError, "downgrade"):
            decide_publish(_version("0.1.9"), _version("0.2.0"))
        with self.assertRaisesRegex(PublishGuardError, "X.Y.Z"):
            _version("0.2")

    def test_push_unchanged_writes_false_and_upgrade_writes_true(self) -> None:
        for current_version, expected in (("1.0.0", "false"), ("1.0.1", "true")):
            with (
                self.subTest(current_version=current_version),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                previous, current = _release_fixture(root, _manifest("1.0.0"), current_version)
                output = root / "github-output.txt"
                with (
                    patch("scripts.registry_publish_guard.ROOT", root),
                    patch("scripts.registry_publish_guard.PYPROJECT", root / "pyproject.toml"),
                ):
                    self.assertEqual(
                        main(
                            _guard_args(
                                event_name="push",
                                previous_ref=previous,
                                current_ref=current,
                                expected_candidate=current,
                                output=output,
                            )
                        ),
                        0,
                    )
                values = output.read_text(encoding="utf-8")
                self.assertIn(f"should_publish={expected}", values)
                self.assertIn(f"candidate_commit={current}", values)

    def test_dispatch_requires_matching_version_candidate_and_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous, current = _release_fixture(root, _manifest("0.9.0"), "1.0.0")
            for expected_version, candidate, expected_status in (
                ("1.0.0", current, 0),
                ("1.0.1", current, 1),
                ("1.0.0", previous, 1),
            ):
                with self.subTest(version=expected_version, candidate=candidate):
                    output = root / f"output-{expected_version}-{candidate[:7]}.txt"
                    with (
                        patch("scripts.registry_publish_guard.ROOT", root),
                        patch("scripts.registry_publish_guard.PYPROJECT", root / "pyproject.toml"),
                    ):
                        self.assertEqual(
                            main(
                                _guard_args(
                                    event_name="workflow_dispatch",
                                    previous_ref=previous,
                                    current_ref=current,
                                    expected_candidate=candidate,
                                    expected_version=expected_version,
                                    output=output,
                                )
                            ),
                            expected_status,
                        )
                    self.assertEqual(output.exists(), expected_status == 0)

    def test_dispatch_duplicate_version_fails_without_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous, current = _release_fixture(root, _manifest("1.0.0"), "1.0.0")
            output = root / "github-output.txt"
            with (
                patch("scripts.registry_publish_guard.ROOT", root),
                patch("scripts.registry_publish_guard.PYPROJECT", root / "pyproject.toml"),
            ):
                self.assertEqual(
                    main(
                        _guard_args(
                            event_name="workflow_dispatch",
                            previous_ref=previous,
                            current_ref=current,
                            expected_candidate=current,
                            expected_version="1.0.0",
                            output=output,
                        )
                    ),
                    1,
                )
            self.assertFalse(output.exists())

    def test_initial_version_requires_proven_previous_commit_without_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous, current = _release_fixture(root, None, "1.0.0")
            output = root / "github-output.txt"
            with (
                patch("scripts.registry_publish_guard.ROOT", root),
                patch("scripts.registry_publish_guard.PYPROJECT", root / "pyproject.toml"),
            ):
                self.assertEqual(
                    main(
                        _guard_args(
                            event_name="workflow_dispatch",
                            previous_ref=previous,
                            current_ref=current,
                            expected_candidate=current,
                            expected_version="1.0.0",
                            output=output,
                        )
                    ),
                    0,
                )
            self.assertIn("reason=initial_version", output.read_text(encoding="utf-8"))

    def test_invalid_previous_and_current_identities_fail_without_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous, current = _release_fixture(root, "[project\n", "1.0.0")
            cases = (
                (previous, current, current),
                ("0" * 40, current, current),
                ("a" * 40, current, current),
                (previous, previous, previous),
            )
            for index, (previous_ref, current_ref, candidate) in enumerate(cases):
                with self.subTest(index=index):
                    output = root / f"output-{index}.txt"
                    with (
                        patch("scripts.registry_publish_guard.ROOT", root),
                        patch("scripts.registry_publish_guard.PYPROJECT", root / "pyproject.toml"),
                    ):
                        self.assertEqual(
                            main(
                                _guard_args(
                                    event_name="push",
                                    previous_ref=previous_ref,
                                    current_ref=current_ref,
                                    expected_candidate=candidate,
                                    output=output,
                                )
                            ),
                            1,
                        )
                    self.assertFalse(output.exists())

    def test_git_manifest_lookup_error_fails_closed(self) -> None:
        completed = subprocess.CompletedProcess(args=["git"], returncode=0, stdout=b"", stderr=b"")
        failed = subprocess.CompletedProcess(args=["git"], returncode=128, stdout=b"", stderr=b"")
        with (
            patch("scripts.registry_publish_guard.subprocess.run", side_effect=[completed, failed]),
            self.assertRaisesRegex(PublishGuardError, "cannot determine"),
        ):
            read_previous_version_from_git("a" * 40)


if __name__ == "__main__":
    unittest.main()
