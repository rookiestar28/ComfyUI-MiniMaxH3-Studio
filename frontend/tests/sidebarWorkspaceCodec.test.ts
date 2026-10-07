import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { decodeSidebarWorkspaceProjection } from "../src/contracts/sidebarWorkspaceCodec";
import { validSidebarWorkspace } from "./sidebarWorkspaceFixture";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "..", "..");

interface MediaReceiptVector {
  readonly name: string;
  readonly producer_case: {
    readonly task_mode: "i2va";
    readonly reference_roles: readonly ["first_frame"];
  };
  readonly media_receipt: {
    readonly status: "verified";
    readonly queue_ready: true;
    readonly asset_count: 1;
    readonly image_count: 1;
    readonly video_count: 0;
    readonly audio_count: 0;
    readonly binding_count: 1;
  };
}

interface MediaReceiptVectorDocument {
  readonly schema: "h3-context-sidebar-media-receipt-vectors/1";
  readonly vectors: readonly MediaReceiptVector[];
}

const mediaReceiptVectors = JSON.parse(
  readFileSync(
    join(
      REPO_ROOT,
      "tests",
      "fixtures",
      "sidebar_workspace_media_receipt_vectors.json",
    ),
    "utf8",
  ),
) as MediaReceiptVectorDocument;

