# Project Files

Use **Project file** in Production, the Editor summary, or the editor's Project menu. A `.h3proj`
file preserves accepted editable project data: segment order and relationships, draft prompts,
planning script and storyboard, source roles, tracks, cuts, text, fonts, transforms, effects and
the audio settings supported by the current editor. Unapplied Inspector drafts and temporary
selection, playhead, zoom and layout are not included.

## Save And Open

**Save project** uses the browser's native save picker when available. **File written** means
the write and close completed for the captured revision. Edits made afterward remain unsaved.
**Download project** is also available. Browsers without the native picker use this fallback
automatically. **Download prepared** does not confirm that the browser wrote a file to disk.

**Open project** creates a new editable project. It does not replace your ComfyUI workflow or
submit a Context, generation or render. **Previous project** returns to the previous in-memory
view; it is not a backup and remains subject to workspace expiry. Export important work before
switching, reloading or restarting. Invalid, unsupported or oversized files leave current work
unchanged. The current format is version 1 and accepts at most 2 MiB of UTF-8 JSON.

## Media Is Separate

The file contains media identifiers and permitted timing and integrity facts, not media bytes,
host paths, URLs, credentials, execution permissions or an undo/redo history. Opening a file
does not read or restore media automatically. Missing media leaves the document editable;
operations that need a current source remain unavailable.

If a compatible video has already been explicitly retained in Settings, choose **Refresh retained
videos**, select it for the missing media row, then **Relink media**. Relinking checks the current
bytes and complete supported timing profile before creating a fresh source binding. It does not
generate video or start a preview. A changed or incompatible copy is refused. Retained media is
optional; Save and Open remain usable when recovery storage is disabled.

Cancelling a relink request cannot prove that the server did not already commit it. An
outcome-unknown message requires reopening the saved file as a new project before continuing.

## Privacy And Limits

Exported files include your draft prompts and editable text. Treat them as private documents
when sharing. Media must be preserved separately. Files are not instructions, plugins or a way
to authorize access to a source merely by naming it. The existing editor's track, clip, timing,
font and audio-profile limits still apply; opening a file does not enable unsupported audio
editing. There is no cloud synchronization or automatic project-file backup in this workflow.

## Optional Server Recovery

Durable recovery and retained videos require a trusted single-user Windows host reachable only
through loopback, fixed local NTFS storage, no permissive CORS and no public-origin override.
Linux host-path support does not enable this durable-storage qualification. A refused setup
leaves manual Save/Open available; it does not silently fall back to another storage location.

**Save draft recovery** is off by default. Enabling it stores draft prompts, titles, accepted
editable content and a bounded data-only undo/redo history on this trusted, single-user local
server. It does not enable the separate metadata or video-retention settings. Browser content
is not backed up in localStorage or IndexedDB. Only drafts admitted by the server are covered.

**Include retained video links** is a separate choice. It protects only already retained videos
that have been freshly verified and bound to this project. It neither copies unretained video
nor enables media retention. Missing media remains editable data and requires explicit relinking.

**Save recovery now** waits for a server acknowledgement. **Recovery saved** covers that exact
document revision, not later edits, a prepared download or an export. A failed write of an already
tracked server draft leaves that draft protected from workspace expiry; retry or explicitly
discard before replacing it. A transport or qualification failure does not prove that browser-only
edits reached the server. Visible
workspace renewal continues independently, and a hidden or disconnected browser is not the
authority for backend saves. Draft changes still waiting to reach the server may be lost.

After reconnecting or restarting, choose a recovery and **Restore as a new project**. This is
explicit: reconnect never loads sensitive draft content or starts generation, rendering or uploads.
Restored undo/redo uses fresh data ownership, not previous execution permissions or cursors.
Recoveries are limited to 16 per owner, 64 globally, and 10 MiB per snapshot, including up to
64 undo and 64 redo entries within an 8 MiB history budget. Closed, inactive recoveries expire
after seven days while recovery is enabled; active or unsaved projects are not removed by that
time limit. **Clear recovery** affects only the chosen inactive recovery, not exported files or
other projects. Disabling recovery keeps existing saved data until explicit clear or later expiry.

A process crash can lose edits without a durable acknowledgement. Coalescing targets a maximum
two-second scheduling delay under normal operation, in addition to actual storage time; this is
not a hard real-time, zero-loss or power-loss guarantee. Manual project files remain a separate
backup workflow.
