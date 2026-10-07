import { expect, type Locator, type Page } from "@playwright/test";

class SidebarRegion {
  constructor(
    protected readonly page: Page,
    readonly region: Locator,
  ) {}

  async waitForVisible(): Promise<void> {
    await expect(this.region).toBeVisible();
  }
}

export class ContextForm extends SidebarRegion {
  async revisePrompt(value: string): Promise<void> {
    const input = this.region.getByRole("textbox", { name: /prompt/i });
    await input.fill(value);
    await expect(input).toHaveValue(value);
  }
}

export class ReferencePicker extends SidebarRegion {
  async selectReference(name: string): Promise<void> {
    const control = this.region.getByRole("button", { name });
    await control.click();
    await expect(control).toHaveAttribute("aria-pressed", "true");
  }
}

export class GenerationPanel extends SidebarRegion {
  async waitForPhase(name: string): Promise<void> {
    await expect(this.region.getByText(name, { exact: true })).toBeVisible();
  }
}

export class ProductionWorkbench extends SidebarRegion {
  async selectSegment(name: string): Promise<void> {
    const control = this.region.getByRole("button", { name });
    await control.click();
    await expect(control).toHaveAttribute("aria-pressed", "true");
  }
}

export class SettingsPanel extends SidebarRegion {
  async selectLanguage(value: "en" | "zh-TW" | "zh-CN"): Promise<void> {
    const language = this.region.getByLabel(/language|語言|语言/i);
    await language.selectOption(value);
    await expect(language).toHaveValue(value);
  }
}

export class ErrorPanel extends SidebarRegion {
  async waitForAlert(text: string | RegExp): Promise<void> {
    await expect(this.region.getByRole("alert")).toContainText(text);
  }
}

export class H3SidebarPage {
  readonly navigation: Locator;
  readonly context: ContextForm;
  readonly references: ReferencePicker;
  readonly generation: GenerationPanel;
  readonly production: ProductionWorkbench;
  readonly settings: SettingsPanel;
  readonly errors: ErrorPanel;

  constructor(readonly page: Page) {
    this.navigation = page.getByRole("navigation", { name: /H3 Context/i });
    this.context = new ContextForm(
      page,
      page.getByRole("main").or(page.locator("#h3-context-e2e-container")),
    );
    this.references = new ReferencePicker(page, page.locator(".h3r").first());
    this.generation = new GenerationPanel(page, page.locator(".h3g").first());
    this.production = new ProductionWorkbench(
      page,
      page.locator("#production-sidebar-container"),
    );
    this.settings = new SettingsPanel(page, page.locator(".h3s").first());
    this.errors = new ErrorPanel(page, page.locator(".h3e").first());
  }

  async openPage(name: string): Promise<void> {
    const button = this.navigation.getByRole("button", { name });
    await button.click();
    await expect(button).toHaveAttribute("aria-current", "page");
  }
}
