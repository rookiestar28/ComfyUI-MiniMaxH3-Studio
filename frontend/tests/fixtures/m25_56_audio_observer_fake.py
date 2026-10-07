"""Bounded subprocess double for the TypeScript observer-wrapper contract tests."""

from __future__ import annotations

import json
import sys
import time

READY = {
    "ready": True,
    "selector": "include_test_browser_process_tree",
    "browserExecutableSha256": "a" * 64,
}
METRICS = {
    "sample_rate": 48000,
    "channels": 2,
    "samples_analyzed": 0,
    "window_samples": 480,
    "silence_floor": 0.01,
    "silence_runs": [],
    "impulse_floor": 0.5,
    "impulse_events": [],
    "envelopes": [],
    "raw_audio_retained": False,
}
CAPTURE = {
    "schema": "h3.context.process_audio_observer_capture.v3",
    "selector": READY["selector"],
    "sampleRate": 48000,
    "observationFormat": "pcm_float32_stereo",
    "browserExecutableSha256": READY["browserExecutableSha256"],
    "packets": [],
    "onsets": [],
    "metrics": METRICS,
    "discontinuityPackets": 0,
    "timestampErrorPackets": 0,
    "silentPackets": 0,
    "packetTimeline": {
        "clockSource": "device_position",
        "valid": True,
        "positionBreaks": 0,
        "positionGaps": 0,
        "positionOverlaps": 0,
        "discontinuityPackets": 0,
        "timestampErrorPackets": 0,
        "qpcValid": True,
        "qpcBreaks": 0,
        "qpcGaps": 0,
        "qpcOverlaps": 0,
        "qpcGapFramesFilled": 0,
    },
    "rawAudioRetained": False,
    "microphoneOpened": False,
}


def main() -> None:
    pid = int(sys.argv[sys.argv.index("--pid") + 1])
    if pid == 127:
        print(json.dumps({"failure": "process_loopback_activation_result_0x80004005"}), flush=True)
        raise SystemExit(1)
    print(json.dumps(READY), flush=True)
    sys.stdin.readline()
    if pid == 124:
        time.sleep(0.05)
        sys.stdout.write("x" * 2_000_001)
        sys.stdout.flush()
        return
    result = dict(CAPTURE)
    if pid == 125:
        result.pop("metrics")
    elif pid == 126:
        result["browserExecutableSha256"] = "b" * 64
    print(json.dumps({"result": result}), flush=True)


if __name__ == "__main__":
    main()
