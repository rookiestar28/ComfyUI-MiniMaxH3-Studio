// B-M1605-COPY-01: the whole-video sequence pane, the NLE inspector and the plain-reason tables
// state what happened in the reader's language. Machine codes, state tokens, internal
// identifiers and qualification or receipt vocabulary stay out of visible text; the codes stay
// on data attributes for diagnostics.

import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NleInspectorTabs } from "../src/components/nle/NleInspectorTabs";
import { NleMonitor } from "../src/components/nle/NleMonitor";
import { monitorStatusText } from "../src/components/nle/NleMonitorChips";
import { NleOverlay } from "../src/components/nle/NleOverlay";
import { nleCopy } from "../src/components/nle/nleCopy";
import { NleTimeline } from "../src/components/nle/NleTimeline";
import {
  PLAIN_REASON_TABLES,
  plainReason,
} from "../src/components/plainReasons";
import { decodeManagedReadiness } from "../src/contracts/managedQualificationCodec";
import type { ManagedSequenceProjection } from "../src/host/managedSequenceClient";
import type { Locale } from "../src/i18n/catalog";
import { createSidebarRetention } from "../src/state/sidebarRetention";
import {
  initialNlePlanningState,
  initialNleReadinessState,
  initialNleSequenceState,
} from "../src/state/nleWorkspaceState";
import {
  fp,
  planningProjection,
  productionProjection,
  readyReadiness,
} from "./support/nleSequenceFixture";
import { bindingFixture, expandedState } from "./support/nleWorkspaceBinding";
import { ProductionAndEditorSequence } from "./support/nleSequenceComposition";
import {
  SMOKE_SHAPE,
  authoringReady,
  snapshotFixture,
} from "./support/nleWorkspaceFixture";

afterEach(cleanup);

const LOCALES: readonly Locale[] = ["en", "zh-TW", "zh-CN"];
/** A snake_case machine token such as `planning_refused_409` or `cross_dissolve_v1`. */
const MACHINE_TOKEN = /\b[a-z][a-z0-9]*_[a-z0-9_]+\b/u;
const INTERNAL_VOCABULARY =
  /qualif|receipt|authorit|fingerprint|projection|process-local|資格|收據|权限收据|投影/iu;

function visibleText(root: HTMLElement): string {
  const clone = root.cloneNode(true) as HTMLElement;
  // Free text the user typed or the fixture supplied is not product copy.
  for (const node of clone.querySelectorAll("small, textarea, input"))
    node.remove();
  return clone.textContent ?? "";
}

/** Visible text plus every accessible name and tooltip under `root`. */
function exposed(root: HTMLElement): string {
  return [
    visibleText(root),
    ...[...root.querySelectorAll("small")].map((node) => node.textContent),
    ...[...root.querySelectorAll("[aria-label],[title]")].flatMap((element) => [
      element.getAttribute("aria-label") ?? "",
      element.getAttribute("title") ?? "",
    ]),
  ].join("\n");
}

describe("B-M1605-COPY-01 plain reason tables", () => {
  it("names the same codes in every locale with plain sentences", () => {
    const en = PLAIN_REASON_TABLES.en;
    for (const locale of LOCALES) {
      const tables = PLAIN_REASON_TABLES[locale];
      for (const kind of Object.keys(en) as (keyof typeof en)[]) {
        expect(
          Object.keys(tables[kind].codes).sort(),
          `${locale} ${kind}`,
        ).toEqual(Object.keys(en[kind].codes).sort());
        for (const sentence of [
          tables[kind].fallback,
          ...Object.values(tables[kind].codes),
        ]) {
          expect(sentence, `${locale} ${kind}`).not.toMatch(MACHINE_TOKEN);
          expect(sentence, `${locale} ${kind}`).not.toMatch(
            INTERNAL_VOCABULARY,
          );
        }
      }
    }
  });

  it("falls back to a plain sentence instead of an unknown code", () => {
    for (const locale of LOCALES)
      for (const kind of [
        "readiness",
        "planning",
        "sequence",
        "assembly",
        "importRefusal",
        "editRejected",
        "editFailed",
      ] as const) {
        const text = plainReason(locale, kind, "a_code_no_table_names");
        expect(text).not.toContain("a_code_no_table_names");
        expect(text.length).toBeGreaterThan(0);
      }
  });
});

