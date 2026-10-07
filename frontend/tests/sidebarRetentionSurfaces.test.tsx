// M25-21 section 14.4: per-surface editing-state retention for the Production workbench. Each
// case edits through real user interactions, releases the view
// (unmount), remounts against the same session store and reads the restored controls back, then
// proves the declared invalidation, truthful-notice and no-replay rules.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ProductionWorkbench,
  type ProductionViewState,
} from "../src/components/ProductionWorkbench";
import { decodeProductionWorkbenchProjection } from "../src/contracts/productionWorkbenchCodec";
import {
  createSidebarRetention,
  type SidebarRetention,
} from "../src/state/sidebarRetention";
import { unavailableProductionAssemblyWire } from "./support/productionAssemblyWire";

afterEach(cleanup);

function workbenchProjection(
  options: {
    workspaceId?: string;
    revision?: number;
    fingerprint?: string;
  } = {},
) {
  const workspaceId = options.workspaceId ?? "workspace_1";
  const revision = options.revision ?? 2;
  const fingerprint = options.fingerprint ?? `sha256:${"a".repeat(64)}`;
  const segment = (index: number, predecessor: string | null) => ({
    segment_id: `segment_${index}`,
    ordinal: index,
    task_mode: "t2va",
    duration: {
      duration_milliseconds: 4167,
      delivered_milliseconds: 4458,
      frame_count: 107,
      snapped: true,
    },
    relation: predecessor === null ? "independent" : "predecessor",
    predecessor_segment_id: predecessor,
    boundary_kind: predecessor === null ? "independent" : "native_handoff",
    closure_state: "dirty_self",
    job_state: "planned",
    artifact_state: "unavailable",
    continuity_state: "unavailable",
    delivered_geometry: null,
  });
  return decodeProductionWorkbenchProjection({
    schema: "h3.context.production_workbench.projection.v1",
    workspace_handle: `pw_${"a".repeat(43)}`,
    workspace_id: workspaceId,
    workspace_revision: revision,
    workspace_fingerprint: fingerprint,
    segments: [segment(1, null), segment(2, "segment_1"), segment(3, null)],
    selected_segment_ids: ["segment_1"],
    run: { state: "ready", completed: 0, total: 3 },
    generation_sequence: null,
    reconstruction: { state: "unavailable" },
    assembly: unavailableProductionAssemblyWire(),
    authority_versions: ["h3.context.generation_sequence_projection.v1"],
    outputs: [],
    allowed_actions: [
      "set_segment_relation",
      "reorder_segments",
      "set_selection",
      "read_projection",
      "release_workspace",
    ],
    blocker_codes: [],
    limits: { max_segments: 64, max_outputs: 65 },
  });
}

function ready(projection = workbenchProjection()): ProductionViewState {
  return { status: "ready", projection };
}

const NOTICE =
  "Unsent relation and position edits were cleared because this workspace changed.";

function relationSelect(): HTMLSelectElement {
  return screen.getByRole("combobox", {
    name: "Relationship for segment 1",
  }) as HTMLSelectElement;
}

function predecessorSelect(): HTMLSelectElement {
  return screen.getByRole("combobox", {
    name: "Predecessor segment",
  }) as HTMLSelectElement;
}

function moveTarget(ordinal: number): HTMLInputElement {
  return screen.getByRole("spinbutton", {
    name: `Move segment ${ordinal} to position`,
  }) as HTMLInputElement;
}

function authorityDetails(): HTMLDetailsElement {
  return document.querySelector("details.h3p-au") as HTMLDetailsElement;
}

function editDistinctiveDrafts(): void {
  fireEvent.change(relationSelect(), { target: { value: "predecessor" } });
  fireEvent.change(predecessorSelect(), { target: { value: "segment_3" } });
  fireEvent.change(moveTarget(1), { target: { value: "3" } });
  const details = authorityDetails();
  details.open = true;
  fireEvent(details, new Event("toggle"));
}

