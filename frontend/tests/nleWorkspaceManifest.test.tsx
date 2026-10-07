// M25-16: every manifest row has an executable control (or a readable observation) in the
// rendered workspace, every canonical control issues exactly its manifest command, and no
// deferred-audio control, UA media control or phantom focus target exists.

import { cleanup, fireEvent, render } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import { NleExportMenu } from "../src/components/nle/NleExportMenu";
import { NleMonitorChips } from "../src/components/nle/NleMonitorChips";
import { createMonitorStatusChannel } from "../src/components/nle/nleMonitorChannel";
import { NleWorkspace } from "../src/components/nle/NleWorkspace";
import type { NleWorkspaceBinding } from "../src/components/nle/nleWorkspaceBinding";
import { NLE_OPERATION_IDS } from "../src/contracts/compositionCodec";
import {
  SMOKE_SHAPE,
  authoringReady,
  snapshotFixture,
} from "./support/nleWorkspaceFixture";
import {
  availableDisposition,
  bindingFixture,
  expandedState,
} from "./support/nleWorkspaceBinding";
import { productionProjection } from "./support/nleSequenceFixture";

type Row = {
  operation_id: string;
  control_selector: string | null;
  authority_class: string;
  command: string | null;
  role: string;
  accessible_name: string | null;
};
const manifest = JSON.parse(
  readFileSync(
    join(
      __dirname,
      "..",
      "..",
      "governance",
      "contracts",
      "nle_control_coverage_manifest_v1.json",
    ),
    "utf8",
  ),
) as { command_rows: Row[]; non_command_rows: Row[] };

/**
 * M25-45: the monitor controls that exist only in the state they apply to -- the two halves of the
 * one play/pause button, and the recovery offer inside the picture. The monitor unit suite proves
 * each one appears and drives the session in its own state; the contract here is that a healthy,
 * unopened monitor shows exactly the play half and offers no recovery.
 */
const STATE_EXCLUSIVE = new Set([
  "transport.play",
  "transport.pause",
  "transport.recover",
]);

afterEach(() => cleanup());

function renderWorkspace(
  selection: string[],
  pane: "assets" | "text" | "sequence" = "assets",
  emptyTimeline = false,
) {
  const ready = authoringReady(SMOKE_SHAPE, {
    selection,
    redo_cursor: `h3.context.timeline_history_cursor.v1:12:${"b".repeat(64)}`,
  });
  const authoring =
    emptyTimeline && "timelineHistory" in ready && ready.timelineHistory
      ? {
          ...ready,
          timelineHistory: {
            ...ready.timelineHistory,
            snapshot: { ...ready.timelineHistory.snapshot, clips: [] },
          },
        }
      : ready;
  const binding = bindingFixture({
    authoring,
    runtime: availableDisposition(),
    production: {
      status: "ready",
      projection: productionProjection(undefined, {
        outputs: [
          {
            output_handle: `out_${"1".repeat(40)}`,
            ordinal: 1,
            state: "ready",
            segment_id: "segment_1",
            preview: false,
          },
        ],
        allowed_actions: [
          "set_selection",
          "read_projection",
          "release_workspace",
          "import_production_outputs_to_authoring",
        ],
      }),
    },
    importAction: {
      state: {
        status: "idle",
        editorStatus: "unverified",
        refusal: null,
        receipt: null,
      },
      selectionState: () => ({ eligible: false, reason: null, busy: false }),
      onImport: () => undefined,
      onRetry: () => undefined,
      onOpen: () => undefined,
    },
    state: expandedState({}, { width: 1280, height: 800 }),
  });
  (binding as { state: NleWorkspaceBinding["state"] }).state = {
    ...binding.state,
    surface: { ...binding.state.surface, pane },
    render: { status: "read", capability: null },
  };
  // M25-44: the render card moved into the chrome bar's Export popover, which stays mounted
  // (hidden) while closed; M25-45 moved the monitor's status and audio chips into the same bar.
  // Render the chrome the overlay renders, beside the workspace, as the overlay does.
  const monitorStatus = createMonitorStatusChannel();
  const view = render(
    <>
      <NleExportMenu
        binding={binding}
        open={false}
        onOpenChange={() => undefined}
      />
      <NleMonitorChips locale="en" channel={monitorStatus} />
      <NleWorkspace
        binding={binding}
        onEdgeGestureActive={() => undefined}
        monitorStatus={monitorStatus}
      />
    </>,
  );
  return { binding, view };
}

