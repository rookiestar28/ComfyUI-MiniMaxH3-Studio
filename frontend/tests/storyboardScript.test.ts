// The storyboard script splitter: long content written as shots becomes reviewed storyboard rows
// on the production clock. The rows must cover the target exactly, because the planning route
// refuses any other coverage, and a Context prompt's own cut times must never be reused, because
// they belong to one clip.

import { describe, expect, it } from "vitest";

import {
  STORYBOARD_SCRIPT_MAX_CHARACTERS,
  STORYBOARD_SCRIPT_MAX_SHOTS,
  STORYBOARD_SHOT_MAX_CHARACTERS,
  splitStoryboardScript,
  storyboardScriptFromPrompt,
  type StoryboardScriptResult,
} from "../src/state/storyboardScript";

function spans(result: StoryboardScriptResult): [number, number][] {
  if (!result.ok) throw new Error(`refused: ${result.code}`);
  return result.rows.map((row) => [row.startMilliseconds, row.endMilliseconds]);
}

function code(result: StoryboardScriptResult): string {
  return result.ok ? "ok" : result.code;
}

function marked(count: number): string {
  return Array.from(
    { length: count },
    (_, index) => `[Shot ${index + 1}] Beat ${index + 1}.`,
  ).join("\n");
}

describe("timed storyboard scripts", () => {
  const script = [
    "[Shot 1] The courier leaves the depot.",
    "[Shot 2] At 00:08.000, the courier crosses the bridge.",
    "[Shot 3] At 00:20.000, rain starts over the market.",
    "[Shot 4] At 00:33.000, the courier shelters under an awning.",
    "[Shot 5] At 00:47.000, the parcel is delivered.",
  ].join("\n");

  it("uses each marker's time as the cut and the target as the last end", () => {
    const result = splitStoryboardScript(script, 60);
    expect(result.ok && result.timing).toBe("timed");
    expect(spans(result)).toEqual([
      [0, 8_000],
      [8_000, 20_000],
      [20_000, 33_000],
      [33_000, 47_000],
      [47_000, 60_000],
    ]);
    if (!result.ok) return;
    expect(result.rows.map((row) => row.shotId)).toEqual([
      "shot-1",
      "shot-2",
      "shot-3",
      "shot-4",
      "shot-5",
    ]);
    expect(result.rows.map((row) => row.ordinal)).toEqual([1, 2, 3, 4, 5]);
    expect(result.rows.every((row) => !row.hardBoundary)).toBe(true);
    expect(result.rows[1]!.text).toBe("the courier crosses the bridge.");
    expect(result.rows.some((row) => row.text.includes("[Shot"))).toBe(false);
  });

  it("reads the shorter minute:second form and shots written on one line", () => {
    expect(
      spans(
        splitStoryboardScript(
          "[Shot 1] Opening. [Shot 2] At 0:08, middle. [Shot 3] At 01:05, end.",
          90,
        ),
      ),
    ).toEqual([
      [0, 8_000],
      [8_000, 65_000],
      [65_000, 90_000],
    ]);
  });

  it.each([
    ["a start at the target", "[Shot 1] A. [Shot 2] At 00:30.000, B.", 30],
    ["a start past the target", "[Shot 1] A. [Shot 2] At 00:45.000, B.", 30],
    ["a start at zero", "[Shot 1] A. [Shot 2] At 00:00.000, B.", 30],
    [
      "starts that go backwards",
      "[Shot 1] A. [Shot 2] At 00:20.000, B. [Shot 3] At 00:10.000, C.",
      30,
    ],
    [
      "a repeated start",
      "[Shot 1] A. [Shot 2] At 00:10.000, B. [Shot 3] At 00:10.000, C.",
      30,
    ],
  ])("refuses %s", (_name, text, target) => {
    expect(code(splitStoryboardScript(text, target))).toBe("script_time_range");
  });

  it("refuses a script that times only some of its later shots", () => {
    expect(
      code(
        splitStoryboardScript(
          "[Shot 1] A. [Shot 2] At 00:10.000, B. [Shot 3] C.",
          30,
        ),
      ),
    ).toBe("script_mixed_timing");
  });
});

describe("evenly spaced storyboard scripts", () => {
  it.each([
    [6, 60, [10, 10, 10, 10, 10, 10]],
    [20, 60, Array.from({ length: 20 }, () => 3)],
    [7, 60, [9, 9, 9, 9, 8, 8, 8]],
    [4, 30, [8, 8, 7, 7]],
    [1, 45, [45]],
    [4, 4, [1, 1, 1, 1]],
  ])(
    "gives %i untimed shots whole seconds that cover %i s",
    (count, target, seconds) => {
      const result = splitStoryboardScript(marked(count), target);
      expect(result.ok && result.timing).toBe("even");
      const covered = spans(result);
      expect(covered.map(([start, end]) => (end - start) / 1_000)).toEqual(
        seconds,
      );
      expect(covered[0]![0]).toBe(0);
      expect(covered[covered.length - 1]![1]).toBe(target * 1_000);
      for (const [index, [start, end]] of covered.entries()) {
        expect(start % 1_000).toBe(0);
        expect(end - start).toBeGreaterThanOrEqual(1_000);
        if (index > 0) expect(start).toBe(covered[index - 1]![1]);
      }
    },
  );

  it("treats blank-line separated paragraphs as shots when there is no marker", () => {
    const result = splitStoryboardScript(
      "The courier leaves\nthe depot.\n\n  \nThe courier crosses the bridge.\r\n\r\nThe parcel is delivered.",
      30,
    );
    expect(spans(result)).toEqual([
      [0, 10_000],
      [10_000, 20_000],
      [20_000, 30_000],
    ]);
    expect(result.ok && result.rows.map((row) => row.text)).toEqual([
      "The courier leaves the depot.",
      "The courier crosses the bridge.",
      "The parcel is delivered.",
    ]);
  });

  it("refuses more shots than the target has whole seconds", () => {
    expect(code(splitStoryboardScript(marked(11), 10))).toBe(
      "script_too_many_shots",
    );
    expect(code(splitStoryboardScript(marked(10), 10))).toBe("ok");
  });
});