/**
 * B-M1605-DRAFT-01: mount `loading`, then deliver `ready` and edit `find()` from the mutation
 * microtask of the delivering commit, i.e. after that commit and before the scheduler task that runs
 * its passive effects, where a real input event can also land. Rendering here bypasses `act`, which
 * would otherwise flush those effects first and hide the window. Returns the control's value once
 * everything has settled and the value a remount against the same store restores.
 */
async function editInsideDeliveringCommit(
  loading: ReactNode,
  ready: ReactNode,
  find: () => HTMLInputElement | HTMLSelectElement | null,
  value: string,
): Promise<{ edited: boolean; settled: string; remounted: string }> {
  const env = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean };
  const previousActEnvironment = env.IS_REACT_ACT_ENVIRONMENT;
  env.IS_REACT_ACT_ENVIRONMENT = false;
  const settle = () => new Promise((resolve) => setTimeout(resolve, 50));
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  let edited = false;
  const observer = new MutationObserver(() => {
    const control = find();
    if (edited || control === null) return;
    edited = true;
    const prototype =
      control instanceof HTMLSelectElement
        ? HTMLSelectElement.prototype
        : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(
      control,
      value,
    );
    control.dispatchEvent(
      new Event(control instanceof HTMLSelectElement ? "change" : "input", {
        bubbles: true,
      }),
    );
  });
  try {
    root.render(loading);
    await settle();
    expect(find()).toBeNull();
    observer.observe(host, { childList: true, subtree: true });
    root.render(ready);
    await settle();
    observer.disconnect();
    const settled = find()?.value ?? "";
    root.unmount();
    const again = createRoot(host);
    again.render(ready);
    await settle();
    const remounted = find()?.value ?? "";
    again.unmount();
    return { edited, settled, remounted };
  } finally {
    observer.disconnect();
    host.remove();
    env.IS_REACT_ACT_ENVIRONMENT = previousActEnvironment;
  }
}

