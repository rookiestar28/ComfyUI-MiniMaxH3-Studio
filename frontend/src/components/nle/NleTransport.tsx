// M25-45: the monitor's transport -- one row of current/total timecode and five icon controls. It
// issues no M25-11 command and touches no revision or history: it is `local_transport` exactly as
// the range input it replaces was, and the fit/100% toggle and full screen are views of the same
// composition, never edits of it.
//
// M25-64 (row #25, R7): the timecode on the left, the step and play controls centred, and the view
// controls on the right, borderless, with a round Play. The view pair is its own icon group: one
// group cannot hold a centred group and a right-aligned one without `display: contents`, which
// would break the description's placement and resting box (B-M2561-07), both measured from the
// group's own rectangle.

import { useEffect, useRef, useState, type RefObject } from "react";
import { formatTimelineTimecode } from "../../runtime/timelineNavigation";
import type { Locale } from "../../i18n/catalog";
import { NleIconButton, NleIconGroup } from "./NleIconActions";
import { fill, nleCopy } from "./nleCopy";
import type { PictureView } from "./nleMonitorGeometry";

/** How long a refused or unsupported full-screen request stays announced. */
const FULLSCREEN_NOTICE_MS = 6_000;

function timecode(frame: number | null, fps: number): string {
  if (frame === null || !Number.isInteger(frame) || frame < 0)
    return "--:--:--:--";
  return formatTimelineTimecode(frame, fps);
}

