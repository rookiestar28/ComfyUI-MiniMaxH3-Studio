// The storyboard script controls in Production's planning section: long content is written as a
// script, split into the reviewed shot rows, and only those rows reach the existing admission.

import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NlePlanningSection } from "../src/components/nle/NlePlanningSection";
import { nleCopy } from "../src/components/nle/nleCopy";
import { plainReason } from "../src/components/plainReasons";
import type { Locale } from "../src/i18n/catalog";
import {
  initialNlePlanningState,
  initialNleReadinessState,
  type NlePlanningState,
} from "../src/state/nleWorkspaceState";
import { fp, planningProjection } from "./support/nleSequenceFixture";
import { spyActions } from "./support/nleWorkspaceBinding";

afterEach(cleanup);

const CONTEXT_PROMPT =
  "integrated_multimodal_description: [Shot 1] A sphere enters a room. " +
  "[Shot 2] At 00:05.000, the sphere stops. " +
  "[Shot 3] At 00:10.000, the sphere leaves.\n\n" +
  "overall_soundscape: N/A\n\nnon_diegetic_music: N/A";

function subject(
  options: {
    locale?: Locale;
    planning?: Partial<NlePlanningState>;
    contextPromptText?: string;
  } = {},
) {
  const actions = spyActions();
  const view = render(
    <NlePlanningSection
      locale={options.locale ?? "en"}
      planning={{
        ...initialNlePlanningState,
        status: "prepared",
        projection: planningProjection() as never,
        boundWorkspaceFingerprint: fp("a"),
        storyboardReviewOpen: true,
        ...options.planning,
      }}
      readiness={initialNleReadinessState}
      workspaceFingerprint={fp("a")}
      enabled
      actions={actions}
      contextPromptText={options.contextPromptText}
    />,
  );
  const control = <T extends HTMLElement>(id: string) =>
    view.container.querySelector<T>(`[data-h3-nle-control="${id}"]`)!;
  const box = () => control<HTMLTextAreaElement>("planning.script_text");
  const status = () =>
    view.container.querySelector<HTMLElement>(
      '[data-h3-nle-status="planning-script"]',
    );
  const type = (value: string) =>
    fireEvent.change(box(), { target: { value } });
  return { actions, view, control, box, status, type };
}

