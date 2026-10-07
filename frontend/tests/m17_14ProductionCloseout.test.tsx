/**
 * M17-14 production acceptance closeout, browser half.
 *
 * These rows join accepted surfaces into the flows a user performs. They do not restate what each
 * per-item suite already proves; each row asserts a seam that only exists when two accepted items
 * meet. Every fixture value is frozen by
 * `.planning/260818-M17-14_PRODUCTION_CLOSEOUT_PLAN.md` section 3.2.1 and follows from the single
 * authored duration of 8 000 ms.
 */
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { H3Sidebar } from "../src/components/H3Sidebar";
import { ProductionWorkbench } from "../src/components/ProductionWorkbench";
import { decodeProductionWorkbenchProjection } from "../src/contracts/productionWorkbenchCodec";
import { unavailableProductionAssemblyWire } from "./support/productionAssemblyWire";
import {
  createAppModeController,
  type AppModeInputs,
} from "../src/host/appMode";
import { SUPPORTED_LOCALES } from "../src/i18n/catalog";
import { initialShellState } from "../src/state/shellState";
import {
  MISSING_ASSET,
  generationProfile,
  loadAvailableProfile,
} from "./support/generationProfileFixture";
import { fixtureBackedAppModeHost } from "./support/hostSeamTestDouble";
import { createQualifiedBasePrompt } from "./support/qualifiedBasePrompt";
import { loadSyntheticTemplate } from "./support/templateFixture";
import {
  acceptedQueueResponse,
  syntheticPromptUuid,
} from "./support/queuePromptTestDouble";

afterEach(cleanup);

const CLOSEOUT_DURATION_MS = 8_000;
const CLOSEOUT_FRAMES = 192;
const CLOSEOUT_EDIT_DURATION_MS = 5_875;
const CLOSEOUT_EDIT_FRAMES = 141;
const fingerprint = `sha256:${"a".repeat(64)}`;

const closeoutDuration = {
  duration_milliseconds: CLOSEOUT_DURATION_MS,
  delivered_milliseconds: CLOSEOUT_DURATION_MS,
  frame_count: CLOSEOUT_FRAMES,
  snapped: false,
};

function closeoutProjectionWire(
  overrides: {
    selected?: readonly string[];
    cutDuration?: typeof closeoutDuration;
    allowed?: readonly string[];
  } = {},
): Record<string, unknown> {
  return {
    schema: "h3.context.production_workbench.projection.v1",
    workspace_handle: `pw_${"a".repeat(43)}`,
    workspace_id: "workspace.m17.closeout",
    workspace_revision: 1,
    workspace_fingerprint: fingerprint,
    segments: [
      {
        segment_id: "segment_root",
        ordinal: 1,
        task_mode: "t2va",
        duration: closeoutDuration,
        relation: "independent",
        predecessor_segment_id: null,
        boundary_kind: "independent",
        closure_state: "clean",
        job_state: "succeeded",
        artifact_state: "complete",
        continuity_state: "unavailable",
        delivered_geometry: null,
      },
      {
        segment_id: "segment_native",
        ordinal: 2,
        task_mode: "fl2va",
        duration: closeoutDuration,
        relation: "predecessor",
        predecessor_segment_id: "segment_root",
        boundary_kind: "native_handoff",
        closure_state: "dirty_self",
        job_state: "planned",
        artifact_state: "unavailable",
        continuity_state: "native_frame_handoff",
        delivered_geometry: null,
      },
      {
        segment_id: "segment_cut",
        ordinal: 3,
        task_mode: "t2va",
        duration: overrides.cutDuration ?? closeoutDuration,
        relation: "cut",
        predecessor_segment_id: null,
        boundary_kind: "cut",
        closure_state: "clean",
        job_state: "succeeded",
        artifact_state: "complete",
        continuity_state: "unavailable",
        delivered_geometry: null,
      },
    ],
    selected_segment_ids: overrides.selected ?? [],
    run: { state: "ready", completed: 2, total: 3 },
    generation_sequence: null,
    reconstruction: { state: "unavailable" },
    assembly: unavailableProductionAssemblyWire(),
    authority_versions: ["h3.context.generation_sequence_projection.v1"],
    outputs: [],
    allowed_actions: overrides.allowed ?? [
      "set_selection",
      "reorder_segments",
      "delete_segment",
      "set_segment_relation",
      "read_projection",
    ],
    blocker_codes: [],
    limits: { max_segments: 64, max_outputs: 65 },
  };
}