export function NleTransport({
  locale,
  playing,
  controlsAvailable,
  currentFrame,
  lastFrame,
  fps,
  view,
  onView,
  onPlay,
  onPause,
  onStep,
  fullscreenTarget,
}: {
  locale: Locale;
  playing: boolean;
  controlsAvailable: boolean;
  currentFrame: number | null;
  lastFrame: number;
  fps: number;
  view: PictureView;
  onView(next: PictureView): void;
  onPlay(): void;
  onPause(): void;
  onStep(direction: -1 | 1): void;
  /** The owned R2 element full screen is requested on; never a foreign or host element. */
  fullscreenTarget: RefObject<HTMLElement | null>;
}) {
  const text = nleCopy(locale);
  const transport = text.monitor.transport;
  const [fullscreen, setFullscreen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined,
  );

  // IMPORTANT: the element's own flag is never the authority. The user leaves full screen with
  // Escape, with F11, or by the document losing it for reasons this component never sees, and a
  // button labelled "Leave full screen" that no longer can is worse than no button. The document's
  // event is the only fact; the state below is a mirror of it.
  useEffect(() => {
    const sync = () => {
      const target = fullscreenTarget.current;
      setFullscreen(target !== null && document.fullscreenElement === target);
    };
    sync();
    document.addEventListener("fullscreenchange", sync);
    return () => document.removeEventListener("fullscreenchange", sync);
  }, [fullscreenTarget]);

  // IMPORTANT (M25-45): while this monitor owns the screen, Escape leaves full screen and goes no
  // further. The listener is on the document in the capture phase because entering full screen can
  // move focus off the dialog's subtree entirely -- Chromium focuses the fullscreen element, which
  // carries no tabindex -- and the dialog's own Escape handler then never sees the key, so the
  // first Escape would close the workspace behind the screen the user was looking at.
  //
  // GUARD (B-M2545-31): armed for the component's whole life, and it decides from the document --
  // never gated on `fullscreen`, the mirrored state. The mirror is one React commit behind the
  // truth: `requestFullscreen` resolves, the browser sets `document.fullscreenElement`, and
  // `fullscreenchange` plus its render land a task later. Gating the effect on the mirror left the
  // guard unarmed for exactly the first Escape after full screen was granted -- the one this guard
  // exists for, since that is when focus has just been moved off the dialog's subtree. Measured
  // directly: the key arrived, no handler ran (the instrumented handler recorded nothing at all),
  // and the screen stayed. The condition below is the one the mirror's own comment above already
  // names as the only fact. Do not "simplify" this back into a `fullscreen`-gated effect, and do
  // not consume Escape unconditionally either: a monitor that owns no screen must let the dialog
  // have its key, which `nleTransportFullscreen.test.tsx` asserts from both sides.
  useEffect(() => {
    const leave = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      const target = fullscreenTarget.current;
      if (target === null || document.fullscreenElement !== target) return;
      event.preventDefault();
      event.stopPropagation();
      // `?.()?.` on both: a browser without `exitFullscreen` returns undefined here, and calling
      // `.catch` on that would throw inside a capture-phase key handler.
      void document.exitFullscreen?.()?.catch(() => undefined);
    };
    document.addEventListener("keydown", leave, true);
    return () => document.removeEventListener("keydown", leave, true);
  }, [fullscreenTarget]);

  useEffect(
    () => () => {
      if (noticeTimer.current !== undefined) clearTimeout(noticeTimer.current);
    },
    [],
  );

  const announce = (message: string) => {
    if (noticeTimer.current !== undefined) clearTimeout(noticeTimer.current);
    setNotice(message);
    noticeTimer.current = setTimeout(() => {
      noticeTimer.current = undefined;
      setNotice(null);
    }, FULLSCREEN_NOTICE_MS);
  };

  const timecodeLabel = fill(transport.timecodeLabel, {
    current: timecode(currentFrame, fps),
    total: timecode(lastFrame, fps),
  });

  const toggleFullscreen = () => {
    const target = fullscreenTarget.current;
    if (target === null) return;
    if (document.fullscreenElement === target) {
      // A rejected exit leaves the mirror alone: `fullscreenchange` still owns the truth.
      void document.exitFullscreen?.().catch(() => undefined);
      return;
    }
    if (typeof target.requestFullscreen !== "function") {
      announce(transport.fullscreenUnavailable);
      return;
    }
    // Refusal is bounded and silent for playback: the composition keeps running exactly as it was.
    void target
      .requestFullscreen()
      .catch(() => announce(transport.fullscreenRefused));
  };

  return (
    <div className="h3-nle-transport" data-h3-nle-transport="">
      {/* R7: below a 360 px row the total is hidden (CSS container query), and stays in the
          output's accessible name and tooltip. */}
      <output
        className="h3-nle-timecode"
        data-h3-nle-timecode=""
        aria-label={timecodeLabel}
        title={timecodeLabel}
      >
        <span data-h3-nle-timecode-part="current">
          {timecode(currentFrame, fps)}
        </span>
        <span aria-hidden="true" data-h3-nle-timecode-part="separator">
          /
        </span>
        <span data-h3-nle-timecode-part="total">
          {timecode(lastFrame, fps)}
        </span>
      </output>
      <NleIconGroup label={transport.label} control="transport">
        <NleIconButton
          icon="stepBack"
          label={text.monitor.previousFrame}
          description={transport.previousFrameDescription}
          hue="info"
          control="transport.step_back"
          disabled={!controlsAvailable}
          onActivate={() => onStep(-1)}
        />
        <NleIconButton
          icon={playing ? "pause" : "play"}
          label={playing ? text.monitor.pause : text.monitor.play}
          description={
            playing ? transport.pauseDescription : transport.playDescription
          }
          hue="ok"
          control={playing ? "transport.pause" : "transport.play"}
          disabled={!controlsAvailable}
          onActivate={playing ? onPause : onPlay}
        />
        <NleIconButton
          icon="stepForward"
          label={text.monitor.nextFrame}
          description={transport.nextFrameDescription}
          hue="info"
          control="transport.step_forward"
          disabled={!controlsAvailable}
          onActivate={() => onStep(1)}
        />
      </NleIconGroup>
      <NleIconGroup label={transport.viewLabel} control="transport_view">
        <NleIconButton
          icon="fit"
          label={view === "fit" ? transport.actual : transport.fit}
          description={
            view === "fit"
              ? transport.actualDescription
              : transport.fitDescription
          }
          hue="understand"
          control="transport.view"
          pressed={view === "actual"}
          disabled={false}
          onActivate={() => onView(view === "fit" ? "actual" : "fit")}
        />
        <NleIconButton
          icon={fullscreen ? "fullscreenExit" : "fullscreen"}
          label={fullscreen ? transport.fullscreenExit : transport.fullscreen}
          description={
            fullscreen
              ? transport.fullscreenExitDescription
              : transport.fullscreenDescription
          }
          hue="edit"
          control="transport.fullscreen"
          pressed={fullscreen}
          disabled={false}
          onActivate={toggleFullscreen}
        />
      </NleIconGroup>
      <span
        className="h3-nle-vh"
        role="status"
        aria-live="polite"
        data-h3-nle-status="fullscreen"
      >
        {notice ?? transport.view[view]}
      </span>
    </div>
  );
}
