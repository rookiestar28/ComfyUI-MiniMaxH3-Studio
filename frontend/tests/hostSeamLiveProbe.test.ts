import { describe, expect, it } from "vitest";

import {
  buildBackendHostSeamObservations,
  frontendProbeReadiness,
  HostProbePolicyError,
  HostTransportUnavailableError,
  isHostTransportUnavailable,
  observeFrontendHostSeamsInPage,
  requireHostHttpAvailability,
} from "./support/hostSeamLiveProbe";

describe("shared HC-09/HC-10 live seam observation authority", () => {
  it("builds the six backend witnesses in canonical seam order", () => {
    const observations = buildBackendHostSeamObservations({
      objectInfo: {
        "comfyui_h3_context.one": { display_name: "One" },
        MiniMaxH3ImageToVideo: {},
        MiniMaxH3ReferenceToVideo: {},
      },
      generationProfile: {
        schema: "h3.context.generation_profile.v1",
        families: [{}],
      },
      generationProfileStatus: 200,
      extensionBundleManifestEntries: 1,
    });
    expect(observations).toHaveLength(6);
    expect(observations.map((row) => row.seam_id)).toEqual([
      "backend.comfy_api.latest",
      "backend.folder_paths.get_filename_list",
      "backend.node_class_mappings",
      "backend.node_display_name_mappings",
      "backend.prompt_server.routes",
      "backend.web_directory",
    ]);
    expect(
      observations.every(
        (row) => row.presence === "present" && row.readiness_state === "ready",
      ),
    ).toBe(true);
  });

  it("fails individual backend witnesses closed without inventing host values", () => {
    const observations = buildBackendHostSeamObservations({
      objectInfo: { "comfyui_h3_context.one": { display_name: 7 } },
      generationProfile: { schema: "wrong", families: [] },
      generationProfileStatus: 404,
      extensionBundleManifestEntries: 0,
    });
    expect(observations.map((row) => row.readiness_state)).toEqual([
      "absent",
      "absent",
      "ready",
      "absent",
      "absent",
      "absent",
    ]);
  });

  it("exposes one self-contained frontend probe function for browser evaluation", () => {
    expect(typeof observeFrontendHostSeamsInPage).toBe("function");
    const source = observeFrontendHostSeamsInPage.toString();
    expect(source).toContain("frontend.api.fetch_api");
    expect(source).toContain("frontend.litegraph.registered_node_types");
    expect(source).not.toContain("HC-09");
    expect(source).not.toContain("HC-10");
  });

  it("requires the full bounded stability window before frontend observation", () => {
    expect(frontendProbeReadiness(7)).toBe("UNAVAILABLE");
    expect(frontendProbeReadiness(8)).toBe("READY");
    expect(() => frontendProbeReadiness(-1)).toThrow("stability sample");
  });

  it("distinguishes transport unavailability from policy violations", () => {
    expect(
      isHostTransportUnavailable(new HostTransportUnavailableError()),
    ).toBe(true);
    expect(isHostTransportUnavailable(new HostProbePolicyError())).toBe(false);
    expect(isHostTransportUnavailable(new Error("ordinary failure"))).toBe(
      false,
    );
  });

  it("classifies ordinary non-200 responses as availability, not policy", () => {
    expect(() => requireHostHttpAvailability(200)).not.toThrow();
    for (const status of [404, 500, 503])
      expect(() => requireHostHttpAvailability(status)).toThrow(
        HostTransportUnavailableError,
      );
    for (const status of [301, 302, 307, 308])
      expect(() => requireHostHttpAvailability(status)).toThrow(
        HostProbePolicyError,
      );
    expect(() => requireHostHttpAvailability(99)).toThrow(HostProbePolicyError);
  });
});
