// M25-44 (one NLE): the `clip_editor` tab's read-only project summary under the launcher. The
// compact sidebar editor it replaces was also the only home of the Authoring workspace's lifecycle
// actions, so they live here: start from the current Context, refresh, and a labelled two-step
// release. Nothing here edits the timeline; editing happens in the full editor only.

import { useEffect, useState } from "react";

import type {
  AuthoringIntent,
  AuthoringViewState,
} from "../../state/authoringViewState";
import type { NleAuthoringStateV2 } from "../../contracts/authoringWorkbenchCodec";
import { nominalFps } from "../../runtime/nleProjectSettings";
import { formatTimelineTimecode } from "../../runtime/timelineNavigation";
import { plainReason } from "../plainReasons";
import type { Locale } from "../../i18n/catalog";
import { NleIconButton, NleIconGroup } from "./NleIconActions";
import { fill, nleCopy } from "./nleCopy";

export function NleProjectSummary({
  locale,
  authoring,
  contextAvailable,
  overlayOpen,
  onStart,
  onIntent,
}: {
  locale: Locale;
  authoring: AuthoringViewState;
  contextAvailable: boolean;
  overlayOpen: boolean;
  onStart(): void;
  onIntent(intent: AuthoringIntent): void;
}) {
  const text = nleCopy(locale).summary;
  const [confirming, setConfirming] = useState(false);
  const projection =
    "projection" in authoring ? authoring.projection : undefined;
  const history =
    "timelineHistory" in authoring ? authoring.timelineHistory : undefined;
  const historyV2 =
    "timelineHistoryV2" in authoring ? authoring.timelineHistoryV2 : undefined;
  const receiptV2 =
    "lastTimelineReceiptV2" in authoring
      ? authoring.lastTimelineReceiptV2
      : undefined;
  const authoringV2: NleAuthoringStateV2 | undefined =
    historyV2?.authoring ?? receiptV2?.authoring;
  const snapshot =
    historyV2 !== undefined
      ? (historyV2.renderSnapshot ?? undefined)
      : receiptV2 !== undefined
        ? (receiptV2.renderSnapshot ?? undefined)
        : history?.snapshot;
  const busy = authoring.status === "pending" || authoring.status === "loading";
  const workspace = projection?.workspaceHandle ?? null;
  const currentTimelineRevision =
    snapshot?.timelineRevision ?? authoringV2?.timelineRevision;
  // A pending confirmation belongs to the workspace it was asked about.
  useEffect(() => setConfirming(false), [workspace, overlayOpen]);
  const noWorkspace =
    authoring.status === "absent" ||
    authoring.status === "released" ||
    authoring.status === "gone" ||
    (authoring.status === "error" && projection === undefined);

  let sentence: string;
  if (authoring.status === "absent") sentence = text.absent;
  else if (authoring.status === "released") sentence = text.released;
  else if (authoring.status === "gone") sentence = text.gone;
  else if (authoring.status === "loading" && projection === undefined)
    sentence = text.loading;
  else if (authoring.status === "error" && projection === undefined)
    sentence = plainReason(locale, "editFailed", authoring.reason);
  else if (currentTimelineRevision === undefined) sentence = text.notLoaded;
  else
    sentence = fill(text.revision, {
      revision: currentTimelineRevision,
    });

  const durationFrames = Number(snapshot?.output.durationFrames);
  const fps = nominalFps(snapshot?.output.frameRate);
  const hasActions = noWorkspace || projection !== undefined;

  return (
    <section
      className="h3-nle-summary"
      aria-label={text.title}
      data-h3-nle-summary={authoring.status}
    >
      <h3>{text.title}</h3>
      <p role="status" aria-live="polite">
        {sentence}
      </p>
      {snapshot !== undefined || authoringV2 !== undefined ? (
        <p data-h3-nle-summary-counts="">
          {fill(text.counts, {
            tracks: authoringV2?.tracks.length ?? snapshot?.tracks.length ?? 0,
            clips: authoringV2?.clips.length ?? snapshot?.clips.length ?? 0,
          })}
          {authoringV2 !== undefined
            ? ` · ${fill(text.contentExtent, {
                timecode: formatTimelineTimecode(
                  authoringV2.contentEndExclusive,
                  fps,
                ),
              })} · ${fill(text.editCapacity, {
                timecode: formatTimelineTimecode(
                  authoringV2.editCapacityFrames,
                  fps,
                ),
              })}`
            : Number.isSafeInteger(durationFrames) && durationFrames >= 0
              ? ` · ${fill(text.duration, {
                  timecode: formatTimelineTimecode(durationFrames, fps),
                })}`
              : ""}
        </p>
      ) : null}
      {authoring.status === "pending" ? <p>{text.pending}</p> : null}
      {/* The owner's sidebar action-row rule (M25-41, M25-43): icon tiles on the shared column
          grid, the full label as the accessible name and the description, or the reason an action
          is unavailable, on hover and focus. They are lifecycle actions, never editing controls. */}
      {hasActions ? (
        <NleIconGroup label={text.actions}>
          {noWorkspace ? (
            <NleIconButton
              icon="project"
              hue="production"
              control="start-authoring"
              kind="action"
              label={text.start}
              description={
                contextAvailable ? text.describe.start : text.startUnavailable
              }
              disabled={busy || !contextAvailable}
              onActivate={onStart}
            />
          ) : null}
          {projection !== undefined ? (
            <NleIconButton
              icon="refresh"
              hue="info"
              control="refresh-authoring"
              kind="action"
              label={text.refresh}
              description={text.describe.refresh}
              disabled={busy}
              onActivate={() =>
                onIntent({
                  action:
                    historyV2 !== undefined ||
                    receiptV2 !== undefined ||
                    history !== undefined
                      ? "read_timeline_history"
                      : "read_projection",
                })
              }
            />
          ) : null}
          {projection !== undefined && !confirming ? (
            <NleIconButton
              icon="release"
              hue="audit"
              control="release-authoring"
              kind="action"
              label={text.release}
              description={
                overlayOpen ? text.releaseWhileOpen : text.describe.release
              }
              disabled={busy || overlayOpen}
              onActivate={() => setConfirming(true)}
            />
          ) : null}
          {projection !== undefined && confirming ? (
            <>
              <NleIconButton
                icon="confirm"
                hue="audit"
                control="confirm-release-authoring"
                kind="action"
                label={text.confirmRelease}
                description={
                  overlayOpen
                    ? text.releaseWhileOpen
                    : text.describe.confirmRelease
                }
                disabled={busy || overlayOpen}
                onActivate={() => {
                  setConfirming(false);
                  onIntent({ action: "release_workspace" });
                }}
              />
              <NleIconButton
                icon="keep"
                hue="ok"
                control="keep-authoring"
                kind="action"
                label={text.keep}
                description={text.describe.keep}
                disabled={busy}
                onActivate={() => setConfirming(false)}
              />
            </>
          ) : null}
        </NleIconGroup>
      ) : null}
      {!noWorkspace || contextAvailable ? null : <p>{text.startUnavailable}</p>}
    </section>
  );
}
