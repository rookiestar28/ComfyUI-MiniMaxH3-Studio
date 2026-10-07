"""Only real server facts and a non-served qualified filesystem select a recovery owner."""

from __future__ import annotations

import importlib
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace

from comfyui_h3_context.core.durable_workspace_state import DurableStateError

MODULE = "comfyui_h3_context.adapters.recovery_owner"


def server_facts(**overrides: object) -> tuple[SimpleNamespace, SimpleNamespace]:
    args = SimpleNamespace(listen="127.0.0.1", multi_user=False, enable_cors_header=None)
    for name, value in overrides.items():
        setattr(args, name, value)
    return args, SimpleNamespace(users={"default": "default"})


class RecoveryOwnerTests(unittest.TestCase):
    def module(self) -> ModuleType:
        self.assertIsNotNone(
            importlib.util.find_spec(MODULE), "missing server-owned recovery qualification port"
        )
        return importlib.import_module(MODULE)

    def test_all_declared_listeners_and_single_user_mapping_must_qualify(self) -> None:
        module = self.module()
        args, users = server_facts(listen="127.0.0.1,::1")
        module.qualify_host(args, users, public_origins_configured=False)
        for changes in (
            {"listen": "127.0.0.1,0.0.0.0"},
            {"listen": "localhost"},
            {"listen": "::"},
            {"listen": "127.0.0.1,"},
            {"multi_user": True},
            {"multi_user": 0},
            {"enable_cors_header": "*"},
        ):
            with self.subTest(changes=changes), self.assertRaises(DurableStateError):
                module.qualify_host(*server_facts(**changes), public_origins_configured=False)
        for mapping in ({}, {"default": "default", "foreign": "foreign"}, {"default": "other"}):
            with self.subTest(mapping=mapping), self.assertRaises(DurableStateError):
                module.qualify_host(
                    args, SimpleNamespace(users=mapping), public_origins_configured=False
                )
        with self.assertRaises(DurableStateError):
            module.qualify_host(args, users, public_origins_configured=True)

    def test_request_uses_actual_socket_and_peer_not_host_or_profile_header(self) -> None:
        module = self.module()
        for local, peer, accepted in (
            (("127.0.0.1", 8188), ("127.0.0.1", 9000), True),
            (("::1", 8188, 0, 0), ("::1", 9000, 0, 0), True),
            (("127.0.0.1", 8188), ("192.0.2.1", 9000), False),
            (("192.0.2.1", 8188), ("127.0.0.1", 9000), False),
            (None, ("127.0.0.1", 9000), False),
        ):
            transport = SimpleNamespace(
                get_extra_info=lambda name, local=local, peer=peer: {
                    "sockname": local,
                    "peername": peer,
                }.get(name)
            )
            request = SimpleNamespace(
                transport=transport, headers={"Host": "127.0.0.1:8188", "comfy-user": "default"}
            )
            with self.subTest(local=local, peer=peer):
                self.assertEqual(module.request_is_loopback(request), accepted)
        self.assertFalse(
            module.request_is_loopback(SimpleNamespace(headers={"Host": "127.0.0.1:8188"}))
        )

    def test_private_owner_is_stable_server_derived_and_has_no_default_off_io(self) -> None:
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).absolute()
            private = root / "private"
            host = SimpleNamespace(
                private_root=lambda: private,
                served_roots=lambda: (root / "input", root / "output", root / "temp"),
            )
            port = module.RecoveryOwnerPort(
                host=host,
                host_facts=lambda: server_facts(),
                public_origins=lambda: False,
                filesystem=lambda path: True,
            )
            first, second = port.resolve(), port.resolve()
            self.assertEqual(first, second)
            self.assertRegex(first.owner_id, r"^owner_[0-9a-f]{32}$")
            self.assertNotIn("default", first.owner_id)
            self.assertFalse(private.exists())
            unsafe = module.RecoveryOwnerPort(
                host=SimpleNamespace(
                    private_root=lambda: root / "temp/private", served_roots=host.served_roots
                ),
                host_facts=lambda: server_facts(),
                public_origins=lambda: False,
                filesystem=lambda path: True,
            )
            with self.assertRaises(DurableStateError):
                unsafe.resolve()
            unsupported = module.RecoveryOwnerPort(
                host=host,
                host_facts=lambda: server_facts(),
                public_origins=lambda: False,
                filesystem=lambda path: False,
            )
            with self.assertRaisesRegex(DurableStateError, "filesystem_unqualified"):
                unsupported.resolve()

    @unittest.skipUnless(os.name == "nt", "native fixed NTFS qualification is Windows-specific")
    def test_native_volume_qualification_reads_real_fixed_ntfs_without_creating_tail(self) -> None:
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            tail = Path(directory).absolute() / "not-created"
            self.assertTrue(module.fixed_ntfs(tail))
            self.assertFalse(tail.exists())


if __name__ == "__main__":
    unittest.main()
