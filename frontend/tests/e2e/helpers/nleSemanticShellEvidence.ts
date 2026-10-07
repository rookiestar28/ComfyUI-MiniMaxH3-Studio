// M25-20 F1 corrective, browser half: one evidence attachment per corpus row this journey set
// exercises in the real browser -- the 11 `ui_invariant.*` / 2 `import_integration.*` shell rows
// (`comfyui_h3_context/core/semantic_conformance.py`) and the 151 `render_and_browser` rows
// (`semantic_conformance_cases.py`'s `build_recipes()`). Each attachment is written by the
// Playwright test that actually exercised the row through a real gesture on the real integrated
// shell, never merely cited by a test title.
//
// The shell-invariant/import-integration rows carry no rendered artifact (`observation:
// "shell_invariant"` / `"import_integration"`): the browser interaction *is* the whole
// observation, so `canvas_width`/`canvas_height` stay at their default 0. A `render_and_browser`
// row's presented canvas is real, so those two fields carry the monitor canvas's actual backing
// dimensions (`canvas.width`/`canvas.height`, never the CSS box size). The record below is
// attached through the existing `evidenceCapture` helper (privacy-scrubbed JSON, discoverable
// through Playwright's own reporters and `test-results/` attachments for each test) rather than a
// bespoke file format, so this evidence lives beside the ordinary CI artifacts for the run instead
// of a second ad hoc manifest a backend runner would have to know how to find.
//
// `executed` is true only once the real interaction ran to completion and the facts below were
// actually read from the live page; it is never set true speculatively. `missing` names any fact
// that could not be observed through a real, reachable browser gesture -- including a targeted
// explanation of *why* -- rather than omitting the row or inventing a value for it.

import type { TestInfo } from "@playwright/test";

import { evidenceCapture } from "./evidence";

// M25-20 F1 corrective (join-fixed contract): the structured landmark shapes the browser stage
// document's `render_and_browser` rows must carry, mirrored field-for-field from
// `scripts/nle_semantic_report.py`'s own readers (`_source_landmarks`, `_geometry`, `_patches`,
// `_alphas`, `_text`). Every value in these is a real, browser-measured observation -- never a
// value copied back out of the composition object the row was given, per the standing rule that a
// frame or landmark not actually observed belongs in `missing`, not filled in here.
export type SourceMappingEntry = Readonly<{
  output_frame: number;
  source_frame: number;
  /** Decimal or rational string (`Fraction(str)`-parseable), e.g. `"1.5"` or `"3/2"`. */
  source_time: string;
}>;

export type GeometryEntry = Readonly<{
  label: string;
  left: number;
  top: number;
  right: number;
  bottom: number;
}>;

export type PatchEntry = Readonly<{
  label: string;
  size_px: number;
  edge_distance_px: number;
  red: number;
  green: number;
  blue: number;
}>;

export type AlphaEntry = Readonly<{
  label: string;
  output_frame: number;
  alpha_milli: number;
}>;

export type TextEntry = Readonly<{
  content: string;
  font_identity: string;
  line_count: number;
  weight: number;
  style: string;
  align: string;
  visible_glyph_ratio_milli?: number;
  geometry?: readonly GeometryEntry[];
}>;

export type ShellObservation = Readonly<{
  case_id: string;
  executed: boolean;
  facts: Readonly<Record<string, string | number | boolean | null>>;
  missing: readonly string[];
  /** The monitor canvas's actual backing width, in pixels. 0 when the row has no canvas. */
  canvas_width?: number;
  /** The monitor canvas's actual backing height, in pixels. 0 when the row has no canvas. */
  canvas_height?: number;
  source_mapping?: readonly SourceMappingEntry[];
  geometry?: readonly GeometryEntry[];
  patches?: readonly PatchEntry[];
  color_patches?: readonly PatchEntry[];
  alphas?: readonly AlphaEntry[];
  text?: TextEntry;
}>;

// M25-20 B-65: the snapshot fields that are the composition's content. Undo and redo are judged
// on these alone, on both halves of an `import_integration` row (the render stage applies the
// same list, `scripts/nle_semantic_import_scenario.py`'s `TIMELINE_CONTENT_FIELDS`): the product's
// timeline fingerprint folds the revision in, so a history step that restores the very same
// composition never restores the same fingerprint, and a fingerprint comparison would report a
// correct undo as a failure.
export type TimelineContentSource = Readonly<{
  output?: unknown;
  capability?: unknown;
  assets: unknown;
  tracks: unknown;
  clips: unknown;
  audioExtension: unknown;
}>;

export function timelineContent(snapshot: TimelineContentSource): string {
  return JSON.stringify({
    output: snapshot.output,
    capability: snapshot.capability,
    assets: snapshot.assets,
    tracks: snapshot.tracks,
    clips: snapshot.clips,
    audioExtension: snapshot.audioExtension,
  });
}

export async function recordShellObservation(
  testInfo: TestInfo,
  observation: ShellObservation,
): Promise<void> {
  await evidenceCapture(testInfo).attach(
    `semantic_conformance.${observation.case_id}`,
    observation,
  );
}
