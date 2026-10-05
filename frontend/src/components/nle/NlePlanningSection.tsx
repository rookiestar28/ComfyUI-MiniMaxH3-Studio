// M25-36: the shared whole-video planning surface used by the compact Production
// workbench and the expanded NLE. Both callers receive the same session state and actions.

import { useState } from "react";

import type { SegmentationPolicy } from "../../contracts/productionPlanningCodec";
import type { Locale } from "../../i18n/catalog";
import {
  clampTargetSeconds,
  type NleWorkspaceState,
  type StoryboardShotDraft,
} from "../../state/nleWorkspaceState";
import {
  STORYBOARD_SCRIPT_MAX_CHARACTERS,
  splitStoryboardScript,
  storyboardScriptFromPrompt,
  type StoryboardScriptError,
} from "../../state/storyboardScript";
import { plainReason } from "../plainReasons";
import { NleIconButton, NleIconGroup } from "./NleIconActions";
import { fill, nleCopy } from "./nleCopy";
import type { NleWorkspaceBinding } from "./nleWorkspaceBinding";

const POLICIES: readonly SegmentationPolicy[] = [
  "auto_storyboard",
  "fixed_5",
  "fixed_10",
  "fixed_12",
  "fixed_15",
];

function tone(state: string): "ok" | "info" | "warn" | "danger" | "idle" {
  if (
    state === "succeeded" ||
    state === "finished" ||
    state === "ready" ||
    state === "reused"
  )
    return "ok";
  if (state === "failed" || state === "error" || state === "cancelled")
    return "danger";
  if (
    state.startsWith("paused") ||
    state === "held" ||
    state.startsWith("recovery") ||
    state.startsWith("current_segment")
  )
    return "warn";
  if (state === "idle") return "idle";
  return "info";
}

/** What the last script action did: the rows it wrote, or why it wrote none. */
type ScriptOutcome =
  | Readonly<{
      code: "script_split_timed" | "script_split_even";
      count: number;
    }>
  | Readonly<{ code: StoryboardScriptError | "script_context_empty" }>;

