import type { Page } from "@playwright/test";

type ForeignArrowHotkey = { count: number; remove: () => void };

/**
 * Runs `body` while a document keydown listener cancels the default of every arrow key, as the
 * ComfyUI-Easy-Use hotkeys-js binding does on a supplied host (M26-05 runtime-18), and returns how
 * many arrow keys reached that listener. A range left to its native step loses those keys; an
 * owned range steps itself and stops them, so the count stays zero.
 */
export async function underForeignArrowHotkey(
  page: Page,
  body: () => Promise<void>,
): Promise<number> {
  await page.evaluate(() => {
    const scope = window as unknown as {
      __h3ForeignArrowHotkey?: ForeignArrowHotkey;
    };
    if (scope.__h3ForeignArrowHotkey !== undefined)
      throw new Error("a foreign arrow hotkey is already installed");
    const listener = (event: KeyboardEvent) => {
      if (!event.key.startsWith("Arrow")) return;
      state.count += 1;
      event.preventDefault();
    };
    const state: ForeignArrowHotkey = {
      count: 0,
      remove: () => document.removeEventListener("keydown", listener),
    };
    document.addEventListener("keydown", listener);
    scope.__h3ForeignArrowHotkey = state;
  });
  let count = -1;
  try {
    await body();
  } finally {
    count = await page.evaluate(() => {
      const scope = window as unknown as {
        __h3ForeignArrowHotkey?: ForeignArrowHotkey;
      };
      const state = scope.__h3ForeignArrowHotkey;
      state?.remove();
      delete scope.__h3ForeignArrowHotkey;
      return state?.count ?? -1;
    });
  }
  return count;
}
