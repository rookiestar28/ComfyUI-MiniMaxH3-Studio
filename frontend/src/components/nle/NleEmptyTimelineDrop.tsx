import { useEffect, useRef, useState, useSyncExternalStore } from "react";

import type { NleAuthoringStateV2 } from "../../contracts/authoringWorkbenchCodec";
import type { NleBinInsertChannel } from "../../runtime/nleBinInsertChannel";
import {
  admitTimelineInsert,
  commandForGesture,
  NLE_GESTURE_IDLE,
  reduceNleTimelineGesture,
  type NleTimelineGestureState,
} from "../../runtime/nleTimelineGesture";
import {
  contentXFromClientX,
  frameFromX,
  LANE_LEAD_IN_PX,
  trackHeaderWidthPx,
} from "../../runtime/timelineGeometry";
import type { AuthoringIntent } from "../../state/authoringViewState";
import type { Locale } from "../../i18n/catalog";
import { build, freshIdentifier } from "./nleCommandBuilders";
import { fill, nleCopy } from "./nleCopy";
import { moveRefusalText } from "./nleTimelineSurface";

const ROW_HEIGHT = 56;
const COARSE_QUERY = "(pointer: coarse)";
function subscribePointer(notify: () => void) {
  const query = window.matchMedia?.(COARSE_QUERY);
  query?.addEventListener("change", notify);
  return () => query?.removeEventListener("change", notify);
}
const readCoarse = () => window.matchMedia?.(COARSE_QUERY).matches ?? false;

