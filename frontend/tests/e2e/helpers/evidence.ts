import type { TestInfo } from "@playwright/test";

// GUARD: this pattern is matched against JSON, not against the original strings, and in JSON every
// literal backslash is already doubled. So a Windows path `C:\Users` reaches here as `C:\\Users`
// and needs *two* backslash characters to match, while an ordinary sentence that happens to put a
// colon before a line break -- `Call Log:\n`, which is Playwright's own error text -- reaches here
// as `g:` followed by a single backslash. Matching one backslash therefore rejected honest evidence
// for any row whose explanation quoted a browser error, twice, and the row could not be recorded at
// all. Never relax this back to a single backslash to "catch more": it catches escape sequences,
// not paths. The UNC branch is here for the same reason in reverse -- `\\host\share` never had a
// drive letter and the drive-letter branch has always missed it.
const FORBIDDEN_EVIDENCE =
  /(?:https?:\/\/(?!127\.0\.0\.1)|[A-Z]:\\\\|\\\\\\\\[A-Z0-9]|credential|cookie|prompt_text|media_bytes|signed_url)/i;

/**
 * Whether a serialized evidence body carries something that must never leave the machine.
 *
 * Exported so the rule itself can be tested in both directions -- a real path is refused, an honest
 * multi-line explanation is not -- without standing up a browser.
 */
export function isPrivacyUnsafeEvidence(body: string): boolean {
  return FORBIDDEN_EVIDENCE.test(body);
}

export type EvidenceCapture = Readonly<{
  attach(name: string, value: Readonly<Record<string, unknown>>): Promise<void>;
}>;

export function evidenceCapture(testInfo: TestInfo): EvidenceCapture {
  return Object.freeze({
    async attach(name, value) {
      const body = JSON.stringify(value, null, 2);
      if (isPrivacyUnsafeEvidence(body))
        throw new Error(`privacy-unsafe E2E evidence rejected: ${name}`);
      await testInfo.attach(name, {
        body: Buffer.from(body, "utf8"),
        contentType: "application/json",
      });
    },
  });
}