describe("B-M1605-COPY-01 sequence pane", () => {
  function pane(locale: Locale) {
    const failedProjection = {
      parentSequenceId: "parent.1",
      state: "paused_failure",
      revision: 4,
      slots: [
        { segmentId: "segment.one", ordinal: 1, state: "artifact_verified" },
        { segmentId: "segment.two", ordinal: 2, state: "failed" },
      ],
    } as unknown as ManagedSequenceProjection;
    const production = productionProjection(fp("a"), {
      assembly: {
        schema: "h3.context.production_assembly.projection.v1",
        state: "failed",
        progress: { completed: 1, total: 2 },
        capability_fingerprint: fp("b"),
        managed_sequence_fingerprint: fp("c"),
        artifact_receipt_fingerprints: [fp("d"), fp("7")],
        cut_boundary_receipt_fingerprints: [fp("8")],
        output_profile_id: "legacy_av_30fps_48khz_stereo",
        assembly_job_id: "job.1",
        authorization_fingerprint: fp("f"),
        receipt_fingerprint: null,
        failure_code: "assembly_execution_failed",
        projection_fingerprint: fp("e"),
      },
      allowed_actions: ["read_projection", "retry_assembly"],
    });
    const projection = planningProjection({
      admission_id: "admission_owned",
      proposal: {
        proposal_id: "proposal_owned",
        revision: 1,
        fingerprint: fp("6"),
        importable: false,
        blocker_codes: ["hard_content_crosses_boundary"],
        start_hold_codes: ["managed_execution_qualification_pending"],
        segments: [1, 2].map((ordinal) => ({
          segment_id: `segment_${ordinal}`,
          ordinal,
          task_mode: "t2va",
          duration_seconds: 10,
          local_prompt: "A blue sphere turns.",
        })),
      },
    });
    const binding = bindingFixture({
      locale,
      production: { status: "ready", projection: production },
      contextAvailable: true,
      state: expandedState({
        planning: {
          ...initialNlePlanningState,
          status: "error",
          projection,
          boundWorkspaceFingerprint: fp("a"),
          error: "planning_refused_409",
        },
        readiness: {
          ...initialNleReadinessState,
          status: "held",
          readiness: decodeManagedReadiness({
            ...readyReadiness(),
            status: "held",
            reason: "qualification_assets_unresolved",
            qualification_fingerprint: null,
            qualification: null,
          }),
          boundPlanFingerprint: fp("b"),
        },
        sequence: {
          ...initialNleSequenceState,
          ui: "failed",
          parentSequenceId: "parent.1",
          projection: failedProjection,
          failure: "queue_callback_incomplete",
        },
      }),
    } as Parameters<typeof bindingFixture>[0]);
    (
      binding.actions as { sequenceStartable: () => boolean }
    ).sequenceStartable = vi.fn(() => false);
    (
      binding.actions as { recoveryPointerPresent: () => boolean }
    ).recoveryPointerPresent = vi.fn(() => true);
    return render(<ProductionAndEditorSequence binding={binding} />);
  }

  it.each(LOCALES)(
    "renders every code as a plain sentence in %s and keeps the code on the element",
    (locale) => {
      const view = pane(locale);
      const text = visibleText(view.container);
      expect(text).not.toMatch(MACHINE_TOKEN);
      expect(text).not.toMatch(INTERNAL_VOCABULARY);
      expect(text).not.toContain("segment.two");
      expect(text).not.toContain("proposal_owned");
      const code = (status: string) =>
        view.container
          .querySelector(`[data-h3-nle-status="${status}"]`)
          ?.getAttribute("data-code");
      expect(code("planning-error")).toBe("planning_refused_409");
      expect(code("readiness-reason")).toBe("qualification_assets_unresolved");
      expect(code("sequence-failure")).toBe("queue_callback_incomplete");
      expect(code("assembly-failure")).toBe("assembly_execution_failed");
      expect(
        view.container
          .querySelector('[data-h3-nle-status="proposal-blockers"]')
          ?.getAttribute("data-codes"),
      ).toBe("hard_content_crosses_boundary");
      expect(text).toContain(
        plainReason(locale, "readiness", "qualification_assets_unresolved"),
      );
      expect(text).toContain(
        plainReason(locale, "sequence", "queue_callback_incomplete"),
      );
    },
  );

  it.each(LOCALES)(
    "M25-63: the editor's Sequence tab says where planning lives and stays plain in %s",
    (locale) => {
      const view = pane(locale);
      const tab = view.container.querySelector<HTMLElement>(
        '[data-h3-nle-region="sequence"]',
      )!;
      const copy = nleCopy(locale);
      expect(tab.querySelector("h4")!.textContent).toBe(copy.sequence.title);
      expect(
        tab.querySelector('[data-h3-nle-status="planning-location"]')!
          .textContent,
      ).toBe(copy.sequence.planningLocation);
      expect(
        tab.querySelector('[data-h3-nle-control^="planning."]'),
      ).toBeNull();
      const text = exposed(tab);
      expect(text).not.toMatch(MACHINE_TOKEN);
      expect(text).not.toMatch(INTERNAL_VOCABULARY);
      expect(text).toContain(
        plainReason(locale, "readiness", "qualification_assets_unresolved"),
      );
    },
  );
});

