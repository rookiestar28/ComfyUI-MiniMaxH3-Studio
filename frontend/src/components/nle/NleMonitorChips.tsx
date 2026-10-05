// M25-45: the monitor's status and embedded-audio state, in the chrome bar.
//
// IMPORTANT (master plan 5.2): they used to be two full-width text rows under the picture, and the
// picture area is the row they took the height from. They keep their identifiers, their live
// regions and their full text -- only their place changes -- because the status a user needs while
// something is wrong must stay readable, and the journeys assert these exact hooks.

import { useSyncExternalStore } from "react";
import type { Locale } from "../../i18n/catalog";
import type { NleMonitorStatus } from "../../runtime/nleWorkspaceRuntime";
import { fill, nleCopy } from "./nleCopy";
import type { NleMonitorStatusChannel } from "./nleMonitorChannel";

type Copy = ReturnType<typeof nleCopy>;

export function monitorStatusText(
  text: Copy,
  composition: NleMonitorStatus["composition"],
): string {
  if (composition.blocker === "cleanup_pending")
    return text.monitor.status.cleanup_pending;
  if (composition.status === "blocked")
    return fill(text.monitor.status.blocked, {
      reason:
        text.monitor.blocker[composition.blocker ?? "source_unavailable"] ?? "",
    });
  return text.monitor.status[composition.status];
}

/** M25-64 (A64-6): the unavailable overlay's plain reason; `monitorStatusText` stays the status. */
export function monitorUnavailableReason(
  text: Copy,
  composition: NleMonitorStatus["composition"],
): string {
  if (composition.blocker === "cleanup_pending")
    return text.monitor.unavailableReason.cleanup_pending;
  return text.monitor.unavailableReason[
    composition.blocker ?? "source_unavailable"
  ];
}

export function NleMonitorChips({
  locale,
  channel,
}: {
  locale: Locale;
  channel: NleMonitorStatusChannel;
}) {
  const text = nleCopy(locale);
  const status = useSyncExternalStore(
    channel.subscribe,
    channel.snapshot,
    channel.snapshot,
  );
  return (
    <>
      {/* No composition is showing: the monitor's own fallback region says so where the user is
          looking, and a second status here would claim a state the monitor is not in. */}
      {status === null ? null : (
        <span
          className="h3-nle-status"
          role="status"
          aria-live="polite"
          data-h3-nle-status="monitor"
          title={monitorStatusText(text, status.composition)}
        >
          {monitorStatusText(text, status.composition)}
        </span>
      )}
      <AudioStatus locale={locale} status={status?.audio ?? null} />
    </>
  );
}

export function AudioStatus({
  locale,
  status,
}: {
  locale: Locale;
  status: NleMonitorStatus["audio"];
}) {
  const text = nleCopy(locale);
  const state = status?.state ?? "silent";
  const tone =
    state === "following"
      ? "ok"
      : state === "unavailable"
        ? "warn"
        : state === "seeking"
          ? "info"
          : "idle";
  const owner = status?.owner_clip_id
    ? fill(text.audio.owner, { clip: status.owner_clip_id })
    : text.audio.noOwner;
  return (
    <div
      className="h3-nle-audio"
      data-h3-nle-status="audio"
      data-h3-nle-audio-state={state}
      data-h3-nle-audio-reason={status?.reason ?? "closed"}
      aria-live="off"
      // The owner clip and the standing audio policy are the tooltip; only the state word is
      // visible, so the chrome bar never grows or wraps as the state changes during playback.
      title={`${owner} ${text.audio.policy}`}
    >
      <span
        className="h3-nle-chip"
        data-tone={tone}
        title={status?.reason ?? "closed"}
      >
        {text.audio.state[state]}
      </span>
      <span className="h3-nle-vh">{owner}</span>
      <span className="h3-nle-vh">{text.audio.policy}</span>
    </div>
  );
}