export function NleEmptyTimelineDrop({
  authoring,
  busy,
  channel,
  locale,
  onIntent,
}: {
  authoring: NleAuthoringStateV2;
  busy: boolean;
  channel: NleBinInsertChannel;
  locale: Locale;
  onIntent(intent: AuthoringIntent): Promise<void>;
}) {
  const text = nleCopy(locale).timeline;
  const coarse = useSyncExternalStore(
    subscribePointer,
    readCoarse,
    () => false,
  );
  const header = trackHeaderWidthPx(coarse);
  const origin = header + LANE_LEAD_IN_PX;
  const grid = useRef<HTMLDivElement>(null);
  const draft = useRef<NleTimelineGestureState>(NLE_GESTURE_IDLE);
  const [preview, setPreview] =
    useState<NleTimelineGestureState>(NLE_GESTURE_IDLE);
  const [notice, setNotice] = useState("");
  const tracks = [...authoring.tracks].sort((a, b) => a.order - b.order);
  const mappingKey = `empty:${header}:${authoring.editCapacityFrames}`;
  const latest = useRef({ authoring, busy, tracks, onIntent, text });
  latest.current = { authoring, busy, tracks, onIntent, text };
  useEffect(() => {
    const element = grid.current;
    if (element === null || typeof ResizeObserver === "undefined") return;
    let width = element.getBoundingClientRect().width;
    const observer = new ResizeObserver(() => {
      const nextWidth = element.getBoundingClientRect().width;
      if (nextWidth !== width && draft.current.phase === "dragging")
        channel.cancel(
          "mapping_changed",
          draft.current.draft.pointerId ?? undefined,
        );
      width = nextWidth;
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [channel]);
  useEffect(() => {
    let ownedPointer: number | null = null;
    const unsubscribe = channel.subscribe((event) => {
      const current = latest.current;
      if (event.type === "begin") {
        const captured = event.authority;
        const state = current.authoring;
        // CRITICAL: empty authoring has no render snapshot. Admit only its actual catalog/CAS;
        // substituting a render identity silently targets a different or stale workspace.
        if (
          current.busy ||
          grid.current === null ||
          captured.workspaceHandle !== state.workspaceHandle ||
          captured.workspaceRevision !== state.workspaceRevision ||
          captured.timelineRevision !== state.timelineRevision ||
          captured.timelineFingerprint !== state.timelineFingerprint ||
          captured.authoringFingerprint !== state.authoringFingerprint ||
          !state.assets.some((asset) => asset.assetId === captured.assetId)
        ) {
          channel.cancel("authority_unavailable", event.pointerId);
          return;
        }
        const next = reduceNleTimelineGesture(draft.current, {
          type: "begin_insert_from_bin",
          identity: { ...captured, mappingKey, clipIds: [] },
          pointerId: event.pointerId,
          originClientX: event.clientX,
          originClientY: event.clientY,
          pixelsPerFrame: 1,
          assetId: captured.assetId,
          durationFrames: captured.durationFrames,
        });
        if (next === draft.current || next.phase !== "dragging") {
          channel.cancel("gesture_busy", event.pointerId);
          return;
        }
        ownedPointer = event.pointerId;
        draft.current = next;
        return;
      }
      const active = draft.current;
      if (
        active.phase !== "dragging" ||
        active.draft.insert === null ||
        active.draft.pointerId !== event.pointerId
      )
        return;
      if (event.type === "cancel") {
        ownedPointer = null;
        draft.current = NLE_GESTURE_IDLE;
        setPreview(NLE_GESTURE_IDLE);
        setNotice("");
        return;
      }
      const element = grid.current;
      if (element === null) return;
      const rect = element.getBoundingClientRect();
      const x =
        contentXFromClientX(event.clientX, rect.left, header) +
        element.scrollLeft;
      const inside =
        event.clientX >= rect.left + origin &&
        event.clientX < rect.right &&
        event.clientY >= rect.top &&
        event.clientY < rect.bottom &&
        x >= 0 &&
        x < current.authoring.editCapacityFrames;
      const row = Math.floor(
        (event.clientY - rect.top + element.scrollTop) / ROW_HEIGHT,
      );
      const track = inside ? current.tracks[row] : undefined;
      const asset = current.authoring.assets.find(
        (a) => a.assetId === active.draft.insert!.assetId,
      );
      const frame = frameFromX(x, 0, 1, current.authoring.editCapacityFrames);
      const duration = Math.min(
        active.draft.insert.sourceDurationFrames,
        current.authoring.editCapacityFrames - frame,
      );
      const admission =
        track === undefined ||
        asset === undefined ||
        current.authoring.clips.length >= 128
          ? {
              admitted: false,
              reason: "target_exhaustion" as const,
              trackId: null,
            }
          : admitTimelineInsert({
              asset,
              startFrame: frame,
              durationFrames: duration,
              timelineDurationFrames: current.authoring.editCapacityFrames,
              tracks: current.tracks,
              clips: current.authoring.clips,
              targetTrackId: track.trackId,
            });
      const updated = reduceNleTimelineGesture(active, {
        type: "update_insert_from_bin",
        clientX: event.clientX,
        clientY: event.clientY,
        targetFrame: frame,
        targetTrackId: admission.trackId,
        durationFrames: duration,
        admitted: admission.admitted,
        reason: admission.reason,
      });
      draft.current = updated;
      if (event.type === "move") {
        if (updated.phase === "dragging" && updated.draft.movementPx >= 4) {
          setPreview(updated);
          setNotice(
            admission.admitted
              ? fill(current.text.insertDraft, { frame })
              : fill(current.text.insertRefused, {
                  reason: moveRefusalText(
                    current.text.moveRefusals,
                    admission.reason,
                  ),
                }),
          );
        }
        return;
      }
      ownedPointer = null;
      const next = reduceNleTimelineGesture(updated, {
        type: "release",
        requestId: `insert-${Date.now()}-${event.pointerId}`,
      });
      draft.current = NLE_GESTURE_IDLE;
      setPreview(NLE_GESTURE_IDLE);
      const command = commandForGesture(next);
      if (command?.kind !== "insert_from_bin") return;
      const captured = next.phase === "submitting" ? next.draft.identity : null;
      if (captured === null) return;
      void current.onIntent({
        action: "apply_timeline_commands",
        capturedTimeline: {
          workspaceHandle: captured.workspaceHandle,
          workspaceRevision: captured.workspaceRevision,
          timelineRevision: captured.timelineRevision,
          timelineFingerprint: captured.timelineFingerprint,
          authoringFingerprint: captured.authoringFingerprint,
        },
        commands: [
          build.insertAssetClip({
            clipId: freshIdentifier(current.authoring, "clip"),
            trackId: command.targetTrackId,
            assetId: command.assetId,
            startFrame: command.startFrame,
            durationFrames: command.durationFrames,
            sourceStartFrame: 0,
            text: null,
          }),
        ],
      });
    });
    return () => {
      if (ownedPointer !== null)
        channel.cancel("receiver_rebound_or_unmounted", ownedPointer);
      unsubscribe();
    };
  }, [
    channel,
    busy,
    mappingKey,
    authoring.workspaceHandle,
    authoring.workspaceRevision,
    authoring.timelineRevision,
    authoring.timelineFingerprint,
    authoring.authoringFingerprint,
  ]);
  const insert = preview.phase === "dragging" ? preview.draft.insert : null;
  const ghostRow =
    insert === null
      ? -1
      : tracks.findIndex((track) => track.trackId === insert.targetTrackId);
  return (
    <>
      <div
        ref={grid}
        className="h3-nle-empty-drop"
        data-h3-nle-empty-drop=""
        data-h3-nle-authoring-tracks=""
        data-h3-nle-lane-origin-px={origin}
        data-h3-nle-pixels-per-frame="1"
        data-h3-nle-view-start="0"
        style={{ maxHeight: ROW_HEIGHT * 4 }}
      >
        <div
          className="h3-nle-empty-drop-axis"
          style={{
            width: origin + authoring.editCapacityFrames,
            height: tracks.length * ROW_HEIGHT,
          }}
        >
          {tracks.map((track, index) => (
            <div
              key={track.trackId}
              className="h3-nle-empty-drop-row"
              data-h3-nle-track={track.trackId}
              data-h3-nle-track-kind={track.kind}
              data-kind={track.kind}
              style={{ top: index * ROW_HEIGHT, height: ROW_HEIGHT }}
            >
              <span
                className="h3-nle-empty-drop-header"
                style={{ width: header }}
              >
                {text.trackKind[track.kind]}
              </span>
              <span
                aria-hidden="true"
                className="h3-nle-empty-drop-zero"
                style={{ left: origin }}
              />
            </div>
          ))}
          {insert === null || ghostRow < 0 ? null : (
            <div
              className="h3-nle-empty-drop-ghost"
              data-h3-nle-insert-ghost=""
              aria-hidden="true"
              style={{
                left: origin + insert.targetFrame,
                width: insert.durationFrames,
                top: ghostRow * ROW_HEIGHT + 4,
                height: ROW_HEIGHT - 8,
              }}
            />
          )}
        </div>
      </div>
      <p className="h3-nle-note" role="status">
        {notice}
      </p>
    </>
  );
}
