import { expect, type Locator, type Page } from "@playwright/test";

export class WorkflowTabs {
  readonly tabs: Locator;

  constructor(private readonly page: Page) {
    this.tabs = page.locator('[role="tablist"] [role="tab"]');
  }

  async open(name: string): Promise<void> {
    const tab = this.page.getByRole("tab", { name });
    await tab.click();
    await expect(tab).toHaveAttribute("aria-selected", "true");
  }

  async count(): Promise<number> {
    return this.tabs.count();
  }
}

export class Toasts {
  constructor(private readonly page: Page) {}

  async waitFor(text: string | RegExp): Promise<void> {
    await expect(
      this.page.getByRole("alert").filter({ hasText: text }),
    ).toBeVisible();
  }
}

export class QueueSurface {
  constructor(private readonly page: Page) {}

  async count(): Promise<number> {
    return this.page.evaluate(
      () =>
        (
          window as unknown as {
            __H3_CONTEXT_E2E_QUEUE_COUNT__?: number;
          }
        ).__H3_CONTEXT_E2E_QUEUE_COUNT__ ?? 0,
    );
  }
}