describe("M25-63 top bar and bin copy", () => {
  function overlay(locale: Locale) {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
    const state = expandedState();
    render(
      <NleOverlay
        binding={bindingFixture({
          locale,
          state: { ...state, surface: { ...state.surface, pane: "assets" } },
          authoring: authoringReady(SMOKE_SHAPE),
        })}
      />,
    );
    return document.querySelector<HTMLElement>('[role="dialog"]')!;
  }

  function expectPlain(root: HTMLElement, locale: Locale) {
    // The monitor and audio diagnostics are neither shown nor announced.
    const clone = root.cloneNode(true) as HTMLElement;
    for (const node of clone.querySelectorAll("[data-h3-nle-diagnostics]"))
      node.remove();
    const text = exposed(clone);
    expect(text, locale).not.toMatch(MACHINE_TOKEN);
    expect(text, locale).not.toMatch(INTERNAL_VOCABULARY);
    expect(text, locale).not.toMatch(/revision|\brev\b|修訂|修订/iu);
    return text;
  }

  it.each(LOCALES)(
    "names the top bar, the bin toolbar, the cards and both menus plainly in %s",
    async (locale) => {
      const copy = nleCopy(locale);
      const dialog = overlay(locale);
      const header = dialog.querySelector<HTMLElement>(".h3-nle-header")!;
      expect(header.querySelector("h2")!.textContent).toBe(copy.barTitle);
      const save = header.querySelector<HTMLElement>(".h3-nle-save")!;
      expect(save.getAttribute("data-h3-nle-save-state")).toBe("saved");
      expect(save.textContent).toBe(copy.save.saved);
      expect(save.textContent).not.toMatch(/\d/u);
      expectPlain(header, locale);

      const bin = dialog.querySelector<HTMLElement>(
        '[data-h3-nle-area="bin"]',
      )!;
      const binText = expectPlain(bin, locale);
      expect(
        bin.querySelector("[data-h3-nle-media-count]")!.textContent,
      ).toMatch(/·\s*\d+$/u);
      expect(binText).toContain(copy.assets.addTip);
      // The sort and filter menu.
      await act(async () =>
        fireEvent.click(
          bin.querySelector('[data-h3-nle-control="media.sort_filter"]')!,
        ),
      );
      const sortMenu = document.querySelector<HTMLElement>(
        `[role="menu"][aria-label="${copy.assets.sortFilter}"]`,
      )!;
      expect(sortMenu.querySelectorAll('[role="menuitemradio"]').length).toBe(
        6,
      );
      expectPlain(sortMenu, locale);
      await act(async () => fireEvent.keyDown(sortMenu, { key: "Escape" }));
      // A card's menu.
      await act(async () =>
        fireEvent.contextMenu(bin.querySelector(".h3-nle-media-primary")!),
      );
      const assetMenu = document.querySelector<HTMLElement>(
        "[data-h3-nle-asset-menu]",
      )!;
      expect(
        [...assetMenu.querySelectorAll('[role="menuitem"]')].map(
          (item) => item.textContent,
        ),
      ).toEqual([
        copy.assets.addToTimeline,
        copy.assets.insertAtPlayhead,
        copy.assets.overwriteAtPlayhead,
      ]);
      expectPlain(assetMenu, locale);
    },
  );

  it.each(LOCALES)(
    "B-M2563-11: the empty monitor points at the card's visible controls in %s",
    (locale) => {
      const copy = nleCopy(locale);
      // Since M25-63 a card shows "+" and is dragged; "Add to timeline" is only in its context
      // menu. The hint names what the card shows, as the empty lane's hint does. M25-64 moves the
      // monitor's hint to the empty-timeline overlay's key.
      for (const hint of [copy.monitor.emptyHint, copy.timeline.emptyLane])
        expect(hint, locale).toContain("+");
      expect(copy.monitor.emptyHint, locale).not.toContain(
        copy.assets.addToTimeline,
      );
      // "Drag" in each locale: the hint names both ways a card reaches the timeline.
      expect(copy.monitor.emptyHint, locale).toMatch(/drag|拖曳|拖到/u);
    },
  );
});