function closeoutProjection(
  overrides: Parameters<typeof closeoutProjectionWire>[0] = {},
) {
  return decodeProductionWorkbenchProjection(closeoutProjectionWire(overrides));
}

describe("C06 exact Production action and timeline projection", () => {
  it("draws the timeline in derived frames while showing the authored duration", () => {
    render(
      <ProductionWorkbench
        state={{ status: "ready", projection: closeoutProjection() }}
        locale="en"
        onIntent={vi.fn()}
      />,
    );
    const segments = screen.getAllByTestId("production-timeline-segment");
    expect(segments).toHaveLength(3);
    // Equal authored durations must occupy equal track. A track proportional to anything else --
    // ordinal, index, a stored frame count nobody derived -- would draw three different widths for
    // three identical segments.
    for (const segment of segments) {
      expect(segment.style.flexGrow).toBe(String(CLOSEOUT_FRAMES));
      expect(segment.getAttribute("data-snapped")).toBeNull();
    }
  });

  it("redraws the track when one authored duration changes, and only that one", () => {
    const { rerender } = render(
      <ProductionWorkbench
        state={{ status: "ready", projection: closeoutProjection() }}
        locale="en"
        onIntent={vi.fn()}
      />,
    );
    rerender(
      <ProductionWorkbench
        state={{
          status: "ready",
          projection: closeoutProjection({
            cutDuration: {
              duration_milliseconds: CLOSEOUT_EDIT_DURATION_MS,
              delivered_milliseconds: CLOSEOUT_EDIT_DURATION_MS,
              frame_count: CLOSEOUT_EDIT_FRAMES,
              snapped: false,
            },
          }),
        }}
        locale="en"
        onIntent={vi.fn()}
      />,
    );
    const widths = screen
      .getAllByTestId("production-timeline-segment")
      .map((segment) => segment.style.flexGrow);
    expect(widths).toEqual([
      String(CLOSEOUT_FRAMES),
      String(CLOSEOUT_FRAMES),
      String(CLOSEOUT_EDIT_FRAMES),
    ]);
  });

  it("selects through the timeline without inventing a revision of its own", async () => {
    const onIntent = vi.fn();
    render(
      <ProductionWorkbench
        state={{ status: "ready", projection: closeoutProjection() }}
        locale="en"
        onIntent={onIntent}
      />,
    );
    const segments = screen.getAllByTestId("production-timeline-segment");
    const control = segments[1]?.querySelector("button");
    expect(control).not.toBeNull();
    await userEvent.click(control as HTMLElement);
    // The browser asks; it never decides. The intent names the segment and nothing else, so the
    // canonical revision stays the backend's to mint.
    expect(onIntent).toHaveBeenCalledTimes(1);
    expect(onIntent.mock.calls[0]?.[0]).toEqual({
      action: "set_selection",
      segmentIds: ["segment_native"],
    });
  });

  it("marks the selected segment and no other", () => {
    render(
      <ProductionWorkbench
        state={{
          status: "ready",
          projection: closeoutProjection({ selected: ["segment_native"] }),
        }}
        locale="en"
        onIntent={vi.fn()}
      />,
    );
    const pressed = screen
      .getAllByTestId("production-timeline-segment")
      .map((segment) =>
        segment.querySelector("button")?.getAttribute("aria-pressed"),
      );
    expect(pressed).toEqual(["false", "true", "false"]);
  });
});

