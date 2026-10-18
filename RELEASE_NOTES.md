# Release 1.3.2

## App Mode output verification

- App Mode runs finish on current ComfyUI versions. ComfyUI now annotates each saved video entry
  with media details and, when its asset system is enabled, an asset identifier. 1.3.0 accepted
  only the exact earlier entry shape, so it ignored the saved video, and the finished run stayed at
  "Verifying the exact saved output" indefinitely.
- Saved video entries are read by their file name, folder and output type; any other detail is
  ignored and never stored.
- If ComfyUI's saved-video message is missing or not recognized when the run finishes, the sidebar
  reads that run's ComfyUI history for up to about 16 seconds. Exactly one saved video completes
  the run through the same verification as before. Otherwise the run ends with the existing "could
  not be verified safely" message and **Continue with native nodes**; the saved file is left
  unchanged and nothing is queued again. A saved-video message that arrives while the sidebar is
  still waiting still completes the run.

## Reference limits

- The Reference Registry, reference normalization and reference-set authoring refuse more than
  three audio tracks in total, including video soundtracks. The Registry node refuses before
  generation starts; authoring refuses when a source is added or restored. Before, canvas graphs
  could pass up to six audio tracks, and only the native adapter refused them when the run reached
  it. The sidebar limits and the native node are unchanged.

## Developer notes

- Tests decode the current ComfyUI saved-output shape. A model-free host row decodes the host's real
  saved-output message and history record. The weight-backed sample also checks that the sidebar
  reaches its finished state.

## Upgrade and limits

Replace the complete package, restart ComfyUI and refresh the browser. Project, receipt and settings
formats and the other public data formats are unchanged from 1.3.0. A graph that passes more than
three audio tracks in total to the reference nodes was already refused by the native adapter; it is
now refused before generation starts, with a message naming the audio limit.

---

# Release 1.3.0

## Reference generation

- Ref2VA now executes linked image, video and audio references from the sidebar and bundled
  Reference graphs. The Reference Registry accepts the individual numbered inputs created by
  these graphs while preserving callers that provide grouped reference values.
- Select up to 9 images, 3 videos and 3 standalone audio references. The combined limit is
  12 files and 3 audio tracks, including soundtracks taken from videos. At least one image or
  video is required in the sidebar. Video soundtracks are included for all selected videos or
  excluded for all; a selection that exceeds the combined limits is refused before execution.
- Reorder or remove individual references with keyboard-accessible controls. Source chips and
  final labels show their order before graph application, and each new loader occupies its own
  row. Paired soundtracks keep their video identity; standalone audio numbering stays independent.
- Ref2VA duration hints are guidance for preparing inputs. Selecting canvas nodes does not read
  their media files or verify their duration.

## Sequence assembly

- New assemblies default to **Original size (24 fps)**: the segments' common original size,
  24 fps video and 32 kHz stereo audio, with one aggregate video encode. Segments with mismatched
  sizes are refused. The previous **512 × 512 (30 fps)** format with 48 kHz stereo sound remains
  selectable before assembly.
- View a per-cut report after assembly. **smooth**, **visible change** and **not measured** are
  bounded measurements of the segment boundary, not guarantees of visual/audio continuity.
  Assembly still joins independently generated segments and does not create new transition frames.
- A sequence that finishes before the media runtime activates now offers assembly after the
  runtime becomes available and Production is read again. Runtime checks remain non-blocking.
- English assembly inputs show segment and cut counts without fixed plural nouns. Both Chinese
  translations keep their existing meaning.

## Developer diagnostics

- Browser test failure evidence includes bounded, sanitized presentation timings for the current
  interaction phase. Reports omit prompt, media, path, URL and credential content. This improves
  diagnosis; no presentation-time budget or performance guarantee changes.
- Frontend unit tests use a bounded worker pool so evidence collection keeps its original deadline.
- Sequence browser fixtures use the current assembly profile contract and cover unavailable,
  ready, running and completed states. Executable admission comments now state the integrity
  threat model and the limits of the Windows read pin; the mechanism is unchanged.
- Workspace maintenance tools bind removal to a reviewed manifest, reverify quarantined entries
  before resumed removal, and handle WSL links explicitly while refusing unproven reparse tags.

## Upgrade and limits

Replace the complete package, restart ComfyUI and refresh the browser. Save editable projects and
preserve media separately before updating. Project format version 1 and its existing title
decoration compatibility remain unchanged. Assembly receipts use the new version 2 profile/cut
report contract; older software that understands only version 1 refuses the new receipts.

Use the current bundled reference examples. For an older saved API prompt, connect each reference
to its own numbered Registry input instead of placing multiple links inside one input value.

The editor's final render remains 1920 × 1080 at 24 fps and is separate from sequence assembly.
The supported FFmpeg build, native Windows media/recovery requirements, manual tool setup and
local-only perception setup are unchanged. Longer-video sequence generation remains limited to
text-to-video plans; this release does not enable continuous-sequence generation or qualify new
hosts or models.

---

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
