// M25-16 sequence pane at the component seam: every planning, readiness, B1 and assembly
// control is wired to exactly one explicit binding action, and its `disabled` gate follows the
// typed state (unavailable Production/Context, busy, stale, missing admission/proposal/plan,
// readiness status, B1 UI state, Production allowed actions). The session semantics behind the
// actions are covered by `nleWorkspaceSequence.test.ts`; this file pins the JSX wiring.

import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { NleWorkspaceBinding } from "../src/components/nle/nleWorkspaceBinding";
import type { AutomaticPlanProjection } from "../src/contracts/productionPlanningCodec";
import { decodeManagedReadiness } from "../src/contracts/managedQualificationCodec";
import type { ManagedSequenceProjection } from "../src/host/managedSequenceClient";
import {
  initialNlePlanningState,
  initialNleReadinessState,
  initialNleSequenceState,
  type NleWorkspaceState,
} from "../src/state/nleWorkspaceState";
import {
  fp,
  plan,
  planningProjection,
  productionProjection,
  proposedProjection,
  readyReadiness,
} from "./support/nleSequenceFixture";
import { bindingFixture, expandedState } from "./support/nleWorkspaceBinding";
import { ProductionAndEditorSequence } from "./support/nleSequenceComposition";

afterEach(cleanup);

type Overrides = Partial<{
  planning: Partial<NleWorkspaceState["planning"]>;
  readiness: Partial<NleWorkspaceState["readiness"]>;
  sequence: Partial<NleWorkspaceState["sequence"]>;
  production: NleWorkspaceBinding["production"];
  contextAvailable: boolean;
  startable: boolean;
  pointer: boolean;
}>;

function subject(overrides: Overrides = {}) {
  const binding = bindingFixture({
    production: overrides.production ?? {
      status: "ready",
      projection: productionProjection(),
    },
    contextAvailable: overrides.contextAvailable ?? true,
    state: expandedState({
      planning: { ...initialNlePlanningState, ...overrides.planning },
      readiness: { ...initialNleReadinessState, ...overrides.readiness },
      sequence: { ...initialNleSequenceState, ...overrides.sequence },
    }),
  });
  (binding.actions as { sequenceStartable: () => boolean }).sequenceStartable =
    vi.fn(() => overrides.startable ?? false);
  (
    binding.actions as { recoveryPointerPresent: () => boolean }
  ).recoveryPointerPresent = vi.fn(() => overrides.pointer ?? false);
  const view = render(<ProductionAndEditorSequence binding={binding} />);
  const control = (id: string) =>
    view.container.querySelector<HTMLButtonElement | HTMLInputElement>(
      `[data-h3-nle-control="${id}"]`,
    )!;
  const editor = view.container.querySelector<HTMLElement>(
    '[data-h3-nle-region="sequence"]',
  )!;
  // M25-63 (A63-4): in every state below the editor's Sequence tab has no planning control; the
  // planning form is Production's.
  expect(editor.querySelector('[data-h3-nle-control^="planning."]')).toBeNull();
  return { binding, view, control, editor };
}

function assemblyCapableProjection(
  state: "unavailable" | "planned" | "failed",
) {
  const allowed =
    state === "unavailable"
      ? "assemble_sequence"
      : state === "planned"
        ? "cancel_assembly"
        : "retry_assembly";
  return productionProjection(fp("a"), {
    assembly: {
      schema: "h3.context.production_assembly.projection.v1",
      state,
      progress: { completed: 0, total: 1 },
      capability_fingerprint: fp("b"),
      managed_sequence_fingerprint: fp("c"),
      artifact_receipt_fingerprints: [fp("d")],
      cut_boundary_receipt_fingerprints: [],
      output_profile_id: "legacy_av_30fps_48khz_stereo",
      assembly_job_id: state === "unavailable" ? null : "job.1",
      authorization_fingerprint: state === "unavailable" ? null : fp("f"),
      receipt_fingerprint: null,
      failure_code: state === "failed" ? "assembly_failed" : null,
      projection_fingerprint: fp("e"),
    },
    allowed_actions: [
      "add_segment_from_context",
      "set_selection",
      "read_projection",
      "release_workspace",
      allowed,
    ],
  });
}