describe("C10 unavailable capability catalog", () => {
  it("cannot act on a capability the projection did not allow", async () => {
    const onIntent = vi.fn();
    render(
      <ProductionWorkbench
        state={{
          status: "ready",
          projection: closeoutProjection({ allowed: ["set_selection"] }),
        }}
        locale="en"
        onIntent={onIntent}
      />,
    );
    // The accepted design keeps a disallowed control visible and disabled rather than removing it,
    // so the layout does not shift under the user. What must never happen is the control still
    // acting. Both halves are asserted: disabled, and inert when clicked.
    for (const name of [/delete segment/i, /replace with current context/i]) {
      const controls = screen.getAllByRole("button", { name });
      expect(controls.length).toBeGreaterThan(0);
      for (const control of controls) {
        expect((control as HTMLButtonElement).disabled).toBe(true);
        await userEvent.click(control);
      }
    }
    expect(onIntent).not.toHaveBeenCalled();
  });

  it("advertises no provider, upload or model capability anywhere on the surface", () => {
    const { container } = render(
      <ProductionWorkbench
        state={{ status: "ready", projection: closeoutProjection() }}
        locale="en"
        onIntent={vi.fn()}
      />,
    );
    const text = (container.textContent ?? "").toLowerCase();
    for (const forbidden of ["api key", "upload", "provider", "endpoint"]) {
      expect(text).not.toContain(forbidden);
    }
  });
});

describe("C07 exact copy, settings and preference matrix", () => {
  const pageRegistry = {
    selected: "context" as const,
    pages: [
      { id: "context" as const },
      { id: "production" as const },
      { id: "settings" as const },
    ],
  };
  const appMode = {
    capability: { status: "ready" as const },
    durationResolution: {
      status: "resolved" as const,
      resolution: {
        schema: "h3.context.duration_resolution.v1" as const,
        requested_seconds: CLOSEOUT_DURATION_MS / 1000,
        requested_milliseconds: CLOSEOUT_DURATION_MS,
        effective_milliseconds: CLOSEOUT_DURATION_MS,
        frame_count: CLOSEOUT_FRAMES,
        snapped: false,
      },
    },
    imageSources: [],
    mediaSources: [],
    onStart: vi.fn(),
  };

  function pageButtons(container: HTMLElement): HTMLButtonElement[] {
    return [
      ...container.querySelectorAll("nav [data-page-id]"),
    ] as HTMLButtonElement[];
  }

  it("keeps the accepted page order with exactly one current page", () => {
    const { container } = render(
      <H3Sidebar
        state={initialShellState}
        appMode={appMode}
        pageRegistry={pageRegistry}
        languageSettings={{ status: "ready", value: "auto", pending: false }}
        onLanguageWrite={vi.fn()}
      />,
    );
    const buttons = pageButtons(container);
    expect(
      buttons.map((button) => button.getAttribute("data-page-id")),
    ).toEqual(["context", "production", "settings"]);
    // `aria-current` is the cue that survives forced colours, monochrome and colour blindness, and
    // exactly one page may claim it -- two would mean two surfaces claiming the same authority.
    const current = buttons.filter(
      (button) => button.getAttribute("aria-current") === "page",
    );
    expect(current).toHaveLength(1);
    expect(current[0]?.getAttribute("data-page-id")).toBe("context");
  });

  it("renders exactly one page body, and it is the current page's", () => {
    const { container, rerender } = render(
      <H3Sidebar
        state={initialShellState}
        appMode={appMode}
        pageRegistry={pageRegistry}
        languageSettings={{ status: "ready", value: "auto", pending: false }}
        onLanguageWrite={vi.fn()}
      />,
    );
    // Context is current: its authoring control exists and the other pages' do not.
    expect(screen.getByRole("textbox", { name: /intent/i })).toBeTruthy();
    expect(screen.queryByRole("combobox", { name: "Language" })).toBeNull();

    rerender(
      <H3Sidebar
        state={initialShellState}
        appMode={appMode}
        pageRegistry={{ ...pageRegistry, selected: "settings" }}
        languageSettings={{ status: "ready", value: "auto", pending: false }}
        onLanguageWrite={vi.fn()}
      />,
    );
    expect(screen.getByRole("combobox", { name: "Language" })).toBeTruthy();
    expect(screen.queryByRole("textbox", { name: /intent/i })).toBeNull();
    expect(
      pageButtons(container).filter(
        (button) => button.getAttribute("aria-current") === "page",
      ),
    ).toHaveLength(1);
  });

  it("declares exactly the three supported locales", () => {
    expect([...SUPPORTED_LOCALES]).toEqual(["en", "zh-TW", "zh-CN"]);
  });
});

