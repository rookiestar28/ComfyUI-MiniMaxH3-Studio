import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NleWorkspace } from "../src/components/nle/NleWorkspace";
import { NleExportMenu } from "../src/components/nle/NleExportMenu";
import { NleProjectSummary } from "../src/components/nle/NleProjectSummary";
import { OUTPUT_CAPABILITY } from "../src/contracts/authoringOutputCodec";
import { decodeProductionAuthoringImportResponse } from "../src/contracts/productionAuthoringImportCodec";
import type { AuthoringViewState } from "../src/state/authoringViewState";
import { importV2ResponseWire } from "./support/productionAuthoringImportFixture";
import { SMOKE_SHAPE, authoringReady } from "./support/nleWorkspaceFixture";
import { bindingFixture, expandedState } from "./support/nleWorkspaceBinding";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function v2EmptyAuthoringWithRetainedV1() {
  const imported = decodeProductionAuthoringImportResponse(
    importV2ResponseWire(),
  );
  if (!("historyProjection" in imported))
    throw new Error("expected a V2 production import response");
  const legacy = authoringReady(SMOKE_SHAPE);
  if (!("timelineHistory" in legacy) || legacy.timelineHistory === undefined)
    throw new Error("expected the retained V1 fixture snapshot");
  return {
    imported,
    authoring: {
      status: "ready",
      projection: imported.authoringProjection,
      timelineHistory: legacy.timelineHistory,
      timelineHistoryV2: imported.historyProjection,
    } satisfies AuthoringViewState,
  };
}

