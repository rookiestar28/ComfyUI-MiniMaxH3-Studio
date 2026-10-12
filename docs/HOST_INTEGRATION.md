# Host integration

This page is for developers and operators who run a ComfyUI host and want to enable two optional
canvas features. Ordinary users do not need it: everything else works without host configuration.

## Contents

- [Official Context-IR service](#official-context-ir-service)
- [Local perception (visual and audio)](#local-perception-visual-and-audio)

## Official Context-IR service

The **H3 Official Context-IR** node sends a prompt to MiniMax's hosted Context-IR service and
returns an enhanced prompt. It does not generate video. The package ships without a connection:
until a host supplies one, the node refuses to run. Assisted-authoring profiles do not configure it.
The service's API is described in MiniMax's
[Context-IR create API](https://platform.minimax.io/docs/api-reference/video-generation-v2-h3-context-ir)
and [task query API](https://platform.minimax.io/docs/api-reference/video-generation-v2-query).

### Registering a connection

Install a factory with `configure_official_context_ir_host(factory)` from
`comfyui_h3_context.adapters.official_context_ir_host`, and pass the returned binding to
`clear_official_context_ir_host(binding)` on teardown. Clearing an older binding cannot remove a
newer one, and replacing or clearing a binding stops its unfinished runs.

For each run, the factory receives an opaque `credential_reference` and returns an
`OfficialContextIRHostServices` with:

- `transport`: the create/query implementation (`OfficialContextIRTransport`);
- `resolver`: a `CredentialResolver` for that reference's session;
- `is_current`: returns `True` only while that session is still authorized;
- `cleanup`: called once when the run ends, successfully or not.

### Host responsibilities

- Check that the session owns the reference before returning services; never use a shared
  credential for an arbitrary reference.
- Keep credentials in memory only, never in node inputs, saved workflows, receipts or logs.
- Keep the session authorized until cleanup and result handling finish; revoking it earlier rejects
  the result.
- Provide the account, destination and connection. The package does not create accounts, look for
  keys or install a provider SDK, and billing belongs to the operator.

### What the node guarantees

- Nothing is requested until the user has given upload and network consent.
- The credential is resolved again before each request, and authority is checked before and after
  each network call.
- Errors from your callbacks are redacted. A missing configuration refuses before any network use.
- Checking the node before queueing does not call your factory.
- If a run is cancelled or revoked locally, a task may already exist on MiniMax's side; polling
  simply stops.

## Local perception (visual and audio)

The **H3 Visual Perception Producer** and **H3 Audio Perception Producer** describe short media on
the host itself: the visual node asks a local Ollama model about selected frames, and the audio node
transcribes short English speech on the CPU. Enable them through an operator-owned file or the
programmatic host integration below. Installing this extension does not install either model.

### Operator-owned file

On a native Windows host, place `perception_host.json` directly in ComfyUI's system-user directory
for `h3_context`. Resolve that location in the host's Python environment, rather than using an
input, output, temporary or custom-node folder:

```python
from pathlib import Path
import folder_paths

settings_file = Path(folder_paths.get_system_user_directory("h3_context")) / "perception_host.json"
```

A visual-only configuration has this shape; replace the example paths with trusted existing paths:

```json
{
  "schema": "h3-context-perception-host/1",
  "python_executable": "C:/operator/perception/.venv/Scripts/python.exe",
  "temporary_root": "C:/operator/perception/private-temp",
  "profiles": ["qwen38_27b_q4_visual_v2"]
}
```

- The file must be UTF-8 JSON, at most 16 KiB, with exactly the fields shown and, optionally,
  `weight_path` and `processor_dir`. Unknown or duplicate fields, `null` values and duplicate or
  unknown profiles are refused. The [JSON schema](../governance/contracts/perception_host_settings_v1.schema.json)
  describes the fields; the loader also checks the actual filesystem.
- `profiles` must contain one or both qualified profile IDs. To enable audio, include
  `whisper_large_v3_cpu_en_v1`, `weight_path` for the existing qualified weight file and
  `processor_dir` for its existing complete processor directory. Omit optional fields when unused.
- Paths must be existing native Windows local-drive absolute paths. The interpreter and weight
  must be regular files; the temporary and processor roots must be directories. Network/device
  paths, traversal, symlinks and junctions or other reparse components are refused. Prepare a
  private writable temporary directory before starting the host; the loader creates nothing.
- The interpreter must already contain the supported dependencies below. The file selects a local
  executable, so allow only the trusted host operator to write it. These values are never workflow
  inputs, browser settings or result fields.
- Restart ComfyUI after creating or changing the file. The extension attempts to read it once on
  genuine host startup and caches a missing or refused configuration. Reloading the extension or
  reading status does not retry. An existing programmatic registration is preserved.
- The read-only `/h3-context/v1/perception/status` endpoint reports only finite readiness, visual/
  audio availability and whether host cancellation is observed. It cannot edit configuration or
  launch a model. If cancellation is not observed, no automatic host-interrupt check is available.

In **Settings → Local perception**, inspect configuration readiness and separate visual/audio
availability. **Refresh status** reads the host's existing readiness; it does not reread the
startup file, retry a refused configuration, edit host paths or launch a model. Restart ComfyUI
after changing the startup file. Assisted-authoring provider settings are a separate connection.

### Programmatic host integration

The existing registration method remains available:

```python
from pathlib import Path
from comfyui_h3_context.adapters.perception_host import (
    PerceptionHostSettings, configure_perception_host, clear_perception_host,
)

binding = configure_perception_host(PerceptionHostSettings(
    python_executable=Path("C:/operator/perception/.venv/Scripts/python.exe"),
    temporary_root=Path("C:/operator/perception/private-temp"),
    weight_path=Path("C:/operator/models/whisper-large-v3.safetensors"),
    processor_dir=Path("C:/operator/models/whisper-large-v3-processor"),
    cancelled=lambda: False,  # supply the host's cancellation check
))
# On teardown, revoke only this registration:
clear_perception_host(binding)
```

- Use trusted absolute paths and a private, writable temporary folder. The weight and processor
  paths are only needed for audio.
- These paths are not workflow inputs and never appear in results.
- Replacing or clearing a registration invalidates earlier results. Keep the cancellation check
  valid until downstream nodes finish.
- Registering performs no model load or network request.

### Supported setup

Only this combination is supported, on native Windows with Python 3.13:

- **Visual**: Ollama **0.35.1** at `127.0.0.1:11434` with
  [`qwen3.8:27b-q4_K_M`](https://ollama.com/library/qwen3.8:27b-q4_K_M) (SHA-256
  `25b843619e944cd0ae6069f94ff4e5e26a16e109ccbc0a66a0f05979ed70098e`).
- **Audio**: Transformers **4.57.6** and Torch **2.14.0+cpu** with NumPy, SciPy, safetensors and the
  tokenizer dependencies, plus the complete
  [Whisper large-v3 snapshot](https://huggingface.co/openai/whisper-large-v3/tree/06f233fe06e710322aca913c1bc4249a0d71fce1):
  the 3,087,130,976-byte weight file (SHA-256
  `a8e94b85976e5864ba3e9525c7e6c83b2a1eca42d4b797a0c7c24d778e40fd95`) and its ten original
  config and tokenizer files.

Both models are Apache-2.0; keep their notices. The nodes never install packages, download models
or use paid services. If the installed runtime or files differ, the nodes refuse to run.

### Inputs and limits

- **Visual** accepts an IMAGE, or a VIDEO that is already decoded at a constant frame rate (at most
  300 frames, 1024 × 1024 pixels, 15 seconds and 256 MiB). File-backed video is refused. Frames are
  resized to 512 pixels and sent to the local Ollama only after the user consents. For a reference
  video, connect the optional `request` input to describe up to five frames from the part of the
  video H3 actually uses; otherwise the first, middle and last frames are used.
- **Audio** accepts mono or stereo audio at 8–48 kHz, up to 15 seconds, and transcribes it offline.
  It gives no word timings or speaker separation, and silence produces no result.
- One request runs at a time; others are refused as busy. A run is limited to 180 seconds and a
  fixed memory ceiling, and cancelling never stops ComfyUI or the Ollama service. Use a dedicated
  Ollama instance to avoid contention.
- Results are partial and uncertain descriptions, not a measure of recognition accuracy. Saved
  receipts hold hashes and identities only, not media or text.

Connect the qualified visual result to the **H3 Full Reference Timeline Producer**. The connected
workflow can be queued when the local visual profile is configured and the user consents to
perception. Keep the same admitted media and request through the chain; the resulting timeline
remains partial. Without a qualified result, use the Plan and Compiler nodes to write prompts.
