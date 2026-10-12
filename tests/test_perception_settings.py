"""Programmatic compatibility and immutable profile admission authority."""

from __future__ import annotations

import importlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO
from unittest.mock import Mock, patch

from comfyui_h3_context.adapters import perception_host as host
from comfyui_h3_context.core.errors import ContractValidationError
from comfyui_h3_context.core.perception_execution import AUDIO_PROFILE, VISUAL_PROFILE

# IMPORTANT: these methods enter Windows-only guards; portable refusal tests stay active.
windows_perception = unittest.skipUnless(
    os.name == "nt", "operator perception settings require Windows"
)


class PerceptionProfileSubsetTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1] / ".tmp/perception-profile-tests"
        root.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=root)
        self.root = Path(self.directory.name)
        self.weight = self.root / "fixture-weight"
        self.weight.write_bytes(b"content-free-fixture")
        self.bindings: list[host.PerceptionHostBinding] = []

    def tearDown(self) -> None:
        for binding in reversed(self.bindings):
            host.clear_perception_host(binding)
        self.directory.cleanup()

    def settings(self, subset: Any) -> host.PerceptionHostSettings:
        try:
            return host.PerceptionHostSettings(
                Path(sys.executable), self.root, self.weight, self.root, profiles=subset
            )
        except TypeError:
            self.fail("explicit_profile_subset_not_supported")

    def bind(self, settings: host.PerceptionHostSettings) -> host.PerceptionHostBinding:
        binding = host.configure_perception_host(settings)
        self.bindings.append(binding)
        return binding

    def test_existing_five_positional_manual_method_preserves_both_profiles(self) -> None:
        self.bind(
            host.PerceptionHostSettings(
                Path(sys.executable), self.root, self.weight, self.root, lambda: False
            )
        )
        self.assertTrue(host.perception_configured(VISUAL_PROFILE))
        self.assertTrue(host.perception_configured(AUDIO_PROFILE))

    def test_visual_only_explicit_subset_cannot_enable_audio_even_with_paths(self) -> None:
        self.bind(self.settings(frozenset({VISUAL_PROFILE})))
        self.assertTrue(host.perception_configured(VISUAL_PROFILE))
        self.assertFalse(host.perception_configured(AUDIO_PROFILE))

    def test_audio_only_subset_cannot_enable_visual(self) -> None:
        self.bind(self.settings(frozenset({AUDIO_PROFILE})))
        self.assertTrue(host.perception_configured(AUDIO_PROFILE))
        self.assertFalse(host.perception_configured(VISUAL_PROFILE))

    def test_empty_unknown_or_mutable_subset_is_refused(self) -> None:
        for subset in (
            frozenset(),
            frozenset({"unknown_profile"}),
            {VISUAL_PROFILE},
            [VISUAL_PROFILE],
            False,
        ):
            with self.subTest(kind=type(subset).__name__):
                with self.assertRaises(ContractValidationError):
                    self.settings(subset)

    def test_subset_mutation_revokes_registered_configuration(self) -> None:
        settings = self.settings(frozenset({VISUAL_PROFILE}))
        binding = self.bind(settings)
        object.__setattr__(settings, "profiles", frozenset({AUDIO_PROFILE}))
        self.assertFalse(host.perception_configured(VISUAL_PROFILE))
        self.assertFalse(host.perception_configured(AUDIO_PROFILE))
        with self.assertRaisesRegex(host.PerceptionExecutionError, "configuration_revoked"):
            host._lease(binding)

    def test_mutated_subset_cannot_forge_snapshot_equality_or_admission(self) -> None:
        class UntrustedSubset(frozenset[str]):
            def __eq__(self, other: object) -> bool:
                raise AssertionError("untrusted_subset_equality_was_invoked")

            def __contains__(self, value: object) -> bool:
                raise AssertionError("untrusted_subset_membership_was_invoked")

        settings = self.settings(frozenset({VISUAL_PROFILE}))
        binding = self.bind(settings)
        object.__setattr__(settings, "profiles", UntrustedSubset({AUDIO_PROFILE}))
        self.assertFalse(host.perception_configured(AUDIO_PROFILE))
        with self.assertRaisesRegex(host.PerceptionExecutionError, "configuration_revoked"):
            host._lease(binding)

    def test_revocation_in_callback_is_checked_before_worker_authority(self) -> None:
        settings = self.settings(frozenset({VISUAL_PROFILE}))
        binding = self.bind(settings)

        def revoke() -> bool:
            object.__setattr__(settings, "profiles", frozenset({AUDIO_PROFILE}))
            return False

        object.__setattr__(settings, "cancelled", revoke)
        # Re-register the actual callback before it mutates the subset; a stale pre-callback
        # lease would test only the existing snapshot check, not the post-callback guard.
        binding = self.bind(settings)
        with self.assertRaisesRegex(host.PerceptionExecutionError, "configuration_revoked"):
            host._lease(binding)

    def test_old_binding_cannot_clear_programmatic_successor(self) -> None:
        old = self.bind(self.settings(frozenset({VISUAL_PROFILE})))
        self.bind(self.settings(frozenset({AUDIO_PROFILE})))
        self.assertFalse(host.clear_perception_host(old))
        self.assertTrue(host.perception_configured(AUDIO_PROFILE))

    def test_non_windows_execution_refuses_before_callback_media_or_worker(self) -> None:
        cancelled = Mock(return_value=False)
        self.bind(host.PerceptionHostSettings(Path(sys.executable), self.root, cancelled=cancelled))
        media = Mock(media_kind="image")
        with (
            patch.object(os, "name", "posix"),
            patch.object(
                host, "_snapshot", side_effect=AssertionError("unsupported_platform_read_media")
            ) as snapshot,
            patch.object(
                host,
                "_run_worker",
                side_effect=AssertionError("unsupported_platform_started_worker"),
            ) as worker,
        ):
            with self.assertRaisesRegex(host.PerceptionExecutionError, "^unsupported_platform$"):
                host.execute_perception(
                    media, profile_id=VISUAL_PROFILE, local_service_consent=True
                )
        cancelled.assert_not_called()
        snapshot.assert_not_called()
        worker.assert_not_called()


