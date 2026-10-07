import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Profiler } from "react";

import {
  alignedTransform,
  NleInspectorTabs,
  rangeFill,
} from "../src/components/nle/NleInspectorTabs";
import { IDENTITY_TRANSFORM } from "../src/components/nle/nleCommandBuilders";
import { createSidebarRetention } from "../src/state/sidebarRetention";
import {
  SMOKE_SHAPE,
  authoringReady,
  snapshotFixture,
} from "./support/nleWorkspaceFixture";

afterEach(() => cleanup());

function subject(
  clipId: string | null,
  onIntent = vi.fn(async () => undefined),
  retention = createSidebarRetention(),
  snapshot = snapshotFixture(SMOKE_SHAPE, 11),
) {
  const selection = clipId === null ? [] : [clipId];
  return {
    onIntent,
    retention,
    view: render(
      <NleInspectorTabs
        locale="en"
        snapshot={snapshot}
        selection={selection}
        authoring={authoringReady(SMOKE_SHAPE, { selection, revision: 11 })}
        onIntent={onIntent}
        retention={retention}
      />,
    ),
  };
}

function commandKinds(onIntent: ReturnType<typeof vi.fn>): string[] {
  return onIntent.mock.calls.map(
    (call) =>
      (call[0] as { commands: readonly { kind: string }[] }).commands[0]!.kind,
  );
}

/** Anchor and the alignment buttons live in Transform's secondary group, closed at rest. */
function openAnchorAndAlignment() {
  fireEvent.click(screen.getByRole("button", { name: "Anchor and alignment" }));
}

