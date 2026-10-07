// M25-41 whole-video sequence planning toolbar: every planning action is an icon-only button
// whose accessible name is the full label, whose concrete description appears in one tooltip on
// hover (also over an unavailable action) and on keyboard focus, and whose gating and dispatch are
// unchanged. The crowded text-button layout this replaces fails every case below.

import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { Profiler } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  NleIconButton,
  NleIconGroup,
} from "../src/components/nle/NleIconActions";
import { nleCopy } from "../src/components/nle/nleCopy";
import type { Locale } from "../src/i18n/catalog";
import {
  initialNlePlanningState,
  initialNleReadinessState,
  initialNleSequenceState,
} from "../src/state/nleWorkspaceState";
import {
  fp,
  planningProjection,
  productionProjection,
} from "./support/nleSequenceFixture";
import { bindingFixture, expandedState } from "./support/nleWorkspaceBinding";
import { ProductionAndEditorSequence } from "./support/nleSequenceComposition";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const ACTIONS = [
  ["planning.prepare_context", "prepare", "planningActions"],
  ["planning.admit_canonical", "admitCanonical", "planningActions"],
  ["planning.review_storyboard", "reviewStoryboard", "planningActions"],
  ["planning.propose", "propose", "planningActions"],
  ["planning.approve_import", "approveImport", "planningActions"],
  ["readiness.request", "readiness", "readinessActions"],
] as const;

function subject(locale: Locale = "en", prepared = false, review = false) {
  const binding = bindingFixture({
    locale,
    production: { status: "ready", projection: productionProjection() },
    contextAvailable: true,
    state: expandedState({
      planning: {
        ...initialNlePlanningState,
        ...(prepared
          ? {
              status: "prepared",
              projection: planningProjection() as never,
              boundWorkspaceFingerprint: fp("a"),
            }
          : {}),
        storyboardReviewOpen: review,
      },
      readiness: initialNleReadinessState,
      sequence: initialNleSequenceState,
    }),
  });
  const view = render(<ProductionAndEditorSequence binding={binding} />);
  const control = (id: string) =>
    view.container.querySelector<HTMLButtonElement>(
      `[data-h3-nle-control="${id}"]`,
    )!;
  const visibleTips = () =>
    view.container.querySelectorAll("[role='tooltip']:not([hidden])");
  return { binding, view, control, visibleTips };
}

