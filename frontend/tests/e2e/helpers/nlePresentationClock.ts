import type { Page } from "@playwright/test";

export type PresentedConflictTiming = Readonly<{
  elapsedMs: number;
  trail: readonly string[];
}>;

/** Measure native activation to the refreshed conflict's DOM publication on one browser clock. */
export async function measurePresentedConflict(
  page: Page,
  options: Readonly<{
    controlSelector: string;
    statusSelector: string;
    expectedRevision: number;
    conflictText: string;
    timeoutMs?: number;
  }>,
  activate: () => Promise<void>,
): Promise<PresentedConflictTiming> {
  const clock = await page.evaluateHandle((input) => {
    let began: number | null = null;
    let ended = false;
    const trail: string[] = [];
    let finish: (value: { elapsedMs: number | null; trail: string[] }) => void;
    const result = new Promise<{ elapsedMs: number | null; trail: string[] }>(
      (resolve) => {
        finish = resolve;
      },
    );
    const complete = (elapsedMs: number | null) => {
      if (ended) return;
      ended = true;
      observer.disconnect();
      document.removeEventListener("click", onClick, true);
      clearTimeout(timer);
      finish({ elapsedMs, trail });
    };
    const observe = () => {
      if (began === null || ended) return;
      const status = document.querySelector(input.statusSelector);
      const text = status?.textContent ?? "";
      const revision = status?.getAttribute("data-h3-nle-timeline-revision");
      const phase = status?.getAttribute("data-h3-nle-authoring");
      const seen = `${status?.getAttribute("data-h3-nle-save-state")} r${revision} ${text}`;
      const elapsed = performance.now() - began;
      if (trail.length < 24 && !trail.some((entry) => entry.endsWith(seen)))
        trail.push(`${Math.round(elapsed)}ms ${seen}`);
      // CRITICAL: old conflict text can still be mounted when a new click starts. Only the
      // refreshed revision and conflict phase prove this transaction's presentation.
      if (
        revision === String(input.expectedRevision) &&
        phase === "conflict" &&
        text.includes(input.conflictText)
      )
        complete(elapsed);
    };
    const onClick = (event: MouseEvent) => {
      if (
        began === null &&
        event.target instanceof Element &&
        event.target.closest(input.controlSelector) !== null
      )
        began = performance.now();
      // Observe after React's handler, through mutations; sampling here would time the old DOM.
    };
    const observer = new MutationObserver(observe);
    observer.observe(document, {
      subtree: true,
      childList: true,
      characterData: true,
      attributes: true,
    });
    document.addEventListener("click", onClick, true);
    const timer = setTimeout(() => complete(null), input.timeoutMs ?? 30_000);
    return { read: () => result, dispose: () => complete(null) };
  }, options);
  try {
    // IMPORTANT: Playwright actionability checks and polling are outside this browser interval.
    await activate();
    const measured = await clock.evaluate((owned) => owned.read());
    if (measured.elapsedMs === null)
      throw new Error(
        "No activated refreshed conflict presentation was observed",
      );
    return { elapsedMs: measured.elapsedMs, trail: measured.trail };
  } finally {
    await clock.evaluate((owned) => owned.dispose());
    await clock.dispose();
  }
}
