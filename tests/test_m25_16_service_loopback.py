"""The real-service evidence host must fail closed at external execution boundaries."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.m25_16_service_loopback import _adapter_registrars


def test_loopback_census_includes_the_two_path_media_lease_registrar() -> None:
    labels = {f"{module.__name__}.{name}" for module, name in _adapter_registrars()}
    assert (
        "comfyui_h3_context.adapters.comfyui_authoring_media_leases."
        "ensure_authoring_media_lease_routes_registered"
    ) in labels


def test_loopback_external_guards_refuse_and_count_in_an_isolated_process() -> None:
    # Audit hooks cannot be removed. Never install this guard in the shared pytest process.
    program = """
import json, socket, subprocess, sys
from contextlib import ExitStack
from scripts.m25_16_service_loopback import _guard_external_effects
from comfyui_h3_context.adapters import prompt_model_transport as transport
counters = {'provider_model_calls': 0, 'outbound_attempts': 0}
with ExitStack() as scope:
    _guard_external_effects(counters, scope)
    attempts = [
        lambda: transport.run_prompt_model_session(),
        lambda: transport.run_remote_prompt_model_session(),
        lambda: transport.probe_native_runtime(),
        lambda: transport.LoopbackJsonExchange.request(None),
        lambda: transport.RemoteHttpsExchange.request(None),
        lambda: socket.getaddrinfo('127.0.0.1', 9),
        lambda: subprocess.Popen([sys.executable, '-c', 'raise SystemExit(99)']),
    ]
    with socket.socket() as connection:
        attempts.append(lambda: connection.connect(('127.0.0.1', 9)))
        reasons = []
        for attempt in attempts:
            try:
                attempt()
            except RuntimeError as error:
                reasons.append(str(error))
            else:
                raise AssertionError('external attempt was allowed')
print(json.dumps({'counters': counters, 'reasons': reasons}))
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    observed = json.loads(result.stdout)
    assert observed["counters"] == {"provider_model_calls": 5, "outbound_attempts": 3}
    assert (
        observed["reasons"]
        == ["provider_model_disabled_in_loopback"] * 5
        + ["outbound_execution_disabled_in_loopback"] * 3
    )
