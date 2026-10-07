import { expect, test } from "../fixtures/h3Page";
import { mkdirSync } from "node:fs";
import { resolve, sep } from "node:path";
import { providerCopy } from "../../../src/i18n/catalog";
import {
  installPinnedHostPalette,
  worstContrastWithin,
} from "../helpers/contrast";

/**
 * M22-06 — provider settings in a real layout engine.
 *
 * Every assertion here is about a transition: a control is clicked and the
 * surface says something different afterwards. Asserting that an element exists
 * would pass just as well against a page whose controls do nothing, which is
 * exactly the failure this item is supposed to make impossible.
 */

test.beforeEach(async ({ page }) => {
  await page.goto("/?mode=production");
});

const SETTINGS_TAB = /^(?:Settings|設定|设置)$/;
const LANGUAGE_FIELD = /^(?:Language|語言|语言)$/;
const PROVIDER_FIELD = /^(?:Provider profile|供應商設定檔|供应商配置文件)$/;
const MODEL_FIELD = /^(?:Exact model|確切模型|确切模型)$/;
const RECHECK =
  /^(?:Reload available models|重新載入可用模型|重新加载可用模型)$/;
const KEY_SUBMIT =
  /^(?:Allow text-only requests and reload models|允許文字傳輸並載入模型|允许文本传输并加载模型)$/;
const LOCALES = ["en", "zh-TW", "zh-CN"] as const;
const PRODUCT_FLOOR = 704;

function providerCaptureDirectory(): string | undefined {
  const value = process.env.H3_PROVIDER_CAPTURE_DIR;
  if (value === undefined) return undefined;
  const planning = resolve(process.cwd(), "..", ".planning") + sep;
  const directory = resolve(value);
  if (!directory.startsWith(planning))
    throw new Error(
      "provider capture directory is outside private workspace evidence",
    );
  mkdirSync(directory, { recursive: true });
  return directory;
}

async function openSettings(page: import("@playwright/test").Page) {
  await page.getByRole("button", { name: SETTINGS_TAB }).click();
  await expect(page.locator(".h3s-pv")).toBeVisible();
}

async function setSidebarWidth(
  page: import("@playwright/test").Page,
  width: number,
): Promise<void> {
  await page.setViewportSize({ width: width + 360, height: 1200 });
  await page
    .locator("#production-sidebar-container")
    .evaluate((node, value) => {
      (node as HTMLElement).style.width = `${value}px`;
      (node as HTMLElement).style.maxWidth = "none";
    }, width);
}

async function setLocale(
  page: import("@playwright/test").Page,
  locale: string,
): Promise<void> {
  await page.getByRole("button", { name: SETTINGS_TAB }).click();
  await page.getByLabel(LANGUAGE_FIELD).selectOption(locale);
  await expect(page.locator("section.h3c")).toHaveAttribute("lang", locale);
}

async function selectProfile(
  page: import("@playwright/test").Page,
  profileId: string,
): Promise<void> {
  await page.getByLabel(PROVIDER_FIELD).selectOption(profileId);
}

async function refreshModels(
  page: import("@playwright/test").Page,
): Promise<void> {
  const state = page.locator(".h3s-pv-state");
  await state.evaluate((node) => {
    const observed = window as typeof window & {
      __h3ProviderBusyObserved?: boolean;
      __h3ProviderBusyObserver?: MutationObserver;
    };
    observed.__h3ProviderBusyObserver?.disconnect();
    observed.__h3ProviderBusyObserved =
      node.getAttribute("aria-busy") === "true";
    const observer = new MutationObserver(() => {
      if (node.getAttribute("aria-busy") !== "true") return;
      observed.__h3ProviderBusyObserved = true;
      observer.disconnect();
    });
    observed.__h3ProviderBusyObserver = observer;
    observer.observe(node, {
      attributes: true,
      attributeFilter: ["aria-busy"],
    });
  });
  // IMPORTANT: install the browser observer before clicking; the bounded busy
  // state can finish before a loaded Windows Playwright driver starts polling.
  await page.getByRole("button", { name: RECHECK }).click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as typeof window & { __h3ProviderBusyObserved?: boolean })
            .__h3ProviderBusyObserved ?? false,
      ),
    )
    .toBe(true);
  await expect(state).toHaveAttribute("aria-busy", "false");
}

