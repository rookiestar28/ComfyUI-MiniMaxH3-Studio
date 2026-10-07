import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProductionWorkbench } from "../src/components/ProductionWorkbench";
import {
  decodeProductionAccumulatedProject,
  productionAccumulatedProjectFingerprint,
} from "../src/contracts/productionAccumulationCodec";
import { decodeProductionWorkbenchProjection } from "../src/contracts/productionWorkbenchCodec";
import { decodeGenerationSequenceProjection } from "../src/contracts/generationSequenceCodec";
import {
  decodeSemanticProposalReviewHandle,
  decodeSemanticProposalReviewProjection,
} from "../src/contracts/semanticProposalReviewCodec";
import type { ProductionProposalRow } from "../src/host/productionProposalDispatcher";
import { generationSequenceWire } from "./generationSequenceFixture";
import { unavailableProductionAssemblyWire } from "./support/productionAssemblyWire";
import { initialNleWorkspaceState } from "../src/state/nleWorkspaceState";
import type { NleWorkspaceBinding } from "../src/components/nle/nleWorkspaceBinding";

const fingerprint = `sha256:${"a".repeat(64)}`;
const emptyProjectMaterial = {
  schema: "h3.context.production_accumulated_project.v1",
  workspace_handle: `pw_${"e".repeat(43)}`,
  workspace_id: "workspace_empty",
  project_revision: 2,
  workspace: null,
  attempts: [
    {
      candidate_id: "segment_empty",
      attempt_id: "attempt_empty",
      member_segment_id: "segment_empty",
      status: "cancelled",
      recovery: "retry",
      committed: false,
    },
  ],
  capabilities: ["read", "admit_generation", "release_generation"],
};
const emptyProject = decodeProductionAccumulatedProject({
  ...emptyProjectMaterial,
  project_fingerprint:
    productionAccumulatedProjectFingerprint(emptyProjectMaterial),
});
const projection = decodeProductionWorkbenchProjection({
  schema: "h3.context.production_workbench.projection.v1",
  workspace_handle: `pw_${"a".repeat(43)}`,
  workspace_id: "workspace_1",
  workspace_revision: 2,
  workspace_fingerprint: fingerprint,
  segments: [
    {
      segment_id: "segment_1",
      ordinal: 1,
      task_mode: "t2va",
      duration: {
        duration_milliseconds: 4167,
        delivered_milliseconds: 4458,
        frame_count: 107,
        snapped: true,
      },
      relation: "independent",
      predecessor_segment_id: null,
      boundary_kind: "independent",
      closure_state: "dirty_self",
      job_state: "planned",
      artifact_state: "complete",
      continuity_state: "unavailable",
      delivered_geometry: {
        format: "mp4",
        frame_count: 107,
        width: 864,
        height: 480,
      },
    },
    {
      segment_id: "segment_2",
      ordinal: 2,
      task_mode: "t2va",
      duration: {
        duration_milliseconds: 12500,
        delivered_milliseconds: 12958,
        frame_count: 311,
        snapped: true,
      },
      relation: "predecessor",
      predecessor_segment_id: "segment_1",
      boundary_kind: "native_handoff",
      closure_state: "dirty_upstream",
      job_state: "planned",
      artifact_state: "unavailable",
      continuity_state: "unavailable",
      delivered_geometry: null,
    },
  ],
  selected_segment_ids: ["segment_1"],
  run: { state: "ready", completed: 0, total: 2 },
  generation_sequence: {
    schema: "h3.context.generation_sequence_projection.v1",
    sequence_id: "sequence.1",
    sequence_fingerprint: `sha256:${"b".repeat(64)}`,
    state_fingerprint: `sha256:${"c".repeat(64)}`,
    workspace_id: "workspace_1",
    workspace_revision: 2,
    workspace_fingerprint: fingerprint,
    correlation: { prompt_id: "prompt.1", execution_node_id: "node.1" },
  },
  reconstruction: { state: "unavailable" },
  assembly: unavailableProductionAssemblyWire(),
  authority_versions: ["h3.context.generation_sequence_projection.v1"],
  outputs: [],
  allowed_actions: [
    "add_segment_from_context",
    "replace_segment_from_context",
    "set_segment_relation",
    "delete_segment",
    "reorder_segments",
    "set_selection",
    "read_projection",
    "release_workspace",
    "submit_generation_job",
  ],
  blocker_codes: ["sequence_authority_unavailable"],
  limits: { max_segments: 64, max_outputs: 65 },
});
const proposalHandle = decodeSemanticProposalReviewHandle({
  schema: "h3.context.semantic_proposal_review_handle.v1",
  review_id: `review_${"r".repeat(32)}`,
  transaction_fingerprint: `sha256:${"b".repeat(64)}`,
  workspace_fingerprint: `sha256:${"c".repeat(64)}`,
  report_fingerprint: `sha256:${"d".repeat(64)}`,
  correlation: { prompt_id: "prompt.1", execution_node_id: "node.1" },
  available: true,
  reason: "review_available",
});
const proposalReview = decodeSemanticProposalReviewProjection({
  schema: "h3.context.semantic_proposal_review.v1",
  review_id: proposalHandle.review_id,
  transaction_fingerprint: proposalHandle.transaction_fingerprint,
  workspace_id: "proposal.workspace",
  workspace_revision: 1,
  workspace_fingerprint: proposalHandle.workspace_fingerprint,
  report_fingerprint: proposalHandle.report_fingerprint,
  attempt: 1,
  revision: 1,
  state: "ready_for_review",
  segment_id: "segment_1",
  correlation: proposalHandle.correlation,
  changed_collections: [],
  groups: [],
  clarifications: [],
  uncertainty_codes: [],
  reason_code: "review_ready",
  actions: {
    proposal_read: true,
    proposal_resolve: false,
    proposal_accept: true,
    proposal_reject: true,
    proposal_cancel: true,
    edit: false,
    regenerate: false,
  },
  action_reasons: {
    edit: "source_owner_unavailable",
    regenerate: "source_owner_unavailable",
  },
  terminal: null,
});

afterEach(cleanup);