describe("decodeSidebarWorkspaceProjection", () => {
  it("keeps validated prompt export available when native composition is unqualified", () => {
    const held = decodeSidebarWorkspaceProjection({
      ...validSidebarWorkspace,
      media_receipt: {
        ...validSidebarWorkspace.media_receipt,
        queue_ready: false,
      },
    });
    expect(held.actions.export).toBe(true);
    expect(held.actions.copy_prompt).toBe(true);
    expect(held.media_receipt.queue_ready).toBe(false);
    expect(() =>
      decodeSidebarWorkspaceProjection({
        ...held,
        validation_status: "failed",
      }),
    ).toThrow();
    expect(() =>
      decodeSidebarWorkspaceProjection({ ...held, prompt_text_redacted: true }),
    ).toThrow();
  });
  it("accepts the shared image-only receipt wire vector", () => {
    expect(mediaReceiptVectors.schema).toBe(
      "h3-context-sidebar-media-receipt-vectors/1",
    );
    expect(mediaReceiptVectors.vectors).toHaveLength(1);
    const vector = mediaReceiptVectors.vectors[0];
    expect(vector.producer_case).toEqual({
      task_mode: "i2va",
      reference_roles: ["first_frame"],
    });

    expect(
      decodeSidebarWorkspaceProjection({
        ...validSidebarWorkspace,
        media_receipt: vector.media_receipt,
      }).media_receipt,
    ).toEqual(vector.media_receipt);
  });

  it("accepts the closed backend-owned five-stage projection", () => {
    expect(decodeSidebarWorkspaceProjection(validSidebarWorkspace)).toEqual(
      validSidebarWorkspace,
    );
  });

  it("decodes only explicit content-free guide readiness", () => {
    const incomplete = {
      ...validSidebarWorkspace,
      guide_conformance: {
        schema: "h3.context.guide_conformance.v2",
        readiness: "incomplete",
        reasons: ["fidelity.soundscape.unspecified"],
      },
    };
    expect(
      decodeSidebarWorkspaceProjection(incomplete).guide_conformance,
    ).toEqual(incomplete.guide_conformance);

    for (const guide_conformance of [
      {
        schema: "h3.context.guide_conformance.v2",
        readiness: "ready",
        reasons: ["fidelity.soundscape.unspecified"],
      },
      {
        schema: "h3.context.guide_conformance.v2",
        readiness: "unknown",
        reasons: [],
      },
      {
        schema: "h3.context.guide_conformance.v2",
        readiness: "incomplete",
        reasons: ["fidelity.unknown"],
      },
      {
        schema: "h3.context.guide_conformance.v1",
        readiness: "ready",
        reasons: [],
      },
    ]) {
      expect(() =>
        decodeSidebarWorkspaceProjection({
          ...validSidebarWorkspace,
          guide_conformance,
        }),
      ).toThrow(/guide|readiness|reason/i);
    }
  });

  it("accepts well-formed resource provenance without exact version pinning", () => {
    const changed = {
      ...validSidebarWorkspace,
      resources: {
        ...validSidebarWorkspace.resources,
        core_version: "0.33.0",
        frontend_version: "1.49.6",
      },
    };
    expect(decodeSidebarWorkspaceProjection(changed).resources).toEqual(
      changed.resources,
    );
    expect(() =>
      decodeSidebarWorkspaceProjection({
        ...changed,
        resources: { ...changed.resources, frontend_version: "latest" },
      }),
    ).toThrow(/resource profile/i);
    expect(() =>
      decodeSidebarWorkspaceProjection({
        ...changed,
        resources: {
          ...changed.resources,
          frontend_version: `${"9".repeat(65)}.0`,
        },
      }),
    ).toThrow(/resource/i);
  });

  it("accepts ordinary creative text while rejecting credential-shaped secrets", () => {
    expect(
      decodeSidebarWorkspaceProjection({
        ...validSidebarWorkspace,
        prompt_text: "A secret garden at dusk.",
      }).prompt_text,
    ).toBe("A secret garden at dusk.");
    for (const prompt_text of [
      "secret=hidden",
      "password: hidden",
      "https://private.invalid/path",
      "C:\\Users\\private\\clip.png",
    ]) {
      expect(() =>
        decodeSidebarWorkspaceProjection({
          ...validSidebarWorkspace,
          prompt_text,
        }),
      ).toThrow(/sensitive|invalid/i);
    }
  });

  it("rejects unknown, sensitive, contradictory and oversized state", () => {
    for (const value of [
      { ...validSidebarWorkspace, browser_truth: true },
      { ...validSidebarWorkspace, prompt_text: "token=hidden" },
      {
        ...validSidebarWorkspace,
        lifecycle: "ready",
        validation_status: "not_run",
      },
      {
        ...validSidebarWorkspace,
        prompt_text: "x".repeat(65_537),
      },
    ]) {
      expect(() => decodeSidebarWorkspaceProjection(value)).toThrow();
    }
  });

  it("rejects reordered stages and browser-invented Director candidates", () => {
    expect(() =>
      decodeSidebarWorkspaceProjection({
        ...validSidebarWorkspace,
        stages: [...validSidebarWorkspace.stages].reverse(),
      }),
    ).toThrow(/stage/i);
    expect(() =>
      decodeSidebarWorkspaceProjection({
        ...validSidebarWorkspace,
        reference_candidates: [
          ...validSidebarWorkspace.reference_candidates,
          {
            asset_id: "filename-derived",
            kind: "image",
            label: "<Picture 2>",
            ordinal: 2,
            paired_with: null,
          },
        ],
      }),
    ).toThrow(/candidate|binding/i);
  });

  it("accepts only bounded backend-derived subject candidates", () => {
    expect(
      decodeSidebarWorkspaceProjection(validSidebarWorkspace)
        .subject_candidates,
    ).toEqual([
      {
        subject_id: "subject_1",
        ordinal: 1,
        label: "<Subject 1>",
        display: "the baker",
      },
    ]);
    for (const subject_candidates of [
      [
        {
          subject_id: "subject_1",
          ordinal: 1,
          label: "<Subject 2>",
          display: "the baker",
        },
      ],
      [
        {
          subject_id: "subject_1",
          ordinal: 0,
          label: "<Subject 0>",
          display: "the baker",
        },
      ],
      [
        {
          subject_id: "subject_1",
          ordinal: 1,
          label: "<Subject 1>",
          display: "x".repeat(513),
        },
      ],
    ]) {
      expect(() =>
        decodeSidebarWorkspaceProjection({
          ...validSidebarWorkspace,
          subject_candidates,
        }),
      ).toThrow(/subject candidate/i);
    }
    expect(() =>
      decodeSidebarWorkspaceProjection({
        ...validSidebarWorkspace,
        task_mode: "t2va",
        profile: "h3_base",
      }),
    ).toThrow(/subject candidate.*profile/i);
    expect(() =>
      decodeSidebarWorkspaceProjection({
        ...validSidebarWorkspace,
        subject_candidates: [
          {
            subject_id: "subject_2",
            ordinal: 2,
            label: "<Subject 2>",
            display: "the bakery",
          },
          ...validSidebarWorkspace.subject_candidates,
        ],
      }),
    ).toThrow(/subject candidate.*order/i);
  });
});

it("refuses an old successful guide receipt even with a current workspace envelope", () => {
  const old = structuredClone(validSidebarWorkspace);
  const wire = old as unknown as Record<string, unknown>;
  wire.guide_conformance = {
    schema: "h3.context.guide_conformance.v1",
    readiness: "ready",
    reasons: [],
  };
  expect(() => decodeSidebarWorkspaceProjection(wire)).toThrow(
    "guide conformance schema",
  );
});