async function selectExactModel(
  page: import("@playwright/test").Page,
  modelId: string,
): Promise<void> {
  await page.getByLabel(MODEL_FIELD).selectOption(modelId);
  await expect(page.locator(".h3s-pv-state")).toHaveAttribute(
    "aria-busy",
    "false",
  );
}

async function configureRemote(
  page: import("@playwright/test").Page,
): Promise<void> {
  await selectProfile(page, "remote.example.gpt");
  await page
    .getByLabel(/^(?:API key|API 金鑰|API 密钥)$/)
    .fill("test-credential-secret-value");
  await page.getByRole("button", { name: KEY_SUBMIT }).click();
  await expect(page.getByLabel(MODEL_FIELD).locator("option")).toHaveCount(2);
  await selectExactModel(page, "gpt-4o-mini");
  await expect(
    page.locator(".h3s-pv-consent [data-consent-status]"),
  ).toHaveAttribute("data-consent-status", "granted");
  await expect(page.locator(".h3s-pv-state")).toHaveAttribute(
    "data-readiness",
    "ready",
  );
}

test("selecting a remote profile reveals its disclosure and its consent gate", async ({
  page,
}) => {
  await openSettings(page);
  await expect(page.locator(".h3s-pv-disclosure")).toHaveCount(0);
  await expect(page.locator(".h3s-pv-consent")).toHaveCount(0);

  await selectProfile(page, "remote.example.gpt");

  const disclosure = page.locator(".h3s-pv-disclosure");
  await expect(disclosure).toBeVisible();
  await expect(disclosure).toContainText("internet");
  await expect(disclosure).toContainText("remote_upload");
  await expect(page.locator(".h3s-pv-consent")).toBeVisible();
  // The disclosure is above the consent controls: the facts are readable before
  // the decision is offered, not after it.
  const order = await page.evaluate(() => {
    const first = document.querySelector(".h3s-pv-disclosure");
    const second = document.querySelector(".h3s-pv-consent");
    if (first === null || second === null) return -1;
    return first.compareDocumentPosition(second) &
      Node.DOCUMENT_POSITION_FOLLOWING
      ? 1
      : 0;
  });
  expect(order).toBe(1);
});

test("the qualified native Anthropic profile remains behind remote consent", async ({
  page,
}) => {
  await openSettings(page);
  await selectProfile(page, "anthropic.claude_sonnet_4_6.remote");

  const section = page.locator(".h3s-pv");
  await expect(section).toContainText("Anthropic");
  await expect(page.getByLabel(MODEL_FIELD)).toHaveValue("");
  // B-M1605-COPY-01: the internal qualification state is not product copy.
  await expect(section).not.toContainText("Qualification");
  await expect(section).not.toContainText("qualified");
  // Disclosures remain finite without historical price fields.
  await expect(section).not.toContainText("NaN");
  await expect(section).toContainText("provider_policy");
  await expect(page.locator(".h3s-pv-disclosure")).toContainText(
    "remote_upload",
  );
  await expect(page.locator(".h3s-pv-consent")).toBeVisible();
  await expect(page.locator(".h3s-pv-credential")).toBeVisible();
  await expect(page.locator(".h3s-pv-state")).toHaveAttribute(
    "data-observed",
    "false",
  );
});

