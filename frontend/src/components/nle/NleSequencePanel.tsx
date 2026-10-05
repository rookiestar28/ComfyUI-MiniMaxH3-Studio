// M25-16 sequence pane: the M26 whole-video planning flow (prepare → admit → propose →
// approve/import), the M26-04 readiness gate, the managed serial start with the B1
// leave/return controls, and the M26-06 assembly actions. Every button is one explicit
// accepted action; mount, remount and polling never plan, qualify, queue or assemble.

import type { ProductionViewState } from "../ProductionWorkbench";
import type { Locale } from "../../i18n/catalog";
import { plainReason } from "../plainReasons";
import { type NleWorkspaceState } from "../../state/nleWorkspaceState";
import { fill, nleCopy } from "./nleCopy";
import type { NleWorkspaceBinding } from "./nleWorkspaceBinding";
import { NleReadinessSummary } from "./NlePlanningSection";

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

export function NleSequencePanel({
  binding,
}: {
  binding: NleWorkspaceBinding;
}) {
  const { locale, state, production, contextAvailable, actions } = binding;
  const text = nleCopy(locale);
  const productionProjection =
    "projection" in production ? production.projection : undefined;
  // M25-63: planning happens in Production only. The editor's Sequence tab keeps every run control
  // and the readiness request, under one heading, and says where planning lives.
  return (
    <div className="h3-nle-inspector" data-h3-nle-region="sequence">
      <h4>{text.sequence.title}</h4>
      <p className="h3-nle-note" data-h3-nle-status="planning-location">
        {text.sequence.planningLocation}
      </p>
      {productionProjection === undefined ? (
        <p className="h3-nle-note" data-h3-nle-status="sequence-production">
          {text.sequence.productionUnavailable}
        </p>
      ) : null}
      {!contextAvailable ? (
        <p className="h3-nle-note" data-h3-nle-status="sequence-context">
          {text.sequence.contextUnavailable}
        </p>
      ) : null}
      <NleReadinessSummary
        locale={locale}
        planning={state.planning}
        readiness={state.readiness}
        workspaceFingerprint={productionProjection?.workspaceFingerprint}
        actions={actions}
      />
      <SequenceSection
        locale={locale}
        sequence={state.sequence}
        readiness={state.readiness}
        actions={actions}
        startable={actions.sequenceStartable()}
      />
      {productionProjection !== undefined ? (
        <ol
          className="h3-nle-slots"
          aria-label={text.sequence.segmentDurations}
        >
          {productionProjection.segments.map((segment) => (
            <li key={segment.segmentId}>
              {fill(text.sequence.segmentDuration, {
                ordinal: segment.ordinal,
                requested: segment.duration.requestedMilliseconds / 1000,
                delivered: segment.duration.deliveredMilliseconds / 1000,
                frames: segment.duration.frameCount,
              })}
            </li>
          ))}
        </ol>
      ) : null}
      <AssemblySection
        locale={locale}
        production={production}
        actions={actions}
      />
    </div>
  );
}

function SequenceSection({
  locale,
  sequence,
  readiness,
  actions,
  startable,
}: {
  locale: Locale;
  sequence: NleWorkspaceState["sequence"];
  readiness: NleWorkspaceState["readiness"];
  actions: NleWorkspaceBinding["actions"];
  startable: boolean;
}) {
  const text = nleCopy(locale);
  const ui = sequence.ui;
  const pointer = actions.recoveryPointerPresent();
  return (
    <section aria-label={text.sequence.state[ui]} data-h3-nle-sequence-ui={ui}>
      <div className="h3-nle-form">
        <div className="h3-nle-actions">
          <span
            className="h3-nle-chip"
            data-tone={tone(ui)}
            data-h3-nle-status="sequence"
            data-state={ui}
            role="status"
            aria-live="polite"
          >
            {text.sequence.state[ui]}
          </span>
          {sequence.failure !== null ? (
            <span
              className="h3-nle-danger"
              data-h3-nle-status="sequence-failure"
              data-code={sequence.failure}
            >
              {plainReason(locale, "sequence", sequence.failure)}
            </span>
          ) : null}
        </div>
        <div className="h3-nle-actions">
          <button
            type="button"
            data-h3-nle-control="sequence.start"
            disabled={!startable || readiness.status !== "ready"}
            onClick={() => void actions.startSequence()}
          >
            {text.sequence.start}
          </button>
          <button
            type="button"
            data-h3-nle-control="sequence.detach"
            title={text.sequence.detachHint}
            disabled={ui !== "detach_ready" || sequence.busy}
            onClick={() => void actions.detachSequence()}
          >
            {text.sequence.detach}
          </button>
          <button
            type="button"
            data-h3-nle-control="sequence.reattach"
            disabled={
              sequence.busy ||
              !pointer ||
              ui === "attached_current_child" ||
              ui === "detach_ready" ||
              ui === "starting"
            }
            onClick={() => void actions.reattachSequence()}
          >
            {text.sequence.reattach}
          </button>
          <button
            type="button"
            data-h3-nle-control="sequence.resume"
            disabled={ui !== "resume_ready" || sequence.busy}
            onClick={() => void actions.resumeSequence()}
          >
            {text.sequence.resume}
          </button>
          <button
            type="button"
            data-h3-nle-control="sequence.cancel"
            disabled={
              sequence.busy ||
              ui === "idle" ||
              ui === "finished" ||
              ui === "recovery_unavailable_or_expired"
            }
            onClick={() => void actions.cancelSequence()}
          >
            {text.sequence.cancel}
          </button>
          <button
            type="button"
            data-h3-nle-control="sequence.refresh"
            disabled={sequence.busy || !pointer}
            onClick={() => void actions.refreshSequence()}
          >
            {text.sequence.refresh}
          </button>
        </div>
        {ui === "detach_ready" ||
        ui === "safe_to_leave" ||
        ui === "detaching" ? (
          <p className="h3-nle-note" data-h3-nle-status="detach-hint">
            {text.sequence.detachHint}
          </p>
        ) : null}
        {ui === "safe_to_leave" &&
        sequence.recoveryExpiresAtEpochMs !== null ? (
          <p
            className="h3-nle-note"
            role="status"
            data-h3-nle-status="recovery-deadline"
          >
            {fill(text.sequence.recoveryDeadline, {
              deadline: new Date(
                sequence.recoveryExpiresAtEpochMs,
              ).toISOString(),
            })}
          </p>
        ) : null}
        {pointer && ui === "idle" ? (
          <p className="h3-nle-note" data-h3-nle-status="recovery-pointer">
            {text.sequence.recoveryPointer}
          </p>
        ) : null}
        {sequence.projection !== null ? (
          <ol
            className="h3-nle-slots"
            data-h3-nle-parent={sequence.projection.parentSequenceId}
            data-h3-nle-parent-state={sequence.projection.state}
          >
            {sequence.projection.slots.map((slot) => (
              <li
                key={slot.segmentId}
                data-state={slot.state}
                data-h3-nle-segment={slot.segmentId}
              >
                <span>
                  {fill(text.sequence.slot, {
                    ordinal: slot.ordinal,
                    state: plainReason(locale, "slot", slot.state),
                  })}
                </span>
                {slot.state === "failed" ? (
                  <button
                    type="button"
                    data-h3-nle-control="sequence.retry_segment"
                    disabled={sequence.busy}
                    onClick={() => void actions.retrySegment(slot.segmentId)}
                  >
                    {fill(text.sequence.retrySegment, {
                      ordinal: slot.ordinal,
                    })}
                  </button>
                ) : null}
              </li>
            ))}
          </ol>
        ) : null}
      </div>
    </section>
  );
}