describe("B-M1605-COPY-01 inspector options", () => {
  it.each(LOCALES)(
    "labels blend, transition, effect and weight options in %s",
    (locale) => {
      const view = render(
        <NleInspectorTabs
          locale={locale}
          snapshot={snapshotFixture(SMOKE_SHAPE, 11)}
          selection={["clip-3"]}
          authoring={authoringReady(SMOKE_SHAPE, {
            selection: ["clip-3"],
            revision: 11,
          })}
          onIntent={vi.fn(async () => undefined)}
          retention={createSidebarRetention()}
        />,
      );
      const valued: HTMLOptionElement[] = [];
      for (const tab of ["basic", "colour", "text", "transition"]) {
        fireEvent.click(
          view.container.querySelector<HTMLButtonElement>(
            `[data-h3-nle-property-tab="${tab}"]`,
          )!,
        );
        valued.push(
          ...[...view.container.querySelectorAll("option")].filter((option) =>
            [
              "normal",
              "cross_dissolve_v1",
              "color_adjust_v1",
              "400",
              "700",
            ].includes(option.value),
          ),
        );
      }
      expect(valued.length).toBeGreaterThanOrEqual(4);
      for (const option of valued) {
        expect(option.textContent).not.toBe(option.value);
        expect(option.textContent ?? "").not.toMatch(MACHINE_TOKEN);
      }
    },
  );
});

