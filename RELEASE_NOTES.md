# Release 1.1.0

This release collects the completed improvements since 1.0.0, including the assisted-authoring
updates previously delivered in 1.0.2.

## Projects and recovery

- Save and open editable `.h3proj` files containing accepted Production and editor data, including
  segment relationships, storyboard, tracks, cuts, titles, transforms, effects and clip audio.
- Keep documents editable when media is missing. Explicitly relink compatible retained videos
  after current-byte and timing-profile verification; opening a file does not start work.
- Separately opt in to content-free workspace metadata, verified generated-video retention or
  editable draft recovery. All three settings are off by default.
- Recover acknowledged drafts and bounded data-only undo/redo as a new project after a restart,
  without restoring execution permissions. Pending tracked server drafts are protected from
  workspace expiry; browser-only edits are not presumed saved.
- Generated-video working copies now live outside ComfyUI's served directories. Original outputs
  are unchanged, and cleanup is bounded to recognized dead-owner storage.

## Editor and playback

- Adjust each main-track video's volume, mute and fades in the Inspector, browser preview and
  final render. Overlay videos remain silent; separate audio tracks are not supported.
- Preserve compatible preview resources through edits, prepare playback before use, and improve
  picture/audio handoff at adjacent cuts without keeping revoked sources current.
- Make title insertion atomic, explain unavailable insertion, and improve media-bin dragging
  with owned cancellation.
- Keep timeline shortcuts usable after toolbar actions and contained within the editor, and close
  clip/track menus on an outside press.
- Align the current native renderer qualification, packaged assets, import boundaries and
  downstream artifact guards without weakening source or executable verification.

## Prompt writing and provider setup

- Separate provider connections from exact model selection. Reload models without losing a still
  available selection, and show provider-reported dates, limits and lifecycle information.
- Support local Ollama and observed Gemini assisted execution; OpenAI and Anthropic retain model
  discovery and readiness checks. Provider permission remains explicit and text-only.
- Add guided prompt refinement while preserving exact dialogue, visible text, reference roles
  and timing. Review typed semantic suggestions, including one bounded repair for invalid output.
- Preserve local edits, focus, text selection and scroll position across Reader and comparison
  views. No suggestion is accepted automatically.
- Carry speaker identity and delivery through dialogue prompts, retain usable language tags and
  restrict reference perception to the conditioned footage actually used by generation.

## Compatibility and distributions

- Accept POSIX host roots for verified generated-video closure instead of applying the Windows
  drive-path grammar to Linux hosts. Storage refusals now report the correct category and remedy.
- Build complete Python source distributions from reviewed public tracked sources, including
  developer tests, compressed fixtures, frontend inputs, tooling and governance data.
- Exclude ignored/private records, installed dependencies, caches and stale build metadata from
  public artifacts. Issued source distributions support hash-verified Git-less rebuilds.
- Independently audit actual source, wheel and Registry payloads for required files, exact source
  bytes and unsafe paths. Keep public release automation and its declared helpers available.
- Refresh the README and public guides for the current project, storage and compatibility behavior.

## Upgrade and limits

Replace the complete package, restart ComfyUI and refresh the browser. Save editable projects and
preserve media separately before updating; a ComfyUI workflow does not back up the editor timeline.

The prompt pipeline supports Windows, Linux and WSL. The editor's native media tools, Production
import, assembly and final rendering still require Windows x64 and the qualified FFmpeg build.
Durable metadata, retained videos and draft recovery require a trusted single-user loopback Windows
host with fixed local NTFS storage, no permissive CORS and no public-origin override. POSIX root
admission does not qualify Linux rendering or durable recovery.

Project files do not embed media. Recovery covers only the acknowledged revision and is not a
zero-loss, power-loss or cloud-backup guarantee. Provider/model installation, live generation and
provider costs remain under the user's control.