class PerceptionSettingsFileTests(unittest.TestCase):
    """Real file bytes exercise the loader; no interpreter or model is launched."""

    def setUp(self) -> None:
        source = Path(__file__).resolve().parents[1]
        owned = source / ".tmp/perception-file-tests"
        self.assertTrue(owned.resolve().is_relative_to(source.resolve()))
        owned.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=owned)
        self.root = Path(self.directory.name)
        self.interpreter = self.root / "fixture-python.exe"
        self.interpreter.write_bytes(b"not-executed")
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.file = self.root / "perception_host.json"
        self.value: dict[str, Any] = {
            "schema": "h3-context-perception-host/1",
            "python_executable": str(self.interpreter),
            "temporary_root": str(self.scratch),
            "profiles": [VISUAL_PROFILE],
        }

    def tearDown(self) -> None:
        self.directory.cleanup()

    def write(self, value: object) -> None:
        self.file.write_text(json.dumps(value), encoding="utf-8")

    def loader(self) -> Any:
        # Imported inside file tests, so original queue/subset RED can run on the actual old
        # source without pretending a missing loader import is a behavioral regression.
        return importlib.import_module("comfyui_h3_context.adapters.perception_settings")

    def refused(self, expected: str) -> None:
        loader = self.loader()
        with self.assertRaises(loader.PerceptionSettingsError) as raised:
            loader.load_perception_settings(self.file, cancelled=lambda: False)
        self.assertEqual(str(raised.exception), expected)
        self.assertNotIn(str(self.root), repr(raised.exception))

    @windows_perception
    def test_visual_only_file_binds_the_actual_declared_subset_and_paths(self) -> None:
        self.write(self.value)

        def cancelled() -> bool:
            return False

        loader = self.loader()
        try:
            settings = loader.load_perception_settings(self.file, cancelled=cancelled)
        except loader.PerceptionSettingsError as refusal:
            self.fail("valid_operator_file_was_refused:" + str(refusal))
        self.assertIs(type(settings), host.PerceptionHostSettings)
        self.assertEqual(settings.python_executable, self.interpreter)
        self.assertEqual(settings.temporary_root, self.scratch)
        self.assertEqual(settings.profiles, frozenset({VISUAL_PROFILE}))
        self.assertIs(settings.cancelled, cancelled)
        self.assertNotIn(str(self.root), repr(settings))

    @windows_perception
    def test_absent_file_is_cached_owner_remedy_not_an_implicit_write(self) -> None:
        self.refused("settings_missing")
        self.assertFalse(self.file.exists())

    @windows_perception
    def test_byte_budget_precedes_decoding(self) -> None:
        self.file.write_bytes(b" " * 16385)
        self.refused("settings_too_large")

    @windows_perception
    def test_valid_json_at_exact_byte_budget_is_admitted(self) -> None:
        raw = json.dumps(self.value).encode("utf-8")
        self.file.write_bytes(raw + b" " * (16384 - len(raw)))
        settings = self.loader().load_perception_settings(self.file, cancelled=lambda: False)
        self.assertEqual(settings.profiles, frozenset({VISUAL_PROFILE}))

    @windows_perception
    def test_invalid_utf8_and_duplicate_keys_are_refused(self) -> None:
        for raw in (b"\xff", (json.dumps(self.value)[:-1] + ', "profiles": []}').encode()):
            with self.subTest(case="utf8" if raw == b"\xff" else "duplicate"):
                self.file.write_bytes(raw)
                self.refused("settings_invalid")

    @windows_perception
    def test_deep_json_and_non_finite_values_are_finite_refusals(self) -> None:
        for raw in (b"[" * 2000 + b"]" * 2000, b'{"profiles": [NaN]}'):
            with self.subTest(case="corrupt_json"):
                self.file.write_bytes(raw)
                self.refused("settings_invalid")

    @windows_perception
    def test_path_bearing_file_error_never_becomes_a_diagnostic(self) -> None:
        self.loader()
        self.write(self.value)
        with patch.object(Path, "open", side_effect=PermissionError("untrusted_operator_value")):
            self.refused("settings_unavailable")

    @windows_perception
    def test_reparse_file_directory_and_ancestor_are_refused(self) -> None:
        self.loader()
        self.write(self.value)
        original = Path.lstat
        # Physical basenames vary by checkout; stable role labels preserve paired identities.
        for component, rejected in (
            ("file", self.file),
            ("directory", self.scratch),
            ("ancestor", self.root),
        ):
            with self.subTest(component=component):

                def observed(
                    path: Path, *args: Any, rejected: Path = rejected, **kwargs: Any
                ) -> Any:
                    metadata = original(path, *args, **kwargs)
                    if path != rejected:
                        return metadata
                    fields = {
                        name: getattr(metadata, name)
                        for name in dir(metadata)
                        if name.startswith("st_")
                    }
                    fields["st_file_attributes"] = (
                        getattr(metadata, "st_file_attributes", 0)
                        | stat.FILE_ATTRIBUTE_REPARSE_POINT
                    )
                    return SimpleNamespace(**fields)

                with patch.object(Path, "lstat", observed):
                    self.refused("path_refused")

    @windows_perception
    def test_file_modified_during_bounded_read_is_refused(self) -> None:
        self.loader()
        self.write(self.value)
        original_open = Path.open
        target = self.file
        reads = []

        class ChangedDuringRead:
            def __init__(self, stream: BinaryIO) -> None:
                self.stream = stream

            def __enter__(self) -> Any:
                return self

            def __exit__(self, *args: Any) -> None:
                self.stream.close()

            def fileno(self) -> int:
                return self.stream.fileno()

            def read(self, count: int) -> bytes:
                reads.append(count)
                data = self.stream.read(count)
                with original_open(target, "ab") as changed:
                    changed.write(b" ")
                return data

        def observed(path: Path, *args: Any, **kwargs: Any) -> Any:
            stream = original_open(path, *args, **kwargs)
            return ChangedDuringRead(stream) if path == target and args == ("rb",) else stream

        with patch.object(Path, "open", observed):
            self.refused("path_refused")
        self.assertEqual(reads, [16385], "mutation_refusal_did_not_reach_the_bounded_read")

    @windows_perception
    def test_descriptor_ctime_difference_alone_does_not_refuse_valid_file(self) -> None:
        import os

        self.write(self.value)
        path_metadata = self.file.lstat()
        original_fstat = os.fstat

        def observed(descriptor: int) -> Any:
            metadata = original_fstat(descriptor)
            fields = {
                name: getattr(metadata, name) for name in dir(metadata) if name.startswith("st_")
            }
            # Reproduce only the native path/descriptor ctime disagreement observed after rewrite.
            # Device/inode/type/attributes/size/mtime and the real file read remain unchanged.
            fields["st_ctime_ns"] = path_metadata.st_ctime_ns + 10_000_000
            return SimpleNamespace(**fields)

        loader = self.loader()
        with patch.object(loader.os, "fstat", observed):
            try:
                settings = loader.load_perception_settings(self.file, cancelled=lambda: False)
            except loader.PerceptionSettingsError as refusal:
                self.fail("descriptor_ctime_only_difference_was_refused:" + str(refusal))
        self.assertEqual(settings.profiles, frozenset({VISUAL_PROFILE}))

    def test_private_file_declaration_preserves_directory_budgets(self) -> None:
        from comfyui_h3_context.core import private_storage_layout as layout

        self.assertEqual(len(layout.PRIVATE_STORAGE_SUBTREES), 5)
        self.assertEqual(layout.DURABLE_BYTES, (16 + 1024 + 640) * 1024 * 1024)
        files = layout.PRIVATE_STORAGE_FILES
        self.assertEqual(len(files), 1, "operator_settings_file_ownership_not_declared")
        row = files[0]
        self.assertEqual(
            (row.name, row.owner, row.lifetime, row.limit_bytes, row.limit_scope),
            ("perception_host.json", "perception_host", "durable", 16384, "per_file"),
        )

    def test_public_schema_is_closed_and_requires_explicit_audio_paths(self) -> None:
        import jsonschema

        source = Path(__file__).resolve().parents[1]
        path = source / "governance/contracts/perception_host_settings_v1.schema.json"
        self.assertTrue(path.is_file(), "operator_settings_schema_not_declared")
        schema = json.loads(path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        # The schema describes Windows drive paths, independent of the test host filesystem.
        schema_value = dict(
            self.value,
            python_executable=r"C:\fixture\python.exe",
            temporary_root=r"C:\fixture\scratch",
        )
        jsonschema.validate(schema_value, schema)
        for value in (
            dict(schema_value, unknown="untrusted_operator_value"),
            dict(schema_value, python_executable=False),
            dict(schema_value, profiles=[]),
            dict(schema_value, profiles=[VISUAL_PROFILE, VISUAL_PROFILE]),
            dict(schema_value, profiles=["unknown_profile"]),
            dict(schema_value, weight_path=None),
            dict(schema_value, profiles=[AUDIO_PROFILE]),
            dict(schema_value, python_executable="/fixture/python"),
            dict(schema_value, temporary_root="/fixture/scratch"),
        ):
            with self.subTest(case="closed_schema"):
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(value, schema)

    @windows_perception
    def test_unknown_keys_schema_and_exact_field_types_are_refused(self) -> None:
        cases = (
            dict(self.value, unknown="untrusted_operator_value"),
            dict(self.value, schema="unknown_schema"),
            dict(self.value, profiles=[]),
            dict(self.value, profiles=[VISUAL_PROFILE, VISUAL_PROFILE]),
            dict(self.value, profiles=["unknown_profile"]),
            dict(self.value, profiles=VISUAL_PROFILE),
            dict(self.value, python_executable=False),
            dict(self.value, temporary_root=None),
            dict(self.value, weight_path=None),
            dict(self.value, processor_dir=[]),
            [self.value],
            None,
        )
        for case in cases:
            with self.subTest(case_type=type(case).__name__):
                self.write(case)
                self.refused("settings_invalid")

    @windows_perception
    def test_audio_requires_both_existing_operator_paths(self) -> None:
        self.write(dict(self.value, profiles=[AUDIO_PROFILE]))
        self.refused("settings_invalid")
        weight = self.root / "fixture-weight"
        weight.write_bytes(b"not-read-as-model")
        self.write(
            dict(
                self.value,
                profiles=[AUDIO_PROFILE],
                weight_path=str(weight),
                processor_dir=str(self.scratch),
            )
        )
        settings = self.loader().load_perception_settings(self.file, cancelled=lambda: False)
        self.assertEqual(settings.profiles, frozenset({AUDIO_PROFILE}))

    @windows_perception
    def test_nonlocal_or_ambiguous_paths_are_refused_without_leaking_values(self) -> None:
        for value in (
            "relative.exe",
            "C:relative.exe",
            r"\root-relative.exe",
            r"\\host\share\python.exe",
            r"\\?\C:\python.exe",
            r"\\.\C:\python.exe",
            "C:\\bad\x00value.exe",
            str(self.root / ".." / "fixture-python.exe"),
        ):
            with self.subTest(case="path_shape"):
                self.write(dict(self.value, python_executable=value))
                self.refused("path_refused")

    @windows_perception
    def test_missing_or_wrong_filesystem_types_are_refused(self) -> None:
        for patch_value in (
            {"python_executable": str(self.scratch)},
            {"temporary_root": str(self.interpreter)},
            {"python_executable": str(self.root / "not-present.exe")},
        ):
            with self.subTest(field=next(iter(patch_value))):
                self.write(dict(self.value, **patch_value))
                self.refused("path_refused")

    def test_non_windows_loader_refuses_before_opening_any_file(self) -> None:
        loader = self.loader()
        self.write(self.value)
        with (
            patch.object(loader.os, "name", "posix"),
            patch.object(
                Path, "open", side_effect=AssertionError("unsupported_platform_opened_file")
            ) as opened,
        ):
            self.refused("unsupported_platform")
        opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
