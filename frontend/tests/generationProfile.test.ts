import { describe, expect, it, vi } from "vitest";

import {
  decodeGenerationProfile,
  familyProfileForTaskMode,
  GENERATION_PROFILE_SCHEMA,
} from "../src/contracts/generationProfileCodec";
import {
  createGenerationProfileClient,
  GENERATION_PROFILE_ROUTE,
  MAX_GENERATION_PROFILE_BYTES,
} from "../src/host/generationProfileClient";
import {
  admissionBlocksQueue,
  admissionBlocksRoute,
  admitGenerationProfile,
} from "../src/host/appMode";
import {
  ASSET_RELOCATED,
  generationProfile,
  generationProfileWire,
  MISSING_ASSET,
  TEMPLATE_DRIFT,
  UNSUPPORTED_HOST,
} from "./support/generationProfileFixture";

/**
 * M17-20 phase 2d. The browser gates on the backend's capability decision and
 * owns none of it (D1), so these rows are about two things: refusing a payload
 * that is not that decision, and reading the decision the same way the backend
 * wrote it.
 */

describe("the generation profile projection is decoded, not trusted", () => {
  it("accepts the projection the backend publishes", () => {
    const profile = decodeGenerationProfile(generationProfileWire());
    expect(profile.schema).toBe(GENERATION_PROFILE_SCHEMA);
    expect(profile.families.map((entry) => entry.templateName)).toEqual([
      "video_minimax_h3_t2v",
      "video_minimax_h3_i2v",
      "video_minimax_h3_r2v",
    ]);
    expect(profile.families.map((entry) => entry.family)).toEqual([
      "image_to_video",
      "image_to_video",
      "reference_to_video",
    ]);
    expect(profile.families[0]!.taskModes).toEqual(["t2va"]);
    expect(profile.families[1]!.taskModes).toEqual(["i2va", "fl2va", "l2va"]);
  });

  it("resolves the family from the projection's own task modes", () => {
    const profile = generationProfile();
    for (const mode of ["t2va", "i2va", "fl2va", "l2va"])
      expect(familyProfileForTaskMode(profile, mode)?.family).toBe(
        "image_to_video",
      );
    expect(familyProfileForTaskMode(profile, "ref2va")?.family).toBe(
      "reference_to_video",
    );
    expect(familyProfileForTaskMode(profile, "x2va")).toBeUndefined();
  });

  const rejected: Record<string, unknown> = {
    "a foreign schema": { ...generationProfileWire(), schema: "other.v1" },
    "an unpinned template revision": {
      ...generationProfileWire(),
      template_revision: "not-a-revision",
    },
    "an extra root key": { ...generationProfileWire(), extra: 1 },
    "no families": { ...generationProfileWire(), families: [] },
    "an unknown disposition": generationProfileWire({
      image: { disposition: "degraded", remediation: "none" },
    }),
    // The pairing is the backend's; a payload that re-pairs them is one this
    // shell has not qualified, and rendering its remediation would send the user
    // to fix the wrong thing.
    "a remediation that does not match its disposition": generationProfileWire({
      image: { disposition: "template_drift", remediation: "upgrade_host" },
    }),
    "an unknown slot": generationProfileWire({
      image: {
        disposition: "missing_asset",
        remediation: "select_installed_asset_on_canvas",
        unsatisfied_slots: ["control_net"],
      },
    }),
    "slots on a disposition that is not a missing asset": generationProfileWire(
      {
        image: {
          disposition: "template_drift",
          remediation: "requalify_template",
          unsatisfied_slots: ["video_unet"],
        },
      },
    ),
    "a missing asset that names no slot": generationProfileWire({
      image: {
        disposition: "missing_asset",
        remediation: "select_installed_asset_on_canvas",
        unsatisfied_slots: [],
      },
    }),
    "a repeated slot": generationProfileWire({
      image: {
        disposition: "missing_asset",
        remediation: "select_installed_asset_on_canvas",
        unsatisfied_slots: ["video_unet", "video_unet"],
      },
    }),
  };

  it.each(Object.keys(rejected))("refuses %s", (label) => {
    expect(() => decodeGenerationProfile(rejected[label])).toThrow();
  });

  it("refuses a repeated basis and a task mode claimed twice", () => {
    const wire = generationProfileWire() as { families: unknown[] };
    expect(() =>
      decodeGenerationProfile({
        ...wire,
        families: [wire.families[0], wire.families[0]],
      }),
    ).toThrow();
    const collide = generationProfileWire() as {
      families: Record<string, unknown>[];
    };
    collide.families[1]!.task_modes = ["ref2va", "t2va"];
    expect(() => decodeGenerationProfile(collide)).toThrow();
  });

  it("accepts two bases that share a family", () => {
    // `t2va` and `i2va` have the same anchor and the same slots and load
    // different template bytes. Rejecting the pair -- which an earlier
    // family-keyed uniqueness rule did -- would force the backend to qualify one
    // of them against the other's bytes.
    const profile = generationProfile();
    const image = profile.families.filter(
      (entry) => entry.family === "image_to_video",
    );
    expect(image.map((entry) => entry.templateName)).toEqual([
      "video_minimax_h3_t2v",
      "video_minimax_h3_i2v",
    ]);
    expect(image.flatMap((entry) => entry.taskModes)).toEqual([
      "t2va",
      "i2va",
      "fl2va",
      "l2va",
    ]);
  });

  it("keeps one basis's drift out of the basis beside it", () => {
    // The defect this row exists for: `t2va` and `i2va` share a family, so a
    // family-keyed qualification reported the i2v answer for both and a drifted
    // t2v template was admitted as available.
    const profile = generationProfile({ text: TEMPLATE_DRIFT });
    expect(admitGenerationProfile(profile, "t2va")).toMatchObject({
      status: "refused",
      reason: "template_drift",
    });
    for (const mode of ["i2va", "fl2va", "l2va"])
      expect(admitGenerationProfile(profile, mode)).toEqual({
        status: "admitted",
      });
  });

  it("carries no host filename, path or inventory", () => {
    // The projection names roles. If a field ever started carrying a filename,
    // this is where it would surface, before it reached the sidebar.
    const rendered = JSON.stringify(
      generationProfile({ image: MISSING_ASSET }),
    );
    expect(rendered).not.toMatch(/safetensors|[A-Za-z]:\\|\/home\/|\/Users\//);
  });
});

describe("the profile route is read defensively", () => {
  const okResponse = (body: string) => ({
    ok: true,
    status: 200,
    text: async () => body,
  });

  it("reads the repository's own same-origin route", async () => {
    const fetchApi = vi
      .fn()
      .mockResolvedValue(okResponse(JSON.stringify(generationProfileWire())));
    const profile = await createGenerationProfileClient({ fetchApi }).load();
    expect(fetchApi).toHaveBeenCalledWith(
      GENERATION_PROFILE_ROUTE,
      expect.objectContaining({
        method: "GET",
        credentials: "same-origin",
      }),
    );
    expect(profile.families).toHaveLength(3);
  });

  it("fails closed on an absent seam, a rejected route and an unbounded body", async () => {
    await expect(
      createGenerationProfileClient({}).load(),
    ).rejects.toMatchObject({ failure: "seam_unavailable" });
    await expect(
      createGenerationProfileClient({
        fetchApi: vi.fn().mockResolvedValue({
          ok: false,
          status: 503,
          text: async () => "",
        }),
      }).load(),
    ).rejects.toMatchObject({ failure: "route_rejected", status: 503 });
    await expect(
      createGenerationProfileClient({
        fetchApi: vi
          .fn()
          .mockResolvedValue(
            okResponse("x".repeat(MAX_GENERATION_PROFILE_BYTES + 1)),
          ),
      }).load(),
    ).rejects.toMatchObject({ failure: "payload_rejected" });
    await expect(
      createGenerationProfileClient({
        fetchApi: vi.fn().mockRejectedValue(new Error("host detail")),
      }).load(),
    ).rejects.toMatchObject({ failure: "seam_unavailable" });
  });

  it("keeps the rejected payload out of the error", async () => {
    // Named for what it is -- a host path the projection must never echo back.
    // Calling it `secret` makes the repository's own secret scanner flag the
    // fixture, which is noise rather than a finding.
    const privatePath = "C:/models/private-weight.safetensors";
    const failure = await createGenerationProfileClient({
      fetchApi: vi
        .fn()
        .mockResolvedValue(okResponse(JSON.stringify(privatePath))),
    })
      .load()
      .catch((error: unknown) => error);
    expect(String(failure)).not.toContain(privatePath);
  });
});

describe("admission reads the decision and applies it per route", () => {
  it("admits a family the backend calls available", () => {
    expect(admitGenerationProfile(generationProfile(), "i2va")).toEqual({
      status: "admitted",
    });
  });

  it("refuses a task mode the projection does not cover", () => {
    expect(admitGenerationProfile(generationProfile(), "x2va")).toMatchObject({
      status: "refused",
      reason: "unsupported_task_mode",
      remediation: "upgrade_host",
    });
  });

  it("carries the backend's remediation and unsatisfied roles verbatim", () => {
    expect(
      admitGenerationProfile(
        generationProfile({ text: MISSING_ASSET }),
        "t2va",
      ),
    ).toMatchObject({
      status: "refused",
      reason: "missing_asset",
      remediation: "select_installed_asset_on_canvas",
      unsatisfiedSlots: ["video_unet", "audio_vae"],
    });
  });

  it("keeps one family's refusal out of the other", () => {
    const profile = generationProfile({ reference: UNSUPPORTED_HOST });
    expect(admitGenerationProfile(profile, "t2va")).toEqual({
      status: "admitted",
    });
    expect(admitGenerationProfile(profile, "ref2va")).toMatchObject({
      reason: "unsupported_host",
    });
  });

  it.each([
    // reason | stops materialization | stops adoption | stops the queue after materializing
    ["missing_asset", MISSING_ASSET, false, false, false],
    ["template_drift", TEMPLATE_DRIFT, true, false, false],
    ["unsupported_host", UNSUPPORTED_HOST, true, true, false],
  ] as const)(
    "applies %s to the route it is actually about",
    (_label, override, stopsNew, stopsAdoption, stopsQueue) => {
      const admission = admitGenerationProfile(
        generationProfile({ text: override }),
        "t2va",
      );
      // Weight-name observations are advisory. Template and host capability
      // checks keep their route-specific structural refusals.
      expect(admissionBlocksRoute(admission, false)).toBe(stopsNew);
      expect(admissionBlocksRoute(admission, true)).toBe(stopsAdoption);
      expect(admissionBlocksQueue(admission, false)).toBe(stopsQueue);
      expect(admissionBlocksQueue(admission, true)).toBe(false);
    },
  );

  it("blocks nothing once the backend says available", () => {
    const admission = admitGenerationProfile(generationProfile(), "t2va");
    expect(admissionBlocksRoute(admission, false)).toBe(false);
    expect(admissionBlocksRoute(admission, true)).toBe(false);
    expect(admissionBlocksQueue(admission, false)).toBe(false);
  });

  it.each(["t2va", "i2va", "fl2va", "l2va", "ref2va"])(
    "keeps weight-name observations advisory for %s",
    (taskMode) => {
      for (const verdict of [MISSING_ASSET, ASSET_RELOCATED]) {
        const profile = generationProfile({
          text: verdict,
          image: verdict,
          reference: verdict,
        });
        const admission = admitGenerationProfile(profile, taskMode);
        expect(admissionBlocksRoute(admission, false)).toBe(false);
        expect(
          admissionBlocksQueue(admission, false, {
            unresolvedSlots: ["video_vae"],
          }),
        ).toBe(false);
      }
    },
  );
});

/**
 * M17-28. A host holding official weights under other names or other folders was
 * being told the weights were missing. Admission now accepts them, which makes a
 * second fact load-bearing: the materialized workflow still names the template's
 * own default, so a relocated host is neither "missing" nor "ready".
 */
describe("a relocated official weight is told apart from a missing one", () => {
  it("decodes the disposition and the roles whose widget must change", () => {
    const profile = generationProfile({ image: ASSET_RELOCATED });
    const family = familyProfileForTaskMode(profile, "i2va")!;
    expect(family.disposition).toBe("asset_relocated");
    expect(family.remediation).toBe("select_installed_asset_on_canvas");
    expect(family.unsatisfiedSlots).toEqual(["video_unet", "text_encoder"]);
  });

  it("refuses a relocated family that names no role", () => {
    expect(() =>
      decodeGenerationProfile(
        generationProfileWire({
          image: {
            disposition: "asset_relocated",
            remediation: "select_installed_asset_on_canvas",
            unsatisfied_slots: [],
          },
        }),
      ),
    ).toThrow();
  });

  it("refuses a relocated family carrying another disposition's remediation", () => {
    expect(() =>
      decodeGenerationProfile(
        generationProfileWire({
          image: {
            disposition: "asset_relocated",
            remediation: "upgrade_host",
            unsatisfied_slots: ["video_unet"],
          },
        }),
      ),
    ).toThrow();
  });

  it("lets both routes run, because the fix is on the canvas the route writes", () => {
    const admission = admitGenerationProfile(
      generationProfile({ image: ASSET_RELOCATED }),
      "i2va",
    );
    expect(admission.status).toBe("refused");
    expect(admissionBlocksRoute(admission, false)).toBe(false);
    expect(admissionBlocksRoute(admission, true)).toBe(false);
  });

  it("leaves queue-time weight validation to ComfyUI", () => {
    const admission = admitGenerationProfile(
      generationProfile({ image: ASSET_RELOCATED }),
      "i2va",
    );
    expect(admissionBlocksQueue(admission, false)).toBe(false);
    expect(admissionBlocksQueue(admission, true)).toBe(false);
  });

  it("leaves an unaffected basis available", () => {
    const profile = generationProfile({ image: ASSET_RELOCATED });
    expect(admitGenerationProfile(profile, "ref2va").status).toBe("admitted");
  });
});
