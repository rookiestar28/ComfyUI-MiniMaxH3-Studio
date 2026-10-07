// M25-63 (R9): the top bar's save indicator. One pure model maps every branch the retired footer
// status had to a state, a short label, the full sentence and the live-region text; the revision
// number leaves the visible text and survives only as an exact attribute value.

import { describe, expect, it } from "vitest";

import { plainReason } from "../src/components/plainReasons";
import { fill, nleCopy } from "../src/components/nle/nleCopy";
import type { TimelineReceipt } from "../src/contracts/authoringWorkbenchCodec";
import type { Locale } from "../src/i18n/catalog";
import {
  saveIndicatorModel,
  type SaveIndicatorState,
} from "../src/runtime/nleSaveIndicator";
import type { AuthoringViewState } from "../src/state/authoringViewState";
import { SMOKE_SHAPE, authoringReady } from "./support/nleWorkspaceFixture";

const LOCALES: readonly Locale[] = ["en", "zh-TW", "zh-CN"];
const REVISION = 11;

type WithHistory = AuthoringViewState & { status: "ready" };

function ready(rejection: string | null = null): WithHistory {
  return authoringReady(SMOKE_SHAPE, {
    revision: REVISION,
    rejection: rejection === null ? null : { code: rejection },
  }) as WithHistory;
}

function receipt(state: WithHistory): TimelineReceipt {
  return {
    transactionId: "tx-1",
    snapshot: state.timelineHistory!.snapshot,
  } as unknown as TimelineReceipt;
}

function unknownOutcome(
  base: WithHistory,
  reconciliation: "pending" | "failed",
): AuthoringViewState {
  return {
    status: "error",
    projection: base.projection,
    reason: "outcome_unknown",
    timelineHistory: base.timelineHistory,
    outcomeUnknown: {
      requestId: "req-lost",
      transactionId: "tx-req-lost",
      expectedWorkspaceRevision: 3,
      expectedTimelineRevision: REVISION,
      expectedTimelineFingerprint: "f".repeat(64),
      reconciliation,
    },
  };
}

// The footer sentence as the workspace rendered it before M25-63 (NleWorkspace.tsx at b9198b93,
// `timelineStatus` plus the receipt suffix), kept here as the independent reference that the
// live region must still announce for every state except `saved`.
function footerSentence(authoring: AuthoringViewState, locale: Locale) {
  const text = nleCopy(locale);
  const history =
    "timelineHistory" in authoring ? authoring.timelineHistory : undefined;
  const snapshot = history?.snapshot;
  const status =
    authoring.status === "absent" || authoring.status === "released"
      ? text.timeline.absent
      : authoring.status === "loading"
        ? text.timeline.loading
        : authoring.status === "pending"
          ? text.timeline.pending
          : authoring.status === "conflict"
            ? text.timeline.conflict
            : authoring.status === "error" &&
                authoring.reason === "outcome_unknown"
              ? authoring.outcomeUnknown?.reconciliation === "failed"
                ? text.timeline.outcomeUnknownReadFailed
                : text.timeline.outcomeUnknownReading
              : authoring.status === "error" || authoring.status === "gone"
                ? plainReason(locale, "editFailed", authoring.reason)
                : history?.rejection
                  ? plainReason(locale, "editRejected", history.rejection.code)
                  : snapshot
                    ? fill(text.timeline.revision, {
                        revision: snapshot.timelineRevision,
                      })
                    : "";
  const withReceipt =
    "lastTimelineReceipt" in authoring &&
    authoring.lastTimelineReceipt !== undefined;
  return {
    status,
    live: `${status}${withReceipt ? ` ${text.timeline.receipt}` : ""}`,
  };
}

type Row = Readonly<{
  name: string;
  authoring: () => AuthoringViewState;
  state: SaveIndicatorState;
  revision: number | null;
}>;

