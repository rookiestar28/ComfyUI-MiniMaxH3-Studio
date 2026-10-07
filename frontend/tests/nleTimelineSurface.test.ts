// M25-62: the pure rules of the redesigned timeline surface -- track display names (A62-2), the
// R7 clip edit affordances under both pointer modes (A62-4), and the scroll bar that moves the
// view within the existing clamp (A62-6).

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  CLIP_CENTRE_GRAB_HALF_PX,
  FINE_MENU_TRIGGER_INSET_PX,
  clipEditAffordances,
  moveRefusalText,
  scrollBarGeometry,
  trackDisplayNames,
  viewStartForScrollThumb,
} from "../src/components/nle/nleTimelineSurface";
import { nleCopy } from "../src/components/nle/nleCopy";
import type { CompositionTrack } from "../src/contracts/compositionCodec";
import type { Locale } from "../src/i18n/catalog";
import { maxViewStart } from "../src/runtime/timelineGeometry";

const track = (
  trackId: string,
  kind: CompositionTrack["kind"],
  order: number,
): CompositionTrack => ({ trackId, kind, order, enabled: true, locked: false });

describe("M25-62 track display names", () => {
  const tracks = [
    track("track-3", "text_overlay", 3),
    track("track-0", "primary_video", 0),
    track("track-2", "image_overlay", 2),
    track("track-1", "video_overlay", 1),
    track("track-5", "text_overlay", 5),
  ];

  it("names tracks by kind and order, never by id", () => {
    const names = trackDisplayNames(tracks, nleCopy("en").timeline.trackNames);
    expect(Object.fromEntries(names)).toEqual({
      "track-0": "Main",
      "track-1": "Overlay 1",
      "track-2": "Overlay 2",
      "track-3": "Text 1",
      "track-5": "Text 2",
    });
  });

  it.each<Locale>(["en", "zh-TW", "zh-CN"])(
    "gives every track a distinct name without a raw id in %s",
    (locale) => {
      const names = [
        ...trackDisplayNames(
          tracks,
          nleCopy(locale).timeline.trackNames,
        ).values(),
      ];
      expect(new Set(names).size).toBe(tracks.length);
      for (const name of names) {
        expect(name).not.toMatch(/track-\d/u);
        expect(name.trim().length).toBeGreaterThan(0);
      }
    },
  );
});

describe("M25-62 clip edit affordances (R7)", () => {
  const fine = (
    width: number,
    selected = true,
    draftEdge = null as null | "start" | "end",
  ) => clipEditAffordances({ width, selected, coarse: false, draftEdge });
  const coarse = (width: number, selected = true) =>
    clipEditAffordances({ width, selected, coarse: true, draftEdge: null });

  it("gives an unselected clip no edit controls at any width", () => {
    for (const width of [20, 24, 96, 144, 400])
      for (const mode of [fine(width, false), coarse(width, false)])
        expect(mode).toMatchObject({ grips: "none", rail: false, menu: false });
  });

  it("puts both fine grips inside a selected clip from 24 px and the rail below it", () => {
    expect(fine(24)).toMatchObject({ grips: "both", rail: false });
    expect(fine(23.9)).toMatchObject({ grips: "end", rail: true, menu: false });
    expect(fine(20, true, "start")).toMatchObject({
      grips: "start",
      rail: true,
    });
    expect(fine(20, true, "end")).toMatchObject({ grips: "end", rail: true });
  });

  it("shows the inline clip menu only where it leaves the body centre free to grab", () => {
    expect(fine(95.9).menu).toBe(false);
    expect(fine(96).menu).toBe(true);
    expect(fine(400).menu).toBe(true);
  });

  it("B-M2562-03: never puts the fine menu trigger over the body centre, at any width", () => {
    // The first rule showed the trigger from 48 px, where it covered the centre and a group drag
    // started there opened nothing and moved nothing. The invariant, not the number, is pinned.
    for (let width = 24; width <= 400; width += 0.5) {
      if (!fine(width).menu) continue;
      expect(
        width / 2 + CLIP_CENTRE_GRAB_HALF_PX,
        `width ${width}`,
      ).toBeLessThanOrEqual(width - FINE_MENU_TRIGGER_INSET_PX);
    }
  });

  it("keeps the touch-safe rule under a coarse pointer", () => {
    expect(coarse(143.9)).toMatchObject({
      grips: "none",
      rail: true,
      menu: false,
    });
    expect(coarse(144)).toMatchObject({
      grips: "both",
      rail: false,
      menu: true,
    });
  });

  it("shows the label strip from 40 px and the duration from 96 px, selected or not", () => {
    for (const selected of [true, false]) {
      expect(fine(39.9, selected)).toMatchObject({
        label: false,
        duration: false,
      });
      expect(fine(40, selected)).toMatchObject({
        label: true,
        duration: false,
      });
      expect(fine(95.9, selected).duration).toBe(false);
      expect(fine(96, selected)).toMatchObject({ label: true, duration: true });
    }
  });
});