describe("storyboard script controls", () => {
  it("lives in the review region and offers nothing to split while the box is blank", () => {
    const closed = subject({ planning: { storyboardReviewOpen: false } });
    expect(closed.box()).toBeNull();
    cleanup();
    const { box, control, status } = subject();
    expect(
      box().closest('[data-h3-nle-region="storyboard-review"]'),
    ).not.toBeNull();
    expect(control<HTMLButtonElement>("planning.script_split").disabled).toBe(
      true,
    );
    expect(
      control<HTMLButtonElement>("planning.script_load_context").disabled,
    ).toBe(true);
    expect(status()).toBeNull();
  });

  it("splits a timed script into rows that cover the target and admits nothing by itself", () => {
    const { actions, control, status, type } = subject({
      planning: { targetSeconds: 60 },
    });
    type(
      [
        "[Shot 1] The courier leaves the depot.",
        "[Shot 2] At 00:08.000, the courier crosses the bridge.",
        "[Shot 3] At 00:20.000, rain starts over the market.",
        "[Shot 4] At 00:33.000, the courier shelters under an awning.",
        "[Shot 5] At 00:47.000, the parcel is delivered.",
      ].join("\n"),
    );
    fireEvent.click(control("planning.script_split"));
    expect(actions.setStoryboardRows).toHaveBeenCalledTimes(1);
    const rows = vi.mocked(actions.setStoryboardRows).mock.calls[0]![0];
    expect(
      rows.map((row) => [row.startMilliseconds, row.endMilliseconds]),
    ).toEqual([
      [0, 8_000],
      [8_000, 20_000],
      [20_000, 33_000],
      [33_000, 47_000],
      [47_000, 60_000],
    ]);
    expect(rows.map((row) => row.shotId)).toEqual([
      "shot-1",
      "shot-2",
      "shot-3",
      "shot-4",
      "shot-5",
    ]);
    expect(status()!.dataset.code).toBe("script_split_timed");
    expect(status()!.textContent).toBe(
      "5 shots, cut at the times in the script.",
    );
    expect(actions.admitStoryboard).not.toHaveBeenCalled();
    expect(actions.propose).not.toHaveBeenCalled();
  });

  it("spaces untimed shots evenly across the current target", () => {
    const { actions, control, status, type } = subject({
      planning: { targetSeconds: 30 },
    });
    type("First beat.\n\nSecond beat.\n\nThird beat.");
    fireEvent.click(control("planning.script_split"));
    const rows = vi.mocked(actions.setStoryboardRows).mock.calls[0]![0];
    expect(rows.map((row) => row.endMilliseconds)).toEqual([
      10_000, 20_000, 30_000,
    ]);
    expect(status()!.dataset.code).toBe("script_split_even");
    expect(status()!.textContent).toBe("3 shots, spaced evenly across 30 s.");
  });

  it("names why a script could not be read and leaves the rows untouched", () => {
    const { actions, control, status, type } = subject({
      planning: { targetSeconds: 30 },
    });
    type("[Shot 1] A. [Shot 2] At 00:10.000, B. [Shot 3] C.");
    fireEvent.click(control("planning.script_split"));
    expect(actions.setStoryboardRows).not.toHaveBeenCalled();
    expect(status()!.dataset.code).toBe("script_mixed_timing");
    expect(status()!.classList.contains("h3-nle-danger")).toBe(true);
    expect(status()!.textContent).toBe(
      nleCopy("en").sequence.scriptErrors.script_mixed_timing,
    );
    // Editing the script retires the sentence about the previous attempt.
    type("[Shot 1] A. [Shot 2] B.");
    expect(status()).toBeNull();
  });

  it("loads the Context's shots without the clip's own cut times", () => {
    const { actions, box, control, status } = subject({
      planning: { targetSeconds: 60 },
      contextPromptText: CONTEXT_PROMPT,
    });
    fireEvent.click(control("planning.script_load_context"));
    expect(box().value).toBe(
      "[Shot 1] A sphere enters a room.\n[Shot 2] the sphere stops.\n[Shot 3] the sphere leaves.",
    );
    expect(actions.setStoryboardRows).not.toHaveBeenCalled();
    expect(status()).toBeNull();
    fireEvent.click(control("planning.script_split"));
    const rows = vi.mocked(actions.setStoryboardRows).mock.calls[0]![0];
    expect(rows.map((row) => row.endMilliseconds)).toEqual([
      20_000, 40_000, 60_000,
    ]);
  });

  it("loads a single-shot Context as one line", () => {
    const { box, control, status } = subject({
      contextPromptText:
        "integrated_multimodal_description: [Shot 1] A sphere crosses a room.\n\noverall_soundscape: N/A",
    });
    fireEvent.click(control("planning.script_load_context"));
    expect(box().value).toBe("[Shot 1] A sphere crosses a room.");
    expect(status()).toBeNull();
  });

  it("says so when the Context prompt has no shot list and keeps what was typed", () => {
    const { box, control, status, type } = subject({
      contextPromptText: "No storyboard here.",
    });
    type("My own first beat.");
    fireEvent.click(control("planning.script_load_context"));
    expect(box().value).toBe("My own first beat.");
    expect(status()!.dataset.code).toBe("script_context_empty");
    expect(status()!.textContent).toBe(
      nleCopy("en").sequence.scriptErrors.script_context_empty,
    );
  });

  it("holds the script controls while a planning step is in flight", () => {
    const { box, control } = subject({
      planning: { status: "admitting" },
      contextPromptText: CONTEXT_PROMPT,
    });
    expect(box().disabled).toBe(true);
    expect(
      control<HTMLButtonElement>("planning.script_load_context").disabled,
    ).toBe(true);
  });

  it.each(["en", "zh-TW", "zh-CN"] as const)(
    "names the controls and every outcome in %s without machine tokens",
    (locale) => {
      const text = nleCopy(locale).sequence;
      const { box, control, view } = subject({ locale });
      expect(box().closest("label")!.textContent).toBe(text.scriptLabel);
      for (const [id, label, description] of [
        [
          "planning.script_load_context",
          text.scriptLoadContext,
          text.describe.scriptLoadContext,
        ],
        ["planning.script_split", text.scriptSplit, text.describe.scriptSplit],
      ] as const) {
        const button = control<HTMLButtonElement>(id);
        expect(button.getAttribute("aria-label")).toBe(label);
        expect(button.textContent?.trim()).toBe("");
        expect(button.classList.contains("h3-icon-button")).toBe(true);
        expect(
          button.closest("[role='group']")?.getAttribute("aria-label"),
        ).toBe(text.scriptActions);
        expect(description.length).toBeGreaterThan(0);
      }
      expect(view.container.textContent).toContain(text.scriptHint);
      const sentences = [
        ...Object.values(text.scriptErrors),
        text.scriptSplitTimed,
        text.scriptSplitEven,
        plainReason(locale, "planning", "planning_source_unsupported"),
        plainReason(locale, "planning", "planning_storyboard_unavailable"),
      ];
      for (const sentence of sentences) {
        expect(sentence.length).toBeGreaterThan(0);
        expect(sentence).not.toMatch(/[a-z]+_[a-z_]+/u);
      }
      // Each guidance code has its own sentence, not the table's generic fallback.
      expect(new Set(sentences).size).toBe(sentences.length);
      expect(
        plainReason(locale, "planning", "planning_storyboard_unavailable"),
      ).not.toBe(plainReason(locale, "planning", "planning_refused_422"));
    },
  );

  it("shows the guidance for a generated storyboard that cannot set the cuts", () => {
    const { view } = subject({
      planning: {
        status: "error",
        error: "planning_storyboard_unavailable",
      },
    });
    const error = view.container.querySelector<HTMLElement>(
      '[data-h3-nle-status="planning-error"]',
    )!;
    expect(error.dataset.code).toBe("planning_storyboard_unavailable");
    expect(error.textContent).toBe(
      plainReason("en", "planning", "planning_storyboard_unavailable"),
    );
  });
});