describe("NleWorkspace V2 content extent", () => {
  for (const status of ["ready", "conflict", "pending"] as const) {
    it(`admits empty catalog drag only in an editable ${status} state`, async () => {
      const { imported, authoring } = v2EmptyAuthoringWithRetainedV1();
      const binding = bindingFixture({
        authoring: { ...authoring, status },
        state: expandedState(),
      });
      const view = render(
        <NleWorkspace
          binding={binding}
          onEdgeGestureActive={() => undefined}
        />,
      );
      const grid = view.container.querySelector<HTMLDivElement>(
        "[data-h3-nle-empty-drop]",
      )!;
      vi.spyOn(grid, "getBoundingClientRect").mockReturnValue(
        new DOMRect(100, 200, 800, 168),
      );
      const card = view.container.querySelector<HTMLButtonElement>(
        ".h3-nle-media-primary",
      )!;
      const capture = new Set<number>();
      card.setPointerCapture = vi.fn((id) => {
        capture.add(id);
      });
      card.hasPointerCapture = vi.fn((id) => capture.has(id));
      card.releasePointerCapture = vi.fn((id) => {
        capture.delete(id);
      });
      const pointer = (type: string, x: number, y: number) => {
        const event = new Event(type, { bubbles: true, cancelable: true });
        Object.assign(event, {
          pointerId: 7,
          button: 0,
          buttons: type === "pointerup" ? 0 : 1,
          isPrimary: true,
          clientX: x,
          clientY: y,
        });
        fireEvent(card, event);
      };
      await act(async () => {
        pointer("pointerdown", 10, 10);
        pointer("pointermove", 246, 224);
        pointer("pointerup", 246, 224);
      });
      expect(capture.size).toBe(0);
      if (status === "pending") {
        expect(binding.actions.timeline).not.toHaveBeenCalled();
      } else {
        expect(binding.actions.timeline).toHaveBeenCalledExactlyOnceWith(
          expect.objectContaining({
            action: "apply_timeline_commands",
            capturedTimeline: expect.objectContaining({
              authoringFingerprint:
                imported.historyProjection.authoring.authoringFingerprint,
            }),
            commands: [
              expect.objectContaining({
                kind: "insert_asset_clip",
                payload: expect.objectContaining({
                  clip: expect.objectContaining({ start_frame: 18 }),
                }),
              }),
            ],
          }),
        );
      }
    });
  }

  it("keeps undo and redo reachable when V2 content is empty", () => {
    const { imported, authoring } = v2EmptyAuthoringWithRetainedV1();
    const undoCursor = `h3.context.timeline_history_cursor.v2:1:${"a".repeat(64)}`;
    const redoCursor = `h3.context.timeline_history_cursor.v2:2:${"b".repeat(64)}`;
    const withHistory = (history: typeof imported.historyProjection) =>
      ({
        ...authoring,
        timelineHistoryV2: history,
      }) satisfies AuthoringViewState;
    const initialHistory = {
      ...imported.historyProjection,
      undoCursor,
    };
    const state = expandedState();
    const binding = bindingFixture({
      authoring: withHistory(initialHistory),
      state,
    });
    const timeline = binding.actions.timeline;
    const view = render(
      <NleWorkspace binding={binding} onEdgeGestureActive={() => undefined} />,
    );

    const undo = view.getByRole("button", { name: "Undo" });
    expect((undo as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(undo);
    expect(timeline).toHaveBeenCalledWith(
      expect.objectContaining({
        action: "apply_timeline_commands",
        capturedTimeline: expect.objectContaining({
          workspaceHandle: imported.historyProjection.authoring.workspaceHandle,
          timelineFingerprint:
            imported.historyProjection.authoring.timelineFingerprint,
          authoringFingerprint:
            imported.historyProjection.authoring.authoringFingerprint,
        }),
        commands: [{ kind: "undo", payload: { history_cursor: undoCursor } }],
      }),
    );

    const redoHistory = {
      ...imported.historyProjection,
      redoCursor,
    };
    view.rerender(
      <NleWorkspace
        binding={{ ...binding, authoring: withHistory(redoHistory) }}
        onEdgeGestureActive={() => undefined}
      />,
    );
    const redo = view.getByRole("button", { name: "Redo" });
    expect((redo as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(redo);
    expect(timeline).toHaveBeenLastCalledWith(
      expect.objectContaining({
        action: "apply_timeline_commands",
        capturedTimeline: expect.objectContaining({
          timelineFingerprint:
            imported.historyProjection.authoring.timelineFingerprint,
          authoringFingerprint:
            imported.historyProjection.authoring.authoringFingerprint,
        }),
        commands: [{ kind: "redo", payload: { history_cursor: redoCursor } }],
      }),
    );
  });

  it("keeps an empty V2 authoring catalog editable without reviving the V1 render snapshot", () => {
    const { imported, authoring } = v2EmptyAuthoringWithRetainedV1();
    const binding = bindingFixture({
      authoring,
      state: expandedState(),
    });
    const { container, getByRole, getByLabelText, queryByRole } = render(
      <NleWorkspace binding={binding} onEdgeGestureActive={() => undefined} />,
    );

    expect(queryByRole("slider", { name: "Playhead" })).toBeNull();
    expect(
      container.querySelector('[data-h3-nle-empty-origin="0"]'),
    ).not.toBeNull();
    expect(
      getByLabelText("Timeline").getAttribute("data-h3-nle-edit-capacity"),
    ).toBe("3600");
    fireEvent.click(
      getByRole("button", { name: "Add Clip 01 to the timeline" }),
    );

    expect(binding.actions.timeline).toHaveBeenCalledWith(
      expect.objectContaining({
        action: "apply_timeline_commands",
        capturedTimeline: expect.objectContaining({
          workspaceHandle: imported.historyProjection.workspaceHandle,
          authoringFingerprint:
            imported.historyProjection.authoring.authoringFingerprint,
        }),
      }),
    );
  });

  it("shows V2 content extent and edit capacity independently in the project summary", () => {
    const { authoring } = v2EmptyAuthoringWithRetainedV1();
    const { getByLabelText } = render(
      <NleProjectSummary
        locale="en"
        authoring={authoring}
        contextAvailable
        overlayOpen={false}
        onStart={() => undefined}
        onIntent={() => undefined}
      />,
    );
    const summary = getByLabelText("Clip editor project");
    const counts = summary.querySelector("[data-h3-nle-summary-counts]");

    expect(counts?.textContent).toContain("Content extent 00:00:00:00");
    expect(counts?.textContent).toContain("Edit capacity 00:02:30:00");
  });

  it("does not offer export for empty V2 content while a retained V1 snapshot exists", () => {
    const { authoring } = v2EmptyAuthoringWithRetainedV1();
    const binding = bindingFixture({
      authoring,
      state: expandedState({
        render: {
          status: "read",
          capability: { ...OUTPUT_CAPABILITY, supported: true },
        },
      }),
    });
    const { container } = render(
      <NleExportMenu binding={binding} open onOpenChange={() => undefined} />,
    );

    expect(
      container.querySelector(
        '[data-h3-nle-render="backend_render_unavailable"]',
      ),
    ).not.toBeNull();
    expect(
      container.querySelector('[data-h3-nle-render="available"]'),
    ).toBeNull();
  });
});
