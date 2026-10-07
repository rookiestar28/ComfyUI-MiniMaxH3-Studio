import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { H3Sidebar, initialAppModeDraft } from "../src/components/H3Sidebar";
import {
  appModeArtifactPrefix,
  createAppModeController,
  type AppModeInputs,
} from "../src/host/appMode";
import {
  H3_SHELL_MANIFEST,
  inspectVisibleH3Graph,
} from "../src/host/graphAdapter";
import {
  MODE_TEMPLATE,
  spliceContextPipeline,
  type Json,
} from "../src/host/templateMaterialization";
import { initialShellState } from "../src/state/shellState";
import { loadAvailableProfile } from "./support/generationProfileFixture";
import { fixtureBackedAppModeHost } from "./support/hostSeamTestDouble";
import { createQualifiedBasePrompt } from "./support/qualifiedBasePrompt";
import { acceptedQueueResponse } from "./support/queuePromptTestDouble";
import {
  loadSyntheticTemplate,
  syntheticTemplate,
} from "./support/templateFixture";

afterEach(cleanup);

/**
 * M17-17. Who owns the soundtrack of a reference video.
 *
 * The repository has always had an exact answer: an audio asset carrying
 * `paired_video_id` is the only thing that produces a native
 * `ref_video_audio_*` binding, and `tests/test_m17_17_ref2va_soundtrack_ownership.py`
 * pins that. App Mode never told it. The splice wired the video's own audio
 * track straight into the anchor and handed the Reference Registry only the
 * video, so the typed report said "this run submits no soundtrack" about a graph
 * that submits one -- and no user could say otherwise, because no input existed
 * for it.
 *
 * These rows assert the corrected contract: one declared state, one owner, and
 * the registry and the anchor telling the same story.
 */

const OPTIONS = {
  userIntent: "A red kite crosses the sky while the camera follows its arc.",
  durationSeconds: 5.167,
};

const VIDEO = { type: "LoadVideo", widgetValues: ["clip.mp4"] };
const REGISTRY_TYPE = "comfyui_h3_context.H3Context.ReferenceRegistry";
const COMPONENTS_TYPE = "GetVideoComponents";

type Link = [number, number, number, number, number, string];

const nodes = (workflow: Json): Json[] => (workflow.nodes as Json[]) ?? [];
const links = (workflow: Json): Link[] => (workflow.links as Link[]) ?? [];

function node(workflow: Json, type: string): Json {
  return nodes(workflow).find((entry) => entry.type === type)!;
}

function inputNames(target: Json): string[] {
  return ((target.inputs as Json[]) ?? []).map((input) => String(input.name));
}

/** The `[originNode, originSlot]` an input is fed from, or undefined when unbound. */
function source(
  workflow: Json,
  target: Json,
  name: string,
): [number, number] | undefined {
  const input = ((target.inputs as Json[]) ?? []).find(
    (entry) => entry.name === name,
  );
  if (input === undefined) return undefined;
  const edge = links(workflow).find((link) => link[0] === input.link);
  return edge === undefined ? undefined : [edge[1], edge[2]];
}

function splice(
  soundtrack: "included" | "excluded" | "unavailable" | undefined,
  videos: readonly { type: string; widgetValues: readonly unknown[] }[] = [
    VIDEO,
  ],
): Json {
  return spliceContextPipeline(syntheticTemplate(MODE_TEMPLATE.ref2va!), {
    ...OPTIONS,
    taskMode: "ref2va",
    media: {
      referenceVideos: videos,
      ...(soundtrack === undefined
        ? {}
        : { referenceVideoSoundtrack: soundtrack }),
    },
  }).workflow;
}