describe("M25-62 scroll bar geometry", () => {
  const base = {
    durationFrames: 3_600,
    laneWidth: 800,
    pixelsPerFrame: 1,
    trackWidth: 800,
  };

  it("hides at Fit and whenever the whole extent is in view", () => {
    expect(
      scrollBarGeometry({ ...base, viewStart: 0, pixelsPerFrame: 800 / 3_600 })
        .visible,
    ).toBe(false);
    expect(
      scrollBarGeometry({ ...base, viewStart: 0, durationFrames: 600 }).visible,
    ).toBe(false);
  });

  it("shows a proportional thumb that tracks viewStart across the clamp", () => {
    const high = maxViewStart(base.durationFrames, base.laneWidth, 1);
    const start = scrollBarGeometry({ ...base, viewStart: 0 });
    expect(start.visible).toBe(true);
    expect(start.thumbWidth).toBeCloseTo((800 * 800) / 3_600, 6);
    expect(start.thumbX).toBe(0);
    const end = scrollBarGeometry({ ...base, viewStart: high });
    expect(end.thumbX + end.thumbWidth).toBeCloseTo(800, 6);
    const beyond = scrollBarGeometry({ ...base, viewStart: high + 500 });
    expect(beyond.thumbX).toBeCloseTo(end.thumbX, 6);
  });

  it("keeps a usable thumb on a long extent", () => {
    const long = scrollBarGeometry({
      ...base,
      viewStart: 0,
      durationFrames: 1_000_000,
    });
    expect(long.thumbWidth).toBe(24);
  });

  it("maps a thumb position back to a clamped viewStart", () => {
    const high = maxViewStart(base.durationFrames, base.laneWidth, 1);
    const geometry = scrollBarGeometry({ ...base, viewStart: 0 });
    const input = { ...base };
    expect(viewStartForScrollThumb(0, geometry, input)).toBe(0);
    expect(
      viewStartForScrollThumb(800 - geometry.thumbWidth, geometry, input),
    ).toBeCloseTo(high, 6);
    expect(viewStartForScrollThumb(-50, geometry, input)).toBe(0);
    expect(viewStartForScrollThumb(10_000, geometry, input)).toBeCloseTo(
      high,
      6,
    );
    const middle = viewStartForScrollThumb(
      (800 - geometry.thumbWidth) / 2,
      geometry,
      input,
    );
    expect(middle).toBeCloseTo(high / 2, 6);
  });
});

describe("B-M2562-06 move refusal copy", () => {
  // Every refusal code the move admission can return, read from its source so a new code
  // without words fails here instead of reaching the live region as itself.
  const source = readFileSync(
    join(__dirname, "..", "src", "runtime", "nleTimelineGesture.ts"),
    "utf8",
  );
  const codes = [
    ...new Set(
      [...source.matchAll(/admitted: false, reason: "([a-z_]+)"/gu)].map(
        (match) => match[1]!,
      ),
    ),
  ].sort();

  it("finds the admission's refusal codes", () => {
    expect(codes).toEqual(
      expect.arrayContaining([
        "bounds",
        "group_limit",
        "incompatible_track",
        "locked_track",
        "overlap",
        "primary_track",
        "source_missing",
        "target_exhaustion",
      ]),
    );
  });

  it.each(["en", "zh-TW", "zh-CN"] as const)(
    "words every code in %s and never shows one",
    (locale) => {
      const copy = nleCopy(locale).timeline;
      for (const code of [...codes, null, "a_future_code"]) {
        const text = copy.moveRefused.replace(
          "{reason}",
          moveRefusalText(copy.moveRefusals, code),
        );
        expect(text, String(code)).not.toMatch(/[a-z]+_[a-z]+/u);
        expect(text, String(code)).not.toContain("{");
        if (code !== null)
          expect(text, code).not.toBe(
            copy.moveRefused.replace("{reason}", code),
          );
        if (code !== null && code !== "a_future_code")
          expect(copy.moveRefusals, code).toHaveProperty(code);
      }
      expect(Object.keys(copy.moveRefusals).sort()).toEqual(
        Object.keys(nleCopy("en").timeline.moveRefusals).sort(),
      );
    },
  );
});
