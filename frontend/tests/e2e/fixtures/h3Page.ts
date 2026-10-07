import { expect, test as base } from "@playwright/test";

import { evidenceCapture, type EvidenceCapture } from "../helpers/evidence";
import { graphLoader, type GraphLoader } from "../helpers/graphLoader";
import {
  hostDoubleControls,
  type HostDoubleControls,
} from "../helpers/hostEvents";
import { H3SidebarPage } from "../pageObjects/sidebar";

type H3Fixtures = Readonly<{
  h3: H3SidebarPage;
  host: HostDoubleControls;
  canvas: GraphLoader;
  evidence: EvidenceCapture;
}>;

export const test = base.extend<H3Fixtures>({
  h3: async ({ page }, use) => use(new H3SidebarPage(page)),
  host: [
    async ({ page }, use) => {
      const controls = hostDoubleControls(page);
      await controls.noise(true);
      await use(controls);
      if (!page.isClosed()) await controls.noise(false);
    },
    { auto: true },
  ],
  canvas: async ({ page }, use) => use(graphLoader(page)),
  evidence: async ({}, use, testInfo) => use(evidenceCapture(testInfo)),
});

export { expect };
export type { BrowserContext, Locator, Page, TestInfo } from "@playwright/test";
