import { describe, expect, it, vi } from "vitest";

import {
  probeApiEventTarget,
  probeExtensionRegistrar,
  probeFetchApi,
  probeGraph,
  probeGraphSerializer,
  probeSidebarTabManager,
} from "../src/host/hostSeams";

describe("typed host seam probes", () => {
  it("binds callable members to their host owners", async () => {
    const hostApi = {
      marker: "bound",
      fetchApi(this: { marker: string }) {
        return Promise.resolve(this.marker);
      },
    };
    const result = probeFetchApi(hostApi);
    expect(result.status).toBe("ready");
    if (result.status === "ready")
      await expect(result.value.call({ marker: "ignored" })).resolves.toBe(
        "bound",
      );

    const registerExtension = vi.fn();
    const registrar = probeExtensionRegistrar({ registerExtension });
    expect(registrar.status).toBe("ready");
    if (registrar.status === "ready") registrar.value({ name: "fixture" });
    expect(registerExtension).toHaveBeenCalledWith({ name: "fixture" });
  });

  it("returns closed named refusals for absent or throwing graph seams", () => {
    expect(probeGraph({})).toEqual({
      status: "unavailable",
      reason: "graph_unavailable",
    });
    expect(
      probeGraph({
        get graph() {
          throw new Error("private host detail");
        },
      }),
    ).toEqual({ status: "unavailable", reason: "graph_unreadable" });
    expect(probeGraphSerializer({ graph: {} })).toEqual({
      status: "unavailable",
      reason: "graph_serialize_unavailable",
    });
    expect(probeApiEventTarget({})).toEqual({
      status: "unavailable",
      reason: "api_event_target_unavailable",
    });
  });

  it("admits documented sidebar registration without undocumented helpers", () => {
    const registerSidebarTab = vi.fn();
    const result = probeSidebarTabManager({
      extensionManager: { registerSidebarTab },
    });
    expect(result.status).toBe("ready");
    if (result.status !== "ready") return;
    expect(result.value.enumerate).toBeUndefined();
    expect(result.value.unregister).toBeUndefined();
    result.value.register({
      id: "fixture",
      title: "Fixture",
      tooltip: "Fixture",
      icon: "fixture",
      type: "custom",
      render() {},
      destroy() {},
    });
    expect(registerSidebarTab).toHaveBeenCalledOnce();
  });
});