const PLANNING_CONTROLS = [
  "planning.prepare_context",
  "planning.admit_canonical",
  "planning.review_storyboard",
  "planning.propose",
  "planning.approve_import",
];

describe("M25-16 sequence pane planning controls", () => {
  it("disables every planning control without a Production target or Context claim", () => {
    const withoutProduction = subject({ production: { status: "absent" } });
    expect(
      withoutProduction.view.container.querySelector(
        '[data-h3-nle-status="sequence-production"]',
      ),
    ).not.toBeNull();
    for (const id of PLANNING_CONTROLS)
      expect(withoutProduction.control(id).disabled, id).toBe(true);
    expect(withoutProduction.control("planning.target_seconds").disabled).toBe(
      true,
    );
    cleanup();
    const withoutContext = subject({ contextAvailable: false });
    expect(
      withoutContext.view.container.querySelector(
        '[data-h3-nle-status="sequence-context"]',
      ),
    ).not.toBeNull();
    for (const id of PLANNING_CONTROLS)
      expect(withoutContext.control(id).disabled, id).toBe(true);
    expect(
      withoutContext.binding.actions.prepareContext,
    ).not.toHaveBeenCalled();
  });

  it("sets the target seconds and policy through the binding and prepares on click", () => {
    const { binding, control } = subject();
    fireEvent.change(control("planning.target_seconds"), {
      target: { value: "20" },
    });
    expect(binding.actions.setTargetSeconds).toHaveBeenCalledWith(20);
    fireEvent.change(control("planning.target_seconds"), {
      target: { value: "500" },
    });
    expect(binding.actions.setTargetSeconds).toHaveBeenLastCalledWith(60);
    fireEvent.change(control("planning.policy"), {
      target: { value: "fixed_10" },
    });
    expect(binding.actions.setPolicy).toHaveBeenCalledWith("fixed_10");
    expect(control("planning.prepare_context").disabled).toBe(false);
    fireEvent.click(control("planning.prepare_context"));
    expect(binding.actions.prepareContext).toHaveBeenCalledTimes(1);
    // Nothing downstream is enabled before a prepared projection exists.
    expect(control("planning.admit_canonical").disabled).toBe(true);
    expect(control("planning.review_storyboard").disabled).toBe(true);
    expect(control("planning.propose").disabled).toBe(true);
    expect(control("planning.approve_import").disabled).toBe(true);
    expect(control("readiness.request").disabled).toBe(true);
  });

  it("admits, reviews, proposes and approves in order, each through its own action", () => {
    const prepared = subject({
      planning: {
        status: "prepared",
        projection: planningProjection() as never,
        boundWorkspaceFingerprint: fp("a"),
      },
    });
    expect(prepared.control("planning.admit_canonical").disabled).toBe(false);
    expect(prepared.control("planning.propose").disabled).toBe(true);
    fireEvent.click(prepared.control("planning.admit_canonical"));
    expect(prepared.binding.actions.admitStoryboard).toHaveBeenCalledWith(
      "canonical_optimized_prompt",
    );
    fireEvent.click(prepared.control("planning.review_storyboard"));
    expect(prepared.binding.actions.openStoryboardReview).toHaveBeenCalledWith(
      true,
    );
    cleanup();
    const admitted = subject({
      planning: {
        status: "admitted",
        projection: planningProjection({
          admission_id: "admission_owned",
        }) as never,
        boundWorkspaceFingerprint: fp("a"),
      },
    });
    expect(admitted.control("planning.propose").disabled).toBe(false);
    expect(admitted.control("planning.approve_import").disabled).toBe(true);
    fireEvent.click(admitted.control("planning.propose"));
    expect(admitted.binding.actions.propose).toHaveBeenCalledTimes(1);
    cleanup();
    const proposed = subject({
      planning: {
        status: "proposed",
        projection: proposedProjection() as never,
        boundWorkspaceFingerprint: fp("a"),
      },
    });
    expect(proposed.control("planning.approve_import").disabled).toBe(false);
    fireEvent.click(proposed.control("planning.approve_import"));
    expect(proposed.binding.actions.approveAndImportPlan).toHaveBeenCalledTimes(
      1,
    );
    expect(proposed.binding.actions.startSequence).not.toHaveBeenCalled();
  });

  it("disables admission, proposal and approval when the Production workspace moved", () => {
    const { control, binding } = subject({
      planning: {
        status: "proposed",
        projection: proposedProjection() as never,
        boundWorkspaceFingerprint: fp("9"),
      },
    });
    for (const id of [
      "planning.admit_canonical",
      "planning.review_storyboard",
      "planning.propose",
      "planning.approve_import",
    ])
      expect(control(id).disabled, id).toBe(true);
    expect(binding.state.planning.boundWorkspaceFingerprint).not.toBe(fp("a"));
  });

  it("edits reviewed storyboard rows through the binding and admits them explicitly", () => {
    const { control, binding, view } = subject({
      planning: {
        status: "prepared",
        projection: planningProjection() as never,
        boundWorkspaceFingerprint: fp("a"),
        storyboardReviewOpen: true,
        storyboardRows: [
          {
            shotId: "shot_1",
            ordinal: 1,
            startMilliseconds: 0,
            endMilliseconds: 10_000,
            text: "A blue sphere turns.",
            hardBoundary: false,
          },
        ],
      },
    });
    expect(
      view.container.querySelector('[data-h3-nle-region="storyboard-review"]'),
    ).not.toBeNull();
    fireEvent.click(control("planning.add_row"));
    expect(binding.actions.setStoryboardRows).toHaveBeenCalledTimes(1);
    const rows = (
      binding.actions.setStoryboardRows as unknown as {
        mock: { calls: unknown[][] };
      }
    ).mock.calls[0]![0] as { shotId: string }[];
    expect(rows).toHaveLength(2);
    expect(control("planning.admit_reviewed").disabled).toBe(false);
    fireEvent.click(control("planning.admit_reviewed"));
    expect(binding.actions.admitStoryboard).toHaveBeenCalledWith(
      "user_reviewed_typed_rows",
    );
  });

  it("requests readiness only for a current imported plan and shows a held reason", () => {
    const { control, binding, view } = subject({
      planning: {
        status: "imported",
        projection: proposedProjection() as never,
        plan: plan(),
        boundWorkspaceFingerprint: fp("a"),
      },
    });
    expect(control("readiness.request").disabled).toBe(false);
    fireEvent.click(control("readiness.request"));
    expect(binding.actions.requestReadiness).toHaveBeenCalledTimes(1);
    expect(control("sequence.start").disabled).toBe(true);
    cleanup();
    const held = subject({
      planning: {
        status: "imported",
        projection: proposedProjection() as never,
        plan: plan(),
        boundWorkspaceFingerprint: fp("a"),
      },
      readiness: {
        status: "held",
        readiness: decodeManagedReadiness({
          ...readyReadiness(),
          status: "held",
          reason: "host_capability_mismatch",
          qualification_fingerprint: null,
          qualification: null,
        }),
        boundPlanFingerprint: fp("b"),
      },
      startable: false,
    });
    expect(
      held.view.container.querySelector('[data-h3-nle-status="readiness"]'),
    ).not.toBeNull();
    expect(held.control("sequence.start").disabled).toBe(true);
    expect(held.control("readiness.request").disabled).toBe(false);
    cleanup();
    const requesting = subject({
      planning: {
        status: "imported",
        plan: plan(),
        boundWorkspaceFingerprint: fp("a"),
      },
      readiness: { status: "requesting" },
    });
    expect(requesting.control("readiness.request").disabled).toBe(true);
    void view;
  });
});