describe("M17-17 the reference video soundtrack has one declared owner", () => {
  it("routes an included soundtrack through the registry, not around it", () => {
    const workflow = splice("included");
    const registry = node(workflow, REGISTRY_TYPE);
    const components = node(workflow, COMPONENTS_TYPE);
    const anchor = nodes(workflow).find((entry) =>
      inputNames(entry).some((name) => name.startsWith("ref_videos.")),
    )!;

    // The registry owns canonical reference identity. If the audio reaches the
    // anchor without reaching the registry, the typed report the sidebar shows
    // and the graph the host runs are describing two different requests.
    expect(inputNames(registry)).toContain("paired_audios.paired_audio0");
    const declared = source(workflow, registry, "paired_audios.paired_audio0");
    const bound = source(
      workflow,
      anchor,
      "ref_video_audios.ref_video_audio_0",
    );
    expect(declared).toEqual([Number(components.id), 1]);
    // Same node, same slot: one soundtrack, not two that happen to agree.
    expect(bound).toEqual(declared);
  });

  it("treats an undeclared soundtrack as included, so accepted routes are unchanged", () => {
    // AC-M17-17-04. Every ref2va route accepted before this item bound the
    // video's audio; the correction declares that rather than changing it.
    expect(JSON.stringify(splice(undefined))).toBe(
      JSON.stringify(splice("included")),
    );
  });

  it("creates no audio owner and no native edge when the soundtrack is excluded", () => {
    const workflow = splice("excluded");
    const registry = node(workflow, REGISTRY_TYPE);
    const anchor = nodes(workflow).find((entry) =>
      inputNames(entry).some((name) => name.startsWith("ref_videos.")),
    )!;

    expect(inputNames(registry)).not.toContain("paired_audios.paired_audio0");
    // The template ships the anchor's soundtrack socket, so what has to be gone
    // is the edge, not the name. A socket left pointing at a detached link is
    // read by the host as a bound input it then cannot resolve.
    expect(
      source(workflow, anchor, "ref_video_audios.ref_video_audio_0"),
    ).toBeUndefined();
    expect(
      ((anchor.inputs as Json[]) ?? []).find(
        (input) => input.name === "ref_video_audios.ref_video_audio_0",
      )?.link ?? null,
    ).toBeNull();
    // The video itself is still a reference: excluding its soundtrack is not
    // excluding the video.
    expect(source(workflow, registry, "videos.video0")).toBeDefined();
    expect(source(workflow, anchor, "ref_videos.ref_video_0")).toBeDefined();
  });

  it("produces the same graph for excluded and unavailable", () => {
    // The two states differ in what the user is told, never in what is queued.
    // A soundtrack the host cannot supply must not become a soundtrack the user
    // declined, and neither may become silence presented as a soundtrack.
    expect(JSON.stringify(splice("unavailable"))).toBe(
      JSON.stringify(splice("excluded")),
    );
  });

  it("binds each soundtrack to its own video when several are submitted", () => {
    const workflow = splice("included", [
      VIDEO,
      { type: "LoadVideo", widgetValues: ["second.mp4"] },
    ]);
    const registry = node(workflow, REGISTRY_TYPE);
    const anchor = nodes(workflow).find((entry) =>
      inputNames(entry).some((name) => name.startsWith("ref_videos.")),
    )!;
    const components = nodes(workflow).filter(
      (entry) => entry.type === COMPONENTS_TYPE,
    );
    expect(components).toHaveLength(2);

    for (const [index, component] of components.entries()) {
      expect(
        source(workflow, registry, `paired_audios.paired_audio${index}`),
      ).toEqual([Number(component.id), 1]);
      expect(
        source(workflow, anchor, `ref_video_audios.ref_video_audio_${index}`),
      ).toEqual([Number(component.id), 1]);
      expect(source(workflow, registry, `videos.video${index}`)).toEqual([
        Number(component.id) - 1,
        0,
      ]);
    }
  });

  it("keeps every soundtrack state a canvas App Mode can read back", () => {
    // The materialize-then-adopt join. A declared soundtrack that made the graph
    // unreadable would leave the user unable to re-enter their own production.
    for (const state of ["included", "excluded", "unavailable"] as const) {
      expect(
        inspectVisibleH3Graph(splice(state), H3_SHELL_MANIFEST),
      ).toMatchObject({ status: "ready", existingGraphCompatible: true });
    }
  });
});

