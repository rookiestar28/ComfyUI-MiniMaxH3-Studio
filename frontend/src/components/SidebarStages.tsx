import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import type {
  SidebarStageId,
  SidebarWorkspaceProjection,
} from "../contracts/sidebarWorkspaceCodec";
import type { SidebarWorkspaceActionName } from "../state/sidebarWorkspace";
import type { Locale } from "../i18n/catalog";
import {
  translate,
  stageChromeCatalog,
  stageLabelsCatalog,
  stageStatusCatalog,
  translateStageSummary,
} from "../i18n/catalog";
import { diagnosticSentence, severityLabel } from "../i18n/diagnostics";
import { NoticeSurface, useNotices } from "./NoticeSurface";
import { promptSegments } from "./promptGrammar";
import { remediationFor } from "./remediation";
import { ReferenceTokenCombobox } from "./ReferenceTokenCombobox";
import { ReferenceTokenPicker } from "./ReferenceTokenPicker";
import type {
  AssistedActionRequest,
  AssistedFailureId,
  AssistedPromptProposalProjection,
} from "../contracts/assistedPromptProposalCodec";

export type WorkspaceActionRequest =
  | {
      action: SidebarWorkspaceActionName;
      payload: Record<string, unknown>;
    }
  | AssistedActionRequest;

export type SidebarStagesDraft = {
  authority: string;
  activeStage: SidebarStageId;
  promptText: string;
  reason: string;
};

export function sidebarStagesAuthority(
  projection: SidebarWorkspaceProjection,
): string {
  return [
    projection.workspace_id,
    projection.report_revision,
    projection.report_fingerprint,
    projection.prompt_fingerprint,
  ].join(":");
}

export function initialSidebarStagesDraft(
  projection: SidebarWorkspaceProjection,
): SidebarStagesDraft {
  return {
    authority: sidebarStagesAuthority(projection),
    activeStage: projection.lifecycle === "ready" ? "execute" : "audit",
    promptText: projection.prompt_text,
    reason: "",
  };
}

export function rebaseSidebarStagesDraft(
  projection: SidebarWorkspaceProjection,
  previous: SidebarStagesDraft,
): SidebarStagesDraft {
  return {
    ...initialSidebarStagesDraft(projection),
    activeStage: previous.activeStage,
  };
}

export function insertReferenceAtSelection(
  promptText: string,
  label: string,
  selectionStart: number,
  selectionEnd: number,
): { promptText: string; caret: number } {
  const start = Math.max(0, Math.min(selectionStart, promptText.length));
  const end = Math.max(start, Math.min(selectionEnd, promptText.length));
  const left = promptText.slice(0, start);
  const right = promptText.slice(end);
  const leading = left.length > 0 && !/\s$/.test(left) ? " " : "";
  const trailing = right.length > 0 && !/^\s/.test(right) ? " " : "";
  return {
    promptText: `${left}${leading}${label}${trailing}${right}`,
    caret: left.length + leading.length + label.length,
  };
}