// M25-64 (R5, rows #26-#31): the monitor header and overlays, Project settings, the clip header and
// every property tab name things plainly. The fixture's own ids, revision numbers, internal unit
// tokens and interval notation never reach a reader, and a user's text keeps its brackets.
describe("M25-64 monitor and inspector copy", () => {
  const base = snapshotFixture(SMOKE_SHAPE, 11);
  const userText = "Intro [v2] (final)";
  const snapshot = {
    ...base,
    clips: base.clips.map((clip) =>
      clip.clipId === "clip-3" && clip.text !== null
        ? { ...clip, text: { ...clip.text, content: userText } }
        : clip,
    ),
  } as typeof base;
  const ids = [
    ...snapshot.clips.map((clip) => clip.clipId),
    ...snapshot.tracks.map((track) => track.trackId),
    ...snapshot.assets.map((asset) => asset.assetId),
  ];

  function expectPlain(text: string, context: string) {
    for (const id of ids) expect(text, `${context} ${id}`).not.toContain(id);
    expect(text, context).not.toMatch(/\bbp\b|‰|m°/u);
    expect(text, context).not.toMatch(/\[\s*\d+\s*,\s*\d+\s*\)/u);
    expect(text, context).not.toMatch(/\b(?:Suspended|Seeking|Following)\b/u);
    expect(text, context).not.toMatch(/revision|修訂|修订/iu);
    expect(text, context).not.toMatch(MACHINE_TOKEN);
    expect(text, context).not.toMatch(INTERNAL_VOCABULARY);
  }

  function inspector(locale: Locale, selection: string[]) {
    return render(
      <NleInspectorTabs
        locale={locale}
        snapshot={snapshot}
        selection={selection}
        authoring={authoringReady(SMOKE_SHAPE, { selection, revision: 11 })}
        onIntent={vi.fn(async () => undefined)}
        retention={createSidebarRetention()}
      />,
    );
  }

  it.each(LOCALES)(
    "states Project settings, the clip header and every tab plainly in %s",
    (locale) => {
      const copy = nleCopy(locale);
      const none = inspector(locale, []);
      const project = exposed(none.container);
      expectPlain(project, `${locale} project`);
      for (const label of [
        copy.project.title,
        copy.project.resolution,
        copy.project.frameRate,
        copy.project.duration,
        copy.project.audio,
        copy.project.export,
        copy.project.hint,
      ])
        expect(project, locale).toContain(label);
      none.unmount();

      const clip = inspector(locale, ["clip-3"]);
      expect(
        clip.container.querySelector("[data-h3-nle-clip-name]")!.textContent,
      ).toBe(copy.timeline.titleClip);
      for (const tab of ["basic", "crop", "colour", "text", "transition"]) {
        fireEvent.click(
          clip.container.querySelector<HTMLButtonElement>(
            `[data-h3-nle-property-tab="${tab}"]`,
          )!,
        );
        expectPlain(exposed(clip.container), `${locale} ${tab}`);
        if (tab === "text")
          expect(clip.container.querySelector("textarea")!.value).toBe(
            userText,
          );
      }
      clip.unmount();

      // A title has no Audio tab; a video clip of a bound source does, after Transition.
      const video = inspector(locale, ["clip-0"]);
      const tabs = [
        ...video.container.querySelectorAll<HTMLButtonElement>(
          "[data-h3-nle-property-tab]",
        ),
      ];
      expect(tabs.map((tab) => tab.dataset.h3NlePropertyTab)).toEqual([
        "basic",
        "crop",
        "colour",
        "transition",
        "audio",
      ]);
      fireEvent.click(tabs.at(-1)!);
      const audio = exposed(video.container);
      expectPlain(audio, `${locale} audio`);
      expect(audio, locale).not.toMatch(/\bmb\b|millibel|毫貝|毫贝/iu);
    },
  );

  it.each(LOCALES)(
    "states the monitor header, both overlays and every unavailable reason plainly in %s",
    (locale) => {
      const copy = nleCopy(locale);
      const view = render(
        <NleMonitor
          locale={locale}
          monitor={{ mode: "unavailable", binding: null }}
          output={snapshot.output}
          selectedClip={snapshot.clips[0]}
          onFrame={() => undefined}
        />,
      );
      const unavailable = exposed(view.container);
      expectPlain(unavailable, `${locale} unavailable`);
      expect(unavailable).toContain(copy.monitor.header);
      expect(unavailable).toContain(copy.monitor.unavailableTitle);
      view.rerender(
        <NleMonitor
          locale={locale}
          monitor={{ mode: "unavailable", binding: null }}
          output={snapshot.output}
          selectedClip={undefined}
          emptyTimeline
          onFrame={() => undefined}
        />,
      );
      const empty = exposed(view.container);
      expectPlain(empty, `${locale} empty`);
      expect(empty).toContain(copy.monitor.emptyTitle);
      expect(empty).toContain(copy.monitor.emptyHint);
      // The composition overlay's reason is the monitor status sentence for each blocker.
      for (const blocker of [
        "source_unavailable",
        "canvas_unavailable",
        "invalid_contract",
        "resource_limit",
        "cleanup_pending",
      ] as const)
        expectPlain(
          monitorStatusText(copy, {
            status: "blocked",
            blocker,
            browserPreviewOnly: true,
            durationFrames: 1,
          }),
          `${locale} ${blocker}`,
        );
      expectPlain(copy.monitor.recover, `${locale} retry`);
    },
  );
});