test("a local profile says so, and offers neither consent nor a credential", async ({
  page,
}) => {
  await openSettings(page);
  await selectProfile(page, "ollama.local.qwen");
  const disclosure = page.locator(".h3s-pv-disclosure");
  await expect(disclosure).toContainText("loopback_http");
  await expect(disclosure).toContainText(
    "Requests stay on this machine. Nothing is sent to a third party.",
  );
  await expect(page.locator(".h3s-pv-consent")).toHaveCount(0);
  await expect(page.locator(".h3s-pv-credential")).toHaveCount(0);
  await expect(page.getByLabel(MODEL_FIELD)).toHaveValue("");
  await expect(page.getByLabel(MODEL_FIELD).locator("option")).toHaveCount(1);
  await refreshModels(page);
  await selectExactModel(page, "qwen3:8b");
  await expect(page.locator(".h3s-pv-state")).toHaveAttribute(
    "data-readiness",
    "ready",
  );
});

test("a remote census stays closed until credential and consent are both present", async ({
  page,
}) => {
  await openSettings(page);
  await selectProfile(page, "remote.example.gpt");
  const models = page.getByLabel(MODEL_FIELD).locator("option");

  await expect(page.getByRole("button", { name: KEY_SUBMIT })).toBeDisabled();
  await expect(models).toHaveCount(1);
  await expect(page.locator(".h3s-pv-state")).toHaveAttribute(
    "data-observed",
    "false",
  );

  await page
    .getByLabel(/^(?:API key|API 金鑰|API 密钥)$/)
    .fill("test-credential-gated-session");
  await page.getByRole("button", { name: KEY_SUBMIT }).click();
  await expect(models).toHaveCount(2);
  await expect(
    page.locator(".h3s-pv-consent [data-consent-status]"),
  ).toHaveAttribute("data-consent-status", "granted");
  await expect(page.getByLabel(MODEL_FIELD)).toHaveValue("");
});

test("readiness moves only when the provider's own conditions are met", async ({
  page,
}) => {
  await openSettings(page);
  await selectProfile(page, "remote.example.gpt");
  const state = page.locator(".h3s-pv-state");
  await expect(state).toHaveAttribute("data-readiness", "not_configured");

  await page
    .getByLabel(/^(?:API key|API 金鑰|API 密钥)$/)
    .fill("test-credential-secret-value");
  await page.getByRole("button", { name: KEY_SUBMIT }).click();
  // One disclosed gesture connects and lists, but does not choose a model.
  await expect(page.getByLabel(MODEL_FIELD).locator("option")).toHaveCount(2);
  await expect(state).toHaveAttribute("data-readiness", "not_configured");
  await selectExactModel(page, "gpt-4o-mini");
  // Selection automatically checks the chosen model under the same connection grant.
  await expect(state).toHaveAttribute("data-readiness", "ready");
  await expect(
    page.locator(".h3s-pv-consent [data-consent-status]"),
  ).toHaveAttribute("data-consent-status", "granted");
});

test("withdrawing consent takes readiness back and is visible immediately", async ({
  page,
}) => {
  await openSettings(page);
  await configureRemote(page);

  await page
    .getByRole("button", { name: /^(?:Withdraw consent|撤回同意)$/ })
    .click();
  await expect(
    page.locator(".h3s-pv-consent [data-consent-status]"),
  ).toHaveAttribute("data-consent-status", "denied");
  await expect(page.locator(".h3s-pv-state")).toHaveAttribute(
    "data-readiness",
    "not_configured",
  );
});

test("the credential is never rendered, and the field empties as it is sent", async ({
  page,
}) => {
  await openSettings(page);
  await selectProfile(page, "remote.example.gpt");
  const field = page.getByLabel(/^(?:API key|API 金鑰|API 密钥)$/);
  await expect(field).toHaveAttribute("type", "password");
  await field.fill("test-credential-secret-value");
  await page.getByRole("button", { name: KEY_SUBMIT }).click();
  await expect(field).toHaveValue("");
  await expect(page.locator(".h3s-pv-credential")).toContainText(
    "Held for this browser session.",
  );
  const rendered = await page.locator(".h3s-pv").innerText();
  expect(rendered).not.toContain("test-credential-secret-value");
  expect(rendered).not.toContain("alue");
  expect(rendered).toContain("Held for this browser session.");
  expect(rendered).not.toContain("Allow uploading attached media");
});

