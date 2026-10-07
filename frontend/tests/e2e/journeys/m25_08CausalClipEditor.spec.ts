import { expect, test } from "../fixtures/h3Page";

// M25-08's causal Clip editor journey, re-aimed by M25-44 (one NLE): the compact editor whose
// selected-VIDEO preview, playhead and per-clip edits this case used to drive is no longer mounted.
// Those behaviours are proven on the full editor (`nleReferenceShell`, `nleWorkspace`,
// `nleCommandMatrix`). What stays here is the causal lifecycle the Clip editor function still
// owns: manual tab activation, an explicit start, no audio or pairing surface, a page round trip
// that keeps the function, a labelled release -- and never a generation request.

const SUMMARY = { name: "Clip editor project", exact: true } as const;

test("the Clip editor function starts, keeps and releases the workspace without an editing surface or a generation request", async ({
  page,
}) => {
  const forbiddenGenerationRequests: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (
      request.method() === "POST" &&
      (path === "/prompt" ||
        path === "/queue" ||
        path.startsWith("/api/prompt"))
    ) {
      forbiddenGenerationRequests.push(path);
    }
  });

  await page.goto("/?mode=production");
  const navigation = page.getByRole("navigation", { name: "H3 Context pages" });
  await expect(navigation).toHaveCount(1);
  expect(
    await navigation
      .locator("button[data-page-id]")
      .evaluateAll((buttons) =>
        buttons.map((button) => button.getAttribute("data-page-id")),
      ),
  ).toEqual(["context", "production", "settings"]);
  await navigation.getByRole("button", { name: "Production" }).click();

  const tablist = page.getByRole("tablist", { name: "Production functions" });
  expect(
    await tablist
      .locator("[role=tab]")
      .evaluateAll((tabs) =>
        tabs.map((tab) => tab.getAttribute("data-h3-director-function")),
      ),
  ).toEqual(["production_workbench", "clip_editor"]);
  const production = tablist.getByRole("tab", { name: "Production" });
  const clipEditor = tablist.getByRole("tab", { name: "Clip editor" });
  await expect(production).toHaveAttribute("aria-selected", "true");
  await production.focus();
  await page.keyboard.press("ArrowRight");
  await expect(clipEditor).toBeFocused();
  await expect(production).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("region", SUMMARY)).toHaveCount(0);
  await expect(page.locator("#authoring-action-count")).toHaveText("0");
  await page.keyboard.press("Enter");
  await expect(clipEditor).toHaveAttribute("aria-selected", "true");
  await expect(page.locator("[data-h3-director-panel]")).toHaveCount(1);

  const summary = page.getByRole("region", SUMMARY);
  await summary
    .getByRole("button", { name: "Start authoring from this context" })
    .click();
  await expect(summary.getByRole("status")).toHaveText(
    "Authoring workspace ready. Open the full editor to load its timeline.",
  );
  await expect(page.locator("#authoring-action-count")).toHaveText("1");
  await expect(page.locator("#authoring-last-action")).toHaveText(
    "create_authoring_workspace",
  );

  // This harness host has no full-editor capability: nothing opens a dialog, and the tab carries
  // no editing, preview, audio or pairing surface in its place.
  await expect(
    page.getByRole("dialog", { name: "Clip editor workspace" }),
  ).toHaveCount(0);
  await expect(page.locator("section.h3a")).toHaveCount(0);
  await expect(page.locator("video")).toHaveCount(0);
  await expect(page.locator("[data-kind=audio]")).toHaveCount(0);
  await expect(summary.getByText(/soundtrack/i)).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: /pair|unlink|volume|mute/i }),
  ).toHaveCount(0);
  await expect(page.locator("#authoring-preview-count")).toHaveText("0");

  const beforeNoAudioDispatch = await page
    .locator("#authoring-action-count")
    .textContent();
  await summary.dispatchEvent("contextmenu");
  await summary.dispatchEvent("dragover");
  await summary.dispatchEvent("drop");
  await expect(page.locator("#authoring-action-count")).toHaveText(
    beforeNoAudioDispatch ?? "1",
  );

  await navigation.getByRole("button", { name: "Context" }).click();
  await expect(page.getByRole("region", SUMMARY)).toHaveCount(0);
  await navigation.getByRole("button", { name: "Production" }).click();
  await expect(page.getByRole("tab", { name: "Clip editor" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  const remounted = page.getByRole("region", SUMMARY);
  await expect(remounted.getByRole("status")).toHaveText(
    "Authoring workspace ready. Open the full editor to load its timeline.",
  );
  await remounted.getByRole("button", { name: "Release workspace" }).click();
  await remounted.getByRole("button", { name: "Confirm release" }).click();
  await expect(remounted.getByRole("status")).toHaveText(
    "The authoring workspace was released.",
  );
  await expect(page.locator("#authoring-action-count")).toHaveText("2");
  expect(forbiddenGenerationRequests).toEqual([]);
});
