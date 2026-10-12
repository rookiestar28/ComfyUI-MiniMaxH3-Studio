import { test, type Page } from "@playwright/test";

type BootEvent = { kind: string; name: string; module: string | null };
type Boot = { phase: "html" | "booted"; events: BootEvent[] };
type Facts = {
  pageErrors: number;
  requestFailures: number;
  mainFrameNavigations: number;
  crashes: number;
  responses: { module: string; status: number }[];
};
const observed = new WeakMap<Page, Facts>();

function moduleLabel(url: URL): string | null {
  if (url.pathname === "/nleShell.tsx") return "nleShell.tsx";
  const relative = url.pathname.split("/frontend/src/")[1];
  return relative && /^[a-zA-Z0-9_/-]+\.[cm]?[jt]sx?$/.test(relative)
    ? `src/${relative}`
    : null;
}

export function observeHarnessBoot(page: Page): void {
  if (observed.has(page)) return;
  const facts: Facts = {
    pageErrors: 0,
    requestFailures: 0,
    mainFrameNavigations: 0,
    crashes: 0,
    responses: [],
  };
  observed.set(page, facts);
  const increment = (key: Exclude<keyof Facts, "responses">) => {
    facts[key] = Math.min(1_000_000, facts[key] + 1);
  };
  page.on("pageerror", () => increment("pageErrors"));
  page.on("requestfailed", () => increment("requestFailures"));
  page.on("crash", () => increment("crashes"));
  page.on("framenavigated", (frame) => {
    if (frame === page.mainFrame()) increment("mainFrameNavigations");
  });
  page.on("response", (response) => {
    const url = new URL(response.url());
    const module = moduleLabel(url);
    if (
      response.status() >= 400 &&
      url.origin === new URL(page.url()).origin &&
      module !== null &&
      facts.responses.length < 32
    )
      facts.responses.push({ module, status: response.status() });
  });
}

export async function requireHarnessBoot(page: Page): Promise<void> {
  // CRITICAL: the module graph can fail while load succeeds. Classify boot before
  // invoking fixtures or clicking a button that the failed module never created.
  const read = () =>
    page.evaluate(
      () => (window as Window & { __h3HarnessBoot?: Boot }).__h3HarnessBoot,
    );
  try {
    await page.waitForFunction(
      () => {
        const boot = (window as Window & { __h3HarnessBoot?: Boot })
          .__h3HarnessBoot;
        return boot?.phase === "booted" || (boot?.events.length ?? 0) > 0;
      },
      undefined,
      { timeout: 4_000 },
    );
  } catch {
    const boot = await read().catch(() => undefined);
    throw new Error(
      `harness boot failed: ${boot === undefined ? "sentinel-missing" : "incomplete"}`,
    );
  }
  const boot = await read();
  if (boot?.phase !== "booted") {
    const kind = boot?.events[0]?.kind;
    throw new Error(
      `harness boot failed: ${["module-error", "window-error", "unhandled-rejection"].includes(kind ?? "") ? kind : "unknown"}`,
    );
  }
}

export async function harnessBootDiagnostics(page: Page) {
  const facts = observed.get(page);
  if (!facts) return undefined;
  const boot = await page
    .evaluate(
      () => (window as Window & { __h3HarnessBoot?: Boot }).__h3HarnessBoot,
    )
    .catch(() => undefined);
  return {
    ...facts,
    phase: boot?.phase ?? "absent",
    events: boot?.events ?? [],
  };
}

test.afterEach(async ({ page }, info) => {
  if (info.status === info.expectedStatus) return;
  const facts = await harnessBootDiagnostics(page);
  if (!facts) return;
  await info.attach("harness-boot-diagnostics", {
    contentType: "application/json",
    body: Buffer.from(JSON.stringify(facts)),
  });
});