describe("storyboard script shape and bounds", () => {
  it.each([
    ["text before the first marker", "Intro. [Shot 1] A. [Shot 2] B."],
    ["a first marker that is not shot one", "[Shot 2] A. [Shot 3] B."],
    ["a skipped ordinal", "[Shot 1] A. [Shot 3] B."],
    ["a repeated ordinal", "[Shot 1] A. [Shot 1] B."],
    ["a timed first shot", "[Shot 1] At 00:00.000, A. [Shot 2] B."],
    ["an empty shot", "[Shot 1] A. [Shot 2]   [Shot 3] C."],
    [
      "two storyboard sections",
      "integrated_multimodal_description: [Shot 1] A.\n\ndetailed_description: [Shot 1] B.",
    ],
  ])("refuses %s", (_name, text) => {
    expect(code(splitStoryboardScript(text, 30))).toBe("script_shape");
  });

  it("names a blank script, an oversized script, shot and shot count", () => {
    expect(code(splitStoryboardScript("  \n\t ", 30))).toBe("script_empty");
    expect(
      code(
        splitStoryboardScript(
          "x".repeat(STORYBOARD_SCRIPT_MAX_CHARACTERS + 1),
          30,
        ),
      ),
    ).toBe("script_too_long");
    expect(
      code(
        splitStoryboardScript(
          `[Shot 1] ${"x".repeat(STORYBOARD_SHOT_MAX_CHARACTERS + 1)}`,
          30,
        ),
      ),
    ).toBe("script_shot_too_long");
    expect(
      code(
        splitStoryboardScript(
          `[Shot 1] ${"x".repeat(STORYBOARD_SHOT_MAX_CHARACTERS)}`,
          30,
        ),
      ),
    ).toBe("ok");
    expect(
      code(splitStoryboardScript(marked(STORYBOARD_SCRIPT_MAX_SHOTS + 1), 60)),
    ).toBe("script_too_many_shots");
    expect(
      code(splitStoryboardScript(marked(STORYBOARD_SCRIPT_MAX_SHOTS), 60)),
    ).toBe("ok");
  });

  it("does not read a reference to a shot as the start of one", () => {
    const result = splitStoryboardScript(
      "[Shot 1] The fox, as seen in <Picture 1> (from [Shot 2]), runs. [Shot 2] The fox rests.",
      20,
    );
    expect(result.ok && result.rows.map((row) => row.text)).toEqual([
      "The fox, as seen in <Picture 1> (from [Shot 2]), runs.",
      "The fox rests.",
    ]);
  });

  it("reads only the storyboard section of a full prompt", () => {
    const prompt =
      "subject_definitions: <Picture 1> is the fox.\n\n" +
      "integrated_multimodal_description: [Shot 1] The fox runs. [Shot 2] The fox rests.\n\n" +
      "overall_soundscape: Wind.\n\nnon_diegetic_music: N/A";
    const result = splitStoryboardScript(prompt, 20);
    expect(result.ok && result.rows.map((row) => row.text)).toEqual([
      "The fox runs.",
      "The fox rests.",
    ]);
  });
});

describe("a script from the Context prompt", () => {
  const prompt =
    "integrated_multimodal_description: [Shot 1] A sphere enters a room. " +
    "[Shot 2] At 00:05.000, the sphere stops. " +
    "[Shot 3] At 00:10.000, the sphere leaves.\n\n" +
    "overall_soundscape: N/A\n\nnon_diegetic_music: N/A";

  it("lists the shots without the clip's own cut times", () => {
    const script = storyboardScriptFromPrompt(prompt);
    expect(script).toBe(
      "[Shot 1] A sphere enters a room.\n[Shot 2] the sphere stops.\n[Shot 3] the sphere leaves.",
    );
    // A 15 s clip's cuts (5 s, 10 s) must not become the cuts of a 60 s video.
    expect(spans(splitStoryboardScript(script, 60))).toEqual([
      [0, 20_000],
      [20_000, 40_000],
      [40_000, 60_000],
    ]);
  });

  it("is empty when the prompt has no readable shot list", () => {
    expect(storyboardScriptFromPrompt("")).toBe("");
    expect(
      storyboardScriptFromPrompt(
        "integrated_multimodal_description: A sphere crosses a room.",
      ),
    ).toBe("");
    expect(storyboardScriptFromPrompt("[Shot 1] A. [Shot 3] B.")).toBe("");
    expect(
      storyboardScriptFromPrompt(
        "x".repeat(STORYBOARD_SCRIPT_MAX_CHARACTERS + 1),
      ),
    ).toBe("");
  });
});
