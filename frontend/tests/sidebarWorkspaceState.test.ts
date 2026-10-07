import { describe, expect, it } from "vitest";

import {
  initialWorkspaceState,
  reduceWorkspaceState,
} from "../src/state/sidebarWorkspace";
import { validSidebarWorkspace } from "./sidebarWorkspaceFixture";

describe("sidebar workspace state", () => {
  it("accepts only the current backend response and exposes loading/error explicitly", () => {
    const ready = reduceWorkspaceState(initialWorkspaceState, {
      type: "received",
      projection: validSidebarWorkspace,
    });
    const loading = reduceWorkspaceState(ready, {
      type: "request",
      action: "stage_prompt",
    });
    expect(loading.status).toBe("loading");
    const staleResponse = {
      ...validSidebarWorkspace,
      report_revision: 0,
    };
    expect(
      reduceWorkspaceState(loading, {
        type: "received",
        projection: staleResponse,
      }),
    ).toMatchObject({ status: "error", reason: "stale_response" });
  });

  it("never promotes a local edit to validated or export-ready", () => {
    const ready = reduceWorkspaceState(initialWorkspaceState, {
      type: "received",
      projection: validSidebarWorkspace,
    });
    const edited = reduceWorkspaceState(ready, {
      type: "edit",
      promptText: "Browser-only draft",
    });
    expect(edited).toMatchObject({ status: "editing" });
    if (edited.status === "editing") {
      expect(edited.projection.actions.export).toBe(true);
      expect(edited.promptText).toBe("Browser-only draft");
    }
  });

  it("atomically replaces authority for a new host execution", () => {
    const current = reduceWorkspaceState(initialWorkspaceState, {
      type: "received",
      projection: validSidebarWorkspace,
    });
    const next = {
      ...validSidebarWorkspace,
      workspace_id: "ws_abcdefghijklmnopqrstuvwxyz012345",
      correlation: { prompt_id: "prompt-2", execution_node_id: "17" },
    };
    expect(
      reduceWorkspaceState(current, {
        type: "host_execution",
        projection: next,
      }),
    ).toEqual({ status: "ready", projection: next });
    expect(
      reduceWorkspaceState(current, { type: "received", projection: next }),
    ).toMatchObject({ status: "error", reason: "incompatible_response" });
  });
});
