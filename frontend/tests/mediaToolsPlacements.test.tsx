// M25-33 AC33-03/06/07: the one Media tools card appears where import, clip preview and final
// output are blocked by the runtime, a continued preview reopens exactly once, a host without the
// renderer offers no install, and the monitor separates "no clip" from "runtime unavailable".

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import wireFixture from "./fixtures/media_runtime_wire_v3.json";
import { AuthoringPreviewMonitor } from "../src/components/AuthoringPreviewMonitor";
import { NleExportMenu } from "../src/components/nle/NleExportMenu";
import { NleWorkspace } from "../src/components/nle/NleWorkspace";
import {
  ProductionWorkbench,
  type ProductionImportToEditor,
} from "../src/components/ProductionWorkbench";
import type { AuthoringPreviewRequest } from "../src/contracts/authoringPreviewCodec";
import {
  decodeMediaRuntimeStatus,
  type MediaRuntimeStatus,
} from "../src/contracts/mediaRuntimeCodec";
import { decodeProductionWorkbenchProjection } from "../src/contracts/productionWorkbenchCodec";
import { UNSUPPORTED_OUTPUT_CAPABILITY } from "../src/host/authoringOutputCapabilityClient";
import {
  initialMediaRuntimeState,
  intentIdentity,
  type MediaRuntimeIntent,
  type MediaRuntimeState,
  type MediaToolsBinding,
} from "../src/state/mediaRuntimeState";
import { SMOKE_SHAPE, authoringReady } from "./support/nleWorkspaceFixture";
import { bindingFixture, expandedState } from "./support/nleWorkspaceBinding";
import { unavailableProductionAssemblyWire } from "./support/productionAssemblyWire";

type Raw = Record<string, unknown> & {
  features: Record<string, { state: string; reason: string | null }>;
};
const samples = wireFixture.samples as unknown as Record<string, Raw>;

function wire(name: string, mutate?: (raw: Raw) => void): MediaRuntimeStatus {
  const raw = JSON.parse(JSON.stringify(samples[name])) as Raw;
  mutate?.(raw);
  return decodeMediaRuntimeStatus(raw);
}

function tools(state: MediaRuntimeState): MediaToolsBinding {
  return {
    state,
    ensure: vi.fn(),
    report: vi.fn(),
    install: vi.fn(),
    cancel: vi.fn(),
    dismiss: vi.fn(),
    rescan: vi.fn(),
    reclaim: vi.fn(),
    useLocalDirectory: vi.fn(),
    restoreAuto: vi.fn(),
  };
}

