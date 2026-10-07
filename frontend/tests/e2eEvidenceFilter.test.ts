// The E2E evidence guard decides what may be written beside a Playwright run. It has to refuse a
// real private path and it has to accept an honest explanation, and the second half is not
// cosmetic: a row whose evidence is refused cannot be recorded at all, so a false positive silently
// removes a row from the corpus rather than making anything safer.

import { describe, expect, it } from "vitest";

import { isPrivacyUnsafeEvidence } from "./e2e/helpers/evidence";

const body = (value: unknown): string => JSON.stringify(value, null, 2);

describe("E2E evidence privacy guard", () => {
  it("refuses a Windows path, a UNC share and a remote URL", () => {
    expect(
      isPrivacyUnsafeEvidence(body({ path: "C:\\Users\\somebody\\clip.mp4" })),
    ).toBe(true);
    expect(
      isPrivacyUnsafeEvidence(body({ path: "\\\\host\\share\\clip.mp4" })),
    ).toBe(true);
    expect(
      isPrivacyUnsafeEvidence(body({ url: "https://example.test/x" })),
    ).toBe(true);
  });

  it("refuses the named secret-bearing keys wherever they appear", () => {
    for (const word of [
      "credential",
      "cookie",
      "prompt_text",
      "media_bytes",
      "signed_url",
    ])
      expect(
        isPrivacyUnsafeEvidence(body({ note: `a ${word} appears here` })),
      ).toBe(true);
  });

  it("accepts an explanation that puts a colon before a line break", () => {
    // Playwright's own error text reads `Call Log:\n...`, and JSON-escaping it leaves `g:` followed
    // by a single backslash. Matching that as a drive letter refused two honest corpus rows.
    expect(
      isPrivacyUnsafeEvidence(
        body({ missing: "waiting failed. Call Log:\nlocator" }),
      ),
    ).toBe(false);
    expect(
      isPrivacyUnsafeEvidence(
        body({ missing: "text: no DOM mirror exists:\nfillText only" }),
      ),
    ).toBe(false);
  });

  it("accepts a repository-relative path and the loopback host the harness uses", () => {
    expect(
      isPrivacyUnsafeEvidence(
        body({ file: "frontend/src/runtime/visualCompositor.ts" }),
      ),
    ).toBe(false);
    expect(
      isPrivacyUnsafeEvidence(body({ url: "http://127.0.0.1:5173/" })),
    ).toBe(false);
  });
});