export function NlePlanningSection({
  locale,
  planning,
  readiness,
  workspaceFingerprint,
  enabled,
  actions,
  targetLabel,
  contextPromptText,
}: {
  locale: Locale;
  planning: NleWorkspaceState["planning"];
  readiness: NleWorkspaceState["readiness"];
  workspaceFingerprint: string | undefined;
  enabled: boolean;
  actions: NleWorkspaceBinding["actions"];
  targetLabel?: string;
  /** The current Context prompt, when the browser holds its text; a source for the script. */
  contextPromptText?: string;
}) {
  const text = nleCopy(locale);
  // The script is working text for this panel only: it is never sent, only the rows it yields.
  const [script, setScript] = useState("");
  const [scriptOutcome, setScriptOutcome] = useState<ScriptOutcome | null>(
    null,
  );
  const busy = ["preparing", "admitting", "proposing", "importing"].includes(
    planning.status,
  );
  const stale =
    planning.boundWorkspaceFingerprint !== null &&
    planning.boundWorkspaceFingerprint !== workspaceFingerprint;
  const projection = planning.projection;
  const proposal = projection?.proposal ?? null;
  const rows = planning.storyboardRows;
  const updateRow = (index: number, patch: Partial<StoryboardShotDraft>) => {
    const next = rows.map((row, position) =>
      position === index ? { ...row, ...patch } : row,
    );
    actions.setStoryboardRows(next);
  };
  const addRow = () => {
    const last = rows[rows.length - 1];
    const start = last ? last.endMilliseconds : 0;
    const next = [
      ...rows,
      {
        shotId: `shot-${rows.length + 1}`,
        ordinal: rows.length + 1,
        startMilliseconds: start,
        endMilliseconds: start + 5_000,
        text: "",
        hardBoundary: false,
      },
    ];
    actions.setStoryboardRows(next);
  };
  const removeRow = (index: number) => {
    const next = rows
      .filter((_, position) => position !== index)
      .map((row, position) => ({
        ...row,
        ordinal: position + 1,
        shotId: `shot-${position + 1}`,
      }));
    actions.setStoryboardRows(next);
  };
  const loadScriptFromContext = () => {
    const loaded = storyboardScriptFromPrompt(contextPromptText ?? "");
    if (loaded.length === 0) {
      setScriptOutcome({ code: "script_context_empty" });
      return;
    }
    setScript(loaded);
    setScriptOutcome(null);
  };
  const splitScript = () => {
    const result = splitStoryboardScript(script, planning.targetSeconds);
    if (!result.ok) {
      // A script that cannot be read leaves the rows as they are.
      setScriptOutcome({ code: result.code });
      return;
    }
    actions.setStoryboardRows(result.rows);
    setScriptOutcome({
      code:
        result.timing === "timed" ? "script_split_timed" : "script_split_even",
      count: result.rows.length,
    });
  };
  return (
    <section
      aria-label={text.sequence.title}
      data-h3-nle-planning-status={planning.status}
    >
      <h4>{text.sequence.title}</h4>
      <div className="h3-nle-form">
        <div className="h3-plan-fields">
          <label className="h3-plan-field">
            <span>{targetLabel ?? text.sequence.target}</span>
            <input
              type="number"
              min={4}
              max={60}
              step={1}
              value={planning.targetSeconds}
              data-h3-nle-control="planning.target_seconds"
              disabled={!enabled || busy}
              onChange={(event) =>
                actions.setTargetSeconds(
                  clampTargetSeconds(Number(event.currentTarget.value)),
                )
              }
            />
          </label>
          <label className="h3-plan-field">
            <span>{text.sequence.policy}</span>
            <select
              value={planning.policy}
              data-h3-nle-control="planning.policy"
              disabled={!enabled || busy}
              onChange={(event) =>
                actions.setPolicy(
                  event.currentTarget.value as SegmentationPolicy,
                )
              }
            >
              {POLICIES.map((policy) => (
                <option key={policy} value={policy}>
                  {text.sequence.policies[policy]}
                </option>
              ))}
            </select>
          </label>
        </div>
        <NleIconGroup label={text.sequence.planningActions}>
          <NleIconButton
            icon="layers"
            hue="info"
            control="planning.prepare_context"
            label={text.sequence.prepare}
            description={text.sequence.describe.prepare}
            disabled={!enabled || busy}
            onActivate={() => void actions.prepareContext()}
          />
          <NleIconButton
            icon="storyboard"
            hue="understand"
            control="planning.admit_canonical"
            label={text.sequence.admitCanonical}
            description={text.sequence.describe.admitCanonical}
            disabled={!enabled || busy || projection === null || stale}
            onActivate={() =>
              void actions.admitStoryboard("canonical_optimized_prompt")
            }
          />
          <NleIconButton
            icon="review"
            hue="edit"
            control="planning.review_storyboard"
            label={text.sequence.reviewStoryboard}
            description={text.sequence.describe.reviewStoryboard}
            pressed={planning.storyboardReviewOpen}
            disabled={!enabled || busy || projection === null || stale}
            onActivate={() =>
              actions.openStoryboardReview(!planning.storyboardReviewOpen)
            }
          />
          <NleIconButton
            icon="split"
            hue="audit"
            control="planning.propose"
            label={text.sequence.propose}
            description={text.sequence.describe.propose}
            disabled={
              !enabled ||
              busy ||
              projection === null ||
              projection.admission_id === null ||
              stale
            }
            onActivate={() => void actions.propose()}
          />
          <NleIconButton
            icon="import"
            hue="production"
            control="planning.approve_import"
            label={text.sequence.approveImport}
            description={text.sequence.describe.approveImport}
            disabled={
              !enabled ||
              busy ||
              proposal === null ||
              !proposal.importable ||
              stale
            }
            onActivate={() => void actions.approveAndImportPlan()}
          />
        </NleIconGroup>
        {stale ? (
          <p
            className="h3-nle-danger"
            role="status"
            data-h3-nle-status="planning-stale"
          >
            {text.sequence.stale}
          </p>
        ) : null}
        {projection !== null ? (
          <p className="h3-nle-note" data-h3-nle-status="planning-context">
            {fill(text.sequence.prepared, {
              revision: projection.workspace_revision,
            })}{" "}
            {fill(text.sequence.sourceAndTarget, {
              source: projection.source_duration_seconds,
              target: projection.target_seconds,
            })}
          </p>
        ) : null}
        {planning.storyboardReviewOpen ? (
          <div className="h3-nle-form" data-h3-nle-region="storyboard-review">
            <label>
              <span>{text.sequence.scriptLabel}</span>
              <textarea
                value={script}
                maxLength={STORYBOARD_SCRIPT_MAX_CHARACTERS}
                data-h3-nle-control="planning.script_text"
                disabled={busy}
                onChange={(event) => {
                  setScript(event.currentTarget.value);
                  setScriptOutcome(null);
                }}
              />
            </label>
            <p className="h3-nle-note">{text.sequence.scriptHint}</p>
            <NleIconGroup label={text.sequence.scriptActions}>
              <NleIconButton
                icon="scriptFromContext"
                hue="understand"
                control="planning.script_load_context"
                label={text.sequence.scriptLoadContext}
                description={text.sequence.describe.scriptLoadContext}
                disabled={busy || !contextPromptText}
                onActivate={loadScriptFromContext}
              />
              <NleIconButton
                icon="scriptSplit"
                hue="edit"
                control="planning.script_split"
                label={text.sequence.scriptSplit}
                description={text.sequence.describe.scriptSplit}
                disabled={busy || script.trim().length === 0}
                onActivate={splitScript}
              />
            </NleIconGroup>
            {scriptOutcome !== null ? (
              <p
                className={
                  "count" in scriptOutcome ? "h3-nle-note" : "h3-nle-danger"
                }
                role="status"
                data-h3-nle-status="planning-script"
                data-code={scriptOutcome.code}
              >
                {"count" in scriptOutcome
                  ? fill(
                      scriptOutcome.code === "script_split_timed"
                        ? text.sequence.scriptSplitTimed
                        : text.sequence.scriptSplitEven,
                      {
                        count: scriptOutcome.count,
                        target: planning.targetSeconds,
                      },
                    )
                  : text.sequence.scriptErrors[scriptOutcome.code]}
              </p>
            ) : null}
            <ol className="h3-nle-storyboard">
              {rows.map((row, index) => (
                <li key={row.shotId}>
                  <label>
                    <span>
                      {fill(text.sequence.shotText, { ordinal: row.ordinal })}
                    </span>
                    <input
                      type="text"
                      value={row.text}
                      onChange={(event) =>
                        updateRow(index, { text: event.currentTarget.value })
                      }
                    />
                  </label>
                  <div className="h3-nle-form-row">
                    <label>
                      <span>{text.sequence.shotStart}</span>
                      <input
                        type="number"
                        min={0}
                        step={1}
                        value={row.startMilliseconds}
                        onChange={(event) =>
                          updateRow(index, {
                            startMilliseconds: Math.max(
                              0,
                              Number(event.currentTarget.value) || 0,
                            ),
                          })
                        }
                      />
                    </label>
                    <label>
                      <span>{text.sequence.shotEnd}</span>
                      <input
                        type="number"
                        min={1}
                        step={1}
                        value={row.endMilliseconds}
                        onChange={(event) =>
                          updateRow(index, {
                            endMilliseconds: Math.max(
                              1,
                              Number(event.currentTarget.value) || 1,
                            ),
                          })
                        }
                      />
                    </label>
                    <label>
                      <span>{text.sequence.hardBoundary}</span>
                      <input
                        type="checkbox"
                        checked={row.hardBoundary}
                        onChange={(event) =>
                          updateRow(index, {
                            hardBoundary: event.currentTarget.checked,
                          })
                        }
                      />
                    </label>
                    <button type="button" onClick={() => removeRow(index)}>
                      {fill(text.sequence.removeRow, { ordinal: row.ordinal })}
                    </button>
                  </div>
                </li>
              ))}
            </ol>
            <NleIconGroup label={text.sequence.reviewActions}>
              <NleIconButton
                icon="add"
                hue="info"
                control="planning.add_row"
                label={text.sequence.addRow}
                description={text.sequence.describe.addRow}
                disabled={rows.length >= 32}
                onActivate={addRow}
              />
              <NleIconButton
                icon="reviewed"
                hue="edit"
                control="planning.admit_reviewed"
                label={text.sequence.admitReviewed}
                description={text.sequence.describe.admitReviewed}
                disabled={
                  busy ||
                  rows.length === 0 ||
                  rows.some(
                    (row) =>
                      row.text.length === 0 ||
                      row.endMilliseconds <= row.startMilliseconds,
                  )
                }
                onActivate={() =>
                  void actions.admitStoryboard("user_reviewed_typed_rows")
                }
              />
            </NleIconGroup>
          </div>
        ) : null}
        {proposal !== null ? (
          <div
            data-h3-nle-region="proposal"
            data-h3-nle-proposal={proposal.proposal_id}
          >
            <p className="h3-nle-note">
              {fill(text.sequence.proposal, {
                count: proposal.segments.length,
              })}
            </p>
            <ol className="h3-nle-slots">
              {proposal.segments.map((segment) => (
                <li
                  key={segment.segment_id}
                  data-h3-nle-segment={segment.segment_id}
                >
                  <span>
                    {fill(text.sequence.proposalSegment, {
                      ordinal: segment.ordinal,
                      mode: plainReason(locale, "taskMode", segment.task_mode),
                      seconds: segment.duration_seconds,
                    })}
                  </span>
                  <small>{segment.local_prompt}</small>
                </li>
              ))}
            </ol>
            {proposal.blocker_codes.length > 0 ? (
              <PlainCodeList
                className="h3-nle-danger"
                label={text.sequence.blockers}
                locale={locale}
                codes={proposal.blocker_codes}
                status="proposal-blockers"
              />
            ) : null}
            {proposal.start_hold_codes.length > 0 ? (
              <PlainCodeList
                className="h3-nle-note"
                label={text.sequence.holds}
                locale={locale}
                codes={proposal.start_hold_codes}
                status="proposal-holds"
              />
            ) : null}
          </div>
        ) : null}
        {planning.plan !== null ? (
          <p
            className="h3-nle-note"
            data-h3-nle-status="plan"
            data-h3-nle-plan={planning.plan.plan_fingerprint}
          >
            {fill(text.sequence.imported, {
              count: planning.plan.segment_ids.length,
            })}
          </p>
        ) : null}
        {planning.error !== null ? (
          <>
            <p
              className="h3-nle-danger"
              role="status"
              data-h3-nle-status="planning-error"
              data-code={planning.error}
            >
              {plainReason(locale, "planning", planning.error)}
            </p>
            {planning.error === "planning_requires_empty_project" ? (
              <NleIconGroup label={text.sequence.planningActions}>
                <NleIconButton
                  icon="project"
                  hue="production"
                  control="planning.create_project"
                  label={text.sequence.createPlannedProject}
                  description={text.sequence.describe.createPlannedProject}
                  disabled={false}
                  onActivate={() => void actions.createPlannedProject()}
                />
              </NleIconGroup>
            ) : null}
          </>
        ) : null}
        <NleReadinessSummary
          locale={locale}
          planning={planning}
          readiness={readiness}
          workspaceFingerprint={workspaceFingerprint}
          actions={actions}
        />
      </div>
    </section>
  );
}

