import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const consumers = [
  ["integratedEditorAcceptance", "processAudioObserverSha256"],
  ["m25_56AudioObserver", "processAudioObserverSha256"],
  ["m25_77ClipAudioParity", "processAudioObserverSha256"],
  ["nleHardeningActions", "PROCESS_AUDIO_OBSERVER_SHA256"],
] as const;

function reviewedPin(source: string, name: string): string {
  const assignments = source.match(new RegExp(`\\bconst\\s+${name}\\s*=`, "g"));
  if (assignments?.length !== 1) throw new Error("observer pin must be unique");
  const literal = source.match(
    new RegExp(
      `\\bconst\\s+${name}\\s*=\\s*(?:(?://[^\\n]*\\n|/\\*[\\s\\S]*?\\*/)\\s*)*(["'])([a-f0-9]{64})\\1\\s*;`,
    ),
  );
  if (!literal)
    throw new Error("observer admission must use a reviewed literal pin");
  return literal[2]!;
}

describe("native audio observer identity", () => {
  const bytes = readFileSync(
    resolve(process.cwd(), "../scripts/process_audio_observer.py"),
  );
  const digest = createHash("sha256").update(bytes).digest("hex");

  it.each(consumers)(
    "%s admits only the reviewed observer bytes",
    (name, variable) => {
      const source = readFileSync(
        resolve(process.cwd(), `tests/e2e/journeys/${name}.spec.ts`),
        "utf8",
      );
      expect(reviewedPin(source, variable)).toBe(digest);
    },
  );

  it("keeps observer bytes stable across Windows and Linux checkouts", () => {
    expect(bytes.includes(Buffer.from("\r\n"))).toBe(false);
  });

  it("does not accept a computed or ambiguous replacement for a reviewed pin", () => {
    expect(() => reviewedPin('const pin = "ab" + "cd";', "pin")).toThrow(
      "reviewed literal pin",
    );
    expect(() =>
      reviewedPin('const pin = "a"; const pin = "b";', "pin"),
    ).toThrow("unique");
    const changed = createHash("sha256")
      .update(Buffer.concat([bytes, Buffer.from("\n")]))
      .digest("hex");
    expect(changed).not.toBe(digest);
    expect(reviewedPin(`const pin = "${digest}";`, "pin")).not.toBe(changed);
  });
});