/**
 * The control that carries the declaration.
 *
 * The state has to be authorable, or `included` is not a default but the only
 * reachable value -- which is the situation M17-17 found. The control appears
 * only once a reference video is selected, because a soundtrack state with no
 * video to own it is a claim about nothing, and App Mode refuses it as such.
 */
describe("M17-17 the soundtrack state is authored, not assumed", () => {
  const mediaSources = [
    {
      node_id: "22",
      label: "Video node 22",
      kind: "video" as const,
      output_slot: 0 as const,
    },
  ];

  function view(draft = initialAppModeDraft, onStart = vi.fn()) {
    const result = render(
      <H3Sidebar
        state={initialShellState}
        appMode={{
          capability: { status: "ready" as const },
          durationResolution: {
            status: "resolved" as const,
            resolution: {
              schema: "h3.context.duration_resolution.v1" as const,
              requested_seconds: 5,
              requested_milliseconds: 5000,
              effective_milliseconds: 5167,
              frame_count: 124,
              snapped: true,
            },
          },
          imageSources: [],
          mediaSources,
          onStart,
          onCancel: vi.fn(),
        }}
        appModeDraft={{ ...draft, taskMode: "ref2va" }}
      />,
    );
    return { ...result, onStart };
  }

  it("offers no soundtrack state until there is a video to own it", () => {
    view();
    expect(
      screen.queryByRole("combobox", { name: "Reference video soundtrack" }),
    ).toBeNull();
  });

  it("offers exactly the three declared states once a video is selected", () => {
    view({ ...initialAppModeDraft, referenceVideos: ["22"] });
    const control = screen.getByRole("combobox", {
      name: "Reference video soundtrack",
    }) as HTMLSelectElement;
    expect([...control.options].map((option) => option.value)).toEqual([
      "included",
      "excluded",
      "unavailable",
    ]);
    expect(control.value).toBe("included");
  });

  it("carries the declared state into the request App Mode starts", () => {
    const { onStart } = view({
      ...initialAppModeDraft,
      referenceVideos: ["22"],
      referenceVideoSoundtrack: "excluded",
    });
    fireEvent.click(screen.getByRole("button", { name: /start h3 app mode/i }));
    const inputs = onStart.mock.calls[0]?.[0] as AppModeInputs;
    // The control has an executed effect: what it says is what the splice reads.
    expect(inputs.reference_video_soundtrack).toBe("excluded");
    expect(inputs.reference_video_sources).toEqual(["22"]);
  });

  it("sends no soundtrack claim when no video was selected", () => {
    const { onStart } = view({
      ...initialAppModeDraft,
      referenceAudios: ["31"],
      referenceVideoSoundtrack: "excluded",
    });
    fireEvent.click(screen.getByRole("button", { name: /start h3 app mode/i }));
    // The request has to actually be sent, or this row would pass for the wrong
    // reason: a blocked submit sends no soundtrack claim either.
    expect(onStart).toHaveBeenCalledTimes(1);
    const inputs = onStart.mock.calls[0]?.[0] as AppModeInputs;
    // App Mode refuses the pair, so the sidebar must not send it. A claim about
    // a video that is not in the request is exactly the unowned state this item
    // removed.
    expect(inputs.reference_video_soundtrack).toBeUndefined();
    expect(inputs.reference_video_sources).toBeUndefined();
  });
});

/**
 * The joined route, driven through the controller the sidebar actually calls.
 *
 * The splice rows prove the graph shape and the control rows prove the
 * declaration is authorable. This is the seam between them: what the user
 * declared has to survive admission, resolution and materialization and still be
 * the thing the canvas carries.
 */