describe("M25-50 tabbed property inspector", () => {
  it("does not commit an equivalent property reseed after accepted identity changes", () => {
    const commits: string[] = [];
    const retention = createSidebarRetention();
    const onIntent = vi.fn(async () => undefined);
    const inspector = (revision: number) => (
      <Profiler id="inspector" onRender={(_id, phase) => commits.push(phase)}>
        <NleInspectorTabs
          locale="en"
          snapshot={snapshotFixture(SMOKE_SHAPE, revision)}
          selection={["clip-0"]}
          authoring={authoringReady(SMOKE_SHAPE, {
            selection: ["clip-0"],
            revision,
          })}
          onIntent={onIntent}
          retention={retention}
        />
      </Profiler>
    );
    const view = render(inspector(11));
    commits.length = 0;
    view.rerender(inspector(12));
    expect(commits).toEqual(["update"]);
    expect(onIntent).not.toHaveBeenCalled();
  });

  it("keeps typing on the current identity and reseeds a genuinely changed accepted property", () => {
    const commits: string[] = [];
    const retention = createSidebarRetention();
    const onIntent = vi.fn(async () => undefined);
    const inspector = (revision: number, position: number) => {
      const base = snapshotFixture(SMOKE_SHAPE, revision);
      return (
        <Profiler id="inspector" onRender={(_id, phase) => commits.push(phase)}>
          <NleInspectorTabs
            locale="en"
            snapshot={{
              ...base,
              clips: base.clips.map((clip) =>
                clip.clipId === "clip-0"
                  ? {
                      ...clip,
                      transform: { ...clip.transform, position_x_bp: position },
                    }
                  : clip,
              ),
            }}
            selection={["clip-0"]}
            authoring={authoringReady(SMOKE_SHAPE, {
              selection: ["clip-0"],
              revision,
            })}
            onIntent={onIntent}
            retention={retention}
          />
        </Profiler>
      );
    };
    const view = render(inspector(11, 0));
    const position = screen.getByRole("spinbutton", { name: "Position X (%)" });
    fireEvent.change(position, { target: { value: "33" } });
    view.rerender(inspector(11, 0));
    expect((position as HTMLInputElement).value).toBe("33");
    commits.length = 0;
    view.rerender(inspector(12, 2500));
    expect((position as HTMLInputElement).value).toBe("25.00");
    expect(commits).toHaveLength(2);
    expect(onIntent).not.toHaveBeenCalled();
  });

  it("does not publish a numeric blur when the text already has its display format", () => {
    const commits: string[] = [];
    const onIntent = vi.fn(async () => undefined);
    render(
      <Profiler id="inspector" onRender={(_id, phase) => commits.push(phase)}>
        <NleInspectorTabs
          locale="en"
          snapshot={snapshotFixture(SMOKE_SHAPE, 11)}
          selection={["clip-0"]}
          authoring={authoringReady(SMOKE_SHAPE, {
            selection: ["clip-0"],
            revision: 11,
          })}
          onIntent={onIntent}
          retention={createSidebarRetention()}
        />
      </Profiler>,
    );
    const position = screen.getByRole("spinbutton", { name: "Position X (%)" });
    fireEvent.change(position, { target: { value: "12.34" } });
    commits.length = 0;
    fireEvent.blur(position);
    expect((position as HTMLInputElement).value).toBe("12.34");
    expect(commits).toEqual([]);
    expect(onIntent).not.toHaveBeenCalled();
  });

  it("admits a dissolve over a lower layer without requiring a same-track neighbour", () => {
    const onIntent = vi.fn(async () => undefined);
    subject("clip-2", onIntent);
    fireEvent.click(screen.getByRole("tab", { name: "Transition" }));
    fireEvent.change(screen.getByRole("combobox", { name: "Transition" }), {
      target: { value: "cross_dissolve_v1" },
    });
    fireEvent.change(
      screen.getByRole("spinbutton", { name: "Transition frames" }),
      {
        target: { value: "12" },
      },
    );
    const apply = document.querySelector<HTMLButtonElement>(
      '[data-h3-nle-control="boundary.transition"]',
    )!;
    expect(apply.disabled).toBe(false);
    fireEvent.click(apply);
    expect(commandKinds(onIntent)).toEqual(["set_transition"]);
    cleanup();

    subject("clip-0");
    fireEvent.click(screen.getByRole("tab", { name: "Transition" }));
    fireEvent.change(screen.getByRole("combobox", { name: "Transition" }), {
      target: { value: "cross_dissolve_v1" },
    });
    fireEvent.change(
      screen.getByRole("spinbutton", { name: "Transition frames" }),
      {
        target: { value: "12" },
      },
    );
    expect(
      document.querySelector<HTMLButtonElement>(
        '[data-h3-nle-control="boundary.transition"]',
      )!.disabled,
    ).toBe(true);
  });

  it("exposes uniform scale on Basic and the picture alignment controls in its secondary group", () => {
    subject("clip-0");
    expect(
      screen.getByRole("switch", { name: "Uniform scale" }),
    ).not.toBeNull();
    const labels = [
      "Align left",
      "Align horizontal centre",
      "Align right",
      "Align top",
      "Align vertical centre",
      "Align bottom",
    ];
    // The group is closed at rest: its controls exist and are not offered.
    for (const label of labels)
      expect(screen.queryByRole("button", { name: label })).toBeNull();
    openAnchorAndAlignment();
    for (const label of labels)
      expect(screen.getByRole("button", { name: label })).not.toBeNull();
  });

  it("links both scale drafts and submits one transform command on Enter", () => {
    const onIntent = vi.fn(async () => undefined);
    subject("clip-0", onIntent);
    // Uniform scale is on for a clip whose scales are equal: one Scale field writes both.
    expect(
      screen
        .getByRole("switch", { name: "Uniform scale" })
        .getAttribute("aria-checked"),
    ).toBe("true");
    const scale = screen.getByRole("spinbutton", { name: "Scale (%)" });
    // M25-64 (R10): 120 % is 12,000 bp on both axes.
    fireEvent.change(scale, { target: { value: "120" } });
    fireEvent.keyDown(scale, { key: "Enter" });
    fireEvent.blur(scale);
    expect(commandKinds(onIntent)).toEqual(["set_visual_transform"]);
    const command = (
      onIntent.mock.calls as unknown as Array<
        [
          {
            commands: Array<{
              payload: {
                transform: { scale_x_bp: number; scale_y_bp: number };
              };
            }>;
          },
        ]
      >
    )[0]![0].commands[0]!;
    expect(command.payload.transform.scale_x_bp).toBe(12000);
    expect(command.payload.transform.scale_y_bp).toBe(12000);
  });

  it("aligns to the actual picture with one bounded transform command", async () => {
    const picture = {
      left: 0,
      right: 200,
      top: 0,
      bottom: 100,
      width: 200,
      height: 100,
    };
    const layer = {
      left: 75,
      right: 125,
      top: 25,
      bottom: 75,
      width: 50,
      height: 50,
    };
    const transform = IDENTITY_TRANSFORM;
    expect(
      alignedTransform(transform, picture, layer, "left").position_x_bp,
    ).toBe(-3750);
    expect(
      alignedTransform(transform, picture, layer, "center_x").position_x_bp,
    ).toBe(0);
    expect(
      alignedTransform(transform, picture, layer, "right").position_x_bp,
    ).toBe(3750);
    expect(
      alignedTransform(transform, picture, layer, "top").position_y_bp,
    ).toBe(-2500);
    expect(
      alignedTransform(transform, picture, layer, "center_y").position_y_bp,
    ).toBe(0);
    expect(
      alignedTransform(transform, picture, layer, "bottom").position_y_bp,
    ).toBe(2500);

    const onIntent = vi.fn(async () => undefined);
    subject("clip-0", onIntent);
    const pictureNode = document.createElement("div");
    pictureNode.className = "h3-nle-picture";
    const canvas = document.createElement("canvas");
    canvas.setAttribute("data-h3-nle-canvas", "composition");
    canvas.getBoundingClientRect = () => picture as DOMRect;
    pictureNode.append(canvas);
    const layerNode = document.createElement("div");
    layerNode.setAttribute("data-h3-nle-transform-overlay", "clip-0");
    layerNode.getBoundingClientRect = () => layer as DOMRect;
    pictureNode.append(layerNode);
    const dialog = document.createElement("div");
    dialog.className = "h3-nle-dialog";
    dialog.append(pictureNode);
    document.body.append(dialog);
    openAnchorAndAlignment();
    try {
      await waitFor(() =>
        expect(
          (
            screen.getByRole("button", {
              name: "Align left",
            }) as HTMLButtonElement
          ).disabled,
        ).toBe(false),
      );
      fireEvent.click(screen.getByRole("button", { name: "Align left" }));
      fireEvent.click(screen.getByRole("button", { name: "Align left" }));
      expect(commandKinds(onIntent)).toEqual(["set_visual_transform"]);
      const command = (
        onIntent.mock.calls as unknown as Array<
          [
            {
              commands: Array<{
                payload: { transform: { position_x_bp: number } };
              }>;
            },
          ]
        >
      )[0]![0].commands[0]!;
      expect(command.payload.transform.position_x_bp).toBe(-3750);
    } finally {
      dialog.remove();
    }
  });

  it("disables alignment with a reason when the selected clip is off-playhead", () => {
    const onIntent = vi.fn(async () => undefined);
    subject("clip-0", onIntent);
    openAnchorAndAlignment();
    const align = screen.getByRole("button", {
      name: "Align left",
    }) as HTMLButtonElement;
    expect(align.disabled).toBe(true);
    expect(align.getAttribute("aria-description")).toMatch(/playhead/i);
    fireEvent.click(align);
    expect(onIntent).not.toHaveBeenCalled();
  });

  it("mounts one linked panel, exposes Text only for title clips, and keeps a read-only empty-selection summary", () => {
    const media = subject("clip-0");
    // clip-0 shows a source with bound audio, so its Audio tab follows Transition.
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "Basic",
      "Crop",
      "Colour",
      "Transition",
      "Audio",
    ]);
    expect(screen.getAllByRole("tabpanel")).toHaveLength(1);
    expect(screen.queryByRole("tab", { name: "Text" })).toBeNull();
    cleanup();

    subject("clip-3");
    const textTab = screen.getByRole("tab", { name: "Text" });
    fireEvent.click(textTab);
    expect(textTab.getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("tabpanel", { name: "Text" }).hidden).toBe(false);
    cleanup();

    subject(null);
    // M25-64 (row #28): with no selection the inspector is Project settings and its hint.
    expect(
      screen.getByText(
        "Select a clip or a track on the timeline to edit its properties.",
      ),
    ).not.toBeNull();
    expect(
      document.querySelector('[data-h3-nle-summary="composition"]'),
    ).not.toBeNull();
    expect(screen.queryByRole("tab")).toBeNull();
  });

  it("shares one numeric draft and commits once on stepper Enter without a blur duplicate", () => {
    const onIntent = vi.fn(async () => undefined);
    subject("clip-0", onIntent);
    // Position has no slider since the control set followed the design; Rotation carries the
    // same contract between a field and its slider.
    const stepper = screen.getByRole("spinbutton", { name: "Rotation (°)" });
    const slider = screen.getByRole("slider", {
      name: "Rotation (°) slider",
    });
    // 5 degrees in the field is 5,000 mdeg on the slider, which keeps the canonical integer.
    fireEvent.change(stepper, { target: { value: "5" } });
    expect((slider as HTMLInputElement).value).toBe("5000");
    expect(onIntent).not.toHaveBeenCalled();
    fireEvent.keyDown(stepper, { key: "Enter" });
    fireEvent.blur(stepper);
    fireEvent.change(stepper, { target: { value: "5" } });
    fireEvent.keyDown(stepper, { key: "Enter" });
    expect(commandKinds(onIntent)).toEqual(["set_visual_transform"]);
  });

  it("coalesces held range keys into one release command and cancellation sends nothing", () => {
    const onIntent = vi.fn(async () => undefined);
    subject("clip-0", onIntent);
    const slider = screen.getByRole("slider", {
      name: "Rotation (°) slider",
    });
    fireEvent.keyDown(slider, { key: "ArrowRight", repeat: true });
    fireEvent.keyDown(slider, { key: "ArrowRight", repeat: true });
    expect(onIntent).not.toHaveBeenCalled();
    fireEvent.keyUp(slider, { key: "ArrowRight" });
    fireEvent.keyUp(slider, { key: "ArrowRight" });
    expect(commandKinds(onIntent)).toEqual(["set_visual_transform"]);

    const other = screen.getByRole("slider", {
      name: "Scale (%) slider",
    });
    fireEvent.keyDown(other, { key: "ArrowRight" });
    fireEvent.keyDown(other, { key: "Escape" });
    expect(commandKinds(onIntent)).toEqual(["set_visual_transform"]);
    expect(
      (
        screen.getByRole("spinbutton", {
          name: "Scale (%)",
        }) as HTMLInputElement
      ).value,
    ).toBe("100");
  });

  it("rejects invalid crop drafts and resets a changed group as one command", () => {
    const onIntent = vi.fn(async () => undefined);
    subject("clip-0", onIntent);
    fireEvent.click(screen.getByRole("tab", { name: "Crop" }));
    fireEvent.change(
      screen.getByRole("spinbutton", { name: "Crop left (%)" }),
      {
        target: { value: "90" },
      },
    );
    fireEvent.change(
      screen.getByRole("spinbutton", { name: "Crop right (%)" }),
      {
        target: { value: "20" },
      },
    );
    expect(screen.getByRole("alert").textContent).toContain(
      "must total less than 10,000",
    );
    expect(
      (
        document.querySelector(
          '[data-h3-nle-control="visual.crop"]',
        ) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    expect(onIntent).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Reset Crop" }));
    expect(commandKinds(onIntent)).toEqual([]);

    cleanup();
    const textIntent = vi.fn(async () => undefined);
    subject("clip-3", textIntent);
    fireEvent.click(screen.getByRole("tab", { name: "Text" }));
    fireEvent.click(screen.getByRole("button", { name: "Reset Text content" }));
    expect(commandKinds(textIntent)).toEqual(["set_text_content"]);
  });

  it("keeps multiline Enter and IME local and applies text only with Ctrl/Cmd+Enter or Apply", () => {
    const onIntent = vi.fn(async () => undefined);
    subject("clip-3", onIntent);
    fireEvent.click(screen.getByRole("tab", { name: "Text" }));
    const panel = screen.getByRole("tabpanel", { name: "Text" });
    const textarea = within(panel).getByRole("textbox", { name: "Text" });
    fireEvent.change(textarea, { target: { value: "Line one\nLine two" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    fireEvent.compositionStart(textarea);
    fireEvent.keyDown(textarea, {
      key: "Enter",
      ctrlKey: true,
      isComposing: true,
    });
    expect(onIntent).not.toHaveBeenCalled();
    fireEvent.compositionEnd(textarea);
    fireEvent.keyDown(textarea, { key: "Enter", ctrlKey: true });
    fireEvent.click(
      panel.querySelector('[data-h3-nle-control="text.content"]')!,
    );
    expect(commandKinds(onIntent)).toEqual(["set_text_content"]);
  });

  it("keeps all seven property command kinds executable on their canonical tabs", () => {
    const cases = [
      {
        clip: "clip-0",
        tab: "Basic",
        label: "Opacity (%)",
        value: "50",
        control: "visual.opacity_blend",
        kind: "set_opacity_blend",
      },
      {
        clip: "clip-0",
        tab: "Crop",
        label: "Crop left (%)",
        value: "5",
        control: "visual.crop",
        kind: "set_crop",
      },
      {
        clip: "clip-0",
        tab: "Colour",
        label: "Brightness (%)",
        value: "10",
        control: "visual.effect",
        kind: "set_effect",
      },
      {
        clip: "clip-3",
        tab: "Text",
        label: "Size (px)",
        value: "64",
        control: "text.style",
        kind: "set_text_style",
      },
    ] as const;
    for (const item of cases) {
      const onIntent = vi.fn(async () => undefined);
      subject(item.clip, onIntent);
      fireEvent.click(screen.getByRole("tab", { name: item.tab }));
      fireEvent.change(screen.getByRole("spinbutton", { name: item.label }), {
        target: { value: item.value },
      });
      fireEvent.click(
        document.querySelector(`[data-h3-nle-control="${item.control}"]`)!,
      );
      expect(commandKinds(onIntent)).toEqual([item.kind]);
      cleanup();
    }

    const base = snapshotFixture(SMOKE_SHAPE, 11);
    const snapshot = {
      ...base,
      clips: base.clips.map((clip) =>
        clip.clipId === "clip-1"
          ? {
              ...clip,
              transition: { kind: "cross_dissolve_v1", durationFrames: 12 },
            }
          : clip,
      ),
    };
    const onIntent = vi.fn(async () => undefined);
    subject("clip-1", onIntent, createSidebarRetention(), snapshot);
    fireEvent.click(screen.getByRole("tab", { name: "Transition" }));
    fireEvent.click(screen.getByRole("button", { name: "Reset Transition" }));
    expect(commandKinds(onIntent)).toEqual(["set_transition"]);
  });
});

// M25-64 (rows #28-#31, R10; A64-3 and A64-4): Project settings with no selection, a clip header
// named like the timeline names the clip, and every bp, per-mille and millidegree field shown and
// entered in % or degrees while the drafts and commands stay canonical integers.
describe("M25-64 inspector", () => {
  function payload(onIntent: ReturnType<typeof vi.fn>, index = 0) {
    return (
      onIntent.mock.calls[index]![0] as {
        commands: readonly {
          payload: Record<string, Record<string, number>>;
        }[];
      }
    ).commands[0]!.payload;
  }

  function visible(root: HTMLElement): string {
    const clone = root.cloneNode(true) as HTMLElement;
    for (const node of clone.querySelectorAll(".h3-nle-vh, input, small"))
      node.remove();
    return clone.textContent ?? "";
  }

  it("shows Project settings from the output with no revision, counts or raw ids", () => {
    subject(null);
    const project = document.querySelector<HTMLElement>(
      '[data-h3-nle-summary="composition"]',
    )!;
    expect(project.querySelector("h4")!.textContent).toBe("Project");
    const rows = [...project.querySelectorAll("[data-h3-nle-setting]")].map(
      (row) => [
        row.getAttribute("data-h3-nle-setting"),
        row.querySelector("dt")!.textContent,
        row.querySelector("dd")!.textContent,
      ],
    );
    expect(rows).toEqual([
      ["resolution", "Resolution", "320 × 180"],
      ["frameRate", "Frame rate", "24 fps"],
      ["duration", "Duration", "00:02:00:00"],
      ["audio", "Audio", "AAC · 48 kHz · mono"],
      ["export", "Export format", "MP4 · H.264"],
    ]);
    expect(
      project
        .querySelector('[data-h3-nle-setting="duration"] dd')!
        .hasAttribute("data-h3-nle-mono"),
    ).toBe(true);
    const text = visible(project);
    expect(text).not.toMatch(/revision|\b11\b|tracks,|clips\b/iu);
    expect(text).not.toMatch(/\b(?:clip|track|asset)-\d+\b|vid-|img-/u);
  });

  it("heads a clip with its timeline name, its range and its length instead of its id", () => {
    subject("clip-0");
    const header = document.querySelector<HTMLElement>(".h3-nle-clip-header")!;
    const clip = snapshotFixture(SMOKE_SHAPE, 11).clips.find(
      (member) => member.clipId === "clip-0",
    )!;
    expect(header.querySelector("h4")!.textContent).toMatch(/^Clip \d{2}$/u);
    const range = header.querySelector(
      "[data-h3-nle-clip-range]",
    )!.textContent!;
    const [span, length] = range.split(" · ");
    expect(span).toMatch(
      /^\d{2}:\d{2}:\d{2}:\d{2} – \d{2}:\d{2}:\d{2}:\d{2}$/u,
    );
    expect(length).toBe(`${(clip.durationFrames / 24).toFixed(2)} s`);
    // Tabs first, then the header, then the panel, as in the canvas.
    const section = document.querySelector<HTMLElement>(
      "[data-h3-nle-selected-clip]",
    )!;
    const order = [...section.children]
      .filter((child) => !child.classList.contains("h3-nle-vh"))
      .map((child) => child.getAttribute("role") ?? child.className);
    expect(order).toEqual(["tablist", "h3-nle-clip-header", "tabpanel"]);
    const text = visible(section);
    expect(text).not.toContain("clip-0");
    expect(text).not.toMatch(/\bbp\b|‰|m°|\[\s*\d+\s*,\s*\d+\s*\)/u);
  });

  it("enters 12.34 % as 1234 bp, 1.234 degrees as 1234 mdeg and 12.3 % of a per-mille field as 123", () => {
    const onIntent = vi.fn(async () => undefined);
    subject("clip-0", onIntent);
    const x = screen.getByRole("spinbutton", { name: "Position X (%)" });
    // The same stored 0 bp, in the design's two-decimal display; the attributes are unpadded.
    expect((x as HTMLInputElement).value).toBe("0.00");
    expect(x.getAttribute("step")).toBe("0.01");
    expect(x.getAttribute("min")).toBe("-400");
    expect(x.getAttribute("max")).toBe("400");
    fireEvent.change(x, { target: { value: "12.34" } });
    fireEvent.keyDown(x, { key: "Enter" });
    expect(payload(onIntent).transform!.position_x_bp).toBe(1_234);
    cleanup();

    const rotation = vi.fn(async () => undefined);
    subject("clip-0", rotation);
    const field = screen.getByRole("spinbutton", { name: "Rotation (°)" });
    fireEvent.change(field, { target: { value: "1.234" } });
    // The slider announces the display value (Position, which has no slider now, carried this).
    expect(
      screen
        .getByRole("slider", { name: "Rotation (°) slider" })
        .getAttribute("aria-valuetext"),
    ).toBe("1.234 °");
    fireEvent.keyDown(field, { key: "Enter" });
    expect(payload(rotation).transform!.rotation_mdeg).toBe(1_234);
    cleanup();

    const effect = vi.fn(async () => undefined);
    subject("clip-0", effect);
    fireEvent.click(screen.getByRole("tab", { name: "Colour" }));
    fireEvent.change(
      screen.getByRole("spinbutton", { name: "Brightness (%)" }),
      { target: { value: "12.3" } },
    );
    fireEvent.click(
      document.querySelector('[data-h3-nle-control="visual.effect"]')!,
    );
    expect(payload(effect).effect!.brightness_permille).toBe(123);
  });

  it("rejects over-precise, out-of-range and malformed text with the validation element", () => {
    const onIntent = vi.fn(async () => undefined);
    subject("clip-0", onIntent);
    const x = screen.getByRole("spinbutton", { name: "Position X (%)" });
    const apply = document.querySelector<HTMLButtonElement>(
      '[data-h3-nle-control="visual.transform"]',
    )!;
    for (const raw of ["12.345", "400.01", "-400.01", "1e2", ""]) {
      fireEvent.change(x, { target: { value: raw } });
      expect(x.getAttribute("aria-invalid"), raw).toBe("true");
      const error = document.getElementById(
        x.getAttribute("aria-describedby")!,
      )!;
      expect(error.className, raw).toBe("h3-nle-validation");
      expect(error.textContent, raw).toBe(
        "Enter a value from -400 to 400 % with at most 2 decimal places.",
      );
      fireEvent.keyDown(x, { key: "Enter" });
      expect(apply.disabled, raw).toBe(true);
    }
    expect(onIntent).not.toHaveBeenCalled();
    // The limit itself is admitted.
    fireEvent.change(x, { target: { value: "-400" } });
    expect(x.getAttribute("aria-invalid")).toBe("false");
    fireEvent.keyDown(x, { key: "Enter" });
    expect(payload(onIntent).transform!.position_x_bp).toBe(-40_000);
  });

  it("keeps untouched values, keys, cancel and linked scale on the canonical integer", () => {
    const onIntent = vi.fn(async () => undefined);
    subject("clip-0", onIntent);
    const opacity = screen.getByRole("spinbutton", { name: "Opacity (%)" });
    // 10,000 bp prints as 100 and commits nothing when untouched.
    expect((opacity as HTMLInputElement).value).toBe("100");
    fireEvent.keyDown(opacity, { key: "Enter" });
    expect(onIntent).not.toHaveBeenCalled();
    // Keys step one canonical unit (0.01 %), and Escape returns to the accepted value.
    fireEvent.keyDown(opacity, { key: "ArrowDown" });
    expect((opacity as HTMLInputElement).value).toBe("99.99");
    fireEvent.keyDown(opacity, { key: "PageDown" });
    expect((opacity as HTMLInputElement).value).toBe("99.89");
    fireEvent.keyDown(opacity, { key: "Escape" });
    expect((opacity as HTMLInputElement).value).toBe("100");
    fireEvent.keyDown(opacity, { key: "Home" });
    expect((opacity as HTMLInputElement).value).toBe("0");
    fireEvent.keyDown(opacity, { key: "Enter" });
    expect(commandKinds(onIntent)).toEqual(["set_opacity_blend"]);
    // The slider stays on the integer and announces the display value.
    const slider = screen.getByRole("slider", { name: "Scale (%) slider" });
    expect((slider as HTMLInputElement).value).toBe("10000");
    expect(slider.getAttribute("aria-valuetext")).toBe("100 %");
    // Uniform scale moves both axes in display units: with the switch off, each shows the value.
    fireEvent.change(screen.getByRole("spinbutton", { name: "Scale (%)" }), {
      target: { value: "50.5" },
    });
    fireEvent.click(screen.getByRole("switch", { name: "Uniform scale" }));
    for (const name of ["Scale X (%)", "Scale Y (%)"])
      expect(
        (screen.getByRole("spinbutton", { name }) as HTMLInputElement).value,
      ).toBe("50.5");
    // Text that already denotes the draft stays as typed: "12.3" is not rewritten to "12.30"
    // under the caret once the draft reaches the field.
    const y = screen.getByRole("spinbutton", { name: "Position Y (%)" });
    fireEvent.change(y, { target: { value: "12.3" } });
    expect((y as HTMLInputElement).value).toBe("12.3");
    fireEvent.keyDown(y, { key: "Enter" });
    expect(payload(onIntent, 1).transform!.position_y_bp).toBe(1_230);
  });

  it.each([
    ["zh-TW", "位置 X（%）", "旋轉（°）"],
    ["zh-CN", "位置 X（%）", "旋转（°）"],
  ] as const)("names unit fields with their symbol in %s", (locale, x, r) => {
    render(
      <NleInspectorTabs
        locale={locale}
        snapshot={snapshotFixture(SMOKE_SHAPE, 11)}
        selection={["clip-0"]}
        authoring={authoringReady(SMOKE_SHAPE, {
          selection: ["clip-0"],
          revision: 11,
        })}
        onIntent={vi.fn(async () => undefined)}
        retention={createSidebarRetention()}
      />,
    );
    expect(screen.getByRole("spinbutton", { name: x })).not.toBeNull();
    expect(screen.getByRole("spinbutton", { name: r })).not.toBeNull();
    for (const label of document.querySelectorAll(
      "[data-h3-nle-property] > label",
    ))
      expect(label.textContent).not.toMatch(/bp|‰|m°|%|°/u);
  });
});

// The Basic tab's control set, value format and sliders follow the approved design: one Scale row
// under a Uniform scale switch, Position as two fields on one row, Rotation, then Blend; Anchor
// and alignment in a closed secondary group; a section's commit button only while its draft is
// uncommitted. Canonical integers, commands and commit boundaries are unchanged.
describe("inspector Basic tab follows the design's control set", () => {
  type Transform = Record<
    | "scale_x_bp"
    | "scale_y_bp"
    | "position_x_bp"
    | "position_y_bp"
    | "rotation_mdeg"
    | "anchor_x_bp"
    | "anchor_y_bp",
    number
  >;

  function transformOf(
    onIntent: ReturnType<typeof vi.fn>,
    index = 0,
  ): Transform {
    return (
      onIntent.mock.calls[index]![0] as {
        commands: readonly { payload: { transform: Transform } }[];
      }
    ).commands[0]!.payload.transform;
  }

  function field(name: string): HTMLInputElement {
    return screen.getByRole("spinbutton", { name }) as HTMLInputElement;
  }

  function commitButton(control: string): HTMLButtonElement {
    return document.querySelector<HTMLButtonElement>(
      `[data-h3-nle-control="${control}"]`,
    )!;
  }

  /** What a section offers, top to bottom; anything inside a closed group is left out. */
  function offered(section: HTMLElement): string[] {
    const out: string[] = [];
    for (const node of section.querySelectorAll<HTMLElement>(
      "[data-h3-nle-property-pair], [data-h3-nle-property], [role='switch'], [data-h3-nle-disclosure], select, [data-h3-nle-control]",
    )) {
      if (node.closest("[hidden]") !== null) continue;
      if (node.matches("[data-h3-nle-property-pair]"))
        out.push(`pair:${node.dataset.h3NlePropertyPair}`);
      else if (node.matches("[role='switch']"))
        out.push(`switch:${node.getAttribute("aria-label")}`);
      else if (node.matches("[data-h3-nle-disclosure]"))
        out.push(`more:${node.textContent}`);
      else if (node.matches("select"))
        out.push(
          `select:${node.closest("label")!.querySelector("span")!.textContent}`,
        );
      else if (node.matches("[data-h3-nle-property]"))
        out.push(
          `${node.closest("[data-h3-nle-property-pair]") === null ? "row" : "field"}:${node.dataset.h3NleProperty}`,
        );
      else out.push(`control:${node.dataset.h3NleControl}`);
    }
    return out;
  }

  function withScales(x: number, y: number) {
    const base = snapshotFixture(SMOKE_SHAPE, 11);
    return {
      ...base,
      clips: base.clips.map((clip) =>
        clip.clipId === "clip-0"
          ? {
              ...clip,
              transform: {
                ...IDENTITY_TRANSFORM,
                scale_x_bp: x,
                scale_y_bp: y,
              },
            }
          : clip,
      ),
    };
  }

  it.each([
    [
      "en",
      "Transform",
      [
        "row:Scale",
        "pair:Position",
        "field:Position X",
        "field:Position Y",
        "row:Rotation",
        "switch:Uniform scale",
        "more:Anchor and alignment",
      ],
      "Blend",
      ["row:Opacity", "select:Blend"],
    ],
    [
      "zh-TW",
      "變形",
      [
        "row:縮放",
        "pair:位置",
        "field:位置 X",
        "field:位置 Y",
        "row:旋轉",
        "switch:等比縮放",
        "more:錨點與對齊",
      ],
      "混合",
      ["row:不透明度", "select:混合"],
    ],
    [
      "zh-CN",
      "变换",
      [
        "row:缩放",
        "pair:位置",
        "field:位置 X",
        "field:位置 Y",
        "row:旋转",
        "switch:等比缩放",
        "more:锚点与对齐",
      ],
      "混合",
      ["row:不透明度", "select:混合"],
    ],
  ] as const)(
    "offers the design's rows in the design's order in %s",
    (locale, transformName, transformRows, blendName, blendRows) => {
      render(
        <NleInspectorTabs
          locale={locale}
          snapshot={snapshotFixture(SMOKE_SHAPE, 11)}
          selection={["clip-0"]}
          authoring={authoringReady(SMOKE_SHAPE, {
            selection: ["clip-0"],
            revision: 11,
          })}
          onIntent={vi.fn(async () => undefined)}
          retention={createSidebarRetention()}
        />,
      );
      const transform = screen.getByRole("region", { name: transformName });
      expect(offered(transform)).toEqual(transformRows);
      expect(offered(screen.getByRole("region", { name: blendName }))).toEqual(
        blendRows,
      );
      // Position is two fields and no slider; the three sliders are Scale, Rotation and Opacity.
      expect(
        transform.querySelector(
          '[data-h3-nle-property-pair] input[type="range"]',
        ),
      ).toBeNull();
      expect(screen.getAllByRole("slider")).toHaveLength(3);
    },
  );

  it("shows each value in the design's format with its unit inside the field", () => {
    subject("clip-0");
    expect(field("Scale (%)").value).toBe("100");
    expect(field("Position X (%)").value).toBe("0.00");
    expect(field("Position Y (%)").value).toBe("0.00");
    expect(field("Rotation (°)").value).toBe("0.0");
    expect(field("Opacity (%)").value).toBe("100");
    for (const [name, unit, prefix] of [
      ["Scale (%)", "%", null],
      ["Position X (%)", "%", "X"],
      ["Position Y (%)", "%", "Y"],
      ["Rotation (°)", "°", null],
      ["Opacity (%)", "%", null],
    ] as const) {
      const well = field(name).parentElement!;
      expect(well.className, name).toContain("h3-nle-value");
      expect(well.querySelector(".h3-nle-unit")!.textContent, name).toBe(unit);
      expect(
        well.querySelector(".h3-nle-prefix")?.textContent ?? null,
        name,
      ).toBe(prefix);
    }
  });

  it("gives each field its own text when a tab replaces another's rows", () => {
    subject("clip-0");
    // Rotation ("0.0") and Crop right sit at the same place in their sections; a field must not
    // inherit the text of the one it replaces.
    fireEvent.click(screen.getByRole("tab", { name: "Crop" }));
    for (const name of [
      "Crop left (%)",
      "Crop top (%)",
      "Crop right (%)",
      "Crop bottom (%)",
    ])
      expect(field(name).value, name).toBe("0");
    fireEvent.click(screen.getByRole("tab", { name: "Basic" }));
    expect(field("Rotation (°)").value).toBe("0.0");
    expect(field("Position X (%)").value).toBe("0.00");
  });

  it("puts a pair's validation message after both of its fields", () => {
    subject("clip-0");
    const x = field("Position X (%)");
    fireEvent.change(x, { target: { value: "400.5" } });
    const pair = x.closest(".h3-nle-pair-fields")!;
    expect(
      [...pair.children].map((child) =>
        child.matches("small")
          ? "message"
          : child.getAttribute("data-h3-nle-property"),
      ),
    ).toEqual(["Position X", "Position Y", "message"]);
  });

  it("never rounds a value, and rewrites typed text in the display format only on blur", () => {
    const onIntent = vi.fn(async () => undefined);
    subject("clip-0", onIntent);
    const x = field("Position X (%)");
    fireEvent.change(x, { target: { value: "5" } });
    expect(x.value).toBe("5");
    fireEvent.blur(x);
    expect(x.value).toBe("5.00");
    fireEvent.change(x, { target: { value: "12.34" } });
    fireEvent.blur(x);
    expect(x.value).toBe("12.34");
    const rotation = field("Rotation (°)");
    fireEvent.change(rotation, { target: { value: "1.234" } });
    fireEvent.blur(rotation);
    expect(rotation.value).toBe("1.234");
    fireEvent.change(rotation, { target: { value: "90" } });
    fireEvent.blur(rotation);
    expect(rotation.value).toBe("90.0");
    // Text that denotes no value is left for the user to correct, with its message.
    fireEvent.change(x, { target: { value: "12.345" } });
    fireEvent.blur(x);
    expect(x.value).toBe("12.345");
    expect(x.getAttribute("aria-invalid")).toBe("true");
    // Blur is not a commit boundary.
    expect(onIntent).not.toHaveBeenCalled();
    fireEvent.change(x, { target: { value: "12.34" } });
    fireEvent.keyDown(x, { key: "Enter" });
    expect(transformOf(onIntent).position_x_bp).toBe(1_234);
    expect(transformOf(onIntent).rotation_mdeg).toBe(90_000);
  });

  it("turns uniform scale off into two independent fields without sending anything", () => {
    const onIntent = vi.fn(async () => undefined);
    subject("clip-0", onIntent);
    const toggle = () => screen.getByRole("switch", { name: "Uniform scale" });
    expect(toggle().getAttribute("data-h3-nle-control")).toBe(
      "transform.link_scale",
    );
    fireEvent.click(toggle());
    expect(toggle().getAttribute("aria-checked")).toBe("false");
    expect(screen.queryByRole("spinbutton", { name: "Scale (%)" })).toBeNull();
    fireEvent.change(field("Scale X (%)"), { target: { value: "150" } });
    expect(field("Scale Y (%)").value).toBe("100");
    expect(onIntent).not.toHaveBeenCalled();
    // Turning it back on makes the scales uniform: Y takes X, as one command.
    fireEvent.click(toggle());
    expect(commandKinds(onIntent)).toEqual(["set_visual_transform"]);
    expect(transformOf(onIntent).scale_x_bp).toBe(15_000);
    expect(transformOf(onIntent).scale_y_bp).toBe(15_000);
    expect(toggle().getAttribute("aria-checked")).toBe("true");
    expect(field("Scale (%)").value).toBe("150");
    expect(
      screen.queryByRole("spinbutton", { name: "Scale X (%)" }),
    ).toBeNull();
  });

  it("sends nothing when uniform scale is switched off and on over equal scales", () => {
    const onIntent = vi.fn(async () => undefined);
    subject("clip-0", onIntent);
    const toggle = () => screen.getByRole("switch", { name: "Uniform scale" });
    fireEvent.click(toggle());
    fireEvent.click(toggle());
    expect(toggle().getAttribute("aria-checked")).toBe("true");
    expect(onIntent).not.toHaveBeenCalled();
  });

  it("opens a clip whose accepted scales differ with the switch off and both fields", () => {
    const onIntent = vi.fn(async () => undefined);
    subject(
      "clip-0",
      onIntent,
      createSidebarRetention(),
      withScales(12_000, 8_000),
    );
    const toggle = screen.getByRole("switch", { name: "Uniform scale" });
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    expect(field("Scale X (%)").value).toBe("120");
    expect(field("Scale Y (%)").value).toBe("80");
    expect(screen.queryByRole("spinbutton", { name: "Scale (%)" })).toBeNull();
    fireEvent.click(toggle);
    expect(commandKinds(onIntent)).toEqual(["set_visual_transform"]);
    expect(transformOf(onIntent).scale_x_bp).toBe(12_000);
    expect(transformOf(onIntent).scale_y_bp).toBe(12_000);
  });

  it("states each slider's filled part from its origin to its value", () => {
    expect(rangeFill(10_000, 0, 10_000)).toEqual({ from: 0, to: 1 });
    expect(rangeFill(0, -180_000, 180_000)).toEqual({ from: 0.5, to: 0.5 });
    expect(rangeFill(-90_000, -180_000, 180_000)).toEqual({
      from: 0.25,
      to: 0.5,
    });
    expect(rangeFill(90_000, -180_000, 180_000)).toEqual({
      from: 0.5,
      to: 0.75,
    });
    // A draft beyond the slider's span fills to its end; an empty span fills nothing.
    expect(rangeFill(30_000, 0, 20_000)).toEqual({ from: 0, to: 1 });
    expect(rangeFill(5, 5, 5)).toEqual({ from: 0, to: 0 });

    subject("clip-0");
    const ends = (name: string) => {
      const slider = screen.getByRole("slider", { name }) as HTMLInputElement;
      return [
        Number(slider.style.getPropertyValue("--h3-range-from")),
        Number(slider.style.getPropertyValue("--h3-range-to")),
      ];
    };
    // Scale's slider spans 0.01 % to 200 %, so 100 % sits at its middle, as the design draws it.
    const scale = screen.getByRole("slider", {
      name: "Scale (%) slider",
    }) as HTMLInputElement;
    expect([scale.min, scale.max]).toEqual(["1", "20000"]);
    expect(ends("Scale (%) slider")[0]).toBe(0);
    expect(ends("Scale (%) slider")[1]).toBeCloseTo(0.5, 3);
    expect(ends("Rotation (°) slider")).toEqual([0.5, 0.5]);
    expect(ends("Opacity (%) slider")).toEqual([0, 1]);
  });

  it("widens the scale slider to an accepted scale beyond 200 % and keeps the field's range", () => {
    subject(
      "clip-0",
      undefined,
      createSidebarRetention(),
      withScales(30_000, 30_000),
    );
    const scale = screen.getByRole("slider", {
      name: "Scale (%) slider",
    }) as HTMLInputElement;
    expect(scale.max).toBe("30000");
    expect(scale.value).toBe("30000");
    expect(field("Scale (%)").getAttribute("max")).toBe("800");
  });

  it("offers a section's commit button only while its draft is uncommitted", () => {
    subject("clip-0");
    expect(commitButton("visual.transform").hidden).toBe(true);
    expect(commitButton("visual.opacity_blend").hidden).toBe(true);
    const x = field("Position X (%)");
    fireEvent.change(x, { target: { value: "5" } });
    expect(commitButton("visual.transform").hidden).toBe(false);
    expect(commitButton("visual.transform").disabled).toBe(false);
    expect(commitButton("visual.opacity_blend").hidden).toBe(true);
    fireEvent.keyDown(x, { key: "Escape" });
    expect(commitButton("visual.transform").hidden).toBe(true);
    fireEvent.change(screen.getByRole("combobox", { name: "Blend" }), {
      target: { value: "multiply" },
    });
    expect(commitButton("visual.opacity_blend").hidden).toBe(false);

    for (const [tab, name, value, control] of [
      ["Crop", "Crop left (%)", "5", "visual.crop"],
      ["Colour", "Brightness (%)", "10", "visual.effect"],
      ["Transition", "Transition frames", "12", "boundary.transition"],
    ] as const) {
      fireEvent.click(screen.getByRole("tab", { name: tab }));
      expect(commitButton(control).hidden, control).toBe(true);
      fireEvent.change(field(name), { target: { value } });
      if (control === "boundary.transition")
        fireEvent.change(screen.getByRole("combobox", { name: "Transition" }), {
          target: { value: "cross_dissolve_v1" },
        });
      expect(commitButton(control).hidden, control).toBe(false);
    }
    cleanup();

    subject("clip-3");
    fireEvent.click(screen.getByRole("tab", { name: "Text" }));
    expect(commitButton("text.content").hidden).toBe(true);
    expect(commitButton("text.style").hidden).toBe(true);
    fireEvent.change(field("Size (px)"), { target: { value: "64" } });
    expect(commitButton("text.style").hidden).toBe(false);
    expect(commitButton("text.content").hidden).toBe(true);
  });

  it("leaves focus in the edited field when its section's commit button is pressed", () => {
    const onIntent = vi.fn(async () => undefined);
    subject("clip-0", onIntent);
    const x = field("Position X (%)");
    x.focus();
    fireEvent.change(x, { target: { value: "5" } });
    // A press that took focus would blur the field first, and the blur's rewrite of "5" to
    // "5.00" is a render of its own ahead of the command's: one more than the discrete-edit
    // budget the journeys count.
    expect(fireEvent.mouseDown(commitButton("visual.transform"))).toBe(false);
    expect(document.activeElement).toBe(x);
    fireEvent.click(commitButton("visual.transform"));
    expect(commandKinds(onIntent)).toEqual(["set_visual_transform"]);
    expect(transformOf(onIntent).position_x_bp).toBe(500);
    expect(x.value).toBe("5");
  });

  it("keeps Anchor in the closed secondary group, and opens it on a retained anchor draft", () => {
    const onIntent = vi.fn(async () => undefined);
    const retention = createSidebarRetention();
    subject("clip-0", onIntent, retention);
    const more = () =>
      screen.getByRole("button", { name: "Anchor and alignment" });
    expect(more().getAttribute("aria-expanded")).toBe("false");
    expect(
      screen.queryByRole("spinbutton", { name: "Anchor X (%)" }),
    ).toBeNull();
    fireEvent.click(more());
    expect(more().getAttribute("aria-expanded")).toBe("true");
    expect(field("Anchor X (%)").value).toBe("50");
    fireEvent.change(field("Anchor X (%)"), { target: { value: "25" } });
    cleanup();

    // The uncommitted anchor draft comes back, so its group opens with it.
    subject("clip-0", onIntent, retention);
    expect(more().getAttribute("aria-expanded")).toBe("true");
    expect(field("Anchor X (%)").value).toBe("25");
    fireEvent.keyDown(field("Anchor X (%)"), { key: "Enter" });
    expect(commandKinds(onIntent)).toEqual(["set_visual_transform"]);
    expect(transformOf(onIntent).anchor_x_bp).toBe(2_500);
  });
});
