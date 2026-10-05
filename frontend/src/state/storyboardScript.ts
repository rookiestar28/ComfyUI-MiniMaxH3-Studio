// A storyboard script is the long content of a whole video, written as shots. This module turns
// one into the reviewed storyboard rows the planning route already accepts; it is pure and the
// script text never leaves the browser -- only the rows are sent, by the existing admission.
//
// GUARD: the rows are on the production clock (0..target). A Context prompt describes one clip,
// so the cut times its markers carry are that clip's own; `storyboardScriptFromPrompt` drops
// them instead of reusing them. Carrying them over places a 15 s clip's cuts at the start of a
// longer video and stretches the last shot across the rest.

import type { StoryboardShotDraft } from "./nleWorkspaceState";

export const STORYBOARD_SCRIPT_MAX_CHARACTERS = 65_536;
export const STORYBOARD_SCRIPT_MAX_SHOTS = 32;
export const STORYBOARD_SHOT_MAX_CHARACTERS = 8_192;

export type StoryboardScriptError =
  | "script_empty"
  | "script_shape"
  | "script_mixed_timing"
  | "script_time_range"
  | "script_too_many_shots"
  | "script_shot_too_long"
  | "script_too_long";

export type StoryboardScriptResult =
  | Readonly<{
      ok: true;
      rows: readonly StoryboardShotDraft[];
      timing: "timed" | "even";
    }>
  | Readonly<{ ok: false; code: StoryboardScriptError }>;

// The same section boundary the canonical prompt renderer writes.
const SECTION =
  /(?:^|\n\n)(?:integrated_multimodal_description|detailed_description): ([\s\S]*?)(?=\n\n[a-z][a-z0-9_]{0,63}: |$)/gu;
// `[Shot N]`, optionally followed by its cut time: ` At MM:SS.mmm,` or the shorter ` At M:SS,`.
const MARKER =
  /\[Shot ([1-9][0-9]{0,2})\](?: At ([0-9]{1,2}):([0-5][0-9])(?:\.([0-9]{3}))?,)?/gu;
// `from [Shot N]` names a shot inside a reference sentence; it does not start one.
const REFERENCE_PREFIX = "from ";

type Marker = Readonly<{
  ordinal: number;
  start: number | null;
  index: number;
  end: number;
}>;

type Shot = Readonly<{ body: string; start: number | null }>;

function storyboardBody(text: string): string | null {
  const sections = [...text.matchAll(SECTION)];
  if (sections.length > 1) return null;
  return sections.length === 1 ? sections[0]![1]! : text;
}

function markers(body: string): Marker[] {
  const found: Marker[] = [];
  for (const match of body.matchAll(MARKER)) {
    const index = match.index;
    if (
      body.slice(Math.max(0, index - REFERENCE_PREFIX.length), index) ===
      REFERENCE_PREFIX
    )
      continue;
    found.push({
      ordinal: Number(match[1]),
      start:
        match[2] === undefined
          ? null
          : Number(match[2]) * 60_000 +
            Number(match[3]) * 1_000 +
            Number(match[4] ?? 0),
      index,
      end: index + match[0].length,
    });
  }
  return found;
}

function oneLine(value: string): string {
  return value.replace(/\s+/gu, " ").trim();
}

/** The shots of a marked body, or `null` when the markers do not form one storyboard. */
function markedShots(body: string, found: readonly Marker[]): Shot[] | null {
  if (body.slice(0, found[0]!.index).trim().length > 0) return null;
  const shots: Shot[] = [];
  for (const [position, marker] of found.entries()) {
    if (marker.ordinal !== position + 1) return null;
    // The first shot owns the start of the video, so it never names a time.
    if (position === 0 && marker.start !== null) return null;
    const next = found[position + 1];
    const text = oneLine(body.slice(marker.end, next?.index ?? body.length));
    if (text.length === 0) return null;
    shots.push({ body: text, start: marker.start });
  }
  return shots;
}

function rows(
  bodies: readonly string[],
  starts: readonly number[],
  targetMilliseconds: number,
): StoryboardShotDraft[] {
  return bodies.map((text, index) => ({
    shotId: `shot-${index + 1}`,
    ordinal: index + 1,
    startMilliseconds: starts[index]!,
    endMilliseconds: starts[index + 1] ?? targetMilliseconds,
    text,
    hardBoundary: false,
  }));
}

