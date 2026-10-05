/**
 * M21-03 AC-08 — the canonical prompt grammar, tokenised for display only.
 *
 * The prompt is backend-owned text. Highlighting it must therefore be a pure
 * partition: `segments(text).map((s) => s.text).join("")` is `text`, byte for
 * byte, for every input. Nothing here rewrites, normalises or trims, and the
 * caller renders the segments behind a transparent textarea so the bytes the
 * user edits are the bytes the backend receives.
 *
 * Only the grammar this repository actually declares is recognised. Inventing a
 * highlight for prose would make the surface look like it understands more than
 * it does, which is the failure this list is deliberately short to avoid.
 */

export type PromptSegmentKind =
  /** Nothing this build claims to recognise. */
  | "plain"
  /** The official field headings of the rendered document. */
  | "field"
  /** `[Shot 1]`, the shot marker of guide section 4.2. */
  | "shot"
  /** `(S1)`, the segment marker. */
  | "segment"
  /** `<Picture 1>`, `<Video 2>`, `<Audio 1>`, `<Subject 3>`. */
  | "reference"
  /** `<d>...</d>`, the exact-dialogue block. */
  | "dialogue";

export type PromptSegment = Readonly<{ text: string; kind: PromptSegmentKind }>;

/** One alternation per declared form, in the order a scanner should try them. */
const GRAMMAR: ReadonlyArray<
  Readonly<{ kind: PromptSegmentKind; test: RegExp }>
> = [
  {
    kind: "field",
    test: /^(?:integrated_multimodal_description|overall_soundscape|non_diegetic_music):/,
  },
  { kind: "dialogue", test: /^<d>[\s\S]*?<\/d>/ },
  { kind: "reference", test: /^<(?:Picture|Video|Audio|Subject) \d{1,3}>/ },
  { kind: "shot", test: /^\[Shot \d{1,3}\]/ },
  { kind: "segment", test: /^\(S\d{1,3}\)/ },
];

/** The characters that can begin a declared form, so plain runs skip fast. */
const STARTERS = /[<[(inos]/;

const MAX_HIGHLIGHTED = 65_536;

/**
 * Partition `text` into display segments.
 *
 * Above `MAX_HIGHLIGHTED` characters the whole string is returned as one plain
 * segment: a prompt that large is already past every contract bound this
 * product accepts, and scanning it would cost more than the highlight is worth.
 */
export function promptSegments(text: string): readonly PromptSegment[] {
  if (typeof text !== "string" || text.length === 0) return [];
  if (text.length > MAX_HIGHLIGHTED) return [{ text, kind: "plain" }];
  const segments: PromptSegment[] = [];
  let plain = "";
  let index = 0;
  const flush = () => {
    if (plain.length > 0) {
      segments.push({ text: plain, kind: "plain" });
      plain = "";
    }
  };
  while (index < text.length) {
    const rest = text.slice(index);
    const character = rest[0]!;
    let matched: PromptSegment | undefined;
    if (STARTERS.test(character))
      for (const rule of GRAMMAR) {
        const found = rule.test.exec(rest);
        if (found !== null) {
          matched = { text: found[0], kind: rule.kind };
          break;
        }
      }
    if (matched === undefined) {
      plain += character;
      index += 1;
      continue;
    }
    flush();
    segments.push(matched);
    index += matched.text.length;
  }
  flush();
  return segments;
}

export const MAX_HIGHLIGHTED_PROMPT = MAX_HIGHLIGHTED;
