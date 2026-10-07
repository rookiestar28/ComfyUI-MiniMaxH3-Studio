import { expect, test, type Page } from "@playwright/test";
import {
  openIntegratedShell,
  shellSnapshot,
  shellSurface,
} from "../helpers/nleShell";
import { observeFrontendHostSeamsInPage } from "../../support/hostSeamLiveProbe";

test.use({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1 });

type KeyCounts = Record<"keydown" | "keyup" | "keypress", number>;
type KeyWitness = { capture: KeyCounts; bubble: KeyCounts };
type HostKeyWitness = {
  undoCapture: number;
  undoActions: number;
  canvasKeyups: number;
  ghostActions: number;
};

async function installHostCapture(
  page: Page,
  ghost: boolean,
  changeMode = false,
) {
  await page.evaluate(
    async ({ withGhost, changeMode }) => {
      const modulePath = "/hostKeyboard.ts";
      const fixture = (await import(
        /* @vite-ignore */ modulePath
      )) as typeof import("../../../e2e/hostKeyboard");
      (
        window as Window & { editorHostKeyWitness?: HostKeyWitness }
      ).editorHostKeyWitness = fixture.installHostCapture(
        withGhost,
        changeMode,
      );
    },
    { withGhost: ghost, changeMode },
  );
}

const hostWitness = (page: Page) =>
  page.evaluate(
    () =>
      (window as Window & { editorHostKeyWitness?: HostKeyWitness })
        .editorHostKeyWitness!,
  );

async function installKeyWitness(page: Page) {
  await page.evaluate(() => {
    const counts = () => ({ keydown: 0, keyup: 0, keypress: 0 });
    const witness = { capture: counts(), bubble: counts() };
    (window as Window & { editorKeyWitness?: KeyWitness }).editorKeyWitness =
      witness;
    for (const type of ["keydown", "keyup", "keypress"] as const) {
      window.addEventListener(
        type,
        (event) => {
          if (event.key === "x") witness.capture[type] += 1;
        },
        true,
      );
      window.addEventListener(type, (event) => {
        if (event.key === "x") witness.bubble[type] += 1;
      });
    }
  });
}

const keyWitness = (page: Page) =>
  page.evaluate(
    () =>
      (window as Window & { editorKeyWitness?: KeyWitness }).editorKeyWitness!,
  );

test("unhandled editor keys stay inside the portal and host keys resume after close", async ({
  page,
}) => {
  const oracle = await openIntegratedShell(page, "smoke");
  const clip = page.locator(
    '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
  );
  await clip.click();
  await expect(clip).toBeFocused();
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(1);
  await installKeyWitness(page);
  await page.keyboard.press("x");
  expect(await keyWitness(page)).toEqual({
    capture: { keydown: 1, keyup: 1, keypress: 1 },
    bubble: { keydown: 0, keyup: 0, keypress: 0 },
  });
  expect(oracle.transactions).toHaveLength(1);

  await page.locator('[data-h3-nle-action="close"]').click();
  await expect(page.locator(shellSurface)).toHaveCount(0);
  await page.keyboard.press("x");
  expect(await keyWitness(page)).toEqual({
    capture: { keydown: 2, keyup: 2, keypress: 2 },
    bubble: { keydown: 1, keyup: 1, keypress: 1 },
  });
  expect(oracle.transactions).toHaveLength(1);
});

test("older callable undo capture is masked before editor undo and resumes after close", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke", {
    beforeOpen: (target) => installHostCapture(target, false),
  });
  const clip = page.locator(
    '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
  );
  await clip.click();
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(1);
  await page.keyboard.press("Control+z");
  expect((await hostWitness(page)).undoActions).toBe(0);
  expect((await hostWitness(page)).undoCapture).toBe(1);
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(2);
  await page.locator('[data-h3-nle-action="close"]').click();
  await expect(page.locator(shellSurface)).toHaveCount(0);
  await page.keyboard.press("Control+z");
  expect((await hostWitness(page)).undoActions).toBe(1);
});

test("canonical ghost Delete and keyup stay suspended until the editor closes", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke", {
    beforeOpen: (target) => installHostCapture(target, true),
  });
  const clip = page.locator(
    '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
  );
  await clip.click();
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(1);
  await page.keyboard.press("Delete");
  expect((await hostWitness(page)).ghostActions).toBe(0);
  expect((await hostWitness(page)).canvasKeyups).toBe(0);
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(2);
  await expect(page.locator('[data-h3-nle-clip="clip-0"]')).toHaveCount(0);
  await page.locator('[data-h3-nle-action="close"]').click();
  await expect(page.locator(shellSurface)).toHaveCount(0);
  await page.keyboard.press("Delete");
  expect((await hostWitness(page)).ghostActions).toBe(1);
  expect((await hostWitness(page)).canvasKeyups).toBe(1);
});