// M25-62 (R5): the redesigned timeline surface -- track headers, clips, grips, the rail and the
// empty lane -- names things by display name and timecode. The fixture's ids stay on data
// attributes only; interval notation and internal unit tokens never reach a reader.
describe("M25-62 timeline surface copy", () => {
  function timeline(locale: Locale, empty = false) {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
    const authoring = authoringReady(SMOKE_SHAPE, { selection: ["clip-0"] });
    if (authoring.status !== "ready" || !authoring.timelineHistory)
      throw new Error("fixture must be ready");
    const snapshot = empty
      ? { ...authoring.timelineHistory.snapshot, clips: [] }
      : authoring.timelineHistory.snapshot;
    return render(
      <div className="h3-nle-dialog">
        <NleTimeline
          locale={locale}
          snapshot={snapshot}
          selection={empty ? [] : ["clip-0"]}
          authoring={authoring}
          gridFrames={1}
          playheadFrame={0}
          highlightedAssetIds={[]}
          onIntent={vi.fn(async () => undefined)}
          onEdgeGestureActive={vi.fn()}
        />
      </div>,
    );
  }

  function expectPlain(text: string, locale: Locale) {
    expect(text, locale).not.toMatch(/\b(?:clip|track)-\d+\b/u);
    expect(text, locale).not.toMatch(/\[\s*\d+\s*,\s*\d+\s*\)/u);
    expect(text, locale).not.toMatch(/\b(?:Suspended|Seeking|Following)\b/u);
    expect(text, locale).not.toMatch(/\bbp\b|‰|m°/u);
    expect(text, locale).not.toMatch(MACHINE_TOKEN);
    expect(text, locale).not.toMatch(/revision \d+/iu);
  }

  it.each(LOCALES)(
    "states the wide layout, the rail and the empty lane plainly in %s",
    (locale) => {
      const wide = timeline(locale);
      fireEvent.change(
        wide.container.querySelector(
          '[data-h3-nle-control="transport.zoom_continuous"]',
        )!,
        { target: { value: "1000" } },
      );
      expect(
        wide.container.querySelector('[data-h3-nle-inline-grips="true"]'),
      ).not.toBeNull();
      expectPlain(exposed(wide.container), locale);
      fireEvent.click(
        wide.container.querySelector(
          '[data-h3-nle-control="transport.zoom_fit"]',
        )!,
      );
      expect(wide.container.querySelector(".h3-nle-trim-rail")).not.toBeNull();
      expectPlain(exposed(wide.container), locale);
      wide.unmount();

      const empty = timeline(locale, true);
      expect(
        empty.container.querySelector("[data-h3-nle-empty-lane]")!.textContent,
      ).not.toBe("");
      expectPlain(exposed(empty.container), locale);
    },
  );
});
