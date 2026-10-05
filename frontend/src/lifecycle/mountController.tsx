import type { ReactNode } from "react";
import type { Root } from "react-dom/client";

type CreateRoot = (container: Element | DocumentFragment) => Root;
type Cleanup = () => void;
type RenderContext = {
  signal: AbortSignal;
  own(cleanup: Cleanup): void;
  ownAsync(cleanup: Promise<Cleanup>): void;
};

type MountRecord = {
  active: boolean;
  container: HTMLElement;
  ownedMount: HTMLDivElement;
  root: Root;
  abort: AbortController;
  style: HTMLStyleElement;
  cleanups: Cleanup[];
  ownershipObserver?: MutationObserver;
};

export type MountController = {
  readonly generation: number;
  mount(
    container: HTMLElement,
    factory: (context: RenderContext) => ReactNode,
  ): void;
  update(factory: (context: RenderContext) => ReactNode): void;
  unmount(): void;
};

const defaultShellStyle =
  ":root{--h3-context-mounted:1}[data-h3-context-mount]{display:flex;flex-direction:column;min-block-size:100%}[data-h3-context-mount]>.h3c{box-sizing:border-box;flex:1 0 auto;min-block-size:100%}";
const mountMarker = "data-h3-context-mount";

// The sidebar's stylesheet is injected into someone else's page, so it is bounded like every other
// input this package accepts. The number is a ceiling on a runaway stylesheet, not a design target:
// M21-03 grew the shipped sheet to 32 811 characters against a 32 768 bound and the throw below took
// the whole extension down on every real host, because nothing in the gate ever paired the bound
// with the stylesheet that actually ships. `tests/test_sidebar_style_budget.py` now does, and it
// reads this constant out of this file -- keep the literal here greppable.
export const MAX_SHELL_STYLE_LENGTH = 65_536;

export function createMountController(dependencies: {
  createRoot: CreateRoot;
  styles?: string;
}): MountController {
  const shellStyle = dependencies.styles ?? defaultShellStyle;
  if (shellStyle.length < 1 || shellStyle.length > MAX_SHELL_STYLE_LENGTH)
    throw new Error("sidebar styles exceed their bound");
  let current: MountRecord | undefined;
  let generation = 0;

  const release = (record: MountRecord): void => {
    if (current === record) current = undefined;
    if (!record.active) return;
    record.active = false;
    let firstFailure: unknown;
    let failed = false;
    const attempt = (cleanup: Cleanup): void => {
      try {
        cleanup();
      } catch (error) {
        if (!failed) {
          failed = true;
          firstFailure = error;
        }
      }
    };
    attempt(() => record.ownershipObserver?.disconnect());
    attempt(() => record.abort.abort());
    for (const cleanup of [...record.cleanups].reverse()) {
      attempt(cleanup);
    }
    record.cleanups.length = 0;
    attempt(() => record.root.unmount());
    attempt(() => record.style.remove());
    attempt(() => record.ownedMount.remove());
    if (failed) throw firstFailure;
  };

  const unmount = (): void => {
    generation += 1;
    const record = current;
    if (record === undefined) return;
    release(record);
  };

  const mount = (
    container: HTMLElement,
    factory: (context: RenderContext) => ReactNode,
  ): void => {
    unmount();
    generation += 1;
    const abort = new AbortController();
    const style = document.createElement("style");
    style.dataset.h3Context = "";
    style.textContent = shellStyle;
    const ownedMount = document.createElement("div");
    ownedMount.setAttribute(mountMarker, "");
    let root: Root;
    try {
      root = dependencies.createRoot(ownedMount);
    } catch (error) {
      abort.abort();
      style.remove();
      ownedMount.remove();
      throw error;
    }
    const record: MountRecord = {
      active: true,
      container,
      ownedMount,
      root,
      abort,
      style,
      cleanups: [],
    };
    current = record;
    const own = (cleanup: Cleanup): void => {
      if (typeof cleanup !== "function")
        throw new TypeError("cleanup must be callable");
      if (record.active && current === record) record.cleanups.push(cleanup);
      else cleanup();
    };
    const ownAsync = (cleanup: Promise<Cleanup>): void => {
      void cleanup.then(own, () => undefined);
    };
    const previousChildren = [...container.childNodes];
    try {
      container.replaceChildren(ownedMount);
      document.head.append(style);
      const ownershipObserver = new MutationObserver(() => {
        if (
          !record.active ||
          current !== record ||
          (record.container.childNodes.length === 1 &&
            record.container.firstChild === record.ownedMount)
        )
          return;
        // CRITICAL: ComfyUI can reuse a connected custom-sidebar slot without calling destroy.
        // Release only H3-owned resources here; a later destroy must not erase the new extension.
        generation += 1;
        try {
          release(record);
        } catch {
          // Ownership is already lost, so cleanup must finish without escaping into the host loop.
        }
      });
      record.ownershipObserver = ownershipObserver;
      ownershipObserver.observe(container, { childList: true });
      root.render(factory({ own, ownAsync, signal: abort.signal }));
    } catch (error) {
      const stillOwnsSlot = ownedMount.parentNode === container;
      try {
        release(record);
      } catch {
        // The render/append failure remains the first failure after complete rollback.
      }
      if (stillOwnsSlot) container.replaceChildren(...previousChildren);
      throw error;
    }
  };

  const update = (factory: (context: RenderContext) => ReactNode): void => {
    const record = current;
    if (record === undefined || !record.active)
      throw new Error("sidebar view is not mounted");
    const own = (cleanup: Cleanup): void => {
      if (typeof cleanup !== "function")
        throw new TypeError("cleanup must be callable");
      if (record.active && current === record) record.cleanups.push(cleanup);
      else cleanup();
    };
    const ownAsync = (cleanup: Promise<Cleanup>): void => {
      void cleanup.then(own, () => undefined);
    };
    record.root.render(factory({ own, ownAsync, signal: record.abort.signal }));
  };

  return {
    get generation() {
      return generation;
    },
    mount,
    update,
    unmount,
  };
}