test("Escape closes the editor before its trailing keyup without cancelling the host ghost", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke", {
    beforeOpen: (target) => installHostCapture(target, true),
  });
  await page.keyboard.press("Escape");
  expect((await hostWitness(page)).ghostActions).toBe(0);
  expect((await hostWitness(page)).canvasKeyups).toBe(0);
  await expect(page.locator(shellSurface)).toHaveCount(0);
  await page.keyboard.press("x");
  expect((await hostWitness(page)).canvasKeyups).toBe(1);
  await page.keyboard.press("Escape");
  expect((await hostWitness(page)).ghostActions).toBe(1);
});

for (const target of ["grid", "toolbar", "splitter", "number", "text"] as const)
  test(`unhandled keys remain contained with native ${target} focus`, async ({
    page,
  }) => {
    const oracle = await openIntegratedShell(page, "smoke", {
      beforeOpen: (tab) => installHostCapture(tab, false),
    });
    await page
      .locator(
        '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
      )
      .click();
    await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(1);
    if (target === "text")
      await page.locator('[data-h3-nle-pane="text"]').click();
    const control = page
      .locator(
        target === "grid"
          ? '[data-h3-nle-focus-fallback="timeline"]'
          : target === "toolbar"
            ? '[data-h3-nle-control="transport.zoom_in"]'
            : target === "splitter"
              ? '[data-h3-nle-splitter="top_timeline"]'
              : target === "number"
                ? '.h3-nle-value input[type="number"]'
                : ".h3-nle-text-bin input",
      )
      .first();
    await control.focus();
    await expect(control).toBeFocused();
    const oldValue = target === "text" ? await control.inputValue() : null;
    await installKeyWitness(page);
    await page.keyboard.press("x");
    expect(await keyWitness(page)).toEqual({
      capture: { keydown: 1, keyup: 1, keypress: 1 },
      bubble: { keydown: 0, keyup: 0, keypress: 0 },
    });
    if (oldValue !== null) await expect(control).toHaveValue(`${oldValue}x`);
    expect((await hostWitness(page)).canvasKeyups).toBe(0);
    expect(oracle.transactions).toHaveLength(1);
  });

test("native fullscreen retains containment and Escape exits fullscreen before the dialog", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke", {
    beforeOpen: (tab) => installHostCapture(tab, true),
  });
  await page.locator('[data-h3-nle-control="transport.fullscreen"]').click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          document.fullscreenElement?.classList.contains("h3-nle-monitor") ??
          false,
      ),
    )
    .toBe(true);
  await page.locator(`${shellSurface} .h3-nle-monitor`).focus();
  await installKeyWitness(page);
  await page.keyboard.press("x");
  expect((await keyWitness(page)).bubble).toEqual({
    keydown: 0,
    keyup: 0,
    keypress: 0,
  });
  expect((await hostWitness(page)).canvasKeyups).toBe(0);
  await page.keyboard.press("Escape");
  await expect
    .poll(() => page.evaluate(() => document.fullscreenElement === null))
    .toBe(true);
  await expect(page.locator(shellSurface)).toBeVisible();
  expect((await hostWitness(page)).ghostActions).toBe(0);
  await page.locator('[data-h3-nle-action="export"]').click();
  await expect(page.locator('[data-h3-nle-popover="export"]')).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.locator('[data-h3-nle-popover="export"]')).toBeHidden();
  await expect(page.locator(shellSurface)).toBeVisible();
});

test("canonical callbacks registered again with the same identity remain suspended", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke", {
    beforeOpen: (tab) => installHostCapture(tab, true),
  });
  await page.evaluate(async () => {
    const modulePath = "/hostKeyboard.ts";
    const { app } = (await import(
      /* @vite-ignore */ modulePath
    )) as typeof import("../../../e2e/hostKeyboard");
    document.addEventListener("keyup", app.canvas._key_callback, true);
    document.addEventListener("keydown", app.canvas._ghostKeyHandler!, true);
  });
  await page.keyboard.press("Backspace");
  expect((await hostWitness(page)).ghostActions).toBe(0);
  expect((await hostWitness(page)).canvasKeyups).toBe(0);
  await page.locator('[data-h3-nle-action="close"]').click();
  await expect(page.locator(shellSurface)).toHaveCount(0);
  await page.keyboard.press("Backspace");
  expect((await hostWitness(page)).ghostActions).toBe(1);
  expect((await hostWitness(page)).canvasKeyups).toBe(1);
});