describe("M17-18 Production entry rows", () => {
  const contextWorkspaceHandle = `ws_${"b".repeat(43)}`;
  const localeCases = [
    {
      locale: "en" as const,
      preparing: "Preparing Production workspace.",
      loading: "Production action in progress.",
      recover: "Retry Production setup",
      empty: "No generated segments yet.",
      latest: "Latest attempt: Cancelled.",
      newProject: "New project",
    },
    {
      locale: "zh-TW" as const,
      preparing: "正在準備製作工作區。",
      loading: "正在處理製作操作。",
      recover: "重試製作設定",
      empty: "尚無已生成的片段。",
      latest: "最近一次嘗試：已取消。",
      newProject: "新專案",
    },
    {
      locale: "zh-CN" as const,
      preparing: "正在准备制作工作区。",
      loading: "正在处理制作操作。",
      recover: "重试制作设置",
      empty: "尚无已生成的片段。",
      latest: "最近一次尝试：已取消。",
      newProject: "新项目",
    },
  ];

  it("keeps absent and loading rows distinct in all supported locales", () => {
    for (const copy of localeCases) {
      const onIntent = vi.fn();
      const { rerender, unmount } = render(
        <ProductionWorkbench
          locale={copy.locale}
          contextWorkspaceHandle={contextWorkspaceHandle}
          state={{ status: "absent" }}
          onIntent={onIntent}
        />,
      );
      expect(screen.getByText(copy.preparing)).not.toBeNull();
      expect(screen.queryByText(copy.loading)).toBeNull();
      expect(
        screen.queryByRole("button", { name: "Create from current Context" }),
      ).toBeNull();
      expect(onIntent).not.toHaveBeenCalled();

      rerender(
        <ProductionWorkbench
          locale={copy.locale}
          contextWorkspaceHandle={contextWorkspaceHandle}
          state={{ status: "loading" }}
          onIntent={onIntent}
        />,
      );
      expect(screen.getByText(copy.loading)).not.toBeNull();
      expect(screen.queryByText(copy.preparing)).toBeNull();
      expect(
        screen.queryByRole("button", { name: "Create from current Context" }),
      ).toBeNull();
      expect(onIntent).not.toHaveBeenCalled();
      unmount();
    }
  });

  it("renders an empty accumulated project and localized attempt status", () => {
    for (const copy of localeCases) {
      const onNewProject = vi.fn();
      const { unmount } = render(
        <ProductionWorkbench
          locale={copy.locale}
          state={{ status: "empty", project: emptyProject }}
          onIntent={vi.fn()}
          onNewProject={onNewProject}
        />,
      );

      expect(screen.getByText(copy.empty)).not.toBeNull();
      expect(screen.getByText(copy.latest)).not.toBeNull();
      fireEvent.click(screen.getByRole("button", { name: copy.newProject }));
      expect(onNewProject).toHaveBeenCalledOnce();
      unmount();
    }
  });

  it("dispatches create recovery with the stable production focus key", () => {
    for (const copy of localeCases) {
      const onIntent = vi.fn();
      const { unmount } = render(
        <ProductionWorkbench
          locale={copy.locale}
          contextWorkspaceHandle={contextWorkspaceHandle}
          state={{
            status: "error",
            reason: "network",
            recovery: "create",
          }}
          onIntent={onIntent}
        />,
      );
      const retry = screen.getByRole("button", { name: copy.recover });
      expect(screen.getByRole("alert")).toBeTruthy();
      expect(retry.dataset.h3FocusKey).toBe("production-create");
      fireEvent.click(retry);
      expect(onIntent).toHaveBeenCalledWith({
        action: "create_workspace_from_context",
      });
      unmount();
    }
  });

  it("keeps retained-handle read recovery available without Context authority", () => {
    const onIntent = vi.fn();
    render(
      <ProductionWorkbench
        locale="en"
        state={{
          status: "gone",
          reason: "restored_read_failed",
          recovery: "read",
        }}
        onIntent={onIntent}
      />,
    );
    const retry = screen.getByRole("button", {
      name: "Retry Production setup",
    });
    expect(retry.dataset.h3FocusKey).toBe("production-create");
    fireEvent.click(retry);
    expect(onIntent).toHaveBeenCalledWith({ action: "read_projection" });
  });
});