function AssemblySection({
  locale,
  production,
  actions,
}: {
  locale: Locale;
  production: ProductionViewState;
  actions: NleWorkspaceBinding["actions"];
}) {
  const text = nleCopy(locale);
  const projection =
    "projection" in production ? production.projection : undefined;
  if (projection === undefined) return null;
  const assembly = projection.assembly;
  const allowed = new Set(projection.allowedActions);
  const busy =
    production.status === "pending" || production.status === "loading";
  return (
    <section
      aria-label={text.sequence.assembly}
      data-h3-nle-assembly-state={assembly.state}
    >
      <h4>{text.sequence.assembly}</h4>
      <div className="h3-nle-form">
        <p className="h3-nle-note" data-h3-nle-status="assembly">
          {fill(text.sequence.assemblyState, {
            state: plainReason(locale, "assemblyState", assembly.state),
            completed: assembly.progress.completed,
            total: assembly.progress.total,
          })}
        </p>
        {assembly.failureCode !== null ? (
          <p
            className={
              assembly.state === "failed" ? "h3-nle-danger" : "h3-nle-note"
            }
            data-h3-nle-status="assembly-failure"
            data-code={assembly.failureCode}
          >
            {plainReason(locale, "assembly", assembly.failureCode)}
          </p>
        ) : null}
        <p className="h3-nle-note" data-h3-nle-status="assembly-profile">
          {text.sequence.assemblyProfile}
        </p>
        {assembly.capabilityFingerprint !== null ? (
          <p className="h3-nle-note" data-h3-nle-status="assembly-inputs">
            {fill(text.sequence.assemblyInputs, {
              artifacts: assembly.artifactReceiptFingerprints.length,
              cuts: assembly.cutBoundaryReceiptFingerprints.length,
            })}
          </p>
        ) : null}
        {assembly.receiptFingerprint !== null ? (
          <p
            role="status"
            className="h3-nle-note"
            data-h3-nle-status="assembly-receipt"
          >
            {text.sequence.assemblyReceipt}
          </p>
        ) : null}
        <div className="h3-nle-actions">
          <button
            type="button"
            data-h3-nle-control="assembly.assemble"
            disabled={busy || !allowed.has("assemble_sequence")}
            onClick={() => void actions.assembly("assemble_sequence")}
          >
            {text.sequence.assemble}
          </button>
          <button
            type="button"
            data-h3-nle-control="assembly.cancel"
            disabled={busy || !allowed.has("cancel_assembly")}
            onClick={() => void actions.assembly("cancel_assembly")}
          >
            {text.sequence.cancelAssembly}
          </button>
          <button
            type="button"
            data-h3-nle-control="assembly.retry"
            disabled={busy || !allowed.has("retry_assembly")}
            onClick={() => void actions.assembly("retry_assembly")}
          >
            {text.sequence.retryAssembly}
          </button>
          <button
            type="button"
            data-h3-nle-control="production.refresh"
            disabled={busy}
            onClick={() => void actions.refreshProduction()}
          >
            {text.sequence.refresh}
          </button>
        </div>
      </div>
    </section>
  );
}