test("an incompatible live canvas lifetime fails closed for its first key", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke", {
    beforeOpen: (tab) => installHostCapture(tab, false),
  });
  await installKeyWitness(page);
  await page.evaluate(async () => {
    const modulePath = "/hostKeyboard.ts";
    const { app } = (await import(
      /* @vite-ignore */ modulePath
    )) as typeof import("../../../e2e/hostKeyboard");
    app.canvas._events_binded = false;
  });
  await page.keyboard.press("x");
  await expect(page.locator(shellSurface)).toHaveCount(0);
  expect((await keyWitness(page)).bubble).toEqual({
    keydown: 0,
    keyup: 0,
    keypress: 0,
  });
  expect((await hostWitness(page)).canvasKeyups).toBe(0);
  expect(await shellSnapshot(page)).toMatchObject({
    surfaceStatus: "compact_unsupported",
    mountFailed: 1,
    receipts: 0,
  });
  await page.keyboard.press("Control+z");
  expect((await hostWitness(page)).undoActions).toBe(1);
});

for (const changeMode of [false, true])
  test(`older undo filter preserves modifiers, repeat and input positive controls in ${changeMode ? "change" : "ordinary"} mode`, async ({
    page,
  }) => {
    await openIntegratedShell(page, "smoke", {
      beforeOpen: (tab) => installHostCapture(tab, false, changeMode),
    });
    const keys = ["Control+z", "Control+Shift+z", "Control+y", "Meta+z"];
    for (const key of keys) await page.keyboard.press(key);
    await page.keyboard.down("Control");
    await page.keyboard.down("z");
    await page.keyboard.down("z");
    await page.keyboard.up("z");
    await page.keyboard.up("Control");
    expect((await hostWitness(page)).undoActions).toBe(0);
    const captured = (await hostWitness(page)).undoCapture;
    expect(captured).toBe(6);
    await page.locator('[data-h3-nle-action="close"]').click();
    await expect(page.locator(shellSurface)).toHaveCount(0);
    await page.keyboard.press("Control+Alt+z");
    expect((await hostWitness(page)).undoCapture).toBe(captured);
    for (const key of keys) await page.keyboard.press(key);
    expect((await hostWitness(page)).undoActions).toBe(4);
    // Ordinary mode excludes INPUT/textarea; change mode accepts both. Native
    // target input defaults are preserved even when defaultPrevented is already set.
    await page.evaluate(() => {
      for (const tag of ["input", "textarea"] as const) {
        const element = document.createElement(tag);
        element.dataset.hostKeyboardInput = tag;
        document.body.append(element);
      }
    });
    for (const tag of ["input", "textarea"] as const) {
      await page.locator(`[data-host-keyboard-input="${tag}"]`).focus();
      await page.keyboard.press("Control+z");
    }
    expect((await hostWitness(page)).undoActions).toBe(changeMode ? 6 : 4);
  });

for (const shape of ["modal", "canvas"] as const)
  test(`the live readonly probe isolates a throwing ${shape} descriptor`, async ({
    page,
  }) => {
    await openIntegratedShell(page, "smoke", { expandOverlay: false });
    await page.route("**/scripts/app.js", (route) =>
      route.fulfill({
        contentType: "text/javascript",
        body: 'export { app } from "/hostKeyboard.ts";',
      }),
    );
    await page.route("**/scripts/api.js", (route) =>
      route.fulfill({
        contentType: "text/javascript",
        body: 'export { api } from "/hostKeyboard.ts";',
      }),
    );
    await page.evaluate(async (shape) => {
      const modulePath = "/hostKeyboard.ts";
      const { app } = (await import(
        /* @vite-ignore */ modulePath
      )) as typeof import("../../../e2e/hostKeyboard");
      if (shape === "modal")
        Object.defineProperty(app, "constructor", {
          value: new Proxy(function FixtureConstructor() {}, {
            getOwnPropertyDescriptor() {
              throw new Error("foreign shape");
            },
          }),
          configurable: true,
        });
      else
        Object.defineProperty(app, "canvas", {
          value: new Proxy(
            {},
            {
              getOwnPropertyDescriptor() {
                throw new Error("foreign shape");
              },
            },
          ),
          configurable: true,
        });
    }, shape);
    const result = await page.evaluate(observeFrontendHostSeamsInPage);
    const states = Object.fromEntries(
      result.observations
        .filter((row) =>
          [
            "frontend.app.modal_keyboard_guard",
            "frontend.app.canvas.keyboard_capture",
          ].includes(row.seam_id),
        )
        .map((row) => [row.seam_id, row.readiness_state]),
    );
    expect(states).toEqual({
      "frontend.app.modal_keyboard_guard":
        shape === "modal" ? "absent" : "ready",
      "frontend.app.canvas.keyboard_capture":
        shape === "canvas" ? "absent" : "ready",
    });
  });