describe("M17-12 Production workbench UI", () => {
  it("edits the shared project target without queueing or importing", () => {
    const onIntent = vi.fn();
    const setTargetSeconds = vi.fn();
    render(
      <ProductionWorkbench
        locale="en"
        state={{ status: "ready", projection }}
        onIntent={onIntent}
        planning={{
          state: initialNleWorkspaceState,
          contextAvailable: true,
          actions: {
            setTargetSeconds,
          } as unknown as NleWorkspaceBinding["actions"],
        }}
      />,
    );

    fireEvent.change(
      screen.getByRole("spinbutton", {
        name: "Project target duration (seconds)",
      }),
      { target: { value: "24" } },
    );
    expect(setTargetSeconds).toHaveBeenCalledWith(24);
    expect(onIntent).not.toHaveBeenCalled();
  });

  it("shows the accumulating project identity, New project, and added member status", () => {
    const onNewProject = vi.fn();
    render(
      <ProductionWorkbench
        locale="en"
        state={{ status: "ready", projection }}
        onIntent={vi.fn()}
        destination={{ kind: "project", ordinal: 2, segmentCount: 2 }}
        accumulationNotice={{
          kind: "added",
          projectOrdinal: 2,
          segmentOrdinal: 2,
          viewUpdated: true,
        }}
        onNewProject={onNewProject}
      />,
    );

    expect(screen.getByText("Project 2 · 2 segments")).toBeTruthy();
    expect(screen.getByText("Added segment 2").getAttribute("role")).toBe(
      "status",
    );
    expect(
      screen.getByRole("region", { name: "Segment overview" }),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "New project" }));
    expect(onNewProject).toHaveBeenCalledOnce();
  });

  it.each([
    ["en", "864×480 · MP4 · 107 frames"],
    ["zh-TW", "864×480 · MP4 · 107 影格"],
    ["zh-CN", "864×480 · MP4 · 107 帧"],
  ] as const)("renders delivered geometry in %s", (locale, expected) => {
    render(
      <ProductionWorkbench
        locale={locale}
        contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
        state={{ status: "ready", projection }}
        onIntent={vi.fn()}
      />,
    );

    expect(
      screen.getByTestId("production-delivered-geometry").textContent,
    ).toContain(expected);
  });

  it("renders four regions, proportional read-only timeline, and non-drag reorder", async () => {
    const onIntent = vi.fn();
    const { container, rerender } = render(
      <ProductionWorkbench
        locale="en"
        contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
        state={{ status: "ready", projection }}
        onIntent={onIntent}
      />,
    );
    for (const name of ["Segment", "Segment overview", "Run", "Outputs"])
      expect(screen.getByRole("region", { name })).not.toBeNull();
    const bars = screen.getAllByTestId("production-timeline-segment");
    // M17-25: the track is proportional in derived frames, which are producible
    // by construction. The first fixture segment requests 4.167 s and is
    // delivered 107 frames.
    expect(bars[0]?.style.flexGrow).toBe("107");
    expect(bars[1]?.style.flexGrow).toBe("311");
    await userEvent.click(
      screen.getByRole("button", { name: /select segment 2/i }),
    );
    expect(onIntent).toHaveBeenCalledWith({
      action: "set_selection",
      segmentIds: ["segment_2"],
    });
    // M21-03 AC-02: exactly one row is expanded at a time, and the reorder
    // controls belong to it. Selecting is what expands a row, so the projection
    // is re-rendered with the single selection the click above asked for rather
    // than reaching for a control on a collapsed row.
    rerender(
      <ProductionWorkbench
        locale="en"
        contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
        state={{
          status: "ready",
          projection: { ...projection, selectedSegmentIds: ["segment_2"] },
        }}
        onIntent={onIntent}
      />,
    );
    expect(container.querySelectorAll(".h3p-s > li .h3p-m")).toHaveLength(1);
    await userEvent.click(
      screen.getByRole("button", { name: /move segment 2 left/i }),
    );
    expect(onIntent).toHaveBeenCalledWith({
      action: "reorder_segments",
      segmentIds: ["segment_2", "segment_1"],
    });
    rerender(
      <ProductionWorkbench
        locale="en"
        contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
        state={{
          status: "ready",
          projection: { ...projection, selectedSegmentIds: ["segment_1"] },
        }}
        onIntent={onIntent}
      />,
    );
    const position = screen.getByRole("spinbutton", {
      name: /move segment 1 to position/i,
    });
    await userEvent.clear(position);
    await userEvent.type(position, "2");
    await userEvent.click(
      screen.getByRole("button", { name: /apply position for segment 1/i }),
    );
    expect(onIntent).toHaveBeenCalledWith({
      action: "reorder_segments",
      segmentIds: ["segment_2", "segment_1"],
    });
    expect(screen.queryByRole("button", { name: /duration/i })).toBeNull();
    // M17-21: canonical codes stay inspectable in data-state while the visible
    // text is the locale-mapped label, so no raw enum reaches the user.
    for (const state of ["dirty_self", "planned", "complete", "unavailable"])
      expect(
        container.querySelectorAll(`[data-state="${state}"]`).length,
      ).toBeGreaterThan(0);
    for (const [state, label] of [
      ["dirty_self", "Changed here"],
      ["planned", "Planned"],
      ["complete", "Complete"],
      ["unavailable", "Not available"],
    ] as const) {
      const chip = container.querySelector(`.h3p-c[data-state="${state}"]`);
      expect(chip?.textContent).toBe(label);
      expect(chip?.getAttribute("title")).toBe(state);
    }
  });

  it("discards pointer reorder preparation after pointer cancel", () => {
    const onIntent = vi.fn();
    render(
      <ProductionWorkbench
        locale="en"
        contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
        state={{ status: "ready", projection }}
        onIntent={onIntent}
      />,
    );
    const first = screen.getByRole("button", { name: /select segment 1/i });
    expect(first).toBeDefined();
    first?.dispatchEvent(new Event("pointercancel", { bubbles: true }));
    expect(onIntent).not.toHaveBeenCalled();
  });

  it("releases pointer ownership on Escape and ignores a late pointerup", () => {
    const onIntent = vi.fn();
    render(
      <ProductionWorkbench
        locale="en"
        contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
        state={{ status: "ready", projection }}
        onIntent={onIntent}
      />,
    );
    const first = screen.getByRole("button", { name: /select segment 1/i });
    const releasePointerCapture = vi.fn();
    Object.assign(first, {
      setPointerCapture: vi.fn(),
      hasPointerCapture: vi.fn(() => true),
      releasePointerCapture,
    });
    fireEvent.pointerDown(first, { pointerId: 7, isPrimary: true });
    fireEvent.keyDown(first, { key: "Escape" });
    fireEvent.pointerUp(first, { pointerId: 7, isPrimary: true });
    expect(releasePointerCapture).toHaveBeenCalledWith(7);
    expect(onIntent).not.toHaveBeenCalled();
  });

  it("requires labelled confirmation before explicit release", async () => {
    const onIntent = vi.fn();
    render(
      <ProductionWorkbench
        locale="en"
        contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
        state={{ status: "ready", projection }}
        onIntent={onIntent}
      />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Release workspace" }),
    );
    expect(onIntent).not.toHaveBeenCalled();
    await userEvent.click(
      screen.getByRole("button", { name: "Confirm release" }),
    );
    expect(onIntent).toHaveBeenCalledWith({ action: "release_workspace" });
  });

  it("exposes only the exact joined M17-08 command as a direct intent", async () => {
    const wire = generationSequenceWire();
    Object.assign(wire, {
      sequence_id: "sequence.1",
      sequence_fingerprint: `sha256:${"b".repeat(64)}`,
      state_fingerprint: `sha256:${"c".repeat(64)}`,
      workspace_id: "workspace_1",
      workspace_revision: 2,
      workspace_fingerprint: fingerprint,
    });
    Object.assign(wire.progress[0]!, {
      segment_id: "segment_1",
      ordinal: 1,
    });
    Object.assign(wire.eligible_commands[0]!, {
      segment_id: "segment_1",
      ordinal: 1,
      duration: {
        duration_milliseconds: 4167,
        frame_count: 107,
        delivered_milliseconds: 4458,
        snapped: true,
      },
    });
    const generationSequence = decodeGenerationSequenceProjection(wire);
    const command = generationSequence.eligible_commands[0]!;
    const onIntent = vi.fn();
    render(
      <ProductionWorkbench
        locale="en"
        contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
        state={{ status: "ready", projection }}
        generationControls={[
          {
            key: `${generationSequence.sequence_fingerprint}:${generationSequence.state_fingerprint}:${command.job_id}:${command.attempt}`,
            jobId: command.job_id,
            ordinal: command.ordinal,
            disposition: "available",
          },
        ]}
        onIntent={onIntent}
      />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Generate segment 1" }),
    );
    expect(onIntent).toHaveBeenCalledWith({
      action: "submit_generation_job",
      jobId: "job.1",
    });
  });

  it("removes actionable Generate after claim and exposes bounded local failure", () => {
    const onIntent = vi.fn();
    const control = {
      key: `sha256:${"b".repeat(64)}:sha256:${"c".repeat(64)}:job.1:1`,
      jobId: "job.1",
      ordinal: 1,
    } as const;
    const { rerender } = render(
      <ProductionWorkbench
        locale="en"
        contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
        state={{ status: "ready", projection }}
        generationControls={[{ ...control, disposition: "pending" }]}
        onIntent={onIntent}
      />,
    );
    expect(
      screen.queryByRole("button", { name: "Generate segment 1" }),
    ).toBeNull();
    expect(
      screen.getByText("Generation submission pending for segment 1."),
    ).toBeTruthy();

    rerender(
      <ProductionWorkbench
        locale="en"
        contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
        state={{ status: "ready", projection }}
        generationControls={[{ ...control, disposition: "generation_failed" }]}
        onIntent={onIntent}
      />,
    );
    expect(screen.getByRole("alert").textContent).toContain(
      "Generation submission failed for segment 1.",
    );
    expect(onIntent).not.toHaveBeenCalled();
  });

  it("announces controller capacity once for the whole page", () => {
    const onIntent = vi.fn();
    const capacity =
      "Generation controls are unavailable until the page is reloaded.";
    render(
      <ProductionWorkbench
        locale="en"
        contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
        state={{ status: "ready", projection }}
        generationControls={[
          {
            key: "sequence:state:job.1:1",
            jobId: "job.1",
            ordinal: 1,
            disposition: "generation_control_capacity",
          },
          {
            key: "sequence:state:job.2:1",
            jobId: "job.2",
            ordinal: 2,
            disposition: "generation_control_capacity",
          },
        ]}
        onIntent={onIntent}
      />,
    );

    expect(screen.getAllByText(capacity)).toHaveLength(1);
    expect(screen.getByText(capacity).getAttribute("role")).toBe("status");
    expect(onIntent).not.toHaveBeenCalled();
  });

  it("reviews the immutable canonical selection with unique row ARIA joins", async () => {
    const selectedProjection = {
      ...projection,
      selectedSegmentIds: ["segment_1", "segment_2"],
    };
    const reviewHandle = decodeSemanticProposalReviewHandle({
      schema: "h3.context.semantic_proposal_review_handle.v1",
      review_id: `review_${"r".repeat(32)}`,
      transaction_fingerprint: `sha256:${"b".repeat(64)}`,
      workspace_fingerprint: `sha256:${"c".repeat(64)}`,
      report_fingerprint: `sha256:${"d".repeat(64)}`,
      correlation: { prompt_id: "prompt.1", execution_node_id: "node.1" },
      available: true,
      reason: "review_available",
    });
    const proposalRows: readonly ProductionProposalRow[] = [1, 2].map(
      (ordinal) => ({
        segmentId: `segment_${ordinal}`,
        ordinal,
        status: "not_issued",
        actionable: true,
        reviewState: { status: "closed", handle: reviewHandle },
      }),
    );
    const onProposalRead = vi.fn();
    const { container } = render(
      <ProductionWorkbench
        locale="en"
        contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
        state={{ status: "ready", projection: selectedProjection }}
        proposalRows={proposalRows}
        onProposalRead={onProposalRead}
        onProposalClose={() => undefined}
        onProposalAction={() => undefined}
        onIntent={() => undefined}
      />,
    );
    expect(
      screen.getByRole<HTMLButtonElement>("button", {
        name: /^Understand segment( \d+)?$/,
      }).disabled,
    ).toBe(true);
    await userEvent.click(
      screen.getByRole("button", {
        name: /^Understand selected \(\d+\)$/,
      }),
    );
    expect(onProposalRead).toHaveBeenCalledWith(["segment_1", "segment_2"]);
    const reviews = [...container.querySelectorAll(".h3-semantic-review")];
    expect(reviews).toHaveLength(2);
    expect(
      new Set(reviews.map((review) => review.getAttribute("aria-labelledby")))
        .size,
    ).toBe(2);
  });

  it("keeps a closed Production review mounted and restores row-local focus", async () => {
    const selectedProjection = {
      ...projection,
      selectedSegmentIds: ["segment_1"],
    };
    const ready: ProductionProposalRow = {
      segmentId: "segment_1",
      ordinal: 1,
      status: "ready",
      actionable: true,
      reviewState: {
        status: "ready",
        handle: proposalHandle,
        projection: proposalReview,
      },
    };
    const closed: ProductionProposalRow = {
      segmentId: "segment_1",
      ordinal: 1,
      status: "not_issued",
      actionable: true,
      reviewState: { status: "closed", handle: proposalHandle },
    };
    let rerender!: ReturnType<typeof render>["rerender"];
    const view = (row: ProductionProposalRow) => (
      <ProductionWorkbench
        locale="en"
        contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
        state={{ status: "ready", projection: selectedProjection }}
        proposalRows={[row]}
        onProposalRead={() => undefined}
        onProposalClose={() => rerender(view(closed))}
        onProposalAction={() => undefined}
        onIntent={() => undefined}
      />
    );
    ({ rerender } = render(view(ready)));

    await userEvent.click(
      screen.getByRole("button", { name: /close intent review/i }),
    );

    expect(screen.getByRole("button", { name: /understand intent/i })).toBe(
      document.activeElement,
    );
  });

  it("announces proposal source capacity once for the whole page", () => {
    const selectedProjection = {
      ...projection,
      selectedSegmentIds: ["segment_1", "segment_2"],
    };
    const capacity =
      "Proposal source capacity is full; wait for retained sources to expire.";
    render(
      <ProductionWorkbench
        locale="en"
        contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
        state={{ status: "ready", projection: selectedProjection }}
        proposalCapacity
        proposalRows={[1, 2].map((ordinal) => ({
          segmentId: `segment_${ordinal}`,
          ordinal,
          status: "unavailable" as const,
          actionable: false,
          reason: "capacity" as const,
        }))}
        onProposalRead={() => undefined}
        onIntent={() => undefined}
      />,
    );
    expect(screen.getAllByText(capacity)).toHaveLength(1);
    expect(screen.getByText(capacity).getAttribute("role")).toBe("status");
  });

  it("explains an optional missing proposal once without presenting fake progress", () => {
    const selectedProjection = {
      ...projection,
      selectedSegmentIds: ["segment_1", "segment_2"],
    };
    const view = (locale: "en" | "zh-TW" | "zh-CN") => (
      <ProductionWorkbench
        locale={locale}
        contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
        state={{ status: "ready", projection: selectedProjection }}
        proposalRows={[1, 2].map((ordinal) => ({
          segmentId: `segment_${ordinal}`,
          ordinal,
          status: "unavailable" as const,
          actionable: false,
          reason: "not_provided" as const,
        }))}
        onProposalRead={() => undefined}
        onIntent={() => undefined}
      />
    );
    const { rerender } = render(view("en"));

    expect(
      screen.getAllByText(
        "No intent proposal was supplied for the selected segments.",
      ),
    ).toHaveLength(1);
    expect(screen.queryByText(/Proposal review \d+ of \d+/)).toBeNull();
    expect(screen.queryByText("Proposal source unavailable")).toBeNull();
    rerender(view("zh-TW"));
    expect(screen.getByText("所選片段未提供意圖提案。")).toBeDefined();
    rerender(view("zh-CN"));
    expect(screen.getByText("所选片段未提供意图提案。")).toBeDefined();
  });

  it("localizes each unavailable and retryable proposal reason", () => {
    const selectedProjection = {
      ...projection,
      selectedSegmentIds: ["segment_1"],
    };
    const cases = [
      [
        "en",
        "source_unavailable",
        "unavailable",
        "Proposal source unavailable",
      ],
      [
        "en",
        "unbound",
        "unavailable",
        "The intent proposal cannot be matched to this segment.",
      ],
      [
        "en",
        "expired",
        "unavailable",
        "The intent proposal expired; obtain it again from its original Context workflow.",
      ],
      [
        "en",
        "stale",
        "stale",
        "The intent proposal no longer matches this segment.",
      ],
      [
        "en",
        "capacity",
        "unavailable",
        "Proposal source capacity is full; wait for retained sources to expire.",
      ],
      [
        "en",
        "request_failed",
        "failed",
        "The intent proposal could not be read; retry while its source remains valid.",
      ],
      [
        "en",
        "timeout",
        "client_aborted",
        "Reading the intent proposal timed out; retry while its source remains valid.",
      ],
      ["zh-TW", "source_unavailable", "unavailable", "提案來源不可用"],
      ["zh-TW", "unbound", "unavailable", "無法將意圖提案對應到此片段。"],
      [
        "zh-TW",
        "expired",
        "unavailable",
        "意圖提案已過期；請從原始 Context 工作流程重新取得。",
      ],
      ["zh-TW", "stale", "stale", "意圖提案已不再符合此片段。"],
      [
        "zh-TW",
        "capacity",
        "unavailable",
        "提案來源保留空間已滿；請等待既有來源過期。",
      ],
      [
        "zh-TW",
        "request_failed",
        "failed",
        "無法讀取意圖提案；來源仍有效時可以重試。",
      ],
      [
        "zh-TW",
        "timeout",
        "client_aborted",
        "讀取意圖提案逾時；來源仍有效時可以重試。",
      ],
      ["zh-CN", "source_unavailable", "unavailable", "提案来源不可用"],
      ["zh-CN", "unbound", "unavailable", "无法将意图提案匹配到此片段。"],
      [
        "zh-CN",
        "expired",
        "unavailable",
        "意图提案已过期；请从原始 Context 工作流重新获取。",
      ],
      ["zh-CN", "stale", "stale", "意图提案已不再匹配此片段。"],
      [
        "zh-CN",
        "capacity",
        "unavailable",
        "提案来源保留空间已满；请等待现有来源过期。",
      ],
      [
        "zh-CN",
        "request_failed",
        "failed",
        "无法读取意图提案；来源仍有效时可以重试。",
      ],
      [
        "zh-CN",
        "timeout",
        "client_aborted",
        "读取意图提案超时；来源仍有效时可以重试。",
      ],
    ] as const;
    const view = (
      locale: (typeof cases)[number][0],
      reason: (typeof cases)[number][1],
      status: (typeof cases)[number][2],
    ) => {
      const reviewState =
        status === "failed"
          ? ({
              status: "error",
              handle: proposalHandle,
              reason: "request_failed",
            } as const)
          : status === "client_aborted"
            ? ({ status: "closed", handle: proposalHandle } as const)
            : undefined;
      return (
        <ProductionWorkbench
          locale={locale}
          contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
          state={{ status: "ready", projection: selectedProjection }}
          proposalRows={[
            {
              segmentId: "segment_1",
              ordinal: 1,
              status,
              actionable: reviewState !== undefined,
              reason,
              ...(reviewState === undefined ? {} : { reviewState }),
            },
          ]}
          onProposalRead={() => undefined}
          onProposalClose={() => undefined}
          onProposalAction={() => undefined}
          onIntent={() => undefined}
        />
      );
    };
    const [firstLocale, firstReason, firstStatus] = cases[0];
    const { rerender } = render(view(firstLocale, firstReason, firstStatus));

    for (const [locale, reason, status, expected] of cases) {
      rerender(view(locale, reason, status));
      expect(screen.getAllByText(expected)).toHaveLength(1);
    }
  });

  it("prevents an overlapping selected-read batch while a row is loading", () => {
    render(
      <ProductionWorkbench
        locale="en"
        contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
        state={{ status: "ready", projection }}
        proposalRows={[
          {
            segmentId: "segment_1",
            ordinal: 1,
            status: "loading",
            actionable: true,
            reviewState: { status: "loading", handle: proposalHandle },
          },
        ]}
        onProposalRead={() => undefined}
        onIntent={() => undefined}
      />,
    );

    expect(
      screen.getByRole<HTMLButtonElement>("button", {
        name: /^Understand selected \(\d+\)$/,
      }).disabled,
    ).toBe(true);
  });

  it("disables proposal reads when a stale row no longer has an exact binding", () => {
    render(
      <ProductionWorkbench
        locale="en"
        contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
        state={{ status: "ready", projection }}
        proposalRows={[
          {
            segmentId: "segment_1",
            ordinal: 1,
            status: "stale",
            reason: "stale",
            actionable: false,
          },
        ]}
        onProposalRead={() => undefined}
        onIntent={() => undefined}
      />,
    );

    expect(
      screen.getByRole<HTMLButtonElement>("button", {
        name: /^Understand segment( \d+)?$/,
      }).disabled,
    ).toBe(true);
    expect(
      screen.getByRole<HTMLButtonElement>("button", {
        name: /^Understand selected \(\d+\)$/,
      }).disabled,
    ).toBe(true);
  });

  it("binds exact clip preview controls without changing tile selection", async () => {
    const onMediaPreview = vi.fn();
    const onIntent = vi.fn();
    const previewProjection = {
      ...projection,
      reconstruction: { state: "complete" as const },
      outputs: [
        {
          outputHandle: `out_${"1".repeat(40)}`,
          ordinal: 1,
          state: "ready" as const,
          segmentId: null,
          preview: true,
        },
        {
          outputHandle: `out_${"2".repeat(40)}`,
          ordinal: 2,
          state: "ready" as const,
          segmentId: projection.segments[0]!.segmentId,
          preview: true,
        },
        {
          outputHandle: `out_${"3".repeat(40)}`,
          ordinal: 3,
          state: "ready" as const,
          segmentId: projection.segments[1]!.segmentId,
          preview: false,
        },
      ],
      allowedActions: [...projection.allowedActions, "preview_output" as const],
    };
    const view = render(
      <ProductionWorkbench
        locale="en"
        state={{ status: "ready", projection: previewProjection }}
        onIntent={onIntent}
        onMediaPreview={onMediaPreview}
      />,
    );

    const aggregateControls = screen.getAllByRole("button", {
      name: "Preview aggregate output",
    });
    expect(aggregateControls).toHaveLength(1);
    expect(aggregateControls[0]?.getAttribute("aria-controls")).toBe(
      "h3p-preview-panel",
    );
    const clipControls = screen.getAllByRole("button", {
      name: "Preview segment 1",
    });
    expect(clipControls).toHaveLength(2);
    for (const control of clipControls)
      expect(control.getAttribute("aria-controls")).toBe("h3p-preview-panel");
    expect(
      screen.queryByRole("button", { name: "Preview segment 2" }),
    ).toBeNull();
    expect(clipControls[0]!.parentElement?.closest("button")).toBeNull();

    await userEvent.click(clipControls[0]!);
    expect(onMediaPreview).toHaveBeenCalledWith(
      previewProjection.outputs[1]!.outputHandle,
    );
    expect(onIntent).not.toHaveBeenCalled();

    await userEvent.click(
      screen.getByRole("button", { name: "Select segment 2" }),
    );
    expect(onIntent).toHaveBeenCalledWith({
      action: "set_selection",
      segmentIds: [projection.segments[1]!.segmentId],
    });

    view.rerender(
      <ProductionWorkbench
        locale="en"
        state={{ status: "ready", projection: previewProjection }}
        onIntent={onIntent}
        mediaPreview={{
          status: "loading",
          workspaceRevision: previewProjection.workspaceRevision,
          outputHandle: previewProjection.outputs[1]!.outputHandle,
        }}
        onMediaPreview={onMediaPreview}
      />,
    );
    expect(
      screen.getByRole<HTMLButtonElement>("button", {
        name: "Preview aggregate output",
      }).disabled,
    ).toBe(true);
    for (const control of screen.getAllByRole<HTMLButtonElement>("button", {
      name: "Preview segment 1",
    }))
      expect(control.disabled).toBe(true);
  });

  it("reveals loading feedback from the exact clip opener", async () => {
    const originalScrollIntoView = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype,
      "scrollIntoView",
    );
    const scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView,
      writable: true,
    });
    const clipOutput = {
      outputHandle: `out_${"2".repeat(40)}`,
      ordinal: 1,
      state: "ready" as const,
      segmentId: projection.segments[0]!.segmentId,
      preview: true,
    };
    const previewProjection = {
      ...projection,
      outputs: [clipOutput],
      allowedActions: [...projection.allowedActions, "preview_output" as const],
    };
    const onIntent = vi.fn();
    const onMediaPreview = vi.fn();

    try {
      const view = render(
        <ProductionWorkbench
          locale="en"
          state={{ status: "ready", projection: previewProjection }}
          onIntent={onIntent}
          onMediaPreview={onMediaPreview}
        />,
      );
      const sequence = view.container.querySelector<HTMLElement>(
        'section[aria-labelledby="h3p-sequence"]',
      );
      expect(sequence).not.toBeNull();
      const opener = within(sequence!).getByRole("button", {
        name: "Preview segment 1",
      });

      opener.focus();
      await userEvent.keyboard("{Enter}");
      expect(onMediaPreview).toHaveBeenCalledOnce();
      expect(onMediaPreview).toHaveBeenCalledWith(clipOutput.outputHandle);
      expect(onIntent).not.toHaveBeenCalled();

      view.rerender(
        <ProductionWorkbench
          locale="en"
          state={{ status: "ready", projection: previewProjection }}
          onIntent={onIntent}
          mediaPreview={{
            status: "loading",
            workspaceRevision: previewProjection.workspaceRevision,
            outputHandle: clipOutput.outputHandle,
          }}
          onMediaPreview={onMediaPreview}
        />,
      );

      const panel = screen.getByRole("region", { name: "Preview result" });
      expect(panel.closest('[aria-labelledby="h3p-outputs"]')).not.toBeNull();
      expect(within(panel).getByRole("status").textContent).toBe(
        "Preparing preview…",
      );
      expect(scrollIntoView).toHaveBeenCalledOnce();
      expect(scrollIntoView.mock.instances[0]).toBe(panel);
      expect(scrollIntoView).toHaveBeenCalledWith({
        behavior: "auto",
        block: "start",
        inline: "nearest",
      });
      expect(document.activeElement).toBe(panel);

      view.rerender(
        <ProductionWorkbench
          locale="en"
          state={{ status: "ready", projection: previewProjection }}
          onIntent={onIntent}
          mediaPreview={{
            status: "error",
            workspaceRevision: previewProjection.workspaceRevision,
            outputHandle: clipOutput.outputHandle,
            reason: "failed",
          }}
          onMediaPreview={onMediaPreview}
        />,
      );
      expect(within(panel).getByRole("alert").textContent).toBe(
        "Preview unavailable: failed",
      );
      expect(scrollIntoView).toHaveBeenCalledOnce();
      expect(document.activeElement).toBe(panel);
    } finally {
      if (originalScrollIntoView === undefined)
        delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView;
      else
        Object.defineProperty(
          HTMLElement.prototype,
          "scrollIntoView",
          originalScrollIntoView,
        );
    }
  });

  it("B-M1605-PREVIEW-01 reveals the ready player again after the loading reveal of the same open", async () => {
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(
      () => undefined,
    );
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(
      () => undefined,
    );
    const originalScrollIntoView = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype,
      "scrollIntoView",
    );
    const scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView,
      writable: true,
    });
    const clipOutput = {
      outputHandle: `out_${"2".repeat(40)}`,
      ordinal: 1,
      state: "ready" as const,
      segmentId: projection.segments[0]!.segmentId,
      preview: true,
    };
    const previewProjection = {
      ...projection,
      outputs: [clipOutput],
      allowedActions: [...projection.allowedActions, "preview_output" as const],
    };
    const loading = {
      status: "loading" as const,
      workspaceRevision: previewProjection.workspaceRevision,
      outputHandle: clipOutput.outputHandle,
    };
    const ready = {
      status: "ready" as const,
      workspaceRevision: previewProjection.workspaceRevision,
      outputHandle: clipOutput.outputHandle,
      url: "blob:synthetic-ready-preview",
    };
    const workbench = (mediaPreview?: typeof loading | typeof ready) => (
      <ProductionWorkbench
        locale="en"
        state={{ status: "ready", projection: previewProjection }}
        onIntent={() => undefined}
        mediaPreview={mediaPreview}
        onMediaPreview={() => undefined}
      />
    );

    try {
      // The open reveals a one-line loading status; a real preview arrives later and the player then
      // grows the panel below a scroll position that was clamped to that short content.
      const view = render(workbench());
      const opener = within(
        view.container.querySelector<HTMLElement>(
          'section[aria-labelledby="h3p-sequence"]',
        )!,
      ).getByRole("button", { name: "Preview segment 1" });
      opener.focus();
      await userEvent.keyboard("{Enter}");
      view.rerender(workbench(loading));
      const panel = screen.getByRole("region", { name: "Preview result" });
      expect(scrollIntoView).toHaveBeenCalledOnce();
      expect(document.activeElement).toBe(panel);

      view.rerender(workbench(ready));
      expect(screen.getByLabelText("Segment 1 preview")).toBeTruthy();
      expect(scrollIntoView).toHaveBeenCalledTimes(2);
      expect(scrollIntoView.mock.instances[1]).toBe(panel);
      expect(scrollIntoView.mock.calls[1]).toEqual([
        { behavior: "auto", block: "nearest", inline: "nearest" },
      ]);
      expect(document.activeElement).toBe(panel);

      // The settle reveal belongs to that one open only: a later ready render does not scroll.
      view.rerender(workbench({ ...ready, url: "blob:synthetic-later" }));
      expect(scrollIntoView).toHaveBeenCalledTimes(2);

      // Once focus has left the panel, the user has moved on and the arriving player must not scroll.
      await userEvent.click(
        screen.getByRole("button", { name: "Close preview" }),
      );
      view.rerender(workbench());
      await userEvent.keyboard("{Enter}");
      view.rerender(workbench(loading));
      expect(scrollIntoView).toHaveBeenCalledTimes(3);
      // The opener is disabled while loading, so the user moving on is modelled as focus leaving.
      (document.activeElement as HTMLElement).blur();
      expect(document.activeElement).toBe(document.body);
      view.rerender(workbench(ready));
      expect(scrollIntoView).toHaveBeenCalledTimes(3);
    } finally {
      if (originalScrollIntoView === undefined)
        delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView;
      else
        Object.defineProperty(
          HTMLElement.prototype,
          "scrollIntoView",
          originalScrollIntoView,
        );
    }
  });

  it.each([
    ["en", "Preview segment 1"],
    ["zh-TW", "預覽片段 1"],
    ["zh-CN", "预览片段 1"],
  ] as const)("localizes both clip affordances in %s", (locale, label) => {
    const previewProjection = {
      ...projection,
      outputs: [
        {
          outputHandle: `out_${"2".repeat(40)}`,
          ordinal: 1,
          state: "ready" as const,
          segmentId: projection.segments[0]!.segmentId,
          preview: true,
        },
      ],
      allowedActions: [...projection.allowedActions, "preview_output" as const],
    };
    render(
      <ProductionWorkbench
        locale={locale}
        state={{ status: "ready", projection: previewProjection }}
        onIntent={() => undefined}
        onMediaPreview={() => undefined}
      />,
    );

    expect(screen.getAllByRole("button", { name: label })).toHaveLength(2);
  });

  it("renders one native player, samples and boundaries, then restores opener focus", async () => {
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(
      () => undefined,
    );
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(
      () => undefined,
    );
    const previewProjection = {
      ...projection,
      reconstruction: { state: "complete" as const },
      outputs: [
        {
          outputHandle: `out_${"1".repeat(40)}`,
          ordinal: 1,
          state: "ready" as const,
          segmentId: null,
          preview: true,
        },
      ],
      allowedActions: [...projection.allowedActions, "preview_output" as const],
    };
    const onMediaPreviewClose = vi.fn();
    render(
      <ProductionWorkbench
        locale="en"
        state={{ status: "ready", projection: previewProjection }}
        onIntent={() => undefined}
        mediaPreview={{
          status: "ready",
          workspaceRevision: previewProjection.workspaceRevision,
          outputHandle: previewProjection.outputs[0]!.outputHandle,
          url: "blob:bounded-preview",
        }}
        onMediaPreviewClose={onMediaPreviewClose}
      />,
    );

    const player = screen.getByLabelText<HTMLVideoElement>(
      "Aggregate output preview",
    );
    expect(document.activeElement).not.toBe(
      screen.getByRole("region", { name: "Preview result" }),
    );
    expect(player.controls).toBe(true);
    expect(player.autoplay).toBe(false);
    expect(player.preload).toBe("metadata");
    expect(
      screen.getAllByRole("button", { name: /seek preview to sample/i }),
    ).toHaveLength(12);
    const sampler = document.querySelector<HTMLVideoElement>(".h3p-vs");
    expect(sampler).not.toBeNull();
    Object.defineProperty(sampler, "duration", {
      value: 12,
      configurable: true,
    });
    Object.defineProperty(sampler, "currentTime", {
      configurable: true,
      set: () => queueMicrotask(() => fireEvent.seeked(sampler!)),
    });
    fireEvent.loadedMetadata(sampler!);
    await vi.waitFor(() =>
      expect(
        screen.getAllByRole<HTMLButtonElement>("button", {
          name: /seek preview to sample/i,
        })[6]?.disabled,
      ).toBe(false),
    );
    Object.defineProperty(player, "duration", {
      value: 12,
      configurable: true,
    });
    Object.defineProperty(player, "currentTime", {
      value: 6.4,
      configurable: true,
    });
    fireEvent.timeUpdate(player);
    await vi.waitFor(() =>
      expect(
        screen
          .getAllByRole("button", { name: /seek preview to sample/i })[6]
          ?.getAttribute("aria-pressed"),
      ).toBe("true"),
    );
    expect(screen.getByTestId("production-preview-playhead").style.left).toBe(
      `${(6.4 / 12) * 100}%`,
    );
    const boundary = screen.getByRole("button", {
      name: "Boundary before segment 2",
    });
    // M17-25: boundaries are placed by delivered duration, not by an authored
    // frame count. The two fixture segments deliver 4458 ms and 12958 ms.
    expect(boundary.parentElement?.style.left).toBe(
      `${(4458 / (4458 + 12958)) * 100}%`,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Close preview" }),
    );
    expect(onMediaPreviewClose).toHaveBeenCalledOnce();
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Preview aggregate output" }),
    );
  });

  it("labels a clip player, omits aggregate boundaries, and restores its exact opener", async () => {
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(
      () => undefined,
    );
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(
      () => undefined,
    );
    const clipOutput = {
      outputHandle: `out_${"2".repeat(40)}`,
      ordinal: 2,
      state: "ready" as const,
      segmentId: projection.segments[0]!.segmentId,
      preview: true,
    };
    const previewProjection = {
      ...projection,
      reconstruction: { state: "complete" as const },
      outputs: [clipOutput],
      allowedActions: [...projection.allowedActions, "preview_output" as const],
    };
    const onMediaPreview = vi.fn();
    const onMediaPreviewClose = vi.fn();
    const view = render(
      <ProductionWorkbench
        locale="en"
        state={{ status: "ready", projection: previewProjection }}
        onIntent={() => undefined}
        onMediaPreview={onMediaPreview}
        onMediaPreviewClose={onMediaPreviewClose}
      />,
    );
    const opener = screen.getAllByRole("button", {
      name: "Preview segment 1",
    })[0]!;
    await userEvent.click(opener);
    expect(onMediaPreview).toHaveBeenCalledWith(clipOutput.outputHandle);

    view.rerender(
      <ProductionWorkbench
        locale="en"
        state={{ status: "ready", projection: previewProjection }}
        onIntent={() => undefined}
        mediaPreview={{
          status: "ready",
          workspaceRevision: previewProjection.workspaceRevision,
          outputHandle: clipOutput.outputHandle,
          url: "blob:bounded-clip-preview",
        }}
        onMediaPreview={onMediaPreview}
        onMediaPreviewClose={onMediaPreviewClose}
      />,
    );

    expect(screen.getByLabelText("Segment 1 preview")).not.toBeNull();
    expect(
      screen.getAllByRole("button", { name: /seek preview to sample/i }),
    ).toHaveLength(12);
    expect(
      screen.queryByRole("button", { name: /boundary before segment/i }),
    ).toBeNull();
    await userEvent.click(
      screen.getByRole("button", { name: "Close preview" }),
    );
    expect(onMediaPreviewClose).toHaveBeenCalledOnce();
    expect(document.activeElement).toBe(opener);

    const cardOpener = screen.getAllByRole("button", {
      name: "Preview segment 1",
    })[1]!;
    await userEvent.click(cardOpener);
    await userEvent.click(
      screen.getByRole("button", { name: "Close preview" }),
    );
    expect(onMediaPreviewClose).toHaveBeenCalledTimes(2);
    expect(document.activeElement).toBe(cardOpener);
  });

  it("fails closed when a ready preview handle is not in the exact projection", () => {
    const previewProjection = {
      ...projection,
      outputs: [
        {
          outputHandle: `out_${"2".repeat(40)}`,
          ordinal: 1,
          state: "ready" as const,
          segmentId: projection.segments[0]!.segmentId,
          preview: true,
        },
      ],
      allowedActions: [...projection.allowedActions, "preview_output" as const],
    };
    const { container } = render(
      <ProductionWorkbench
        locale="en"
        state={{ status: "ready", projection: previewProjection }}
        onIntent={() => undefined}
        mediaPreview={{
          status: "ready",
          workspaceRevision: previewProjection.workspaceRevision,
          outputHandle: `out_${"9".repeat(40)}`,
          url: "blob:unbound-preview",
        }}
      />,
    );

    expect(screen.getByRole("alert").textContent).toBe("Preview unavailable");
    expect(container.querySelector("video")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Close preview" }),
    ).not.toBeNull();
  });
});