function DefinitionList({
  values,
}: {
  values: Array<[string, string | number]>;
}) {
  return (
    <dl className="h3-context-summary">
      {values.map(([term, value]) => (
        <div key={term}>
          <dt>{term}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function SidebarStages({
  projection,
  locale,
  busy,
  onAction,
  onClientFailure,
  draft,
  onDraftChange,
  assistedAuthorized = false,
  assistedProposal,
  assistedBusy = false,
  assistedFailure,
}: {
  projection: SidebarWorkspaceProjection;
  locale: Locale;
  busy: boolean;
  onAction(request: WorkspaceActionRequest): unknown;
  onClientFailure(): void;
  draft?: SidebarStagesDraft;
  onDraftChange?: (draft: SidebarStagesDraft) => void;
  assistedAuthorized?: boolean;
  assistedProposal?: AssistedPromptProposalProjection;
  assistedBusy?: boolean;
  assistedFailure?: AssistedFailureId;
}) {
  const authority = sidebarStagesAuthority(projection);
  const initialDraft = initialSidebarStagesDraft(projection);
  const [localDraft, setLocalDraft] =
    useState<SidebarStagesDraft>(initialDraft);
  const currentDraft =
    draft?.authority === authority
      ? draft
      : draft !== undefined
        ? rebaseSidebarStagesDraft(projection, draft)
        : localDraft.authority === authority
          ? localDraft
          : rebaseSidebarStagesDraft(projection, localDraft);
  const updateDraft = (patch: Partial<SidebarStagesDraft>): void => {
    const next = { ...currentDraft, ...patch, authority };
    if (onDraftChange !== undefined) onDraftChange(next);
    else setLocalDraft(next);
  };
  const { activeStage: active, promptText, reason } = currentDraft;
  const setActive = (activeStage: SidebarStageId) =>
    updateDraft({ activeStage });
  const setPromptText = (value: string) => updateDraft({ promptText: value });
  const setReason = (value: string) => updateDraft({ reason: value });
  const notices = useNotices();
  // M21-03 AC-09. A remediation's effect is a declared intent, never an
  // inference. `focus_prompt` is the only one this build performs, and it does
  // exactly what its name says: open Audit and put the caret in the prompt.
  const runRemediation = (intent: "focus_prompt", code: string): void => {
    if (intent !== "focus_prompt") return;
    updateDraft({ activeStage: "audit" });
    window.setTimeout(() => {
      const field = document.getElementById("h3-prompt-revision");
      if (field instanceof HTMLTextAreaElement) field.focus();
    }, 0);
    notices.publish({
      id: `remediation:${code}`,
      tier: "transient",
      text: translate(locale, "remediation.editPrompt"),
    });
  };
  const stageLabels = stageLabelsCatalog[locale];
  const stageStatuses = stageStatusCatalog[locale];
  const text = stageChromeCatalog[locale];
  const guideReadinessText = {
    ready: text.guideReady,
    incomplete: text.guideIncomplete,
    modified: text.guideModified,
  }[projection.guide_conformance.readiness];
  const mounted = useRef(true);
  const clientGeneration = useRef(0);
  const promptRef = useRef<HTMLTextAreaElement>(null);
  const pendingPromptCaret = useRef<number | undefined>(undefined);
  const panelId = `h3-stage-${active}`;
  const stage = projection.stages.find((value) => value.stage_id === active);
  const act = (request: WorkspaceActionRequest): void => {
    if (!busy && !assistedBusy) void onAction(request);
  };

  useLayoutEffect(() => {
    const caret = pendingPromptCaret.current;
    const field = promptRef.current;
    if (caret === undefined || field === null) return;
    pendingPromptCaret.current = undefined;
    field.focus();
    field.setSelectionRange(caret, caret);
  }, [promptText]);

  const insertBackendReference = (label: string): void => {
    const field = promptRef.current;
    const insertion = insertReferenceAtSelection(
      promptText,
      label,
      field?.selectionStart ?? promptText.length,
      field?.selectionEnd ?? promptText.length,
    );
    pendingPromptCaret.current = insertion.caret;
    setPromptText(insertion.promptText);
    if (insertion.promptText === promptText && field !== null) {
      pendingPromptCaret.current = undefined;
      field.focus();
      field.setSelectionRange(insertion.caret, insertion.caret);
    }
    act({
      action: "stage_prompt",
      payload: {
        reason: `Insert backend reference ${label}`,
        prompt_text: insertion.promptText,
      },
    });
  };

  useEffect(() => {
    if (
      assistedProposal === undefined ||
      assistedProposal.state !== "active" ||
      assistedProposal.workspace_id !== projection.workspace_id ||
      assistedProposal.report_revision !== projection.report_revision ||
      assistedProposal.report_fingerprint !== projection.report_fingerprint
    )
      return;
    const next = {
      ...currentDraft,
      authority,
      activeStage: "audit" as const,
      promptText: assistedProposal.candidate_text,
    };
    if (onDraftChange !== undefined) onDraftChange(next);
    else setLocalDraft(next);
    // Proposal identity/revision is the transaction boundary. Workspace authority is included
    // explicitly so an old async response cannot rebase a new editor.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    assistedProposal?.proposal_id,
    assistedProposal?.proposal_revision,
    projection.workspace_id,
    projection.report_revision,
    projection.report_fingerprint,
  ]);

  useEffect(() => {
    // IMPORTANT: bind async client completions to exact host workspace authority.
    clientGeneration.current += 1;
  }, [
    projection.workspace_id,
    projection.correlation.prompt_id,
    projection.correlation.execution_node_id,
  ]);

  useEffect(
    () => () => {
      mounted.current = false;
      clientGeneration.current += 1;
    },
    [],
  );

  const copyPrompt = async (): Promise<void> => {
    const generation = clientGeneration.current;
    try {
      if (navigator.clipboard?.writeText === undefined)
        throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(projection.prompt_text);
    } catch {
      if (mounted.current && generation === clientGeneration.current)
        onClientFailure();
    }
  };

  return (
    <>
      <div className="h3-stage-tabs" role="tablist" aria-label={text.stages}>
        {projection.stages.map((value) => (
          <button
            key={value.stage_id}
            id={`h3-tab-${value.stage_id}`}
            type="button"
            role="tab"
            aria-label={stageLabels[value.stage_id]}
            aria-selected={active === value.stage_id}
            aria-controls={`h3-stage-${value.stage_id}`}
            tabIndex={active === value.stage_id ? 0 : -1}
            className={`h3-stage-tab h3-stage-tab--${value.stage_id} h3-stage-tab--${value.status}`}
            data-stage={value.stage_id}
            data-h3-focus-key={`workspace-stage-${value.stage_id}`}
            onClick={() => setActive(value.stage_id)}
            onKeyDown={(event) => {
              const index = projection.stages.findIndex(
                (item) => item.stage_id === value.stage_id,
              );
              const delta =
                event.key === "ArrowRight"
                  ? 1
                  : event.key === "ArrowLeft"
                    ? -1
                    : 0;
              if (delta === 0) return;
              event.preventDefault();
              const next = projection.stages[(index + delta + 5) % 5].stage_id;
              setActive(next);
              document.getElementById(`h3-tab-${next}`)?.focus();
            }}
          >
            <span>{stageLabels[value.stage_id]}</span>
            <small aria-hidden="true">{stageStatuses[value.status]}</small>
          </button>
        ))}
      </div>

      <section
        id={panelId}
        role="tabpanel"
        aria-labelledby={`h3-tab-${active}`}
        className="h3sp"
        data-stage={active}
      >
        <header>
          <p className="h3-context-kicker">{stageLabels[active]}</p>
          <p>
            {stage === undefined
              ? ""
              : translateStageSummary(locale, stage.summary)}
          </p>
        </header>
        <p
          className="h3-meta"
          data-guide-readiness={projection.guide_conformance.readiness}
        >
          <strong>{text.guideReadiness}: </strong>
          {guideReadinessText}
        </p>

        {active === "intent" ? (
          <DefinitionList
            values={[
              [text.task, projection.task_mode],
              [text.profile, projection.profile],
              [text.scope, projection.product_scope],
              [text.revision, projection.report_revision],
              [text.modeStatus, projection.capabilities.mode_status],
              [
                text.supportedModes,
                projection.capabilities.supported_modes.join(", "),
              ],
              [
                text.promptBoundary,
                projection.capabilities.native_prompt_boundary,
              ],
              [
                text.requestedDuration,
                projection.capabilities.output_duration.requested_seconds,
              ],
              [
                text.effectiveDuration,
                projection.capabilities.output_duration.effective_seconds,
              ],
              [
                text.durationRange,
                `${projection.capabilities.output_duration.min_seconds}–${projection.capabilities.output_duration.max_seconds}`,
              ],
              [
                text.durationStatus,
                projection.capabilities.output_duration.status,
              ],
            ]}
          />
        ) : null}

        {active === "media" ? (
          <>
            <DefinitionList
              values={[
                [text.mediaReceipt, projection.media_receipt.status],
                [text.queueReady, String(projection.media_receipt.queue_ready)],
                [
                  text.referenceLimits,
                  `${projection.capabilities.reference_limits.image} / ${projection.capabilities.reference_limits.video} / ${projection.capabilities.reference_limits.audio} / ${projection.capabilities.reference_limits.total}`,
                ],
                [
                  text.timedReference,
                  projection.capabilities.timed_reference_limits.status,
                ],
                ...(projection.capabilities.timed_reference_limits
                  .limitation === null
                  ? []
                  : [
                      [
                        text.timedReferenceLimitation,
                        projection.capabilities.timed_reference_limits
                          .limitation,
                      ] as [string, string],
                    ]),
              ]}
            />
            {projection.reference_candidates.length === 0 ? (
              <p>{text.noBindings}</p>
            ) : (
              <ul className="h3-audit-list" aria-label={text.mediaBindings}>
                {projection.reference_candidates.map((candidate) => (
                  <li key={candidate.asset_id}>
                    <strong>{candidate.label}</strong>
                    <span>{candidate.kind}</span>
                    {candidate.paired_with === null ? null : (
                      <small>
                        {text.pairedWith} {candidate.paired_with}
                      </small>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </>
        ) : null}

        {active === "understand" ? (
          <>
            <DefinitionList
              values={[
                [text.evidenceRecords, projection.evidence.count],
                [
                  text.origins,
                  projection.evidence.origins.join(", ") || text.none,
                ],
                [text.planSteps, projection.plan_steps.length],
                [text.planningPolicy, projection.planning.policy],
                [text.timelineEnd, projection.planning.timeline.end_seconds],
                [
                  text.timelineFrames,
                  projection.planning.timeline.effective_frame_count,
                ],
                [
                  text.durationSource,
                  projection.planning.timeline.duration_source,
                ],
                [text.alternatives, projection.planning.alternatives_status],
                [
                  text.creativeAdditions,
                  projection.planning.creative_additions_status,
                ],
              ]}
            />
            <ol className="h3-audit-list">
              {projection.plan_steps.map((step) => (
                <li key={step.step_id}>
                  <strong>{step.stage}</strong>
                  <span>{step.description}</span>
                  <small>{step.status}</small>
                </li>
              ))}
            </ol>
            {projection.planning.creative_additions.length > 0 ? (
              <ul
                className="h3-audit-list"
                aria-label={text.creativeAdditionValues}
              >
                {projection.planning.creative_additions.map((addition) => (
                  <li key={addition}>{addition}</li>
                ))}
              </ul>
            ) : null}
          </>
        ) : null}

        {active === "audit" ? (
          <div className="h3-audit-editor">
            {projection.exact_text.length > 0 ? (
              <ul className="h3-audit-list" aria-label={text.exactText}>
                {projection.exact_text.map((item) => (
                  <li key={item.constraint_id}>
                    <strong>{item.kind}</strong>
                    <span>{item.text}</span>
                  </li>
                ))}
              </ul>
            ) : null}
            <label htmlFor="h3-prompt-revision">{text.promptRevision}</label>
            {/* M21-03 AC-08. The highlight is an overlay behind a textarea whose
                own text is transparent, so the editable value is never rewritten
                and the bytes staged are the bytes typed. The overlay is
                `aria-hidden` and inert: a screen reader and the keyboard both
                see only the textarea, exactly as before. */}
            <div className="h3-prompt">
              <pre className="h3-prompt-o" aria-hidden="true">
                {promptSegments(promptText).map((segment, index) =>
                  segment.kind === "plain" ? (
                    segment.text
                  ) : (
                    <b key={index} data-grammar={segment.kind}>
                      {segment.text}
                    </b>
                  ),
                )}
                {"\n"}
              </pre>
              <textarea
                ref={promptRef}
                id="h3-prompt-revision"
                className="h3-prompt-i"
                aria-label={text.promptRevision}
                data-h3-focus-key="workspace-prompt"
                value={promptText}
                disabled={
                  busy || assistedBusy || !projection.actions.stage_prompt
                }
                onScroll={(event) => {
                  const overlay = event.currentTarget
                    .previousElementSibling as HTMLElement | null;
                  if (overlay === null) return;
                  overlay.scrollTop = event.currentTarget.scrollTop;
                  overlay.scrollLeft = event.currentTarget.scrollLeft;
                }}
                onChange={(event) => setPromptText(event.currentTarget.value)}
              />
            </div>
            <ReferenceTokenPicker
              references={projection.reference_candidates}
              subjects={projection.subject_candidates}
              disabled={busy || !projection.actions.stage_prompt}
              labels={{
                toolbar: text.referenceToolbar,
                picker: text.referencePicker,
                placeholder: text.referencePickerPlaceholder,
                insert: text.insertReference,
                pairedWith: text.pairedWith,
                kind: {
                  image: text.referenceKindImage,
                  video: text.referenceKindVideo,
                  audio: text.referenceKindAudio,
                  subject: text.referenceKindSubject,
                },
              }}
              onSelect={insertBackendReference}
            />
            <ReferenceTokenCombobox
              promptText={promptText}
              candidates={projection.reference_candidates}
              disabled={busy || !projection.actions.stage_prompt}
              focusKey="workspace-reference"
              labels={{
                field: text.referenceToken,
                listbox: text.backendReferences,
                placeholder: text.referencePlaceholder,
                pairedWith: text.pairedWith,
              }}
              onSelect={(candidate, nextPrompt) => {
                setPromptText(nextPrompt);
                act({
                  action: "stage_prompt",
                  payload: {
                    // Locale-neutral audit reason: UI copy must not alter the
                    // backend-owned staged revision or its fingerprint.
                    reason: `Insert backend reference ${candidate.label}`,
                    prompt_text: nextPrompt,
                  },
                });
              }}
            />
            <label htmlFor="h3-revision-reason">{text.revisionReason}</label>
            <input
              id="h3-revision-reason"
              data-h3-focus-key="workspace-reason"
              value={reason}
              maxLength={1024}
              disabled={busy || !projection.actions.stage_prompt}
              onChange={(event) => setReason(event.currentTarget.value)}
            />
            <button
              type="button"
              data-h3-focus-key="workspace-stage-revision"
              disabled={
                busy ||
                assistedBusy ||
                assistedProposal?.state === "active" ||
                !projection.actions.stage_prompt ||
                reason.trim().length === 0 ||
                promptText === projection.prompt_text
              }
              onClick={() =>
                act({
                  action: "stage_prompt",
                  payload: { reason, prompt_text: promptText },
                })
              }
            >
              {text.stageRevision}
            </button>
            <div className="h3-assisted-review">
              <button
                type="button"
                data-h3-focus-key="workspace-optimize-prompt"
                disabled={
                  busy ||
                  assistedBusy ||
                  !assistedAuthorized ||
                  assistedProposal?.state === "active"
                }
                onClick={() => act({ action: "optimize_prompt", payload: {} })}
              >
                {text.optimizePrompt}
              </button>
              {assistedBusy ? (
                <button
                  type="button"
                  onClick={() =>
                    void onAction({
                      action: "cancel_assisted_execution",
                      payload: {},
                    })
                  }
                >
                  {text.cancelOptimization}
                </button>
              ) : null}
              {assistedProposal?.state === "active" ? (
                <div aria-label={text.assistedReview}>
                  <DefinitionList
                    values={[
                      [
                        text.assistedProvider,
                        assistedProposal.receipt.provider_family,
                      ],
                      [text.assistedModel, assistedProposal.receipt.model_id],
                      [
                        text.assistedAttempts,
                        assistedProposal.receipt.attempts,
                      ],
                      [text.assistedAudit, assistedProposal.audit.length_band],
                    ]}
                  />
                  <button
                    type="button"
                    disabled={
                      busy ||
                      assistedBusy ||
                      promptText.length === 0 ||
                      promptText === assistedProposal.candidate_text
                    }
                    onClick={() =>
                      act({
                        action: "edit_assisted_proposal",
                        payload: {
                          proposal_id: assistedProposal.proposal_id,
                          expected_proposal_revision:
                            assistedProposal.proposal_revision,
                          prompt_text: promptText,
                        },
                      })
                    }
                  >
                    {text.updateProposal}
                  </button>
                  <button
                    type="button"
                    disabled={busy || assistedBusy}
                    onClick={() =>
                      act({
                        action: "accept_assisted_proposal",
                        payload: {
                          proposal_id: assistedProposal.proposal_id,
                          expected_proposal_revision:
                            assistedProposal.proposal_revision,
                        },
                      })
                    }
                  >
                    {text.acceptProposal}
                  </button>
                  <button
                    type="button"
                    disabled={busy || assistedBusy}
                    onClick={() =>
                      act({
                        action: "reject_assisted_proposal",
                        payload: {
                          proposal_id: assistedProposal.proposal_id,
                          expected_proposal_revision:
                            assistedProposal.proposal_revision,
                        },
                      })
                    }
                  >
                    {text.rejectProposal}
                  </button>
                </div>
              ) : null}
              {assistedFailure === undefined ? null : (
                <p role="alert">
                  {assistedFailure === "prompt_model.profile_not_qualified"
                    ? text.assistedProfileNotQualified
                    : text.assistedFailure}
                </p>
              )}
            </div>
            {[...projection.diagnostics, ...projection.limitations].length >
            0 ? (
              <ul className="h3-diagnostics" aria-label={text.diagnostics}>
                {[...projection.diagnostics, ...projection.limitations].map(
                  (item, index) => (
                    <li key={`${item.code}-${index}`} data-code={item.code}>
                      <strong>{severityLabel(locale, item.severity)}</strong>{" "}
                      {diagnosticSentence(locale, item)}
                      {((): ReactNode => {
                        const fix = remediationFor(item.code);
                        // No declared remediation means no action at all: an
                        // action the product cannot perform is worse than none.
                        if (fix === undefined) return null;
                        return (
                          <button
                            type="button"
                            data-remediation={fix.intent}
                            onClick={() =>
                              runRemediation(fix.intent, item.code)
                            }
                          >
                            {translate(locale, fix.label)}
                          </button>
                        );
                      })()}
                    </li>
                  ),
                )}
              </ul>
            ) : (
              <p>{text.noDiagnostics}</p>
            )}
          </div>
        ) : null}

        {active === "execute" ? (
          <>
            <DefinitionList
              values={[
                [text.validation, projection.validation_status],
                [text.guideReadiness, guideReadinessText],
                [text.provider, projection.receipt.provider],
                [text.receipt, projection.receipt.outcome],
                [text.comparison, projection.comparison.reason],
                [text.nativeNode, projection.resources.native_node_id],
                [text.mediaReceipt, projection.media_receipt.status],
                [text.queueReady, String(projection.media_receipt.queue_ready)],
                [
                  text.referenceLimits,
                  `${projection.capabilities.reference_limits.image} / ${projection.capabilities.reference_limits.video} / ${projection.capabilities.reference_limits.audio} / ${projection.capabilities.reference_limits.total}`,
                ],
                [
                  text.timedReference,
                  projection.capabilities.timed_reference_limits.status,
                ],
                [text.proposalDiff, projection.proposal.diff.status],
                [text.reportFingerprint, projection.report_fingerprint],
                [text.promptFingerprint, projection.prompt_fingerprint],
              ]}
            />
            {projection.proposal.changed ? (
              <>
                <p className="h3-proposal">
                  {text.proposedRevision}:{" "}
                  {projection.proposal.reason ?? text.explicitEdit}
                </p>
                <ul
                  className="h3-audit-list"
                  aria-label={text.proposalDiffLines}
                >
                  {projection.proposal.diff.lines.map((line, index) => (
                    <li key={`${index}-${line}`}>{line}</li>
                  ))}
                </ul>
              </>
            ) : (
              <p className="h3-proposal">{text.noProposal}</p>
            )}
          </>
        ) : null}
      </section>

      {/* M21-03 AC-10, AC-11: one notice surface for the whole panel. */}
      <NoticeSurface locale={locale} controller={notices} />
      {/* AC-06 of the plan's scope list: the expectation is stated where the
          action is taken, not in a help panel the user has already left. It is
          the accessible description of Validate, so a screen reader hears the
          limitation before running it rather than after. */}
      <p id="h3-expectation-audio" className="h3-meta">
        {translate(locale, "expectation.audioNotListened")}
      </p>
      <div className="h3-context-actions" aria-label={text.actions}>
        <button
          type="button"
          data-h3-focus-key="workspace-validate"
          aria-describedby="h3-expectation-audio"
          disabled={busy || !projection.actions.validate}
          onClick={() => act({ action: "validate", payload: {} })}
        >
          {text.validate}
        </button>
        <button
          type="button"
          data-h3-focus-key="workspace-export"
          disabled={busy || !projection.actions.export}
          onClick={() => act({ action: "export", payload: {} })}
        >
          {text.export}
        </button>
        <button
          type="button"
          data-h3-focus-key="workspace-copy"
          disabled={busy || !projection.actions.copy_prompt}
          onClick={() => void copyPrompt()}
        >
          {text.copy}
        </button>
        <label className="h3-import-button">
          {text.import}
          <input
            type="file"
            data-h3-focus-key="workspace-import"
            accept="application/json,.json"
            disabled={busy || !projection.actions.import_prompt}
            onChange={(event) => {
              const file = event.currentTarget.files?.[0];
              if (file === undefined) return;
              if (file.size > 70_000) {
                onClientFailure();
                return;
              }
              const generation = clientGeneration.current;
              void file
                .text()
                .then((transferJson) => {
                  if (
                    !mounted.current ||
                    generation !== clientGeneration.current
                  )
                    return;
                  act({
                    action: "import_prompt",
                    payload: { transfer_json: transferJson },
                  });
                })
                .catch(() => {
                  if (
                    mounted.current &&
                    generation === clientGeneration.current
                  )
                    onClientFailure();
                });
            }}
          />
        </label>
        <p aria-live="polite">
          {busy
            ? text.busy
            : projection.lifecycle === "ready"
              ? text.ready
              : text.blocked}
        </p>
      </div>
    </>
  );
}