describe("C12 App Mode closure and refusal invariance", () => {
  const closeoutInputs: AppModeInputs = {
    task_mode: "t2va",
    user_intent: "A safe content-free closeout prompt.",
    duration_milliseconds: CLOSEOUT_DURATION_MS,
    frame_count: CLOSEOUT_FRAMES,
  };

  function mutableHost() {
    let graph: unknown = { nodes: [] };
    const activeWorkflow: object = {
      path: "workflows/m17-14-closeout.json",
    };
    const workflowStore = {
      activeWorkflow,
      openWorkflows: [activeWorkflow] as object[],
    };
    const app = {
      extensionManager: { workflow: workflowStore },
      loadApiJson: vi.fn(),
      loadGraphData: vi.fn(
        (
          value: unknown,
          _clean = true,
          _restoreView = true,
          workflow: object | null = null,
        ) => {
          if (workflow === null) {
            const temporary = {
              path: "workflows/unexpected-closeout-copy.json",
            };
            workflowStore.openWorkflows.push(temporary);
            workflowStore.activeWorkflow = temporary;
          } else workflowStore.activeWorkflow = workflow;
          graph = value;
        },
      ),
      graphToPrompt: vi.fn(async () => ({
        output: createQualifiedBasePrompt(
          closeoutInputs,
          {},
          {
            sharedDurationSource: true,
          },
        ),
        workflow: {},
      })),
      graph: { serialize: () => graph },
    };
    const qualified = fixtureBackedAppModeHost(app, {
      queuePrompt: vi.fn().mockResolvedValue(acceptedQueueResponse(1)),
    });
    return {
      ...qualified,
      graph: () => graph,
      workflowStore,
    };
  }

  it("materializes a canvas that carries the authored duration and derives its length", async () => {
    const host = mutableHost();
    const result = await createAppModeController(host.app, host.api, {
      loadTemplate: loadSyntheticTemplate,
      loadProfile: loadAvailableProfile,
    }).start(closeoutInputs);

    expect(result.queuePromptId).toBe(syntheticPromptUuid(1));
    expect(result.route).toBe("new");
    expect(host.app.loadGraphData.mock.calls[0]?.[3]).toBe(
      host.workflowStore.activeWorkflow,
    );
    expect(host.workflowStore.openWorkflows).toHaveLength(1);

    const nodes = (host.graph() as { nodes: Record<string, any>[] }).nodes;
    const primitive = nodes.find((node) => node.type === "PrimitiveFloat");
    const anchor = nodes.find((node) => node.type === "MiniMaxH3ImageToVideo");
    expect(primitive).toBeDefined();
    expect(anchor).toBeDefined();

    // The canvas carries the authored duration in seconds, and the frame count is produced by the
    // template's own duration chain rather than baked in. This is the duration-first contract
    // reaching the graph: a materialization that wrote a frame count directly would be free to write
    // one the model cannot produce, and the user editing the duration on the canvas would no longer
    // change what runs.
    expect(primitive?.widgets_values).toEqual([CLOSEOUT_DURATION_MS / 1000]);
    const length = anchor?.inputs?.find(
      (input: { name?: string }) => input.name === "length",
    );
    expect(length?.link).toEqual(expect.any(Number));
    expect(length?.widget).toEqual({ name: "length" });
  });

  it("materializes and submits names the host profile cannot resolve", async () => {
    const host = mutableHost();
    const controller = createAppModeController(host.app, host.api, {
      loadTemplate: loadSyntheticTemplate,
      loadProfile: () => generationProfile({ text: MISSING_ASSET }),
    });

    const result = await controller.start(closeoutInputs);
    expect(result.route).toBe("new");
    expect(host.api.queuePrompt).toHaveBeenCalledOnce();
    expect(host.app.loadGraphData).toHaveBeenCalledOnce();
    const graph = host.graph() as { nodes?: unknown[] };
    expect(Array.isArray(graph.nodes) ? graph.nodes.length : 0).toBeGreaterThan(
      0,
    );
  });
});