describe("M17-21 labelled Production presentation", () => {
  // M21-03 moved run state and progress into the status band, where they are
  // stated once instead of twice. What stays under Run is the state that is
  // about the run rather than about the workspace.
  const runKeys = {
    en: ["Reconstruction", "Blockers"],
    "zh-TW": ["重建", "阻擋項"],
    "zh-CN": ["重建", "阻挡项"],
  } as const;

  it("renders Run as labelled key/value rows in every locale", () => {
    for (const locale of ["en", "zh-TW", "zh-CN"] as const) {
      const { container, unmount } = render(
        <ProductionWorkbench
          locale={locale}
          contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
          state={{ status: "ready", projection }}
          onIntent={vi.fn()}
        />,
      );
      const rows = [...container.querySelectorAll(".h3p-kv > div")];
      expect(rows).toHaveLength(2);
      // The status band still carries both, so nothing was dropped: it moved.
      expect(container.querySelector(".h3p-i .h3p-rs .h3p-c")).not.toBeNull();
      expect(container.querySelector(".h3p-i .h3p-meter")).not.toBeNull();
      rows.forEach((row, index) => {
        expect(row.querySelector("dt")?.textContent).toBe(
          runKeys[locale][index],
        );
        expect(row.querySelector("dd")?.textContent?.trim()).not.toBe("");
      });
      // The M17-12 defect rendered runState, progress and reconstruction as
      // three adjacent unlabelled values that read as one unbroken string.
      expect(container.textContent).not.toContain("ready0/2unavailable");
      unmount();
    }
  });

  it("groups segment actions and marks only destruction as dangerous", () => {
    const { container } = render(
      <ProductionWorkbench
        locale="en"
        contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
        state={{ status: "ready", projection }}
        onIntent={vi.fn()}
      />,
    );
    for (const name of ["Inspect", "Compose from Context", "Remove"])
      expect(screen.getByRole("group", { name })).not.toBeNull();
    const danger = [...container.querySelectorAll('[data-variant="danger"]')];
    expect(danger.map((button) => button.textContent)).toEqual([
      "Delete segment 1",
      "Release workspace",
    ]);
    expect(
      screen.getByRole("button", { name: "Add current Context" }).dataset
        .variant,
    ).toBe("primary");
  });

  it("sizes the selection checkbox as a control and labels it visibly", () => {
    const { container } = render(
      <ProductionWorkbench
        locale="en"
        contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
        state={{ status: "ready", projection }}
        onIntent={vi.fn()}
      />,
    );
    const boxes = [
      ...container.querySelectorAll<HTMLInputElement>(
        '.h3p-s input[type="checkbox"]',
      ),
    ];
    expect(boxes).toHaveLength(2);
    for (const box of boxes) {
      const label = box.closest("label");
      expect(label?.textContent?.trim()).toMatch(/^Segment \d+$/);
    }
  });

  it("renders a designed empty state instead of bare policy prose", () => {
    const { container } = render(
      <ProductionWorkbench
        locale="en"
        contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
        state={{ status: "ready", projection }}
        onIntent={vi.fn()}
      />,
    );
    // M21-03 order 6: one quiet line that recedes, not a bordered box that
    // reads as a disabled input. It is still designed copy -- the row's point --
    // and it still says what has to happen for an output to appear.
    const empty = container.querySelector('[data-known="false"]');
    expect(empty?.textContent).toContain("once a segment has finished");
    expect(container.querySelector(".h3p-e")).toBeNull();
  });
});