const PROPERTY_TAB: Readonly<Record<string, string>> = {
  "visual.transform": "basic",
  "visual.opacity_blend": "basic",
  "visual.crop": "crop",
  "visual.effect": "colour",
  "text.content": "text",
  "text.style": "text",
  "boundary.transition": "transition",
  "audio.clip": "audio",
};

/**
 * The one owned group whose controls may carry audio words: a video clip's own gain, mute and
 * fades. Independent audio stays deferred, so the sweep below keeps finding any audio control
 * anywhere else.
 */
const CLIP_AUDIO_GROUP = '[data-h3-nle-group="audio.clip"]';

/** Controls named with an audio word outside the clip's own audio group. */
function audioWordControls(root: HTMLElement): string[] {
  const audioWords = /volume|mute|waveform|gain|pan|solo/iu;
  return [...root.querySelectorAll<HTMLElement>("button,input,select")]
    .filter(
      (control) =>
        // Timeline viewport pan is a view operation, not an audio pan control.
        !control.getAttribute("data-h3-nle-alternative")?.startsWith("pan.") &&
        control.closest(CLIP_AUDIO_GROUP) === null,
    )
    .map(
      (control) =>
        control.getAttribute("aria-label") ?? control.textContent ?? "",
    )
    .filter((name) => audioWords.test(name));
}

function revealControl(
  container: HTMLElement,
  operation: string,
  clipId: string,
): void {
  const tab = PROPERTY_TAB[operation];
  if (tab !== undefined) {
    const control = container.querySelector<HTMLButtonElement>(
      `[data-h3-nle-property-tab="${tab}"]`,
    );
    if (control !== null) fireEvent.click(control);
  }
  // M25-63: range commands live in the first card's menu, opened by a context click.
  if (["range.insert", "range.overwrite"].includes(operation)) {
    const card = container.querySelector(".h3-nle-media-primary");
    if (card !== null) fireEvent.contextMenu(card);
  }
  // M25-79: a trigger button toggles its menu, so a reveal presses it only while the menu is
  // closed; pressing it again for the next row would close the menu that row needs.
  const open = (name: string) =>
    container.querySelector(`[role="menu"][aria-label="${name}"]`) !== null;
  if (
    ["track.add", "track.remove", "track.reorder"].includes(operation) &&
    !open("Track menu")
  )
    fireEvent.click(
      container.querySelector<HTMLButtonElement>(
        '[data-h3-nle-track="track-1"] [data-h3-nle-menu-trigger="track"]',
      )!,
    );
  if (
    [
      "asset.replace",
      "clip.merge",
      "clip.slip",
      "clip.slide",
      "clip.enabled",
    ].includes(operation) &&
    !open("Clip menu")
  ) {
    // M25-62 (B-M2562-03): the inline trigger shows only on a selected clip of at least 96 px,
    // so a narrower one opens its menu the way a mouse user does, with a context click.
    const trigger = container.querySelector<HTMLButtonElement>(
      `[data-h3-nle-clip="${clipId}"] [data-h3-nle-menu-trigger="clip"], ` +
        `.h3-nle-trim-rail[data-h3-nle-trim-clip="${clipId}"] [data-h3-nle-menu-trigger="clip"]`,
    );
    if (trigger !== null) fireEvent.click(trigger);
    else
      fireEvent.contextMenu(
        container.querySelector(
          `[data-h3-nle-clip="${clipId}"] [data-h3-nle-control="selection.set"]`,
        )!,
      );
  }
}