describe("C09 browser resource and retention ceilings", () => {
  it("refuses a projection that exceeds the accepted segment ceiling", () => {
    const base = closeoutProjectionWire();
    const overflowing = {
      ...base,
      segments: Array.from({ length: 65 }, (_value, index) => ({
        ...(base.segments as Record<string, unknown>[])[0],
        segment_id: `segment_bulk_${index}`,
        ordinal: index + 1,
      })),
    };
    // The browser refuses the whole projection rather than rendering the first 64 rows. A truncated
    // render would show a production that is not the one the backend holds.
    expect(() => decodeProductionWorkbenchProjection(overflowing)).toThrow();
  });

  it("refuses a projection whose declared ceilings are not the accepted ones", () => {
    expect(() =>
      decodeProductionWorkbenchProjection({
        ...closeoutProjectionWire(),
        limits: { max_segments: 128, max_outputs: 65 },
      }),
    ).toThrow();
  });
});

describe("C11 preview timeline and teardown", () => {
  it("shows a failed preview as a reason, not as a broken player", () => {
    render(
      <ProductionWorkbench
        state={{ status: "ready", projection: closeoutProjection() }}
        locale="en"
        onIntent={vi.fn()}
        mediaPreview={{
          status: "error",
          workspaceRevision: 1,
          outputHandle: `out_${"a".repeat(40)}`,
          reason: "unavailable",
        }}
      />,
    );
    // A preview that cannot be served says so. An empty <video> would look like a production that
    // rendered nothing rather than a preview that was refused.
    expect(screen.queryByRole("application")).toBeNull();
    expect(document.querySelectorAll("video")).toHaveLength(0);
  });

  it("retains no player, source or canvas once the preview is closed", () => {
    const { rerender } = render(
      <ProductionWorkbench
        state={{ status: "ready", projection: closeoutProjection() }}
        locale="en"
        onIntent={vi.fn()}
        mediaPreview={{
          status: "loading",
          workspaceRevision: 1,
          outputHandle: `out_${"a".repeat(40)}`,
        }}
      />,
    );
    rerender(
      <ProductionWorkbench
        state={{ status: "ready", projection: closeoutProjection() }}
        locale="en"
        onIntent={vi.fn()}
        mediaPreview={{ status: "closed" }}
      />,
    );
    expect(document.querySelectorAll("video")).toHaveLength(0);
    expect(document.querySelectorAll("canvas")).toHaveLength(0);
  });
});

describe("C08 exact proposal attempts and privacy lifecycle", () => {
  const rows = [
    {
      segmentId: "segment_root",
      ordinal: 1,
      status: "ready",
      actionable: true,
    },
    {
      segmentId: "segment_native",
      ordinal: 2,
      status: "failed",
      actionable: false,
      reason: "request_failed",
    },
    {
      segmentId: "segment_cut",
      ordinal: 3,
      status: "ready",
      actionable: false,
    },
  ] as const;

  it("offers no bulk apply even with every segment selected", () => {
    render(
      <ProductionWorkbench
        state={{
          status: "ready",
          projection: closeoutProjection({
            selected: ["segment_root", "segment_native", "segment_cut"],
          }),
        }}
        locale="en"
        onIntent={vi.fn()}
        proposalRows={rows}
      />,
    );
    // Applying every proposal at once would accept content the user never read. The absence of such
    // a control is the product decision, so the row asserts absence rather than a disabled state.
    for (const name of [/apply all/i, /accept all/i, /apply selected/i]) {
      expect(screen.queryAllByRole("button", { name })).toHaveLength(0);
    }
  });

  it("gives every selected segment its own row and offers Accept on none of them", () => {
    const { container } = render(
      <ProductionWorkbench
        state={{
          status: "ready",
          projection: closeoutProjection({
            selected: ["segment_root", "segment_native", "segment_cut"],
          }),
        }}
        locale="en"
        onIntent={vi.fn()}
        proposalRows={rows}
      />,
    );
    const proposalRows_ = [
      ...container.querySelectorAll("[data-proposal-state]"),
    ];
    // One row per selected segment, each carrying its own status. A shared row would let one
    // segment's outcome stand in for another's.
    expect(
      proposalRows_.map((row) => row.getAttribute("data-proposal-state")),
    ).toEqual(["ready", "failed", "ready"]);
    // None of these rows has an opened review, so Accept exists for none of them. Accept must be
    // reachable only from the attempt the user is actually looking at.
    expect(screen.queryAllByRole("button", { name: /accept/i })).toHaveLength(
      0,
    );
  });
});
