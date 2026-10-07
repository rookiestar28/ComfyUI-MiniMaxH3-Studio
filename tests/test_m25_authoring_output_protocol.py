"""Closed output transport grammar; no renderer, host or private artifact authority."""

from __future__ import annotations

import importlib
import importlib.util
import json
import unittest
from typing import Any


def create_wire() -> dict[str, object]:
    return {
        "schema": "h3.authoring.output_create.v1",
        "workspace_handle": "authoring-" + "a" * 32,
        "workspace_revision": 0,
        "timeline_revision": 3,
        "snapshot_fingerprint": "sha256:" + "b" * 64,
        "output_profile_id": "h3.authoring.output.h264_aac_24fps.v1",
        "idempotency_key": "request_0123456789",
    }


class OutputProtocolTests(unittest.TestCase):
    def protocol(self) -> Any:
        name = "comfyui_h3_context.core.authoring_output_protocol"
        self.assertIsNotNone(importlib.util.find_spec(name), "closed output protocol is missing")
        return importlib.import_module(name)

    def test_create_preserves_exact_public_joins(self) -> None:
        protocol = self.protocol()
        wire = create_wire()
        request = protocol.decode_output_create(wire)
        self.assertEqual(request.to_wire(), wire)
        self.assertEqual(request.workspace_revision, 0)
        self.assertEqual(request.timeline_revision, 3)

    def test_create_refuses_every_extra_private_authority_field(self) -> None:
        protocol = self.protocol()
        for field in (
            "job_handle",
            "output_handle",
            "receipt",
            "valid",
            "source_path",
            "url",
            "renderer_argv",
            "plan_fingerprint",
            "currentness_fingerprint",
            "timeout_ms",
        ):
            with self.subTest(field=field), self.assertRaises(protocol.OutputProtocolError):
                protocol.decode_output_create({**create_wire(), field: "untrusted"})

    def test_create_refuses_missing_fields_and_non_objects(self) -> None:
        protocol = self.protocol()
        for field in create_wire():
            wire = create_wire()
            del wire[field]
            with self.subTest(field=field), self.assertRaises(protocol.OutputProtocolError):
                protocol.decode_output_create(wire)
        value: object
        for value in (None, [], "{}", True):
            with self.subTest(value=value), self.assertRaises(protocol.OutputProtocolError):
                protocol.decode_output_create(value)

    def test_create_rejects_bool_revisions_wrong_types_and_overflow(self) -> None:
        protocol = self.protocol()
        for field in ("workspace_revision", "timeline_revision"):
            for value in (True, False, -1, 1_000_001, 1.0, "1", None):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(protocol.OutputProtocolError):
                        protocol.decode_output_create({**create_wire(), field: value})

    def test_create_rejects_foreign_schema_profile_fingerprint_and_workspace(self) -> None:
        protocol = self.protocol()
        for field, values in {
            "schema": ["h3.authoring.render_job_request.v1", "h3.authoring.output_create.v2"],
            "output_profile_id": ["custom", "output.h264_aac_24fps.v2"],
            "snapshot_fingerprint": ["b" * 64, "sha256:" + "B" * 64, "sha256:" + "b" * 64 + "\n"],
            "workspace_handle": [
                "pw_" + "a" * 22,
                "authoring-" + "a" * 31,
                "authoring-" + "A" * 32,
            ],
            "idempotency_key": ["short", "x" * 129, "_" + "a" * 16, "a/b" + "x" * 20],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(protocol.OutputProtocolError):
                        protocol.decode_output_create({**create_wire(), field: value})

    def test_json_limits_duplicates_and_nonfinite_are_rejected(self) -> None:
        protocol = self.protocol()
        raw = json.dumps(create_wire()).encode()
        self.assertEqual(protocol.decode_output_create_json(raw).to_wire(), create_wire())
        malformed = (
            b'{"workspace_revision":0,"workspace_revision":1}',
            b'{"x":NaN}',
            b'{"x":Infinity}',
            b'"value"',
            b"\xff",
            b" " * 4097,
            b"[" * 1000 + b"]" * 1000,
        )
        for body in malformed:
            with (
                self.subTest(body_length=len(body)),
                self.assertRaises(protocol.OutputProtocolError),
            ):
                protocol.decode_output_create_json(body)

    def test_handles_accept_only_exact_namespaces_and_lengths(self) -> None:
        protocol = self.protocol()
        for kind, prefix in (("job", "arj_"), ("output", "aro_")):
            expected = prefix + "aB09_-" * 3 + "abcd"
            self.assertEqual(protocol.require_output_handle(expected, kind=kind), expected)
            for bad in (
                prefix + "a" * 21,
                prefix + "a" * 23,
                expected + "\n",
                "pw_" + "a" * 22,
                "out_" + "a" * 22,
                "render-" + "a" * 32,
                "../output.mp4",
                "https://invalid.example/",
                None,
            ):
                with (
                    self.subTest(kind=kind, bad=bad),
                    self.assertRaises(protocol.OutputProtocolError),
                ):
                    protocol.require_output_handle(bad, kind=kind)
        with self.assertRaises(protocol.OutputProtocolError):
            protocol.require_output_handle("aro_" + "a" * 22, kind="job")

    def test_cancel_is_closed_and_keeps_workspace_binding(self) -> None:
        protocol = self.protocol()
        wire = {
            "schema": "h3.authoring.output_cancel.v1",
            "workspace_handle": create_wire()["workspace_handle"],
        }
        self.assertEqual(
            protocol.decode_output_cancel_json(json.dumps(wire).encode()), wire["workspace_handle"]
        )
        for extra in ("job_id", "receipt", "cancelled"):
            with self.subTest(extra=extra), self.assertRaises(protocol.OutputProtocolError):
                protocol.decode_output_cancel_json(json.dumps({**wire, extra: True}).encode())

    def test_full_and_single_range_have_exact_intervals(self) -> None:
        protocol = self.protocol()
        cases = [
            (None, 200, 0, 100),
            ("bytes=0-0", 206, 0, 1),
            ("bytes=5-12", 206, 5, 13),
            ("bytes=98-999", 206, 98, 100),
            ("bytes=4-", 206, 4, 100),
            ("bytes=-1", 206, 99, 100),
            ("bytes=-999", 206, 0, 100),
        ]
        for header, status, start, stop in cases:
            with self.subTest(header=header):
                selection = protocol.select_output_range(header, 100)
                self.assertEqual(
                    (selection.status, selection.start, selection.stop), (status, start, stop)
                )
                self.assertEqual(selection.byte_length, stop - start)
                headers = protocol.output_media_headers(selection, preview=False)
                self.assertEqual(headers["Content-Length"], str(stop - start))
                if status == 206:
                    self.assertEqual(headers["Content-Range"], f"bytes {start}-{stop - 1}/100")
                else:
                    self.assertNotIn("Content-Range", headers)

    def test_unsatisfiable_range_is_416_with_complete_length_and_no_body(self) -> None:
        protocol = self.protocol()
        for header in ("bytes=100-", "bytes=101-200", "bytes=-0"):
            with self.subTest(header=header):
                selection = protocol.select_output_range(header, 100)
                headers = protocol.output_media_headers(selection, preview=False)
                self.assertEqual(selection.status, 416)
                self.assertEqual(selection.byte_length, 0)
                self.assertEqual(headers["Content-Range"], "bytes */100")
                self.assertEqual(headers["Content-Length"], "0")

    def test_malformed_ranges_fail_without_interpreting_untrusted_text(self) -> None:
        protocol = self.protocol()
        header: object
        for header in (
            "",
            "bytes=",
            "bytes=-",
            "bytes=9-2",
            "bytes=0-1,3-4",
            "items=0-1",
            "bytes=+1-2",
            "bytes=0- 1",
            "bytes=０-１",
            "bytes=0-1\r\nX: value",
            "bytes=" + "9" * 20 + "-",
            "bytes=9223372036854775808-",
            True,
            [],
        ):
            with (
                self.subTest(header=header),
                self.assertRaises(protocol.OutputProtocolError) as caught,
            ):
                protocol.select_output_range(header, 100)
            self.assertEqual(caught.exception.code, "invalid_request")
            self.assertEqual(str(caught.exception), "invalid_request")

    def test_media_headers_are_fixed_private_and_distinguish_original_from_preview(self) -> None:
        protocol = self.protocol()
        for preview in (False, True):
            headers = protocol.output_media_headers(
                protocol.select_output_range(None, 100), preview=preview
            )
            self.assertEqual(headers["Cache-Control"], "private, no-store")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(headers["Accept-Ranges"], "bytes")
            self.assertEqual(headers["Content-Type"], "video/mp4")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            self.assertEqual(
                headers["Content-Disposition"],
                'inline; filename="authoring-preview.mp4"'
                if preview
                else 'attachment; filename="authoring-final.mp4"',
            )
            self.assertFalse({"Location", "ETag", "Last-Modified"} & headers.keys())

    def test_complete_length_must_fit_original_budget(self) -> None:
        protocol = self.protocol()
        for length in (0, -1, True, 1.2, 512 * 1024 * 1024 + 1):
            with self.subTest(length=length), self.assertRaises(protocol.OutputProtocolError):
                protocol.select_output_range(None, length)
        self.assertEqual(
            protocol.select_output_range(None, 512 * 1024 * 1024).byte_length, 512 * 1024 * 1024
        )


if __name__ == "__main__":
    unittest.main()