describe("M17-17 the declared state reaches the canvas", () => {
  const BASE: AppModeInputs = {
    task_mode: "ref2va",
    user_intent: "Preserve the selected reference video.",
    duration_milliseconds: 5167,
    frame_count: 124,
    reference_video_sources: ["22"],
  };

  const serialized = {
    nodes: [{ id: 22, type: "LoadVideo", widgets_values: ["private.mp4"] }],
  };
  const opaque = {
    reference_videos: [
      { class_type: "LoadVideo", inputs: { file: "private.mp4" } },
    ],
  };

  async function materialize(inputs: AppModeInputs): Promise<Json> {
    const activeWorkflow: object = {
      path: "workflows/m17-17-soundtrack.json",
    };
    const workflowStore = {
      activeWorkflow,
      openWorkflows: [activeWorkflow] as object[],
    };
    const loadGraphData = vi.fn(
      (
        _value: unknown,
        _clean = true,
        _restoreView = true,
        workflow: object | null = null,
      ) => {
        if (workflow === null) {
          const temporary = {
            path: "workflows/unexpected-soundtrack-copy.json",
          };
          workflowStore.openWorkflows.push(temporary);
          workflowStore.activeWorkflow = temporary;
        } else workflowStore.activeWorkflow = workflow;
      },
    );
    const app = {
      extensionManager: { workflow: workflowStore },
      loadApiJson: vi.fn(),
      loadGraphData,
      graphToPrompt: vi
        .fn()
        .mockResolvedValueOnce({
          output: { "22": opaque.reference_videos[0] },
          workflow: {},
        })
        .mockResolvedValueOnce({
          output: createQualifiedBasePrompt(inputs, opaque, {
            sharedDurationSource: true,
          }),
          workflow: {},
        }),
      graph: { serialize: () => serialized },
    };
    const qualified = fixtureBackedAppModeHost(app, {
      queuePrompt: vi.fn().mockResolvedValue(acceptedQueueResponse(1)),
    });
    await createAppModeController(qualified.app, qualified.api, {
      loadTemplate: loadSyntheticTemplate,
      loadProfile: loadAvailableProfile,
    }).start(inputs, { replaceExisting: true });
    expect(qualified.app.loadGraphData).toHaveBeenCalledOnce();
    expect(loadGraphData.mock.calls[0]?.[3]).toBe(activeWorkflow);
    expect(workflowStore.openWorkflows).toEqual([activeWorkflow]);
    return qualified.app.loadGraphData.mock.calls[0]?.[0] as Json;
  }

  it("materializes the pairing the user declared, and only that one", async () => {
    const included = await materialize(BASE);
    const registry = node(included, REGISTRY_TYPE);
    expect(inputNames(registry)).toContain("paired_audios.paired_audio0");

    const excluded = await materialize({
      ...BASE,
      reference_video_soundtrack: "excluded",
    });
    const excludedRegistry = node(excluded, REGISTRY_TYPE);
    expect(inputNames(excludedRegistry)).not.toContain(
      "paired_audios.paired_audio0",
    );
    const anchor = nodes(excluded).find((entry) =>
      inputNames(entry).some((name) => name.startsWith("ref_videos.")),
    )!;
    expect(
      source(excluded, anchor, "ref_video_audios.ref_video_audio_0"),
    ).toBeUndefined();
  });

  it("refuses a soundtrack state with no video to own it", async () => {
    await expect(
      materialize({
        task_mode: "ref2va",
        user_intent: "Preserve the selected reference audio.",
        duration_milliseconds: 5167,
        frame_count: 124,
        reference_audio_sources: ["31"],
        reference_video_soundtrack: "excluded",
      }),
      // The exact refusal matters: rejecting for some other reason would make
      // this row pass while the unowned state still reached the canvas.
    ).rejects.toMatchObject({ code: "invalid_request" });
  });

  it("gives each declared state its own artifact location", () => {
    // Three states, three revisions. Two declared requests that resolve to one
    // location would let a regeneration overwrite the artifact of the revision
    // it replaced -- and `excluded` and `unavailable` build the same graph, so
    // the declaration is the only thing that separates them.
    const prefixes = (["included", "excluded", "unavailable"] as const).map(
      (state) =>
        appModeArtifactPrefix({ ...BASE, reference_video_soundtrack: state }),
    );
    expect(new Set(prefixes).size).toBe(3);
    // The default is `included`, so an undeclared request lands where the
    // declared-included one does.
    expect(appModeArtifactPrefix(BASE)).toBe(prefixes[0]);
  });
});