describe("M25-16 control coverage manifest", () => {
  it("lists exactly the 34 profile commands once each", () => {
    expect(manifest.command_rows.map((row) => row.command).sort()).toEqual(
      [...NLE_OPERATION_IDS].sort(),
    );
  });

  it("renders an executable control for every command row across the selection states", () => {
    const snapshot = snapshotFixture(SMOKE_SHAPE);
    const media = snapshot.clips.find((clip) => clip.assetId !== null)!.clipId;
    const text = snapshot.clips.find((clip) => clip.text !== null)!.clipId;
    const second = snapshot.clips.find(
      (clip) => clip.assetId !== null && clip.clipId !== media,
    )!.clipId;
    const seen = new Set<string>();
    for (const [index, selection] of [[media, second], [text]].entries()) {
      for (const pane of ["assets", "text"] as const) {
        const { view } = renderWorkspace(selection, pane);
        // Visit both toolbar modes and both bin tabs: each owns migrated commands.
        if (index === 1)
          fireEvent.click(
            view.getByRole("button", { name: "Main-track ripple" }),
          );
        fireEvent.click(
          view.container.querySelector<HTMLButtonElement>(
            '[data-h3-nle-alternative="move.open"]',
          )!,
        );
        for (const row of manifest.command_rows) {
          revealControl(view.container, row.operation_id, selection[0]!);
          const control = view.container.querySelector<HTMLElement>(
            row.control_selector!,
          );
          if (control === null) continue;
          expect(control.tagName).toBe("BUTTON");
          expect(control.getAttribute("type")).toBe("button");
          expect(control.getAttribute("role") ?? "button").toBe(row.role);
          seen.add(row.operation_id);
        }
        cleanup();
      }
    }
    const missing = manifest.command_rows
      .map((row) => row.operation_id)
      .filter((id) => !seen.has(id));
    expect(missing).toEqual([]);
  });

  it("issues exactly the manifest command when a canonical control is activated", () => {
    const snapshot = snapshotFixture(SMOKE_SHAPE);
    const media = snapshot.clips.find((clip) => clip.assetId !== null)!.clipId;
    const { binding, view } = renderWorkspace([media]);
    const timeline = binding.actions.timeline as unknown as {
      mock: { calls: unknown[][] };
    };
    const activate = (operation: string) => {
      const row = manifest.command_rows.find(
        (member) => member.operation_id === operation,
      )!;
      revealControl(view.container, operation, media);
      if (operation === "track.reorder")
        fireEvent.change(view.getByRole("spinbutton", { name: "Order" }), {
          target: { value: "2" },
        });
      else if (operation === "visual.transform")
        fireEvent.change(
          view.getByRole("spinbutton", { name: "Position X (%)" }),
          {
            target: { value: "5" },
          },
        );
      else if (operation === "visual.crop")
        fireEvent.change(
          view.getByRole("spinbutton", { name: "Crop left (%)" }),
          {
            target: { value: "5" },
          },
        );
      else if (operation === "visual.opacity_blend")
        fireEvent.change(
          view.getByRole("spinbutton", { name: "Opacity (%)" }),
          {
            target: { value: "50" },
          },
        );
      else if (operation === "visual.effect")
        fireEvent.change(
          view.getByRole("spinbutton", { name: "Brightness (%)" }),
          {
            target: { value: "10" },
          },
        );
      else if (operation === "audio.clip")
        fireEvent.change(
          view.getByRole("spinbutton", { name: "Volume (dB)" }),
          {
            target: { value: "-6" },
          },
        );
      const control = view.container.querySelector<HTMLButtonElement>(
        row.control_selector!,
      )!;
      expect(control, operation).not.toBeNull();
      expect(control.disabled, operation).toBe(false);
      const before = timeline.mock.calls.length;
      fireEvent.click(control);
      expect(timeline.mock.calls.length, operation).toBe(before + 1);
      const intent = timeline.mock.calls[before]![0] as {
        action: string;
        commands: { kind: string }[];
      };
      expect(intent.action).toBe("apply_timeline_commands");
      expect(intent.commands).toHaveLength(1);
      expect(intent.commands[0]!.kind).toBe(row.command);
    };
    for (const operation of [
      "track.add",
      "track.enabled",
      "track.locked",
      "track.reorder",
      "asset.replace",
      "clip.remove",
      "clip.enabled",
      "visual.transform",
      "visual.crop",
      "visual.opacity_blend",
      "visual.effect",
      "audio.clip",
      "clip.slip",
      "history.undo",
      "history.redo",
    ])
      activate(operation);
    // Stateful playhead/ripple/roll commands execute through their real toolbar/gesture seam in
    // nleWorkspaceTimelineTools.test.tsx; this fixture intentionally has no opened monitor frame.

    cleanup();
    const emptyWorkspace = renderWorkspace([], "assets", true);
    const emptyTimeline = emptyWorkspace.binding.actions
      .timeline as unknown as {
      mock: { calls: unknown[][] };
    };
    for (const operation of [
      "asset.insert",
      "range.insert",
      "range.overwrite",
    ]) {
      const row = manifest.command_rows.find(
        (member) => member.operation_id === operation,
      )!;
      revealControl(emptyWorkspace.view.container, operation, "");
      const control =
        emptyWorkspace.view.container.querySelector<HTMLButtonElement>(
          row.control_selector!,
        )!;
      const before = emptyTimeline.mock.calls.length;
      fireEvent.click(control);
      expect(emptyTimeline.mock.calls.length, operation).toBe(before + 1);
      expect(
        (
          emptyTimeline.mock.calls[before]![0] as {
            commands: { kind: string }[];
          }
        ).commands[0]!.kind,
      ).toBe(row.command);
    }

    cleanup();
    const textWorkspace = renderWorkspace([], "text", true);
    const textTimeline = textWorkspace.binding.actions.timeline as unknown as {
      mock: { calls: unknown[][] };
    };
    const title = textWorkspace.view.container.querySelector<HTMLButtonElement>(
      '[data-h3-nle-control="title.insert"]',
    )!;
    expect(title.disabled).toBe(false);
    fireEvent.click(title);
    expect(textTimeline.mock.calls.at(-1)?.[0]).toMatchObject({
      action: "apply_timeline_commands",
      commands: [{ kind: "insert_title_clip" }],
    });
  });

  it("renders every non-command observation without a UA media control or audio control", () => {
    const snapshot = snapshotFixture(SMOKE_SHAPE);
    const media = snapshot.clips[0]!.clipId;
    const { view } = renderWorkspace([media]);
    for (const row of manifest.non_command_rows) {
      if (row.control_selector === null) continue;
      if (STATE_EXCLUSIVE.has(row.operation_id)) continue; // asserted together below
      expect(
        view.container.querySelector(row.control_selector),
        row.operation_id,
      ).not.toBeNull();
    }
    expect(
      [...STATE_EXCLUSIVE].filter(
        (id) =>
          view.container.querySelector(`[data-h3-nle-control="${id}"]`) !==
          null,
      ),
    ).toEqual(["transport.play"]);
    expect(view.container.querySelector("video[controls],audio")).toBeNull();
    expect(
      view.container.querySelector("[data-h3-nle-audio-track]"),
    ).toBeNull();
    // The clip's own audio group is open, so its controls are present and excluded by name.
    fireEvent.click(
      view.container.querySelector<HTMLButtonElement>(
        '[data-h3-nle-property-tab="audio"]',
      )!,
    );
    const group = view.container.querySelector<HTMLElement>(CLIP_AUDIO_GROUP)!;
    expect(group.querySelector('input[type="range"]')).not.toBeNull();
    expect(audioWordControls(view.container)).toEqual([]);
    // The exclusion is the group and nothing more: the same control outside it is found.
    const planted = document.createElement("button");
    planted.setAttribute("aria-label", "Track volume");
    group.parentElement!.append(planted);
    expect(audioWordControls(view.container)).toEqual(["Track volume"]);
    planted.remove();
    expect(
      view.container.querySelector(
        '[data-h3-nle-render="backend_render_unavailable"]',
      ),
    ).not.toBeNull();
  });

  it("uses manual APG tab activation with wrapped focus and linked panels", () => {
    const snapshot = snapshotFixture(SMOKE_SHAPE);
    const { binding, view } = renderWorkspace([snapshot.clips[0]!.clipId]);
    const selectPane = binding.actions.selectPane as unknown as {
      mock: { calls: unknown[][] };
    };
    const tabs = [
      ...view.container.querySelectorAll<HTMLButtonElement>(
        '[role="tab"][id^="h3-nle-tab-"]',
      ),
    ];
    expect(tabs.map((tab) => tab.tabIndex)).toEqual([0, -1, -1]);
    expect(tabs[0]!.getAttribute("aria-controls")).toBe("h3-nle-panel-assets");
    expect(
      view.container
        .querySelector('[role="tabpanel"][id^="h3-nle-panel-"]')
        ?.getAttribute("aria-labelledby"),
    ).toBe("h3-nle-tab-assets");

    tabs[0]!.focus();
    fireEvent.keyDown(tabs[0]!, { key: "ArrowRight" });
    expect(document.activeElement).toBe(tabs[1]);
    expect(selectPane.mock.calls).toHaveLength(0);
    fireEvent.keyDown(tabs[1]!, { key: "End" });
    expect(document.activeElement).toBe(tabs[2]);
    fireEvent.keyDown(tabs[2]!, { key: "ArrowRight" });
    expect(document.activeElement).toBe(tabs[0]);
    fireEvent.keyDown(tabs[0]!, { key: " " });
    expect(selectPane.mock.calls.at(-1)?.[0]).toBe("assets");
  });
});