function readyPlanning(): Overrides {
  return {
    planning: {
      status: "imported",
      projection: proposedProjection() as never,
      plan: plan() as AutomaticPlanProjection,
      boundWorkspaceFingerprint: fp("a"),
    },
    readiness: {
      status: "ready",
      readiness: decodeManagedReadiness(readyReadiness()),
      boundPlanFingerprint: fp("b"),
    },
  };
}

describe("M25-16 sequence pane B1 controls", () => {
  it("starts only when the session reports the exact plan startable", () => {
    const notStartable = subject({ ...readyPlanning(), startable: false });
    expect(notStartable.control("sequence.start").disabled).toBe(true);
    cleanup();
    const startable = subject({ ...readyPlanning(), startable: true });
    expect(startable.control("sequence.start").disabled).toBe(false);
    fireEvent.click(startable.control("sequence.start"));
    expect(startable.binding.actions.startSequence).toHaveBeenCalledTimes(1);
    for (const id of [
      "sequence.detach",
      "sequence.reattach",
      "sequence.resume",
      "sequence.cancel",
      "sequence.refresh",
    ])
      expect(startable.control(id).disabled, id).toBe(true);
  });

  it("offers detach only in detach_ready and cancel while a sequence is live", () => {
    const attached = subject({
      sequence: { ui: "attached_current_child", parentSequenceId: "parent.1" },
      pointer: true,
    });
    expect(attached.control("sequence.detach").disabled).toBe(true);
    expect(attached.control("sequence.reattach").disabled).toBe(true);
    expect(attached.control("sequence.cancel").disabled).toBe(false);
    fireEvent.click(attached.control("sequence.cancel"));
    expect(attached.binding.actions.cancelSequence).toHaveBeenCalledTimes(1);
    cleanup();
    const detachReady = subject({
      sequence: { ui: "detach_ready", parentSequenceId: "parent.1" },
      pointer: true,
    });
    expect(detachReady.control("sequence.detach").disabled).toBe(false);
    fireEvent.click(detachReady.control("sequence.detach"));
    expect(detachReady.binding.actions.detachSequence).toHaveBeenCalledTimes(1);
    expect(detachReady.control("sequence.resume").disabled).toBe(true);
    cleanup();
    const busy = subject({
      sequence: {
        ui: "detach_ready",
        parentSequenceId: "parent.1",
        busy: true,
      },
      pointer: true,
    });
    for (const id of [
      "sequence.detach",
      "sequence.reattach",
      "sequence.resume",
      "sequence.cancel",
      "sequence.refresh",
    ])
      expect(busy.control(id).disabled, id).toBe(true);
  });

  it("returns through explicit reattach and resumes only from resume_ready", () => {
    const returned = subject({
      sequence: { ui: "safe_to_leave", parentSequenceId: "parent.1" },
      pointer: true,
    });
    expect(returned.control("sequence.reattach").disabled).toBe(false);
    expect(returned.control("sequence.resume").disabled).toBe(true);
    expect(returned.control("sequence.refresh").disabled).toBe(false);
    fireEvent.click(returned.control("sequence.reattach"));
    expect(returned.binding.actions.reattachSequence).toHaveBeenCalledTimes(1);
    fireEvent.click(returned.control("sequence.refresh"));
    expect(returned.binding.actions.refreshSequence).toHaveBeenCalledTimes(1);
    cleanup();
    const resumable = subject({
      sequence: { ui: "resume_ready", parentSequenceId: "parent.1" },
      pointer: true,
    });
    expect(resumable.control("sequence.resume").disabled).toBe(false);
    fireEvent.click(resumable.control("sequence.resume"));
    expect(resumable.binding.actions.resumeSequence).toHaveBeenCalledTimes(1);
    expect(resumable.binding.actions.startSequence).not.toHaveBeenCalled();
    cleanup();
    const noPointer = subject({ sequence: { ui: "idle" }, pointer: false });
    expect(noPointer.control("sequence.reattach").disabled).toBe(true);
    expect(noPointer.control("sequence.refresh").disabled).toBe(true);
    expect(noPointer.control("sequence.cancel").disabled).toBe(true);
    expect(
      noPointer.view.container.querySelector(
        '[data-h3-nle-status="recovery-pointer"]',
      ),
    ).toBeNull();
    cleanup();
    const withPointer = subject({ sequence: { ui: "idle" }, pointer: true });
    expect(
      withPointer.view.container.querySelector(
        '[data-h3-nle-status="recovery-pointer"]',
      ),
    ).not.toBeNull();
    expect(withPointer.control("sequence.reattach").disabled).toBe(false);
  });

  it("offers per-segment retry only for failed slots and passes the exact segment", () => {
    const projection = {
      parentSequenceId: "parent.1",
      state: "paused_failure",
      revision: 4,
      slots: [
        { segmentId: "segment.one", ordinal: 1, state: "succeeded" },
        { segmentId: "segment.two", ordinal: 2, state: "failed" },
      ],
    } as unknown as ManagedSequenceProjection;
    const { view, binding } = subject({
      sequence: { ui: "failed", parentSequenceId: "parent.1", projection },
      pointer: true,
    });
    const retries = view.container.querySelectorAll<HTMLButtonElement>(
      '[data-h3-nle-control="sequence.retry_segment"]',
    );
    expect(retries).toHaveLength(1);
    expect(
      retries[0]!
        .closest("[data-h3-nle-segment]")!
        .getAttribute("data-h3-nle-segment"),
    ).toBe("segment.two");
    fireEvent.click(retries[0]!);
    expect(binding.actions.retrySegment).toHaveBeenCalledWith("segment.two");
  });
});