test("only an explicit refresh exposes an exact model and no passive probe occurs", async ({
  page,
}) => {
  await openSettings(page);
  await expect(page.locator("#provider-revision")).toHaveText("1");
  await page.waitForTimeout(150);
  await expect(page.locator("#provider-revision")).toHaveText("1");

  await selectProfile(page, "ollama.local.qwen");
  await expect(page.getByLabel(MODEL_FIELD).locator("option")).toHaveCount(1);
  const selectedRevision = await page.locator("#provider-revision").innerText();
  await page.waitForTimeout(150);
  await expect(page.locator("#provider-revision")).toHaveText(selectedRevision);

  await refreshModels(page);
  await expect(page.getByLabel(MODEL_FIELD).locator("option")).toHaveCount(2);
  await expect(page.getByLabel(MODEL_FIELD)).toHaveValue("");
});

test("duplicate admitted identities fail closed and cannot be selected", async ({
  page,
}) => {
  await openSettings(page);
  await selectProfile(page, "ollama.local.qwen");
  await page
    .getByRole("button", { name: "Show duplicate provider census" })
    .click();
  await expect(page.getByLabel(MODEL_FIELD).locator("option")).toHaveCount(1);
  await expect(page.getByLabel(MODEL_FIELD)).toHaveValue("");
  await expect(page.locator(".h3s-pv-state")).toHaveAttribute(
    "data-readiness",
    "not_configured",
  );
});

test("an unsent key is cleared when model context, rejection, or mount changes", async ({
  page,
}) => {
  await openSettings(page);
  await selectProfile(page, "remote.example.gpt");
  const field = page.getByLabel(/^(?:API key|API 金鑰|API 密钥)$/);

  await field.fill("test-credential-session");
  await page.getByRole("button", { name: KEY_SUBMIT }).click();
  await expect(page.getByLabel(MODEL_FIELD).locator("option")).toHaveCount(2);

  await field.fill("test-credential-unsent-model-context");
  await selectExactModel(page, "gpt-4o-mini");
  await expect(field).toHaveValue("");

  await field.fill("test-credential-unsent-rejection-context");
  await page
    .getByRole("button", { name: "Refuse next provider credential" })
    .click();
  await expect(field).toHaveValue("");

  await field.fill("test-credential-unsent-mount-context");
  await page.getByRole("button", { name: "Remount sidebar" }).click();
  await page.getByRole("button", { name: SETTINGS_TAB }).click();
  await expect(page.getByLabel(/^(?:API key|API 金鑰|API 密钥)$/)).toHaveValue(
    "",
  );
});

test("scan detail names an unpinned candidate and offers no way to admit it", async ({
  page,
}) => {
  await openSettings(page);
  await selectProfile(page, "ollama.local.qwen");
  await refreshModels(page);
  const scan = page.locator(".h3s-pv-scan");
  await scan.locator("summary").click();
  await expect(scan).toContainText("some-other-model");
  await expect(scan.locator("button, select, input")).toHaveCount(0);
  const options = await page
    .getByLabel(MODEL_FIELD)
    .locator("option")
    .allInnerTexts();
  expect(options.join("\n")).not.toContain("some-other-model");
});

test("the shipped default says there is nothing to select", async ({
  page,
}) => {
  await openSettings(page);
  await page
    .getByRole("button", { name: "Show shipped provider default" })
    .click();
  await expect(page.getByLabel(PROVIDER_FIELD)).toHaveCount(0);
  await expect(page.locator(".h3s-pv-empty")).toBeVisible();
  await expect(page.locator(".h3s-pv-consent")).toHaveCount(0);
  await expect(page.locator(".h3s-pv-credential")).toHaveCount(0);
});

