import { describe, expect, it } from "vitest";

import { decodeGenerationSequenceProjection } from "../src/contracts/generationSequenceCodec";
import { generationSequenceWire } from "./generationSequenceFixture";

describe("generation sequence projection codec", () => {
  it("accepts the closed content-free projection", () => {
    const projection = decodeGenerationSequenceProjection(
      generationSequenceWire(),
    );
    expect(projection.eligible_commands[0]).toMatchObject({
      job_id: "job.1",
      attempt: 1,
      transaction_id: "generation.command.1",
    });
    expect(projection.progress[0]?.state).toBe("planned");
  });

  it("rejects extensions, malformed fingerprints, and duplicate identities", () => {
    expect(() =>
      decodeGenerationSequenceProjection({
        ...generationSequenceWire(),
        prompt_text: "private",
      }),
    ).toThrow();
    const malformed = generationSequenceWire();
    malformed.eligible_commands[0]!.graph_fingerprint = "foreign";
    expect(() => decodeGenerationSequenceProjection(malformed)).toThrow();
    const duplicate = generationSequenceWire();
    duplicate.progress.push({ ...duplicate.progress[0]! });
    expect(() => decodeGenerationSequenceProjection(duplicate)).toThrow();
  });
});

describe("M17-20 D6 fingerprint domain", () => {
  it("refuses a command whose graph identity declares no domain", () => {
    // The domain is what tells the shell which graph the two fingerprints were
    // taken over. A command without one is a planner this build does not
    // recognise, and guessing would reproduce exactly the ambiguity D6 removes.
    const wire = generationSequenceWire();
    const command = wire.eligible_commands[0] as Record<string, unknown>;
    delete command.fingerprint_domain;
    expect(() => decodeGenerationSequenceProjection(wire)).toThrow();
  });

  it("refuses a domain outside the declared vocabulary", () => {
    const wire = generationSequenceWire();
    (wire.eligible_commands[0] as Record<string, unknown>).fingerprint_domain =
      "whole_workflow";
    expect(() => decodeGenerationSequenceProjection(wire)).toThrow();
  });

  it("accepts the pre-widening domain, so a mixed fleet decodes", () => {
    // Decoding is not admission: an older planner's command must still be
    // readable, because refusing to parse it would hide the very mismatch the
    // sequence authority exists to report.
    const wire = generationSequenceWire();
    (wire.eligible_commands[0] as Record<string, unknown>).fingerprint_domain =
      "context_subgraph";
    expect(
      decodeGenerationSequenceProjection(wire).eligible_commands[0]
        ?.fingerprint_domain,
    ).toBe("context_subgraph");
  });
});
