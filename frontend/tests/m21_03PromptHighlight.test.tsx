/**
 * M21-03 AC-08 — highlighting the prompt must not touch the prompt.
 *
 * The property that matters is a partition: every input, however malformed,
 * comes back out byte-identical. A highlighter that "helpfully" trimmed, joined
 * or normalised would change a backend-owned value and its fingerprint, and no
 * visual check would ever show it.
 */

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  SidebarStages,
  initialSidebarStagesDraft,
} from "../src/components/SidebarStages";
import {
  MAX_HIGHLIGHTED_PROMPT,
  promptSegments,
} from "../src/components/promptGrammar";
import { decodeSidebarWorkspaceProjection } from "../src/contracts/sidebarWorkspaceCodec";

import { validSidebarWorkspace } from "./sidebarWorkspaceFixture";

const css = readFileSync(join(process.cwd(), "src/styles/tokens.css"), "utf8");

const rejoin = (text: string) =>
  promptSegments(text)
    .map((segment) => segment.text)
    .join("");

const kinds = (text: string) =>
  promptSegments(text)
    .filter((segment) => segment.kind !== "plain")
    .map((segment) => `${segment.kind}:${segment.text}`);

afterEach(cleanup);

describe("M21-03 the prompt highlighter is a partition", () => {
  const CASES = [
    "",
    "plain prose with no grammar at all",
    "integrated_multimodal_description: [Shot 1] a subject moves.",
    "<Picture 1> and <Video 12> and <Audio 3> and <Subject 999>",
    "(S1) then (S2)",
    "Exact dialogue: <d>[English] hello</d> after",
    // Malformed on purpose: an unclosed block, a stray bracket, a number that
    // is too long, and a heading that is not one.
    "<d>never closed",
    "[Shot ] [Shot 1234] <Picture> integrated_multimodal_descriptions:",
    "混合中文與 <Picture 1> 的提示詞",
    "\n\n\t  trailing and leading whitespace  \t\n",
    "<<Picture 1>>",
  ] as const;

  it.each(CASES)("returns %j unchanged", (text) => {
    expect(rejoin(text)).toBe(text);
  });

  it("returns every input unchanged under a fuzz of grammar fragments", () => {
    // A property, not a fixture: the parts are shuffled deterministically so a
    // failure is reproducible without a seed to chase.
    const parts = [
      "<Picture 1>",
      "[Shot 2]",
      "(S3)",
      "<d>x</d>",
      "overall_soundscape:",
      "<",
      ">",
      "[",
      "]",
      "d",
      " ",
      "n",
      "o",
      "s",
      "i",
    ];
    for (let seed = 0; seed < 400; seed += 1) {
      let text = "";
      let value = seed * 2654435761;
      for (let index = 0; index < 6; index += 1) {
        value = (value * 1103515245 + 12345) % 2147483648;
        text += parts[value % parts.length];
      }
      expect(rejoin(text), text).toBe(text);
    }
  });

  it("recognises only the grammar this repository declares", () => {
    expect(
      kinds("integrated_multimodal_description: [Shot 1] <Picture 2> (S3)"),
    ).toEqual([
      "field:integrated_multimodal_description:",
      "shot:[Shot 1]",
      "reference:<Picture 2>",
      "segment:(S3)",
    ]);
    // Prose that merely looks structural stays plain.
    expect(kinds("The shot [1] shows picture <two> in segment (S).")).toEqual(
      [],
    );
    expect(kinds("a totally ordinary sentence")).toEqual([]);
  });

  it("stops highlighting rather than scanning an over-long prompt", () => {
    const oversize = "x".repeat(MAX_HIGHLIGHTED_PROMPT + 1);
    const segments = promptSegments(oversize);
    expect(segments).toHaveLength(1);
    expect(segments[0]?.kind).toBe("plain");
    expect(segments[0]?.text).toBe(oversize);
  });
});

describe("M21-03 the overlay is inert", () => {
  const projection = decodeSidebarWorkspaceProjection({
    ...validSidebarWorkspace,
  });

  const view = () =>
    render(
      <SidebarStages
        projection={projection}
        locale="en"
        busy={false}
        draft={{
          ...initialSidebarStagesDraft(projection),
          activeStage: "audit",
        }}
        onDraftChange={vi.fn()}
        onAction={vi.fn()}
        onClientFailure={vi.fn()}
      />,
    );

  it("keeps the overlay out of the accessible tree and out of the way", () => {
    const { container } = view();
    const overlay = container.querySelector(".h3-prompt-o");
    expect(overlay).not.toBeNull();
    expect(overlay?.getAttribute("aria-hidden")).toBe("true");
    expect(overlay?.querySelector("input, textarea, button, a")).toBeNull();
    expect(css).toMatch(/\.h3-prompt-o \{[^}]*pointer-events: none/s);
    // Exactly one control still owns the prompt.
    expect(container.querySelectorAll("#h3-prompt-revision")).toHaveLength(1);
  });

  it("edits the field, not the overlay, and keeps the value byte-identical", async () => {
    const onDraftChange = vi.fn();
    const draft = {
      ...initialSidebarStagesDraft(projection),
      activeStage: "audit" as const,
      promptText: "[Shot 1] <Picture 1>",
    };
    const { container } = render(
      <SidebarStages
        projection={projection}
        locale="en"
        busy={false}
        draft={draft}
        onDraftChange={onDraftChange}
        onAction={vi.fn()}
        onClientFailure={vi.fn()}
      />,
    );
    // The overlay paints exactly the draft's bytes. The trailing newline it
    // renders is a layout guard for a value that ends in one, and is not part
    // of the value.
    const overlay = container.querySelector(".h3-prompt-o");
    expect(overlay?.textContent?.replace(/\n$/, "")).toBe(draft.promptText);
    expect(overlay?.querySelectorAll("[data-grammar]")).toHaveLength(2);

    // Typing reaches the field, and what the field reports is the draft plus
    // exactly the character typed -- no normalisation on the way through.
    await userEvent.type(screen.getByLabelText("Prompt revision"), "!");
    expect(onDraftChange).toHaveBeenCalled();
    const last = onDraftChange.mock.calls.at(-1)?.[0] as { promptText: string };
    expect(last.promptText).toBe(`${draft.promptText}!`);
  });

  it("hands the paint back to the field where the platform owns colour", () => {
    expect(css).toMatch(
      /@media \(forced-colors: active\) \{\s*\.h3-prompt-o \{\s*display: none;[\s\S]*?\.h3-prompt-i \{\s*color: CanvasText;/,
    );
  });
});