describe("M25-21 Production workbench retention", () => {
  it("restores drafts, anchor and expansion after view release without replaying intents", () => {
    const retention = createSidebarRetention();
    const onIntent = vi.fn();
    const first = render(
      <ProductionWorkbench
        locale="en"
        state={ready()}
        onIntent={onIntent}
        retention={retention}
      />,
    );
    editDistinctiveDrafts();
    first.unmount();

    render(
      <ProductionWorkbench
        locale="en"
        state={ready()}
        onIntent={onIntent}
        retention={retention}
      />,
    );
    expect(relationSelect().value).toBe("predecessor");
    expect(predecessorSelect().value).toBe("segment_3");
    expect(moveTarget(1).value).toBe("3");
    expect(authorityDetails().open).toBe(true);
    expect(screen.queryByText(NOTICE)).toBeNull();
    // Remount restores a draft; it never sends one.
    expect(onIntent).not.toHaveBeenCalled();
    // The restored draft is live: applying it submits exactly the retained values once.
    fireEvent.click(
      screen.getByRole("button", { name: "Apply relationship to segment 1" }),
    );
    expect(onIntent).toHaveBeenCalledExactlyOnceWith({
      action: "set_segment_relation",
      segmentId: "segment_1",
      relation: "predecessor",
      predecessorSegmentId: "segment_3",
    });
  });

  it("drops a draft typed against a superseded revision and says so once", () => {
    const retention = createSidebarRetention();
    const first = render(
      <ProductionWorkbench
        locale="en"
        state={ready()}
        onIntent={vi.fn()}
        retention={retention}
      />,
    );
    editDistinctiveDrafts();
    first.unmount();

    const changed = ready(
      workbenchProjection({
        revision: 3,
        fingerprint: `sha256:${"b".repeat(64)}`,
      }),
    );
    const second = render(
      <ProductionWorkbench
        locale="en"
        state={changed}
        onIntent={vi.fn()}
        retention={retention}
      />,
    );
    expect(relationSelect().value).toBe("independent");
    expect(moveTarget(1).value).toBe("1");
    expect(screen.getByText(NOTICE).getAttribute("role")).toBe("status");
    // The expansion is a view preference with no workspace authority; it survives.
    expect(authorityDetails().open).toBe(true);
    second.unmount();

    render(
      <ProductionWorkbench
        locale="en"
        state={changed}
        onIntent={vi.fn()}
        retention={retention}
      />,
    );
    expect(screen.queryByText(NOTICE)).toBeNull();
  });

  it("restores late when the view mounted before its projection, and reports a changed workspace", () => {
    const retention = createSidebarRetention();
    const first = render(
      <ProductionWorkbench
        locale="en"
        state={ready()}
        onIntent={vi.fn()}
        retention={retention}
      />,
    );
    editDistinctiveDrafts();
    first.unmount();

    const same = render(
      <ProductionWorkbench
        locale="en"
        state={{ status: "loading" }}
        onIntent={vi.fn()}
        retention={retention}
      />,
    );
    same.rerender(
      <ProductionWorkbench
        locale="en"
        state={ready()}
        onIntent={vi.fn()}
        retention={retention}
      />,
    );
    expect(relationSelect().value).toBe("predecessor");
    expect(predecessorSelect().value).toBe("segment_3");
    same.unmount();

    const replaced = render(
      <ProductionWorkbench
        locale="en"
        state={{ status: "absent" }}
        onIntent={vi.fn()}
        retention={retention}
      />,
    );
    replaced.rerender(
      <ProductionWorkbench
        locale="en"
        state={ready(workbenchProjection({ workspaceId: "workspace_2" }))}
        onIntent={vi.fn()}
        retention={retention}
      />,
    );
    expect(relationSelect().value).toBe("independent");
    expect(screen.getByText(NOTICE)).toBeDefined();
  });

  it("resets silently on a mounted revision change and forgets the superseded draft", () => {
    const retention = createSidebarRetention();
    const view = render(
      <ProductionWorkbench
        locale="en"
        state={ready()}
        onIntent={vi.fn()}
        retention={retention}
      />,
    );
    editDistinctiveDrafts();
    const accepted = ready(
      workbenchProjection({
        revision: 3,
        fingerprint: `sha256:${"b".repeat(64)}`,
      }),
    );
    view.rerender(
      <ProductionWorkbench
        locale="en"
        state={accepted}
        onIntent={vi.fn()}
        retention={retention}
      />,
    );
    expect(relationSelect().value).toBe("independent");
    expect(screen.queryByText(NOTICE)).toBeNull();
    view.unmount();
    render(
      <ProductionWorkbench
        locale="en"
        state={accepted}
        onIntent={vi.fn()}
        retention={retention}
      />,
    );
    expect(relationSelect().value).toBe("independent");
    expect(screen.queryByText(NOTICE)).toBeNull();
  });

  it("B-M1605-DRAFT-01 keeps a relation edit made before the delivering commit's passive effects", async () => {
    const retention = createSidebarRetention();
    const workbench = (state: ProductionViewState) => (
      <ProductionWorkbench
        locale="en"
        state={state}
        onIntent={() => undefined}
        retention={retention}
      />
    );
    const result = await editInsideDeliveringCommit(
      workbench({ status: "loading" }),
      workbench(ready()),
      () =>
        screen.queryByRole("combobox", {
          name: "Relationship for segment 1",
        }) as HTMLSelectElement | null,
      "cut",
    );
    expect(result).toEqual({ edited: true, settled: "cut", remounted: "cut" });
  });

  it("restores nothing after complete disposal", () => {
    const retention = createSidebarRetention();
    const first = render(
      <ProductionWorkbench
        locale="en"
        state={ready()}
        onIntent={vi.fn()}
        retention={retention}
      />,
    );
    editDistinctiveDrafts();
    first.unmount();
    retention.dispose();
    render(
      <ProductionWorkbench
        locale="en"
        state={ready()}
        onIntent={vi.fn()}
        retention={retention}
      />,
    );
    expect(relationSelect().value).toBe("independent");
    expect(moveTarget(1).value).toBe("1");
    expect(authorityDetails().open).toBe(false);
    expect(screen.queryByText(NOTICE)).toBeNull();
  });
});