const ROWS: readonly Row[] = [
  {
    name: "absent",
    authoring: () => ({ status: "absent" }),
    state: "none",
    revision: null,
  },
  {
    name: "released",
    authoring: () => ({ status: "released" }),
    state: "none",
    revision: null,
  },
  {
    name: "loading",
    authoring: () => ({ status: "loading" }),
    state: "loading",
    revision: null,
  },
  {
    name: "pending",
    authoring: () => ({ ...ready(), status: "pending" }),
    state: "saving",
    revision: REVISION,
  },
  {
    name: "pending after an accepted edit",
    authoring: () => {
      const base = ready();
      return { ...base, status: "pending", lastTimelineReceipt: receipt(base) };
    },
    state: "saving",
    revision: REVISION,
  },
  {
    name: "conflict",
    authoring: () => ({ ...ready(), status: "conflict" }),
    state: "not_saved",
    revision: REVISION,
  },
  {
    name: "outcome unknown, re-reading",
    authoring: () => unknownOutcome(ready(), "pending"),
    state: "checking",
    revision: REVISION,
  },
  {
    name: "outcome unknown, re-read failed",
    authoring: () => unknownOutcome(ready(), "failed"),
    state: "unknown",
    revision: REVISION,
  },
  {
    name: "error",
    authoring: () => ({
      status: "error",
      projection: ready().projection,
      reason: "transport_failed",
      timelineHistory: ready().timelineHistory,
    }),
    state: "failed",
    revision: REVISION,
  },
  {
    name: "error without a history",
    authoring: () => ({ status: "error", reason: "transport_failed" }),
    state: "failed",
    revision: null,
  },
  {
    name: "gone",
    authoring: () => ({ status: "gone", reason: "workspace_expired" }),
    state: "failed",
    revision: null,
  },
  {
    name: "rejection (stale timeline revision)",
    authoring: () => ready("stale_timeline_revision"),
    state: "refused",
    revision: REVISION,
  },
  {
    name: "rejection (invalid command)",
    authoring: () => ready("invalid_command"),
    state: "refused",
    revision: REVISION,
  },
  {
    name: "accepted snapshot",
    authoring: () => ready(),
    state: "saved",
    revision: REVISION,
  },
  {
    name: "accepted snapshot with a receipt",
    authoring: () => {
      const base = ready();
      return { ...base, lastTimelineReceipt: receipt(base) };
    },
    state: "saved",
    revision: REVISION,
  },
  {
    // IMPORTANT: an accepted transaction drops obsolete history before its refresh, and the
    // receipt already owns the new snapshot (NleWorkspace's `monitorSnapshot`). The edit was
    // saved; the indicator must not blink to nothing between the receipt and the refresh.
    name: "accepted receipt before the history refresh",
    authoring: () => {
      const base = ready();
      return {
        status: "ready",
        projection: base.projection,
        lastTimelineReceipt: receipt(base),
      };
    },
    state: "saved",
    revision: REVISION,
  },
  {
    name: "ready before any history",
    authoring: () => ({ status: "ready", projection: ready().projection }),
    state: "none",
    revision: null,
  },
];

const LABELS: Readonly<
  Record<SaveIndicatorState, keyof ReturnType<typeof nleCopy>["save"] | null>
> = {
  none: null,
  loading: "loading",
  saving: "saving",
  not_saved: "notSaved",
  checking: "checking",
  unknown: "unknown",
  failed: "failed",
  refused: "refused",
  saved: "saved",
};

describe("saveIndicatorModel (R9)", () => {
  describe.each(LOCALES)("%s", (locale) => {
    it.each(ROWS)("$name -> $state", (row) => {
      const authoring = row.authoring();
      const model = saveIndicatorModel(authoring, locale);
      const text = nleCopy(locale);
      const footer = footerSentence(authoring, locale);
      expect(model.state).toBe(row.state);
      expect(model.revision).toBe(row.revision);
      const labelKey = LABELS[row.state];
      expect(model.label).toBe(labelKey === null ? "" : text.save[labelKey]);
      if (row.state === "saved") {
        // The saved state reads "Saved." and carries no revision in any text.
        expect(model.live).toBe(text.save.savedLive);
        expect(model.sentence).toBe(text.save.savedLive);
      } else {
        // Every other state announces the retired footer's sentence verbatim, so the readers of
        // the conflict, outcome-unknown, failed and refused sentences keep working.
        expect(model.live).toBe(footer.live);
        expect(model.sentence).toBe(footer.status);
      }
      // The revision number is never visible text, only the attribute value.
      for (const visible of [model.label, model.sentence, model.live])
        expect(visible).not.toContain(String(REVISION));
      expect(model.tone).toBe(
        row.state === "none"
          ? "none"
          : row.state === "saved"
            ? "ok"
            : ["loading", "saving", "checking"].includes(row.state)
              ? "busy"
              : "warning",
      );
    });
  });

  it("never shows a revision number in a label, in any state or locale", () => {
    for (const locale of LOCALES)
      for (const row of ROWS)
        expect(saveIndicatorModel(row.authoring(), locale).label).not.toMatch(
          /\d/,
        );
  });

  it("covers every state it can return", () => {
    const seen = new Set(
      ROWS.map((row) => saveIndicatorModel(row.authoring(), "en").state),
    );
    expect([...seen].sort()).toEqual(
      (Object.keys(LABELS) as SaveIndicatorState[]).sort(),
    );
  });
});