test("a refusal is shown rather than swallowed", async ({ page }) => {
  await openSettings(page);
  await page
    .getByRole("button", { name: "Refuse next provider credential" })
    .click();
  await expect(page.locator(".h3s-pv-rejection")).toHaveAttribute(
    "data-rejection",
    "credential_rejected",
  );
});

test("identity renders byte-identically in all three locales at the product floor", async ({
  page,
}) => {
  await setSidebarWidth(page, PRODUCT_FLOOR);
  await openSettings(page);
  await selectProfile(page, "remote.example.gpt");

  const identity = page.locator(".h3s-pv-identity");
  const captured: string[][] = [];
  for (const locale of LOCALES) {
    await setLocale(page, locale);
    await setSidebarWidth(page, PRODUCT_FLOOR);
    // Opened by a real click the first time round; left alone afterwards,
    // because a switch of locale must not close a detail the user opened.
    if (!(await identity.evaluate((node) => (node as HTMLDetailsElement).open)))
      await identity.locator("summary").click();
    await expect(identity).toHaveAttribute("open", "");
    captured.push(
      await page
        .locator(
          '.h3s-pv-identity [data-h3-verbatim="true"],' +
            ' .h3s-pv-disclosure [data-h3-verbatim="true"]',
        )
        .allInnerTexts(),
    );
    // The panel must not overflow its own column in any locale.
    const overflow = await page
      .locator(".h3s-pv")
      .evaluate((node) => node.scrollWidth - node.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  }
  expect(captured[1]).toEqual(captured[0]);
  expect(captured[2]).toEqual(captured[0]);
  expect(captured[0].join("\n")).toContain("openai_chat_completions");
  await setLocale(page, "en");
});

test("the transfer boundary is stated in every locale and never dropped", async ({
  page,
}) => {
  await setSidebarWidth(page, PRODUCT_FLOOR);
  await openSettings(page);
  await selectProfile(page, "remote.example.gpt");
  for (const locale of LOCALES) {
    await setLocale(page, locale);
    const disclosure = page.locator(".h3s-pv-disclosure");
    await expect(disclosure).toHaveCount(1);
    // The facts survive translation because they are not translated.
    await expect(disclosure).toContainText("internet");
    await expect(disclosure).toContainText("remote_upload");
    await expect(
      disclosure.locator('[data-consent-scope="session_only"]'),
    ).toHaveCount(1);
    const text = await disclosure.innerText();
    expect(text.trim().length).toBeGreaterThan(40);
  }
  await setLocale(page, "en");
});

test("the section is reachable and operable by keyboard alone", async ({
  page,
}) => {
  await openSettings(page);
  const select = page.getByLabel(PROVIDER_FIELD);
  await select.focus();
  await expect(select).toBeFocused();
  await select.selectOption("remote.example.gpt");
  await expect(page.locator(".h3s-pv-consent")).toBeVisible();

  const credential = page.getByLabel(/^(?:API key|API 金鑰|API 密钥)$/);
  await credential.focus();
  await credential.fill("test-credential-keyboard-session");
  await page.getByRole("button", { name: KEY_SUBMIT }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByLabel(MODEL_FIELD).locator("option")).toHaveCount(2);
  await expect(page.locator(".h3s-pv-state")).toHaveAttribute(
    "aria-busy",
    "false",
  );
  const model = page.getByLabel(MODEL_FIELD);
  await model.focus();
  await expect(model).toBeFocused();
  await model.selectOption("gpt-4o-mini");
  await expect(page.locator(".h3s-pv-state")).toHaveAttribute(
    "data-readiness",
    "ready",
  );
  await expect(
    page.locator(".h3s-pv-consent [data-consent-status]"),
  ).toHaveAttribute("data-consent-status", "granted");
});

/**
 * Everything below reaches the surface the way a user does -- select, supply,
 * consent, expand -- because the states that carry the consent text are the
 * ones the accepted `M17-22` sweep can never see: it measures the Settings page
 * in its default state, where no provider is selected and neither the
 * disclosure nor the consent block exists.
 */
async function expandEverything(page: import("@playwright/test").Page) {
  await openSettings(page);
  await configureRemote(page);
  for (const detail of [".h3s-pv-identity", ".h3s-pv-scan"])
    await page.locator(`${detail} > summary`).click();
}

test("the consented surface clears WCAG AA in both themes", async ({
  page,
}) => {
  await expandEverything(page);
  await installPinnedHostPalette(page);
  for (const dark of [true, false]) {
    await page.evaluate((isDark) => {
      document.body.classList.add("litegraph");
      document.documentElement.classList.toggle("dark-theme", isDark);
    }, dark);
    const worst = await worstContrastWithin(page, ".h3s-pv");
    expect(
      worst.ratio,
      `${dark ? "dark" : "light"}: "${worst.text}" at ${worst.color}`,
    ).toBeGreaterThanOrEqual(4.5);
  }
  await page.evaluate(() => {
    document.body.classList.remove("litegraph");
    document.documentElement.classList.remove("dark-theme");
  });
});

test("forced colors keeps every boundary and every control visible", async ({
  page,
}) => {
  await page.emulateMedia({ forcedColors: "active" });
  await expandEverything(page);
  // A boxed region that draws its edge with a background tint alone disappears
  // when the platform replaces backgrounds, so each one is asserted to own a
  // real border. This section deliberately ships no forced-colors override:
  // the borders are ordinary borders, which the platform recolours for us.
  const boxes = [".h3s-pv-disclosure", ".h3s-pv-consent", ".h3s-pv-credential"];
  for (const box of boxes) {
    const style = await page.locator(box).evaluate((node) => {
      const computed = getComputedStyle(node);
      return {
        style: computed.borderTopStyle,
        width: parseFloat(computed.borderTopWidth),
      };
    });
    expect(style.style, box).not.toBe("none");
    expect(style.width, box).toBeGreaterThan(0);
  }
  // Controls stay operable, not merely present.
  await page
    .getByRole("button", { name: /^(?:Withdraw consent|撤回同意)$/ })
    .click();
  await expect(
    page.locator(".h3s-pv-consent [data-consent-status]"),
  ).toHaveAttribute("data-consent-status", "denied");
  await page.emulateMedia({ forcedColors: null });
});

test("nothing in the section animates, so reduced motion has nothing to strip", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expandEverything(page);
  const moving = await page.locator(".h3s-pv").evaluate((root) => {
    const offenders: string[] = [];
    const seconds = (value: string) =>
      value
        .split(",")
        .map((part) =>
          part.trim().endsWith("ms")
            ? parseFloat(part) / 1000
            : parseFloat(part),
        )
        .reduce((worst, current) => Math.max(worst, current || 0), 0);
    for (const node of [root, ...Array.from(root.querySelectorAll("*"))]) {
      const style = getComputedStyle(node);
      if (
        seconds(style.transitionDuration) > 0 ||
        seconds(style.animationDuration) > 0
      )
        offenders.push(node.className || node.tagName);
    }
    return offenders;
  });
  expect(moving).toEqual([]);
  await page.emulateMedia({ reducedMotion: null });
});

