/**
 * M21-03 — the Production page is a column in a fixed order.
 *
 * AC-01, AC-02 and the presentation half of AC-03. The element budget itself is
 * measured in the browser lane, where a real layout exists; what is asserted
 * here is the structure that produces it.
 */

import { cleanup, render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProductionWorkbench } from "../src/components/ProductionWorkbench";
import { decodeProductionWorkbenchProjection } from "../src/contracts/productionWorkbenchCodec";
import { unavailableProductionAssemblyWire } from "./support/productionAssemblyWire";

const css = readFileSync(join(process.cwd(), "src/styles/tokens.css"), "utf8");
const HANDLE = `ws_${"b".repeat(43)}`;

const fingerprint = `sha256:${"a".repeat(64)}`;

const segment = (ordinal: number, blocked: boolean) => ({
  segment_id: `segment_${ordinal}`,
  ordinal,
  task_mode: "t2va",
  duration: {
    duration_milliseconds: 5167,
    delivered_milliseconds: 5167,
    frame_count: 124,
    snapped: false,
  },
  relation: "independent",
  predecessor_segment_id: null,
  boundary_kind: "independent",
  closure_state: blocked ? "requires_full_recompute" : "clean",
  job_state: blocked ? "failed" : "planned",
  artifact_state: blocked ? "failed" : "unavailable",
  continuity_state: "unavailable",
  delivered_geometry: null,
});

const projection = (
  selected: readonly string[],
  blockers: readonly string[] = [],
) =>
  decodeProductionWorkbenchProjection({
    schema: "h3.context.production_workbench.projection.v1",
    workspace_handle: `pw_${"a".repeat(43)}`,
    workspace_id: "workspace_1",
    workspace_revision: 7,
    workspace_fingerprint: fingerprint,
    segments: [segment(1, false), segment(2, true)],
    selected_segment_ids: [...selected],
    run: { state: "ready", completed: 0, total: 2 },
    generation_sequence: {
      schema: "h3.context.generation_sequence_projection.v1",
      sequence_id: "sequence.1",
      sequence_fingerprint: `sha256:${"b".repeat(64)}`,
      state_fingerprint: `sha256:${"c".repeat(64)}`,
      workspace_id: "workspace_1",
      workspace_revision: 7,
      workspace_fingerprint: fingerprint,
      correlation: { prompt_id: "prompt.1", execution_node_id: "node.1" },
    },
    reconstruction: { state: "unavailable" },
    assembly: unavailableProductionAssemblyWire(),
    authority_versions: ["h3.context.generation_sequence_projection.v1"],
    outputs: [],
    allowed_actions: [
      "add_segment_from_context",
      "replace_segment_from_context",
      "set_segment_relation",
      "delete_segment",
      "reorder_segments",
      "set_selection",
      "read_projection",
      "release_workspace",
    ],
    blocker_codes: [...blockers],
    limits: { max_segments: 64, max_outputs: 65 },
  }) as never;

const view = (selected: readonly string[], blockers: readonly string[] = []) =>
  render(
    <ProductionWorkbench
      locale="en"
      contextWorkspaceHandle={HANDLE}
      state={{ status: "ready", projection: projection(selected, blockers) }}
      onIntent={vi.fn()}
    />,
  );

afterEach(cleanup);