describe("M17-23 single-target selection contract", () => {
  const multiSelected = {
    ...projection,
    selectedSegmentIds: ["segment_1", "segment_2"],
  };

  it("offers no destructive target while more than one segment is selected", () => {
    // The removed defect, stated as the review found it: a user selects several
    // segments and presses Delete, and one of them disappears without ever
    // having been identified, because the action silently took the first member
    // of the set in sequence order.
    const onIntent = vi.fn();
    const { container } = render(
      <ProductionWorkbench
        locale="en"
        contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
        state={{ status: "ready", projection: multiSelected }}
        onIntent={onIntent}
      />,
    );

    expect(container.querySelectorAll("[data-edit-target]")).toHaveLength(0);
    expect(container.querySelectorAll('[aria-current="true"]')).toHaveLength(0);
    const remove = screen.getByRole<HTMLButtonElement>("button", {
      name: "Delete segment",
    });
    expect(remove.textContent).toBe("Delete segment");
    expect(remove.disabled).toBe(true);
    expect(
      screen.getByRole<HTMLButtonElement>("button", {
        name: "Replace with current Context",
      }).disabled,
    ).toBe(true);
    // The relationship editor is a single-target editor too, so it is absent
    // rather than silently bound to whichever segment sorts first.
    expect(container.querySelector(".h3p-r")).toBeNull();
    expect(
      screen
        .getByText(
          "Select exactly one segment to replace, delete or relate it.",
        )
        .getAttribute("role"),
    ).toBe("status");

    remove.click();
    expect(onIntent).not.toHaveBeenCalled();
  });

  it("names the sole selected segment in every single-target control", () => {
    const { container } = render(
      <ProductionWorkbench
        locale="en"
        contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
        state={{ status: "ready", projection }}
        onIntent={vi.fn()}
      />,
    );

    expect(
      screen.getByRole<HTMLButtonElement>("button", {
        name: "Delete segment 1",
      }).disabled,
    ).toBe(false);
    expect(
      screen.getByRole("button", {
        name: "Replace segment 1 with current Context",
      }),
    ).not.toBeNull();
    expect(
      screen.getByRole("button", { name: "Apply relationship to segment 1" }),
    ).not.toBeNull();
    const target = container.querySelector("[data-edit-target]");
    expect(target?.getAttribute("aria-current")).toBe("true");
    expect(target?.querySelector(".h3p-et")?.textContent).toBe("Editing");
    // Set membership and the edit target are different affordances: the
    // checkbox states the first, the badge and `aria-current` state the second.
    expect(
      target?.querySelector<HTMLInputElement>("input[type='checkbox']")
        ?.checked,
    ).toBe(true);
  });

  it("counts a set-consuming action over every segment, not the window", () => {
    render(
      <ProductionWorkbench
        locale="en"
        contextWorkspaceHandle={`ws_${"b".repeat(43)}`}
        state={{ status: "ready", projection: multiSelected }}
        proposalRows={[]}
        onProposalRead={vi.fn()}
        onIntent={vi.fn()}
      />,
    );
    expect(
      screen.getByRole("button", { name: "Understand selected (2)" }),
    ).not.toBeNull();
  });
});
