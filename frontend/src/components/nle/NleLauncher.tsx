// M25-16: the single entry to the full editor, rendered only inside the active `clip_editor`
// panel, plus the status region shown when the overlay capability is unsupported. Neither renders
// anything for an audio surface. M25-44 (one NLE): the unsupported state no longer falls back to
// a compact editor, so the region carries the surface status alone.

import { useEffect, useId, useRef } from "react";
import type { Locale } from "../../i18n/catalog";
import type { NleSurfaceState } from "../../state/nleWorkspaceState";
import { nleCopy } from "./nleCopy";
import { focusOpenOverlay, returnFocusAfterClose } from "./NleOverlay";

export function NleLauncher({
  locale,
  surface,
  supported,
  onOpen,
}: {
  locale: Locale;
  surface: NleSurfaceState;
  supported: boolean;
  onOpen(): void;
}) {
  const text = nleCopy(locale);
  const statusId = useId();
  const unsupported = !supported || surface.status === "compact_unsupported";
  // GUARD (M25-20 `overlay_close_return_focus.capability_or_mount_failure`): a refusal that
  // answers the user's own open gesture keeps the launcher mounted, so the control the gesture
  // came from is still there for focus to return to, and returns focus to it once, on the
  // transition. Rendering the bare fallback here instead unmounts the focused button, nothing
  // ever runs `returnFocusAfterClose` for the synchronous capability refusal (no overlay was
  // mounted to run it), and keyboard focus is dropped on `document.body` -- the session's own
  // policy says the launcher is this reason's destination, and the session still accepts a
  // later open from `compact_unsupported`, which needs a control to come from. A host that is
  // unsupported before any gesture (`lastCloseReason` null) keeps the fallback region alone.
  const refusedGesture =
    surface.status === "compact_unsupported" &&
    surface.lastCloseReason === "capability_or_mount_failure";
  const previousStatus = useRef(surface.status);
  useEffect(() => {
    const entered =
      previousStatus.current !== "compact_unsupported" &&
      surface.status === "compact_unsupported";
    previousStatus.current = surface.status;
    if (entered && surface.lastCloseReason === "capability_or_mount_failure")
      returnFocusAfterClose("capability_or_mount_failure");
  }, [surface.status, surface.lastCloseReason]);
  const fallback = (
    <div
      className="h3-nle-fallback"
      role="region"
      aria-label={text.heading}
      data-h3-nle-unavailable="overlay_v1"
    >
      <p
        id={statusId}
        data-h3-nle-status="surface"
        role="status"
        aria-live="polite"
      >
        {text.unsupported}
      </p>
    </div>
  );
  if (unsupported && !refusedGesture) return fallback;
  const busy = surface.status === "opening";
  return (
    <div className="h3-nle-launcher">
      <button
        type="button"
        data-h3-nle-entry="open"
        data-h3-focus-key="nle-open-overlay"
        aria-busy={busy || undefined}
        aria-describedby={unsupported ? statusId : undefined}
        disabled={busy}
        onClick={() => {
          onOpen();
          // GUARD (M25-21 B3-D11): a repeat open while expanded is idempotent -- one root, one
          // generation -- and returns the user to that dialog. This launcher sits behind the
          // modal, so leaving focus here strands the keyboard outside the Tab trap.
          if (surface.status === "expanded") focusOpenOverlay();
        }}
      >
        {busy ? text.launcherBusy : text.launcher}
      </button>
      {unsupported ? fallback : null}
    </div>
  );
}
