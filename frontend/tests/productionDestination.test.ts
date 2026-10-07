import { describe, expect, it } from "vitest";

import { decodeProductionWorkbenchProjection } from "../src/contracts/productionWorkbenchCodec";
import {
  PRODUCTION_DESTINATION_STORAGE_KEY,
  createProductionDestinationStore,
  destinationTarget,
} from "../src/host/productionDestination";
import { productionProjectionWire } from "./support/managedLifecycleWire";

function memoryStorage() {
  const values = new Map<string, string>();
  return {
    values,
    getItem: (name: string) => values.get(name) ?? null,
    setItem: (name: string, value: string) => void values.set(name, value),
    removeItem: (name: string) => void values.delete(name),
  };
}

const handle = (letter: string) => `pw_${letter.repeat(43)}`;

function project(letter: string, id = `workspace_${letter}`) {
  return decodeProductionWorkbenchProjection({
    ...productionProjectionWire(),
    workspace_handle: handle(letter),
    workspace_id: id,
  });
}

describe("M25-36 workflow-scoped Production destination", () => {
  it("previews the next default name without consuming it during render", () => {
    const store = createProductionDestinationStore();
    const workflow = {};

    expect(store.describe(workflow, undefined)).toEqual({
      kind: "new",
      ordinal: 1,
    });
    expect(store.describe(workflow, undefined)).toEqual({
      kind: "new",
      ordinal: 1,
    });
    expect(store.resolve(workflow, undefined)).toEqual({
      kind: "new",
      ordinal: 1,
    });
  });

  it("creates first, then keeps the project the Start bound for later Starts", () => {
    const store = createProductionDestinationStore({
      storage: memoryStorage(),
    });
    const workflow = {};
    const first = store.resolve(workflow, undefined);
    expect(first).toEqual({ kind: "new", ordinal: 1 });
    expect(destinationTarget(first)).toBeNull();
    // Resolving again before the Start publishes does not allocate another name.
    expect(store.resolve(workflow, undefined)).toBe(first);

    const bound = store.bindProject(workflow, project("a"));
    expect(bound).toEqual({
      kind: "project",
      workspaceHandle: handle("a"),
      workspaceId: "workspace_a",
      ordinal: 1,
    });
    expect(store.resolve(workflow, undefined)).toBe(bound);
    expect(destinationTarget(bound, "segment_7")).toEqual({
      workspaceHandle: handle("a"),
      workspaceId: "workspace_a",
      segmentId: "segment_7",
    });
  });

  it("never lets another workflow adopt a bound project", () => {
    const store = createProductionDestinationStore();
    const first = {};
    const second = {};
    store.bindProject(first, project("a"));
    // The second tab sees the same visible project but must create its own.
    expect(store.resolve(second, project("a"))).toEqual({
      kind: "new",
      ordinal: 2,
    });
    expect(store.ownedByAnotherWorkflow(handle("a"), second)).toBe(true);
    expect(() => store.bindProject(second, project("a"))).toThrow(
      "belongs to another workflow",
    );
  });

  it("moves only the exact captured project to an accepted workflow identity", () => {
    const store = createProductionDestinationStore();
    const admittedWorkflow = {};
    const committedWorkflow = {};
    const captured = store.bindProject(admittedWorkflow, project("a"));

    expect(
      store.commitCapturedProject(
        admittedWorkflow,
        committedWorkflow,
        captured,
        project("a"),
      ),
    ).toBe(true);
    expect(store.peek(admittedWorkflow)).toBeUndefined();
    expect(store.peek(committedWorkflow)).toBe(captured);
    expect(store.ownedByAnotherWorkflow(handle("a"), committedWorkflow)).toBe(
      false,
    );
    expect(store.ownedByAnotherWorkflow(handle("a"), admittedWorkflow)).toBe(
      true,
    );
  });

  it("does not replace New project during a captured workflow transition", () => {
    const store = createProductionDestinationStore();
    const admittedWorkflow = {};
    const committedWorkflow = {};
    const captured = store.bindProject(admittedWorkflow, project("a"));
    const newer = store.startNew(admittedWorkflow);

    expect(
      store.commitCapturedProject(
        admittedWorkflow,
        committedWorkflow,
        captured,
        project("a"),
      ),
    ).toBe(false);
    expect(store.peek(admittedWorkflow)).toBe(newer);
    expect(store.peek(committedWorkflow)).toBeUndefined();
    expect(store.ownedByAnotherWorkflow(handle("a"), committedWorkflow)).toBe(
      true,
    );
  });

  it("adopts an unowned visible project and restores its name after reload", () => {
    const storage = memoryStorage();
    const before = createProductionDestinationStore({ storage });
    before.startNew({});
    before.bindProject({}, project("c"));
    const record = JSON.parse(
      storage.values.get(PRODUCTION_DESTINATION_STORAGE_KEY) ?? "{}",
    );
    expect(record).toEqual({
      schema: PRODUCTION_DESTINATION_STORAGE_KEY,
      workspace_handle: handle("c"),
      workspace_id: "workspace_c",
      ordinal: 2,
      next_ordinal: 3,
    });

    const after = createProductionDestinationStore({ storage });
    const workflow = {};
    expect(after.resolve(workflow, project("c"))).toEqual({
      kind: "project",
      workspaceHandle: handle("c"),
      workspaceId: "workspace_c",
      ordinal: 2,
    });
    expect(after.startNew(workflow)).toEqual({ kind: "new", ordinal: 3 });
  });

  it("New project replaces only the destination; the bound name is not reused", () => {
    const store = createProductionDestinationStore();
    const workflow = {};
    store.bindProject(workflow, project("a"));
    expect(store.startNew(workflow)).toEqual({ kind: "new", ordinal: 2 });
    const next = store.bindProject(workflow, project("b"));
    expect(next).toMatchObject({ workspaceHandle: handle("b"), ordinal: 2 });
    // The earlier project keeps its owner, so a second workflow still cannot take it.
    expect(store.ownedByAnotherWorkflow(handle("a"), {})).toBe(true);
  });

  it("bounds live project owners to the Production registry capacity", () => {
    const store = createProductionDestinationStore();
    const workflows = Array.from({ length: 17 }, () => ({}));

    for (let index = 0; index < 16; index += 1) {
      store.bindProject(
        workflows[index] as object,
        project(String.fromCharCode("a".charCodeAt(0) + index)),
      );
    }

    expect(() =>
      store.bindProject(workflows[16] as object, project("q")),
    ).toThrow("owner capacity");
    store.releaseProject(handle("a"));
    expect(
      store.bindProject(workflows[16] as object, project("q")),
    ).toMatchObject({
      kind: "project",
      workspaceHandle: handle("q"),
    });
  });

  it("an unavailable project stays unavailable until New project", () => {
    const storage = memoryStorage();
    const store = createProductionDestinationStore({ storage });
    const workflow = {};
    store.bindProject(workflow, project("a"));
    store.markUnavailable(handle("a"));
    const unavailable = store.resolve(workflow, project("a"));
    expect(unavailable).toEqual({
      kind: "unavailable",
      workspaceHandle: handle("a"),
      ordinal: 1,
    });
    expect(() => destinationTarget(unavailable)).toThrow();
    expect(() => store.bindProject(workflow, project("b"))).toThrow(
      "needs New project",
    );
    expect(storage.values.has(PRODUCTION_DESTINATION_STORAGE_KEY)).toBe(false);
    expect(store.startNew(workflow)).toEqual({ kind: "new", ordinal: 2 });
    expect(store.bindProject(workflow, project("b"))).toMatchObject({
      kind: "project",
      ordinal: 2,
    });
  });

  it.each([
    "not json",
    JSON.stringify({ schema: PRODUCTION_DESTINATION_STORAGE_KEY }),
    JSON.stringify({
      schema: PRODUCTION_DESTINATION_STORAGE_KEY,
      workspace_handle: "ws_private",
      workspace_id: "workspace_a",
      ordinal: 1,
      next_ordinal: 2,
    }),
    JSON.stringify({
      schema: PRODUCTION_DESTINATION_STORAGE_KEY,
      workspace_handle: handle("a"),
      workspace_id: "workspace_a",
      ordinal: 3,
      next_ordinal: 3,
      extra: true,
    }),
  ])("ignores a malformed stored record %#", (raw) => {
    const storage = memoryStorage();
    storage.values.set(PRODUCTION_DESTINATION_STORAGE_KEY, raw);
    const store = createProductionDestinationStore({ storage });
    expect(store.resolve({}, undefined)).toEqual({ kind: "new", ordinal: 1 });
  });
});