describe("M25-41 planning icon toolbar", () => {
  it.each(["en", "zh-TW", "zh-CN"] as const)(
    "renders icon-only buttons named by their labels in %s",
    (locale) => {
      const text = nleCopy(locale).sequence;
      const { control, visibleTips } = subject(locale);
      for (const [id, key, group] of ACTIONS) {
        const button = control(id);
        expect(button.textContent?.trim(), id).toBe("");
        expect(
          button.querySelector("svg[aria-hidden='true']"),
          id,
        ).not.toBeNull();
        expect(button.getAttribute("aria-label"), id).toBe(text[key]);
        expect(button.classList.contains("h3-icon-button"), id).toBe(true);
        const owner = button.closest("[role='group']");
        expect(owner?.getAttribute("aria-label"), id).toBe(text[group]);
        expect(text.describe[key].length, id).toBeGreaterThan(0);
      }
      const hues = ACTIONS.slice(0, 5).map(([id]) => control(id).dataset.hue);
      expect(new Set(hues).size).toBe(5);
      expect(visibleTips()).toHaveLength(0);
    },
  );

  it("describes an action on keyboard focus and hides it on Escape and blur", () => {
    const text = nleCopy("en").sequence;
    const { control, view, visibleTips } = subject("en", true);
    const button = control("planning.admit_canonical");
    act(() => button.focus());
    const tip = view.getByRole("tooltip");
    expect(tip.textContent).toBe(text.describe.admitCanonical);
    expect(button.getAttribute("aria-describedby")).toBe(tip.id);
    fireEvent.keyDown(button, { key: "Escape" });
    expect(visibleTips()).toHaveLength(0);
    expect(button.getAttribute("aria-describedby")).toBeNull();
    act(() => button.blur());
    act(() => button.focus());
    expect(visibleTips()).toHaveLength(1);
    act(() => button.blur());
    expect(visibleTips()).toHaveLength(0);
  });

  it("keeps one description visible across groups", () => {
    vi.useFakeTimers();
    const text = nleCopy("en").sequence;
    const binding = bindingFixture({
      locale: "en",
      production: { status: "ready", projection: productionProjection() },
      contextAvailable: true,
      state: expandedState({
        planning: { ...initialNlePlanningState, storyboardReviewOpen: true },
        readiness: initialNleReadinessState,
        sequence: initialNleSequenceState,
      }),
    });
    const view = render(<ProductionAndEditorSequence binding={binding} />);
    const control = (id: string) =>
      view.container.querySelector<HTMLButtonElement>(
        `[data-h3-nle-control="${id}"]`,
      )!;
    fireEvent.pointerEnter(control("planning.propose").parentElement!);
    act(() => vi.advanceTimersByTime(300));
    expect(view.getByRole("tooltip").textContent).toBe(text.describe.propose);
    act(() => control("planning.add_row").focus());
    const visible = view.container.querySelectorAll(
      "[role='tooltip']:not([hidden])",
    );
    expect(visible).toHaveLength(1);
    expect(visible[0]!.textContent).toBe(text.describe.addRow);
    expect(
      control("planning.propose").getAttribute("aria-describedby"),
    ).toBeNull();
  });

  it("moves a focused description across groups without a React commit", () => {
    let commits = 0;
    const binding = bindingFixture({
      locale: "en",
      production: { status: "ready", projection: productionProjection() },
      contextAvailable: true,
      state: expandedState({
        planning: {
          ...initialNlePlanningState,
          status: "prepared",
          projection: planningProjection() as never,
          boundWorkspaceFingerprint: fp("a"),
          storyboardReviewOpen: true,
        },
        readiness: initialNleReadinessState,
        sequence: initialNleSequenceState,
      }),
    });
    const view = render(
      <Profiler id="icon-actions" onRender={() => (commits += 1)}>
        <ProductionAndEditorSequence binding={binding} />
      </Profiler>,
    );
    const control = (id: string) =>
      view.container.querySelector<HTMLButtonElement>(
        `[data-h3-nle-control="${id}"]`,
      )!;
    const mounted = commits;

    act(() => control("planning.prepare_context").focus());
    act(() => control("planning.add_row").focus());

    // Guard the accepted-edit budget: focus descriptions are owned DOM presentation, not a
    // reason to render the profiled editor root before its command transaction begins.
    expect(commits).toBe(mounted);
    expect(
      control("planning.prepare_context").getAttribute("aria-describedby"),
    ).toBeNull();
    expect(control("planning.add_row").getAttribute("aria-describedby")).toBe(
      view.getByRole("tooltip").id,
    );
  });

  it("describes an unavailable action on hover of its slot after the delay", () => {
    vi.useFakeTimers();
    const text = nleCopy("en").sequence;
    const { control, view, visibleTips } = subject();
    const button = control("planning.propose");
    expect(button.disabled).toBe(true);
    fireEvent.pointerEnter(button.parentElement!);
    expect(visibleTips()).toHaveLength(0);
    act(() => vi.advanceTimersByTime(300));
    expect(view.getByRole("tooltip").textContent).toBe(text.describe.propose);
    fireEvent.pointerLeave(button.closest("[role='group']")!);
    expect(visibleTips()).toHaveLength(0);
  });

  it("uses icon buttons for the storyboard row actions too", () => {
    const text = nleCopy("en").sequence;
    const { control } = subject("en", true, true);
    for (const [id, key] of [
      ["planning.add_row", "addRow"],
      ["planning.admit_reviewed", "admitReviewed"],
    ] as const) {
      const button = control(id);
      expect(button.textContent?.trim(), id).toBe("");
      expect(button.getAttribute("aria-label"), id).toBe(text[key]);
      expect(button.closest("[role='group']")?.getAttribute("aria-label")).toBe(
        text.reviewActions,
      );
    }
  });

  it("releases an armed hover and a visible description when the panel unmounts", () => {
    vi.useFakeTimers();
    const errors = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    const text = nleCopy("en").sequence;
    const armed = subject();
    fireEvent.pointerEnter(armed.control("planning.propose").parentElement!);
    armed.view.unmount();
    const shown = subject("en", true);
    act(() => shown.control("planning.admit_canonical").focus());
    // The released hover delay elapses while another panel shows its description.
    act(() => vi.advanceTimersByTime(300));
    let visible = shown.view.container.querySelectorAll(
      "[role='tooltip']:not([hidden])",
    );
    expect(visible).toHaveLength(1);
    expect(visible[0]!.textContent).toBe(text.describe.admitCanonical);
    shown.view.unmount();
    const next = subject("en", true);
    act(() => next.control("planning.prepare_context").focus());
    visible = next.view.container.querySelectorAll(
      "[role='tooltip']:not([hidden])",
    );
    expect(visible).toHaveLength(1);
    expect(visible[0]!.textContent).toBe(text.describe.prepare);
    expect(errors).not.toHaveBeenCalled();
    errors.mockRestore();
  });

  it("dispatches exactly the bound action on click", () => {
    const { binding, control } = subject("en", true);
    fireEvent.click(control("planning.admit_canonical"));
    expect(binding.actions.admitStoryboard).toHaveBeenCalledTimes(1);
    expect(binding.actions.admitStoryboard).toHaveBeenCalledWith(
      "canonical_optimized_prompt",
    );
    expect(binding.actions.prepareContext).not.toHaveBeenCalled();
  });
});

