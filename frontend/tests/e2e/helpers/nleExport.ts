// M25-44: the final-video render card lives in the full editor's chrome-bar Export popover. The
// popover's content stays mounted while it is closed (a running render keeps its progress), so an
// attribute check still finds its hooks, but a role, label or visibility query needs it open.

import { expect, type Locator, type Page } from "@playwright/test";

export const EXPORT_BUTTON = '[data-h3-nle-action="export"]';
export const EXPORT_POPOVER = '[data-h3-nle-popover="export"]';

/** Opens the Export popover of the open overlay inside `scope`; a no-op when it is already open. */
export async function openExportPanel(scope: Page | Locator): Promise<void> {
  const button = scope.locator(EXPORT_BUTTON);
  await expect(button).toBeVisible();
  if ((await button.getAttribute("aria-expanded")) !== "true")
    await button.click();
  await expect(button).toHaveAttribute("aria-expanded", "true");
  await expect(scope.locator(EXPORT_POPOVER)).toBeVisible();
}