describe("M25-16 sequence pane assembly controls", () => {
  it("enables exactly the assembly action Production allows and dispatches it", () => {
    const assemble = subject({
      production: {
        status: "ready",
        projection: assemblyCapableProjection("unavailable"),
      },
    });
    expect(assemble.control("assembly.assemble").disabled).toBe(false);
    expect(assemble.control("assembly.cancel").disabled).toBe(true);
    expect(assemble.control("assembly.retry").disabled).toBe(true);
    fireEvent.click(assemble.control("assembly.assemble"));
    expect(assemble.binding.actions.assembly).toHaveBeenCalledWith(
      "assemble_sequence",
    );
    cleanup();
    const running = subject({
      production: {
        status: "ready",
        projection: assemblyCapableProjection("planned"),
      },
    });
    expect(running.control("assembly.assemble").disabled).toBe(true);
    expect(running.control("assembly.cancel").disabled).toBe(false);
    fireEvent.click(running.control("assembly.cancel"));
    expect(running.binding.actions.assembly).toHaveBeenCalledWith(
      "cancel_assembly",
    );
    cleanup();
    const failed = subject({
      production: {
        status: "ready",
        projection: assemblyCapableProjection("failed"),
      },
    });
    expect(failed.control("assembly.retry").disabled).toBe(false);
    fireEvent.click(failed.control("assembly.retry"));
    expect(failed.binding.actions.assembly).toHaveBeenCalledWith(
      "retry_assembly",
    );
    fireEvent.click(failed.control("production.refresh"));
    expect(failed.binding.actions.refreshProduction).toHaveBeenCalledTimes(1);
  });

  it("keeps every assembly control disabled without capability and while Production is busy", () => {
    const none = subject();
    for (const id of ["assembly.assemble", "assembly.cancel", "assembly.retry"])
      expect(none.control(id).disabled, id).toBe(true);
    expect(none.control("production.refresh").disabled).toBe(false);
    cleanup();
    const pending = subject({
      production: {
        status: "pending",
        projection: assemblyCapableProjection("unavailable"),
      },
    });
    for (const id of [
      "assembly.assemble",
      "assembly.cancel",
      "assembly.retry",
      "production.refresh",
    ])
      expect(pending.control(id).disabled, id).toBe(true);
    expect(pending.binding.actions.assembly).not.toHaveBeenCalled();
  });
});

