// M25-63 (R9): the save indicator in the clip editor's top bar. It replaces the timeline footer's
// status sentence as the visible save state. Every branch of that sentence maps to one state here,
// in the same precedence; the sentence itself survives verbatim as the tooltip and the visually
// hidden live region, except that an accepted timeline now reads "Saved." instead of naming its
// revision. The revision number is carried only as an attribute value.

import { plainReason } from "../components/plainReasons";
import { nleCopy } from "../components/nle/nleCopy";
import type { Locale } from "../i18n/catalog";
import type { AuthoringViewState } from "../state/authoringViewState";

export type SaveIndicatorState =
  | "none"
  | "loading"
  | "saving"
  | "not_saved"
  | "checking"
  | "unknown"
  | "failed"
  | "refused"
  | "saved";

/** The glyph and colour role: nothing, a pending ring, a check, or a warning. */
export type SaveIndicatorTone = "none" | "busy" | "ok" | "warning";

export type SaveIndicatorModel = Readonly<{
  state: SaveIndicatorState;
  tone: SaveIndicatorTone;
  /** Short visible label; empty when nothing is shown. */
  label: string;
  /** The full sentence: the label's tooltip, and the empty timeline's note. */
  sentence: string;
  /** What the live region announces. */
  live: string;
  /** The accepted timeline revision currently shown, for `data-h3-nle-timeline-revision`. */
  revision: number | null;
}>;

const TONE: Readonly<Record<SaveIndicatorState, SaveIndicatorTone>> = {
  none: "none",
  loading: "busy",
  saving: "busy",
  checking: "busy",
  not_saved: "warning",
  unknown: "warning",
  failed: "warning",
  refused: "warning",
  saved: "ok",
};

const LABEL = {
  none: null,
  loading: "loading",
  saving: "saving",
  not_saved: "notSaved",
  checking: "checking",
  unknown: "unknown",
  failed: "failed",
  refused: "refused",
  saved: "saved",
} as const satisfies Record<
  SaveIndicatorState,
  keyof ReturnType<typeof nleCopy>["save"] | null
>;

export function saveIndicatorModel(
  authoring: AuthoringViewState,
  locale: Locale,
): SaveIndicatorModel {
  const text = nleCopy(locale);
  const history =
    "timelineHistory" in authoring ? authoring.timelineHistory : undefined;
  const historyV2 =
    "timelineHistoryV2" in authoring ? authoring.timelineHistoryV2 : undefined;
  const receipt =
    "lastTimelineReceipt" in authoring
      ? authoring.lastTimelineReceipt
      : undefined;
  const receiptV2 =
    "lastTimelineReceiptV2" in authoring
      ? authoring.lastTimelineReceiptV2
      : undefined;
  // IMPORTANT: an accepted transaction drops obsolete history before its refresh, and its receipt
  // already owns the new snapshot (NleWorkspace's `monitorSnapshot`), so the shown revision falls
  // back to the receipt's; otherwise every edit would blink the indicator to nothing.
  const hasV2Authority = historyV2 !== undefined || receiptV2 !== undefined;
  const shownRevision = hasV2Authority
    ? (historyV2?.authoring.timelineRevision ??
      receiptV2?.authoring.timelineRevision ??
      null)
    : (history?.snapshot.timelineRevision ??
      receipt?.snapshot.timelineRevision ??
      null);
  const rejection = hasV2Authority
    ? (historyV2?.rejection ?? null)
    : (history?.rejection ?? null);
  const [state, sentence]: [SaveIndicatorState, string] =
    authoring.status === "absent" || authoring.status === "released"
      ? ["none", text.timeline.absent]
      : authoring.status === "loading"
        ? ["loading", text.timeline.loading]
        : authoring.status === "pending"
          ? ["saving", text.timeline.pending]
          : authoring.status === "conflict"
            ? ["not_saved", text.timeline.conflict]
            : authoring.status === "error" &&
                authoring.reason === "outcome_unknown"
              ? // R2-F1: the read that reconciles an unknown outcome is a fact of its own; a
                // pending or failed re-read is never described as a completed one.
                authoring.outcomeUnknown?.reconciliation === "failed"
                ? ["unknown", text.timeline.outcomeUnknownReadFailed]
                : ["checking", text.timeline.outcomeUnknownReading]
              : authoring.status === "error" || authoring.status === "gone"
                ? [
                    "failed",
                    plainReason(locale, "editFailed", authoring.reason),
                  ]
                : rejection !== null
                  ? [
                      "refused",
                      plainReason(locale, "editRejected", rejection.code),
                    ]
                  : shownRevision !== null
                    ? ["saved", text.save.savedLive]
                    : ["none", ""];
  const labelKey = LABEL[state];
  return {
    state,
    tone: TONE[state],
    label: labelKey === null ? "" : text.save[labelKey],
    sentence,
    live:
      state === "saved"
        ? sentence
        : receipt !== undefined || receiptV2 !== undefined
          ? `${sentence} ${text.timeline.receipt}`
          : sentence,
    revision: shownRevision,
  };
}
