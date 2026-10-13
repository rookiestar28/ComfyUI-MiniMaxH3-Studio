# Release 1.2.1

## Manual media tools setup

- The extension no longer downloads, extracts or installs FFmpeg. **Settings → Media tools**
  replaces in-panel installation with a manual setup guide: **Open download page** opens the
  exact release page in your browser, and the card shows the archive name and its SHA-256 with a
  copy button.
- Enter the extracted **bin** folder and choose **Use folder**. Both programs are verified against
  their fixed digests before the selection is saved. A supported pair in your Python environment
  or on `PATH` is still found automatically; choose **Check again** after changing `PATH`.
- A **?** button beside **Media tools** explains download, checksum verification (with Windows
  commands), extraction, folder selection and why other builds are refused. It is available in
  English, Traditional Chinese and Simplified Chinese.
- After you select the tools, retry the original media action yourself; it is not resumed
  automatically.
- Clean up known earlier setup leftovers only when requested, preserving unknown files and any
  parked runtime without a verified replacement.

## Sequence assembly fix

- Sequences built from your own ComfyUI video generations now assemble. Earlier versions
  refused these segments: first because of the color tag ComfyUI writes into its video files,
  and then, for 5-second and 15-second segments, because the file states its length rounded to
  the millisecond.
- Segments with that color tag are converted to the standard video color space while they are
  prepared for assembly. Segments that are already standard are prepared as before.

## Upgrade and limits

Replace the complete package, restart ComfyUI and refresh the browser. Tools that 1.2.0 or an
earlier version already installed are still discovered and verified; no action is needed. The
supported build, platforms and project format are unchanged from 1.2.0.

---

# Release 1.2.0

## Titles and editing

- Add strokes, sharp shadows and background padding to titles. Enable or reset
  each decoration separately while preserving the other text settings.
- Preserve literal line breaks, including blank and trailing lines, in both the
  browser preview and native rendering. Text alignment places individual lines
  inside the title canvas; clip alignment places the whole canvas.
- Save accepted decoration settings in project files and acknowledged draft
  recovery, with the existing undo/redo behavior.
- Rename the ripple toggle to **Ripple edits**. Trims and ripple deletes keep
  their existing edited-track scope, including supported overlay tracks.
- Separate timeline viewport, gestures, transport and presentation responsibilities
  while preserving the existing editor commands and interactions.
- Reuse unchanged clip controls while keeping clip identities and action handlers
  synchronized with the current editor state.

- Reduce repeated redraws of unchanged timeline, monitor and media controls during
  edits. Keep seeking and toolbar actions synchronized with the current editor state.

## Optional local perception

- Configure the existing qualified visual or audio profile through an
  operator-owned startup file, or keep using the existing programmatic host
  registration. Configuration is separate from assisted-authoring provider settings.
- Inspect finite local-perception readiness in Settings. The status read does not
  launch a model, edit host paths or install dependencies.
- Queue the full-reference visual timeline when a qualified connected visual result,
  the configured local profile and explicit user consent are available. The resulting
  timeline remains partial.
- Finish owned process termination before cleaning its temporary files, preserving
  cancellation and cleanup failures.

## Developer tools and distributions

- Strengthen Windows validation, subprocess accounting and CI admission while
  preserving complete test identities and native result reporting.
- Provide independently stated native-behavior oracles and explicit catalogue gaps.
- Check declared public Python interfaces against the released baseline with the
  static consumer compatibility tool.
- Separate Production assembly execution and media-use responsibilities without
  changing public generation or persistence contracts.
- Refresh generated contracts, inventories, fingerprints and provenance from their
  actual inputs, with a clean fixed point and explicit failure reporting.
- Keep public source distributions complete for developers, including tests and
  support inputs, while omitting private maintainer material. Preserve executable
  source semantics through public comment/docstring redaction and audit the actual
  source, wheel and Registry archives.

## Upgrade and limits

Replace the complete package, restart ComfyUI and refresh the browser. Save editable
projects and preserve media separately before updating.

Project format version 1 remains in use. Existing 1.1.0 project documents remain
readable. A project using the new optional title decoration members requires the
current reader; 1.1.0 readers refuse those members rather than silently discarding
them. Project files do not embed media or restore execution permissions.

The prompt pipeline supports Windows, Linux and WSL. Local perception execution
in this release supports only its qualified native Windows setup. The editor,
Production import, assembly, native rendering and durable recovery retain their
existing Windows and trusted-local-host requirements. This release does not extend
those features to Linux or WSL. Models and dependencies are never downloaded or
installed automatically; provider permission and costs remain under user control.

---

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
