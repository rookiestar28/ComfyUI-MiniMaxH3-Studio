import { describe, expect, it } from "vitest";

import {
  canonicalStringFingerprint,
  sha256Text,
} from "../src/contracts/canonicalFingerprint";

describe("canonical browser fingerprints", () => {
  it("matches the backend canonical SHA-256 bytes for prompt strings", () => {
    expect(canonicalStringFingerprint("hello")).toBe(
      "sha256:5aa762ae383fbb727af3c7a36d4940a5b8c40a989452d2304fc958ff3f354e7a",
    );
    expect(
      canonicalStringFingerprint("Subject: A safe prompt with <Picture 1>."),
    ).toBe(
      "sha256:a5102d76597965aececb57a5520d23ef29bc7bf884579716921ae9a663ac536a",
    );
  });

  it("hashes arbitrary bounded correlation text without shortening it", () => {
    expect(sha256Text("hello")).toBe(
      "sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
    );
  });
});