const readState = (status: MediaRuntimeStatus): MediaRuntimeState => ({
  ...initialMediaRuntimeState,
  status: "read",
  wire: status,
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("Production import placement", () => {
  const FP = `sha256:${"a".repeat(64)}`;
  const READY_HANDLE = `out_${"1".repeat(40)}`;

  function projection(mixed = false) {
    const readySegment = {
      segment_id: "segment.1",
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
      job_state: "succeeded",
      artifact_state: "complete",
      continuity_state: "unavailable",
      delivered_geometry: {
        format: "mp4",
        frame_count: 107,
        width: 864,
        height: 480,
      },
    };
    return decodeProductionWorkbenchProjection({
      schema: "h3.context.production_workbench.projection.v1",
      workspace_handle: `pw_${"p".repeat(32)}`,
      workspace_id: "workspace.1",
      workspace_revision: 7,
      workspace_fingerprint: FP,
      segments: mixed
        ? [
            readySegment,
            {
              ...readySegment,
              segment_id: "segment.2",
              ordinal: 2,
              relation: "predecessor",
              predecessor_segment_id: "segment.1",
              boundary_kind: "native_handoff",
              job_state: "planned",
              artifact_state: "unavailable",
              delivered_geometry: null,
            },
          ]
        : [readySegment],
      selected_segment_ids: mixed ? ["segment.1", "segment.2"] : ["segment.1"],
      run: { state: "ready", completed: 1, total: mixed ? 2 : 1 },
      generation_sequence: {
        schema: "h3.context.generation_sequence_projection.v1",
        sequence_id: "sequence.1",
        sequence_fingerprint: `sha256:${"b".repeat(64)}`,
        state_fingerprint: `sha256:${"c".repeat(64)}`,
        workspace_id: "workspace.1",
        workspace_revision: 7,
        workspace_fingerprint: FP,
        correlation: { prompt_id: "prompt.1", execution_node_id: "node.1" },
      },
      reconstruction: { state: "unavailable" },
      assembly: unavailableProductionAssemblyWire(),
      authority_versions: ["h3.context.generation_sequence_projection.v1"],
      outputs: [
        {
          output_handle: READY_HANDLE,
          ordinal: 1,
          state: "ready",
          segment_id: "segment.1",
          preview: false,
        },
        ...(mixed
          ? [
              {
                output_handle: `out_${"2".repeat(40)}`,
                ordinal: 2,
                state: "pending",
                segment_id: "segment.2",
                preview: false,
              },
            ]
          : []),
      ],
      allowed_actions: [
        "set_selection",
        "read_projection",
        "import_production_outputs_to_authoring",
      ],
      blocker_codes: [],
      limits: { max_segments: 64, max_outputs: 65 },
    });
  }

  function mount(
    importState: ProductionImportToEditor["state"],
    media: MediaToolsBinding,
    mixed = false,
    selectedSegmentIds?: readonly string[],
  ) {
    const onImport = vi.fn();
    const binding: ProductionImportToEditor = {
      state: importState,
      selectionState: () => ({ eligible: true, reason: null, busy: false }),
      onImport,
      onRetry: vi.fn(),
      onOpen: vi.fn(),
      mediaTools: media,
    };
    const view = render(
      <ProductionWorkbench
        locale="en"
        state={{
          status: "ready",
          projection: {
            ...projection(mixed),
            ...(selectedSegmentIds === undefined ? {} : { selectedSegmentIds }),
          },
        }}
        onIntent={vi.fn()}
        importToEditor={binding}
      />,
    );
    return { view, onImport, onOpen: binding.onOpen };
  }

  const refused = {
    status: "refused",
    editorStatus: "unverified" as const,
    refusal: "service_unavailable",
    receipt: null,
  };
  const idle = {
    status: "idle",
    editorStatus: "unverified" as const,
    refusal: null,
    receipt: null,
  };

  it("imports only the explicitly labeled ready subset of a mixed selection", () => {
    const { onImport } = mount(
      idle,
      tools(readState(wire("status_override"))),
      true,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Import ready (1 of 2)" }),
    );
    expect(onImport).toHaveBeenCalledWith(["segment.1"]);
    expect(
      screen.getByText(/1 pending, 0 failed, 0 without an output/u),
    ).toBeTruthy();
  });

  it("explains empty and not-yet-ready selections without offering an import", () => {
    const media = tools(readState(wire("status_override")));
    const empty = mount(idle, media, true, []);
    expect(
      screen.getByText("Select a segment with a ready output to import."),
    ).toBeTruthy();
    expect(
      (
        screen.getByRole("button", {
          name: "Import selected to editor",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    expect(empty.onImport).not.toHaveBeenCalled();
    cleanup();
    const pending = mount(idle, media, true, ["segment.2"]);
    expect(screen.getByText(/No selected output is ready yet/u)).toBeTruthy();
    expect(
      (
        screen.getByRole("button", {
          name: "Import selected to editor",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    expect(pending.onImport).not.toHaveBeenCalled();
  });

  it("offers editor opening after an accepted import whose history read failed", () => {
    const { onOpen } = mount(
      {
        status: "succeeded",
        editorStatus: "needs_action",
        refusal: null,
        receipt: { rows: [{}] },
      },
      tools(readState(wire("status_override"))),
    );
    fireEvent.click(screen.getByRole("button", { name: "Open editor" }));
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/Import completed/u)).toBeTruthy();
  });

  it("shows the card under a runtime-refused import and continues the eligible selection", () => {
    const media = tools(initialMediaRuntimeState);
    const { view } = mount(refused, media);
    const card = view.container.querySelector<HTMLElement>(
      '[data-h3-media-tools-placement="contextual-sidebar"]',
    )!;
    expect(card).not.toBeNull();
    expect(media.report).toHaveBeenCalledWith("import");
    cleanup();
    const missing = tools(readState(wire("status_setup_required")));
    mount(refused, missing);
    fireEvent.click(
      screen.getByRole("button", { name: "Install and continue" }),
    );
    expect(missing.install).toHaveBeenCalledWith({
      kind: "production_import",
      productionWorkspaceHandle: `pw_${"p".repeat(32)}`,
      productionWorkspaceId: "workspace.1",
      segmentIds: ["segment.1"],
      outputHandles: [READY_HANDLE],
    });
  });

  it("needs only the original action when the runtime is ready", () => {
    const { view } = mount(idle, tools(readState(wire("status_override"))));
    expect(view.container.querySelector("[data-h3-media-tools]")).toBeNull();
    cleanup();
    const other = mount(
      { ...refused, refusal: "workspace_unavailable" },
      tools(readState(wire("status_setup_required"))),
    );
    // A known missing runtime is still shown before the user is refused for it.
    expect(
      other.view.container.querySelector("[data-h3-media-tools]"),
    ).not.toBeNull();
  });

  it("never shows the card for an uncertain import, which keeps its own Retry", () => {
    const { view } = mount(
      { ...idle, status: "uncertain" },
      tools(readState(wire("status_override"))),
    );
    expect(view.container.querySelector("[data-h3-media-tools]")).toBeNull();
  });
});

describe("clip preview placement", () => {
  const identity = {
    workspaceHandle: "authoring-1",
    referenceRevision: 4,
    timelineRevision: 2,
    timelineContentFingerprint: "sha256:" + "a".repeat(64),
    clipId: "clip-1",
    startFrame: 10,
    frames: 24,
    sourceStartFrame: 30,
    fps: 24,
  };
  const intent: MediaRuntimeIntent = {
    kind: "clip_preview",
    workspaceHandle: "authoring-1",
    referenceRevision: 4,
    timelineRevision: 2,
    timelineContentFingerprint: "sha256:" + "a".repeat(64),
    clipId: "clip-1",
    overlayGeneration: null,
  };

  it("reports an unsupported preview, offers setup and reopens once on its own continuation", async () => {
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:active");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    let runtimeReady = false;
    const openPreview = vi.fn(async (_request: AuthoringPreviewRequest) => {
      if (!runtimeReady)
        throw Object.assign(new Error("unsupported"), {
          disposition: "unsupported",
        });
      return { blob: new Blob(["ok"]), audioDisposition: "absent" as const };
    });
    const missing = tools(readState(wire("status_setup_required")));
    const view = (media: MediaToolsBinding) => (
      <AuthoringPreviewMonitor
        locale="en"
        identity={identity}
        openPreview={openPreview}
        returnFocus={null}
        onClose={vi.fn()}
        mediaTools={media}
      />
    );
    const result = render(view(missing));
    await screen.findByText("Preview unavailable for this source format.");
    // The card reports from its mount effect, which can flush after the text is found under load.
    await waitFor(() => expect(missing.report).toHaveBeenCalledWith("preview"));
    fireEvent.click(
      screen.getByRole("button", { name: "Install and continue" }),
    );
    expect(missing.install).toHaveBeenCalledWith(intent);
    expect(openPreview).toHaveBeenCalledTimes(1);

    runtimeReady = true;
    // A continuation for another clip does not reopen this one.
    const ready = readState(wire("status_override"));
    act(() =>
      result.rerender(
        view(
          tools({
            ...ready,
            resume: {
              kind: "clip_preview",
              token: 1,
              identity: intentIdentity({ ...intent, clipId: "clip-2" }),
            },
          }),
        ),
      ),
    );
    expect(openPreview).toHaveBeenCalledTimes(1);
    act(() =>
      result.rerender(
        view(
          tools({
            ...ready,
            resume: {
              kind: "clip_preview",
              token: 2,
              identity: intentIdentity(intent),
            },
          }),
        ),
      ),
    );
    await screen.findByLabelText("Preview clip-1");
    expect(openPreview).toHaveBeenCalledTimes(2);
    expect(result.container.querySelector("[data-h3-media-tools]")).toBeNull();
    // Re-rendering with the same token never reopens it again.
    act(() =>
      result.rerender(
        view(
          tools({
            ...ready,
            resume: {
              kind: "clip_preview",
              token: 2,
              identity: intentIdentity(intent),
            },
          }),
        ),
      ),
    );
    await waitFor(() => expect(openPreview).toHaveBeenCalledTimes(2));
  });

  it("keeps a genuinely unsupported source format free of setup once the runtime is ready", async () => {
    const openPreview = vi.fn(async () => {
      throw Object.assign(new Error("unsupported"), {
        disposition: "unsupported",
      });
    });
    const result = render(
      <AuthoringPreviewMonitor
        locale="en"
        identity={identity}
        openPreview={openPreview}
        returnFocus={null}
        onClose={vi.fn()}
        mediaTools={tools(readState(wire("status_override")))}
      />,
    );
    await screen.findByText("Preview unavailable for this source format.");
    expect(result.container.querySelector("[data-h3-media-tools]")).toBeNull();
  });
});

describe("final output placement and empty timeline", () => {
  function workspace(
    media: MediaToolsBinding | undefined,
    options: { clips?: boolean } = {},
  ) {
    const authoring =
      options.clips === false
        ? authoringReady(SMOKE_SHAPE, {})
        : authoringReady(SMOKE_SHAPE);
    if (options.clips === false && "timelineHistory" in authoring) {
      const history = authoring.timelineHistory!;
      (authoring as { timelineHistory: unknown }).timelineHistory = {
        ...history,
        snapshot: { ...history.snapshot, clips: [] },
      };
    }
    const binding = bindingFixture({
      authoring,
      state: {
        ...expandedState(),
        render: { status: "read", capability: UNSUPPORTED_OUTPUT_CAPABILITY },
      },
      mediaTools: media,
    });
    // M25-44: the final output card lives in the chrome bar's Export popover, shown open here.
    return render(
      <>
        <NleExportMenu
          binding={binding}
          open={true}
          onOpenChange={() => undefined}
        />
        <NleWorkspace binding={binding} onEdgeGestureActive={() => undefined} />
      </>,
    );
  }

  it("offers setup under an unsupported render and captures the snapshot and overlay", () => {
    const media = tools(readState(wire("status_setup_required")));
    const view = workspace(media);
    expect(
      view.container.querySelector(
        '[data-h3-nle-render="backend_render_unavailable"]',
      ),
    ).not.toBeNull();
    const card = view.container.querySelector<HTMLElement>(
      '[data-h3-media-tools-placement="contextual-overlay"]',
    )!;
    expect(card.dataset.h3MediaTools).toBe("setup_required");
    expect(media.report).toHaveBeenCalledWith("render");
    fireEvent.click(
      screen.getByRole("button", { name: "Install and continue" }),
    );
    const captured = (media.install as ReturnType<typeof vi.fn>).mock
      .calls[0]![0] as MediaRuntimeIntent;
    expect(captured).toMatchObject({
      kind: "final_render",
      overlayGeneration: 1,
    });
    // The render action itself is never started from here.
    expect(screen.queryByRole("button", { name: /render/i })).toBeNull();
  });

  it("offers no install where the renderer is not available on the host", () => {
    const status = wire("status_override", (raw) => {
      raw.features.render = {
        state: "unavailable",
        reason: "render_qualification_unavailable",
      };
    });
    const view = workspace(tools(readState(status)));
    expect(view.container.querySelector("[data-h3-media-tools]")).toBeNull();
    expect(
      screen.getByText("Final render unavailable on this host."),
    ).toBeTruthy();
  });

  it("says the timeline has no clips instead of reporting the monitor unavailable", () => {
    const view = workspace(undefined, { clips: false });
    const empty = view.container.querySelector("[data-h3-nle-empty=timeline]");
    // M25-64 (row #27): the empty-timeline overlay's title and hint.
    expect(empty?.textContent).toContain("Nothing on the timeline yet");
    // B-M2563-11: it names the card's visible "+" and drag, not the context menu's command.
    expect(empty?.textContent).toContain("+");
    expect(empty?.textContent).toContain("Media");
    expect(empty?.textContent).not.toContain("Add to timeline");
    expect(view.container.textContent).not.toContain(
      "Composition monitor unavailable.",
    );
    cleanup();
    const withClips = workspace(undefined);
    expect(
      withClips.container.querySelector("[data-h3-nle-empty=timeline]"),
    ).toBeNull();
  });
});