test("the accessible tree of the consented section is a contract", async ({
  page,
}) => {
  await expandEverything(page);
  await expect(page.locator(".h3s-pv")).toMatchAriaSnapshot(`
    - region "Assisted authoring provider":
      - heading "Assisted authoring provider" [level=3]
      - paragraph: Configure an optional language model for a later assisted-authoring action. Nothing is selected automatically, and Settings never executes a draft.
      - paragraph: This browser-session boundary is not a login. Anyone who can access a shared ComfyUI host remains inside that host's security boundary.
      - text: Provider profile
      - combobox "Provider profile":
        - option "None selected"
        - option "OpenAI (remote.example.gpt)" [selected]
        - option "Anthropic (anthropic.claude_sonnet_4_6.remote)"
        - option "Ollama (ollama.local.qwen)"
      - text: Exact model
      - combobox "Exact model":
        - option "No model selected"
        - option "gpt-4o-mini" [selected]
      - paragraph: Ready The provider and selected model checks passed. Assisted execution is not activated for this connection.
      - group:
        - text: Connection and selected model
        - term: Provider
        - definition: OpenAI
        - term: Model
        - definition: gpt-4o-mini
        - term: Wire dialect
        - definition: openai_chat_completions
        - term: Adapter
        - definition: 1.0.0
        - term: Parser
        - definition: h3.prompt_model.draft_json.v1
        - term: Cost class
        - definition: paid_remote
        - term: Retention policy
        - definition: provider_policy
        - term: Limitations
        - definition: remote_activation_pending, retention_depends_on_provider_policy
        - term: Host
        - definition: api.example.com
        - term: Port
        - definition: "443"
      - region "What leaves this computer":
        - heading "What leaves this computer" [level=4]
        - paragraph: Prompt text, revision instructions and derived representations are sent to a third party over HTTPS.
        - term: Destination
        - definition: internet
        - term: Transfer boundary
        - definition: remote_upload
        - term: Accepted input
        - definition: text
        - paragraph: Original media bytes are never transmitted.
        - paragraph: A credential is required for every request.
        - paragraph: A preflight check runs before every request.
        - paragraph: Your consent and your credential last for this ComfyUI session only. Restarting ComfyUI discards both.
        - paragraph: The remote provider may process or retain prompt text under the current provider, account and API-endpoint policy. This page does not promise zero retention.
      - region "Credential":
        - heading "Credential" [level=4]
        - text: API key
        - textbox "API key"
        - button "Reload available models"
        - paragraph: Held for this browser session.
        - button "Discard"
        - paragraph: The key is held in memory for this session and is never written to disk, to a workflow, or to a log.
      - region "Consent":
        - heading "Consent" [level=4]
        - paragraph: This provider needs your explicit permission before any request is made.
        - paragraph: Allowed
        - button "Withdraw consent"
        - paragraph: Your consent and your credential last for this ComfyUI session only. Restarting ComfyUI discards both.
      - group:
        - text: Scan detail
        - list:
          - listitem: gpt-4o-mini Usable
          - listitem: some-other-model Not pinned by this build
  `);
});