describe("M21-03 Production is one column", () => {
  it("declares no second track for the page grid at any width", () => {
    // AC-01. A 704px panel is a column: the two unequal tracks are what
    // produced the right-column voids the redesign exists to remove.
    const layout = css.match(/\.h3p-l \{([^}]*)\}/)?.[1];
    expect(layout).toBeDefined();
    expect(layout).toContain("grid-template-columns: minmax(0, 1fr);");
    expect(css).not.toContain("min-width: 641px");
  });

  it("states workspace, state and progress once, in the status band", () => {
    const { container } = view(["segment_1"]);
    const band = container.querySelector(".h3p-i");
    expect(band).not.toBeNull();
    expect(band?.getAttribute("aria-label")).toBe("Workspace status");
    expect(band?.textContent).toContain("rev 7");
    expect(band?.textContent).toContain("2 segments");
    expect(band?.querySelector(".h3p-rs .h3p-c")).not.toBeNull();
    expect(band?.querySelector(".h3p-meter")).not.toBeNull();
    // The four places that each said part of this are gone, not duplicated.
    expect(container.querySelector(".h3p-rev")).toBeNull();
    const runRows = [...container.querySelectorAll(".h3p-kv > div dt")].map(
      (node) => node.textContent,
    );
    expect(runRows).not.toContain("Run state");
    expect(runRows).not.toContain("Progress");
  });

  it("renders a blocker as a banner, and only when one exists", () => {
    const { container, unmount } = view(["segment_1"]);
    expect(container.querySelector(".h3p-bb")).toBeNull();
    unmount();
    const blocked = view(["segment_1"], ["sequence_authority_unavailable"]);
    const banner = blocked.container.querySelector(".h3p-bb");
    expect(banner).not.toBeNull();
    expect(banner?.textContent).toContain("Sequence authority is unavailable");
  });
});

describe("M21-03 one row per segment, one row expanded", () => {
  it("collapses an unselected segment to a spine that names all five states", () => {
    const { container } = view([]);
    const rows = [...container.querySelectorAll(".h3p-s > li")];
    expect(rows).toHaveLength(2);
    for (const row of rows) {
      expect(row.querySelector(".h3p-ss")).toBeNull();
      expect(row.querySelector(".h3p-m")).toBeNull();
      const spine = row.querySelector(".h3p-sp");
      expect(spine).not.toBeNull();
      expect(spine?.querySelectorAll("i")).toHaveLength(5);
    }
    // AC-02: the accessible name conveys the same five states the rows did.
    expect(
      screen.getByRole("img", {
        name: "Clean, Planned, Not available, Not available, Independent",
      }),
    ).not.toBeNull();
    expect(
      screen.getByRole("img", {
        name: "Full recompute required, Failed, Failed, Not available, Independent",
      }),
    ).not.toBeNull();
  });

  it("expands exactly one row, and only when one segment is selected", () => {
    const single = view(["segment_2"]);
    expect(single.container.querySelectorAll(".h3p-ss")).toHaveLength(1);
    expect(single.container.querySelectorAll(".h3p-m")).toHaveLength(1);
    expect(
      single.container.querySelector("[data-edit-target] .h3p-ss"),
    ).not.toBeNull();
    single.unmount();

    // A set of two is not one target, so nothing expands: an expanded row is a
    // claim about a single segment, and the panel refuses to guess which.
    const many = view(["segment_1", "segment_2"]);
    expect(many.container.querySelectorAll(".h3p-ss")).toHaveLength(0);
    expect(many.container.querySelectorAll(".h3p-sp")).toHaveLength(2);
  });

  it("keeps colour off the critical path for the spine", () => {
    // Each mark is aria-hidden and the group carries the words, so a reader who
    // cannot see the tones loses nothing.
    const { container } = view([]);
    for (const mark of container.querySelectorAll(".h3p-sp > i"))
      expect(mark.getAttribute("aria-hidden")).toBe("true");
    expect(css).toMatch(
      /@media \(forced-colors: active\)[\s\S]*\.h3c \.h3p-sp > i \{[^}]*border: 1px solid CanvasText/,
    );
  });
});

describe("M21-03 the empty regions recede", () => {
  it("shows one quiet line for outputs until an output exists", () => {
    const { container } = view(["segment_1"]);
    const quiet = container.querySelector('[data-known="false"]');
    expect(quiet?.textContent).toContain("once a segment has finished");
    expect(container.querySelector(".h3p-e")).toBeNull();
    expect(css).toMatch(
      /\[data-known="false"\][^{]*\{[^}]*color: var\(--h3-text-dim\)/s,
    );
  });
});