/** Whole-second starts that spread `count` shots across `targetSeconds`, longest first. */
function evenStarts(count: number, targetSeconds: number): number[] {
  const base = Math.floor(targetSeconds / count);
  const longer = targetSeconds % count;
  const starts: number[] = [];
  let cursor = 0;
  for (let index = 0; index < count; index += 1) {
    starts.push(cursor * 1_000);
    cursor += base + (index < longer ? 1 : 0);
  }
  return starts;
}

/**
 * Split a storyboard script into reviewed rows that cover exactly `targetSeconds`.
 *
 * Shots are `[Shot N]` markers, or blank-line separated paragraphs when the script has no
 * marker. When every shot after the first names its start (`[Shot 2] At 00:08.000, ...`) those
 * times are the cuts; when none does, the shots share the target evenly in whole seconds.
 */
export function splitStoryboardScript(
  text: string,
  targetSeconds: number,
): StoryboardScriptResult {
  const refuse = (code: StoryboardScriptError) =>
    Object.freeze({ ok: false as const, code });
  if (text.length > STORYBOARD_SCRIPT_MAX_CHARACTERS)
    return refuse("script_too_long");
  const body = storyboardBody(text.replace(/\r\n?/gu, "\n"));
  if (body === null) return refuse("script_shape");
  if (body.trim().length === 0) return refuse("script_empty");
  if (!Number.isInteger(targetSeconds) || targetSeconds < 1)
    return refuse("script_time_range");
  const targetMilliseconds = targetSeconds * 1_000;

  const found = markers(body);
  let shots: Shot[];
  if (found.length > 0) {
    const marked = markedShots(body, found);
    if (marked === null) return refuse("script_shape");
    shots = marked;
  } else {
    shots = body
      .split(/\n[ \t]*\n/u)
      .map(oneLine)
      .filter((paragraph) => paragraph.length > 0)
      .map((paragraph) => ({ body: paragraph, start: null }));
  }
  if (shots.length > STORYBOARD_SCRIPT_MAX_SHOTS)
    return refuse("script_too_many_shots");
  if (shots.some((shot) => shot.body.length > STORYBOARD_SHOT_MAX_CHARACTERS))
    return refuse("script_shot_too_long");

  const bodies = shots.map((shot) => shot.body);
  const later = shots.slice(1);
  const timed = later.filter((shot) => shot.start !== null).length;
  if (timed > 0) {
    if (timed !== later.length) return refuse("script_mixed_timing");
    const starts = [0, ...later.map((shot) => shot.start!)];
    if (
      starts.some((start, index) => index > 0 && start <= starts[index - 1]!) ||
      starts[starts.length - 1]! >= targetMilliseconds
    )
      return refuse("script_time_range");
    return Object.freeze({
      ok: true as const,
      rows: Object.freeze(rows(bodies, starts, targetMilliseconds)),
      timing: "timed" as const,
    });
  }
  // Every evenly spaced shot must own at least one whole second.
  if (shots.length > targetSeconds) return refuse("script_too_many_shots");
  return Object.freeze({
    ok: true as const,
    rows: Object.freeze(
      rows(bodies, evenStarts(shots.length, targetSeconds), targetMilliseconds),
    ),
    timing: "even" as const,
  });
}

/**
 * The shots a Context prompt already describes, as an editable script without cut times: one
 * `[Shot N]` line per shot. Empty when the prompt carries no readable shot list.
 */
export function storyboardScriptFromPrompt(promptText: string): string {
  if (promptText.length > STORYBOARD_SCRIPT_MAX_CHARACTERS) return "";
  const body = storyboardBody(promptText.replace(/\r\n?/gu, "\n"));
  if (body === null) return "";
  const found = markers(body);
  if (found.length === 0) return "";
  const lines: string[] = [];
  for (const [position, marker] of found.entries()) {
    if (marker.ordinal !== position + 1) return "";
    const next = found[position + 1];
    const text = oneLine(
      (position === 0 ? `${body.slice(0, marker.index)} ` : "") +
        body.slice(marker.end, next?.index ?? body.length),
    );
    if (text.length === 0) return "";
    lines.push(`[Shot ${position + 1}] ${text}`);
  }
  return lines.join("\n");
}