// B-M2561-07: the description is pointer-transparent (D-4), so resting on it is decided by
// geometry. jsdom has no layout; the boxes are stubbed as the browser measured them at 480 px
// (row y 474-518, description 420-468, a 6 px bridge between them).
describe("resting on a description", () => {
  const box = (left: number, top: number, right: number, bottom: number) =>
    ({
      left,
      top,
      right,
      bottom,
      x: left,
      y: top,
      width: right - left,
      height: bottom - top,
      toJSON: () => ({}),
    }) as DOMRect;

  function group() {
    const view = render(
      <NleIconGroup label="Actions">
        <NleIconButton
          icon="layers"
          label="Prepare"
          description="Context description"
          hue="info"
          control="planning.prepare_context"
          disabled={false}
          onActivate={() => undefined}
        />
      </NleIconGroup>,
    );
    const element = view.getByRole("group");
    const tip = view.getByRole("tooltip", { hidden: true });
    element.getBoundingClientRect = () => box(24, 474, 472, 518);
    tip.getBoundingClientRect = () => box(24, 420, 472, 468);
    fireEvent.focus(view.getByRole("button", { name: "Prepare" }));
    expect(tip.hidden).toBe(false);
    return { element, tip };
  }

  it("keeps it while the pointer crosses the bridge and rests on it, and hides it past it", () => {
    const { element, tip } = group();
    fireEvent.pointerLeave(element, { clientX: 65, clientY: 470 });
    expect(tip.hidden).toBe(false);
    fireEvent.pointerMove(document, { clientX: 65, clientY: 444 });
    expect(tip.hidden).toBe(false);
    fireEvent.pointerMove(document, { clientX: 65, clientY: 400 });
    expect(tip.hidden).toBe(true);
  });

  it("hides it when the pointer leaves anywhere else, or presses outside the group", () => {
    const first = group();
    fireEvent.pointerLeave(first.element, { clientX: 65, clientY: 530 });
    expect(first.tip.hidden).toBe(true);
    cleanup();
    const second = group();
    fireEvent.pointerLeave(second.element, { clientX: 65, clientY: 444 });
    expect(second.tip.hidden).toBe(false);
    fireEvent.pointerDown(document.body, { clientX: 65, clientY: 444 });
    expect(second.tip.hidden).toBe(true);
  });
});