describe("M25-63 the editor's Sequence tab", () => {
  it("says planning happens in Production and keeps the readiness request under its own gate", () => {
    const blocked = subject();
    expect(
      blocked.editor.querySelector('[data-h3-nle-status="planning-location"]')
        ?.textContent,
    ).toBe(
      "Plan the video in Production. Closing the editor returns you there.",
    );
    expect(blocked.editor.querySelectorAll("h4")[0]?.textContent).toBe(
      "Whole-video sequence",
    );
    const request = () =>
      blocked.editor.querySelector<HTMLButtonElement>(
        '[data-h3-nle-control="readiness.request"]',
      )!;
    expect(request().disabled).toBe(true);
    const imported = subject({
      planning: {
        status: "imported",
        projection: proposedProjection() as never,
        plan: plan(),
        boundWorkspaceFingerprint: fp("a"),
      },
    });
    const editorRequest = imported.editor.querySelector<HTMLButtonElement>(
      '[data-h3-nle-control="readiness.request"]',
    )!;
    expect(editorRequest.disabled).toBe(false);
    fireEvent.click(editorRequest);
    expect(imported.binding.actions.requestReadiness).toHaveBeenCalledTimes(1);
    expect(
      imported.editor.querySelector('[data-h3-nle-status="readiness"]'),
    ).not.toBeNull();
  });
});