test("native census sorting, literal metadata, filter ownership and comparable captures in all locales", async ({
  page,
}) => {
  const directory = providerCaptureDirectory();
  for (const locale of LOCALES) {
    const text = providerCopy(locale);
    for (const width of [704, 1100]) {
      await page.goto("/?mode=production");
      await setSidebarWidth(page, width);
      await setLocale(page, locale);
      await selectProfile(page, "ollama.local.qwen");
      await refreshModels(page);
      await selectExactModel(page, "qwen3:8b");
      await page.locator(".h3s-pv-identity summary").click();
      if (directory !== undefined)
        await page.screenshot({
          path: resolve(directory, `baseline-${locale}-${width}.png`),
          fullPage: true,
        });

      await page.goto("/?mode=production&native_models=1");
      await setSidebarWidth(page, width);
      await setLocale(page, locale);
      await selectProfile(page, "ollama.local");
      await refreshModels(page);
      const model = page.getByLabel(MODEL_FIELD);
      const values = await model
        .locator("option")
        .evaluateAll((options) =>
          options.map((option) => (option as HTMLOptionElement).value),
        );
      expect(values.slice(0, 7)).toEqual([
        "",
        "fixture/local:choice",
        "fixture/tie-a:local",
        "fixture/tie-b:local",
        "fixture/older:local",
        "fixture/unknown-a:local",
        "fixture/unknown-b:local",
      ]);
      await selectExactModel(page, "fixture/local:choice");
      await expect(page.locator(".h3s-pv-badge")).toContainText([
        text.movingAlias,
        text.retiresOn.split("{date}")[0]!,
      ]);
      await expect(page.locator(".h3s-pv-badge").first()).toHaveAttribute(
        "data-retiring-soon",
        "true",
      );
      await page.locator(".h3s-pv-identity summary").click();
      const identity = page.locator(".h3s-pv-identity");
      for (const fact of [
        "fixture/local:choice",
        "Native choice 中文 😀 <Model>",
        "32768",
        "16384",
        "4096",
        "fixture-family",
        "7B",
        "Q4_K_M",
        `sha256:${"d".repeat(64)}`,
      ])
        await expect(identity).toContainText(fact);
      await expect(identity.locator("img, script")).toHaveCount(0);
      const count = await page.locator("#provider-intent-count").textContent();
      const revision = await page.locator("#provider-revision").textContent();
      const filter = page.getByRole("searchbox", { name: text.filterModels });
      const providerBox = await page.getByLabel(PROVIDER_FIELD).boundingBox();
      const modelBox = await model.boundingBox();
      const filterBox = await filter.boundingBox();
      expect(providerBox).not.toBeNull();
      expect(modelBox).not.toBeNull();
      expect(filterBox).not.toBeNull();
      expect(Math.abs(providerBox!.x - modelBox!.x)).toBeLessThanOrEqual(1);
      expect(
        Math.abs(providerBox!.width - modelBox!.width),
      ).toBeLessThanOrEqual(1);
      expect(Math.abs(providerBox!.x - filterBox!.x)).toBeLessThanOrEqual(1);
      await filter.fill("no-such-model");
      await expect(model.locator("option")).toHaveCount(2);
      await expect(model).toHaveValue("fixture/local:choice");
      await expect(page.locator("#provider-intent-count")).toHaveText(count!);
      await expect(page.locator("#provider-revision")).toHaveText(revision!);
      await filter.fill("native choice");
      await filter.press("Tab");
      await expect(model).toBeFocused();
      if (directory !== undefined)
        await page.screenshot({
          path: resolve(directory, `current-${locale}-${width}.png`),
          fullPage: true,
        });
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth > window.innerWidth,
        ),
      ).toBe(false);
      await selectProfile(page, "anthropic.remote");
      await expect(
        page.getByRole("searchbox", { name: text.filterModels }),
      ).toHaveCount(0);
      await selectProfile(page, "ollama.local");
      await refreshModels(page);
      await expect(
        page.getByRole("searchbox", { name: text.filterModels }),
      ).toHaveValue("");
    }
  }
});

