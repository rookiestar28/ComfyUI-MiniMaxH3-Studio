import { expect, test } from "@playwright/test";

import { openIntegratedShell } from "../helpers/nleShell";
import { harnessBootDiagnostics } from "../helpers/harnessBoot";

for (const failure of ["module404", "evaluation"] as const) {
  test(`integrated harness classifies ${failure} before opening`, async ({
    page,
  }, info) => {
    test.setTimeout(12_000);
    await page.route("**/nleShell.tsx", async (route) => {
      await route.fulfill(
        failure === "module404"
          ? { status: 404, body: "" }
          : {
              contentType: "application/javascript",
              body: 'throw new TypeError("private-message-must-not-be-retained")',
            },
      );
    });
    const began = performance.now();
    await expect(openIntegratedShell(page, "smoke")).rejects.toThrow(
      failure === "module404"
        ? "harness boot failed: module-error"
        : "harness boot failed: window-error",
    );
    expect(performance.now() - began).toBeLessThan(5_000);
    await expect(page.locator("#root").getByRole("button")).toHaveCount(0);
    const boot = await page.evaluate(
      () =>
        (
          window as unknown as {
            __h3HarnessBoot: { phase: string; events: unknown[] };
          }
        ).__h3HarnessBoot,
    );
    expect(JSON.stringify(boot)).not.toContain("private-message");
    await info.attach("harness-boot-diagnostics", {
      contentType: "application/json",
      body: Buffer.from(JSON.stringify(await harnessBootDiagnostics(page))),
    });
  });
}

for (const failure of ["sentinel-missing", "incomplete"] as const) {
  test(`integrated harness refuses ${failure} before opening`, async ({
    page,
  }) => {
    test.setTimeout(12_000);
    if (failure === "sentinel-missing")
      await page.route("**/nleShell.html?**", (route) =>
        route.fulfill({
          contentType: "text/html",
          body: '<div id="root"></div>',
        }),
      );
    else
      await page.route("**/nleShell.tsx", (route) =>
        route.fulfill({
          contentType: "application/javascript",
          body: "// intentionally incomplete harness",
        }),
      );
    const began = performance.now();
    await expect(openIntegratedShell(page, "smoke")).rejects.toThrow(
      `harness boot failed: ${failure}`,
    );
    expect(performance.now() - began).toBeLessThan(5_000);
  });
}

test("healthy integrated harness proves boot before opening", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke");
  expect(
    await page.evaluate(
      () =>
        (window as unknown as { __h3HarnessBoot: { phase: string } })
          .__h3HarnessBoot.phase,
    ),
  ).toBe("booted");
  await expect(
    page.getByRole("button", { name: "Play", exact: true }),
  ).toBeEnabled();
});