/**
 * M25-63: the readiness request and its state, shared by Production's planning section and the
 * editor's Sequence tab. The request needs an accepted plan bound to the current Production
 * workspace, whichever surface asks.
 */
export function NleReadinessSummary({
  locale,
  planning,
  readiness,
  workspaceFingerprint,
  actions,
}: {
  locale: Locale;
  planning: NleWorkspaceState["planning"];
  readiness: NleWorkspaceState["readiness"];
  workspaceFingerprint: string | undefined;
  actions: NleWorkspaceBinding["actions"];
}) {
  const text = nleCopy(locale);
  const stale =
    planning.boundWorkspaceFingerprint !== null &&
    planning.boundWorkspaceFingerprint !== workspaceFingerprint;
  return (
    <>
      <NleIconGroup label={text.sequence.readinessActions}>
        <NleIconButton
          icon="readiness"
          hue="ok"
          control="readiness.request"
          label={text.sequence.readiness}
          description={text.sequence.describe.readiness}
          disabled={
            readiness.status === "requesting" || planning.plan === null || stale
          }
          onActivate={() => void actions.requestReadiness()}
        />
        <span
          className="h3-nle-chip"
          data-tone={tone(readiness.status)}
          data-h3-nle-status="readiness"
          data-state={readiness.status}
        >
          {text.sequence.readinessState[readiness.status]}
        </span>
      </NleIconGroup>
      {readiness.readiness !== null && readiness.status === "held" ? (
        <p
          className="h3-nle-note"
          data-h3-nle-status="readiness-reason"
          data-code={readiness.readiness.reason}
        >
          {plainReason(locale, "readiness", readiness.readiness.reason)}
        </p>
      ) : null}
    </>
  );
}

/** Plain sentences for proposal codes; the codes stay on the element for diagnostics. */
function PlainCodeList({
  className,
  label,
  locale,
  codes,
  status,
}: {
  className: string;
  label: string;
  locale: Locale;
  codes: readonly string[];
  status: string;
}) {
  return (
    <div
      className={className}
      data-h3-nle-status={status}
      data-codes={codes.join(" ")}
    >
      <strong>{label}</strong>
      <ul>
        {[
          ...new Set(
            codes.map((code) => plainReason(locale, "proposal", code)),
          ),
        ].map((sentence) => (
          <li key={sentence}>{sentence}</li>
        ))}
      </ul>
    </div>
  );
}