test("native refresh removes a missing model and diagnostic only focuses the model control", async ({
  page,
}) => {
  await page.goto("/?mode=production&native_models=1");
  await openSettings(page);
  await selectProfile(page, "ollama.local");
  await refreshModels(page);
  await selectExactModel(page, "fixture/local:choice");
  await page
    .getByRole("button", { name: "Remove selected native model from census" })
    .click();
  await refreshModels(page);
  await expect(page.getByLabel(MODEL_FIELD)).toHaveValue("");
  await expect(page.locator(".h3s-pv-state")).toHaveAttribute(
    "data-readiness",
    "not_configured",
  );
  const notice = page.locator(
    '.h3s-pv-diagnostic[data-outcome="prompt_model.model_missing"]',
  );
  await expect(notice).toBeVisible();
  const count = await page.locator("#provider-intent-count").textContent();
  await notice.getByRole("button", { name: "Choose an exact model" }).click();
  await expect(page.getByLabel(MODEL_FIELD)).toBeFocused();
  await expect(page.locator("#provider-intent-count")).toHaveText(count!);
  await selectExactModel(page, "fixture/unknown-a:local");
  await page.locator(".h3s-pv-identity summary").click();
  await expect(page.locator(".h3s-pv-identity")).not.toContainText(
    "fixture-family",
  );
  await expect(page.locator(".h3s-pv-identity")).not.toContainText("32768");
});
