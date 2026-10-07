import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

import { createLocaleStore } from "../src/i18n/localeStore";
import { createExtensionRegistration } from "../src/lifecycle/extensionRegistration";
import { createMountController } from "../src/lifecycle/mountController";
import {
  createShellSession,
  type ShellActions,
  type ShellDeps,
  type ShellRuntime,
} from "../src/lifecycle/shellSession";

describe("mount controller", () => {
  it("updates one mounted root without unmounting component state", () => {
    const root = { render: vi.fn(), unmount: vi.fn() };
    const controller = createMountController({
      createRoot: vi.fn(() => root as never),
    });
    const container = document.createElement("div");
    controller.mount(container, () => <span>first</span>);
    const markerWhileMounted = container.firstElementChild?.getAttribute(
      "data-h3-context-mount",
    );
    const generation = controller.generation;
    controller.update(() => <span>second</span>);
    expect(root.render).toHaveBeenCalledTimes(2);
    expect(root.unmount).not.toHaveBeenCalled();
    expect(controller.generation).toBe(generation);
    controller.unmount();
    expect(markerWhileMounted).toBe("");
    expect(root.unmount).toHaveBeenCalledOnce();
    expect(container.hasAttribute("data-h3-context-mount")).toBe(false);
  });

  it("owns root, aborts, listeners, timers, styles, remount, and repeated destroy", async () => {
    const container = document.createElement("div");
    document.body.append(container);
    const listener = vi.fn();
    const controller = createMountController({ createRoot });

    await act(async () => {
      controller.mount(container, ({ own, signal }) => {
        window.addEventListener("h3-test", listener);
        own(() => window.removeEventListener("h3-test", listener));
        const timer = window.setTimeout(listener, 10_000);
        own(() => window.clearTimeout(timer));
        return (
          <div data-testid="mounted">mounted {String(signal.aborted)}</div>
        );
      });
    });
    const firstGeneration = controller.generation;
    expect(document.querySelectorAll("style[data-h3-context]")).toHaveLength(1);

    await act(async () => {
      controller.unmount();
      controller.mount(container, () => <div>remounted</div>);
    });
    expect(controller.generation).toBeGreaterThan(firstGeneration);
    controller.unmount();
    controller.unmount();
    window.dispatchEvent(new Event("h3-test"));
    expect(listener).not.toHaveBeenCalled();
    expect(document.querySelectorAll("style[data-h3-context]")).toHaveLength(0);
    expect(container.childNodes).toHaveLength(0);
  });

  it("runs cleanup that resolves after destruction and rejects late reattachment", async () => {
    const container = document.createElement("div");
    const controller = createMountController({ createRoot });
    let resolveCleanup!: (cleanup: () => void) => void;
    const cleanup = vi.fn();
    const pending = new Promise<() => void>((resolve) => {
      resolveCleanup = resolve;
    });
    controller.mount(container, ({ ownAsync }) => {
      ownAsync(pending);
      return <div>pending</div>;
    });
    controller.unmount();
    resolveCleanup(cleanup);
    await pending;
    await Promise.resolve();
    expect(cleanup).toHaveBeenCalledOnce();
    expect(container.childNodes).toHaveLength(0);
  });

  it("rolls back a createRoot failure without leaking owned style or container state", () => {
    const container = document.createElement("div");
    container.setAttribute("data-h3-context-mount", "host-owned");
    container.append(document.createElement("span"));
    const controller = createMountController({
      createRoot: () => {
        throw new Error("createRoot failed");
      },
    });
    expect(() => controller.mount(container, () => <div />)).toThrow(
      /createRoot failed/,
    );
    expect(document.querySelectorAll("style[data-h3-context]")).toHaveLength(0);
    expect(container.childNodes).toHaveLength(1);
    expect(container.firstElementChild?.tagName).toBe("SPAN");
    expect(container.getAttribute("data-h3-context-mount")).toBe("host-owned");
  });

  it("restores the exact mount marker after render rollback", () => {
    const container = document.createElement("div");
    container.setAttribute("data-h3-context-mount", "host-owned");
    const root = {
      render: () => {
        throw new Error("render failed");
      },
      unmount: vi.fn(),
    };
    const controller = createMountController({
      createRoot: () => root as never,
    });
    expect(() => controller.mount(container, () => <div />)).toThrow(
      /render failed/,
    );
    expect(root.unmount).toHaveBeenCalledOnce();
    expect(container.childNodes).toHaveLength(0);
    expect(container.getAttribute("data-h3-context-mount")).toBe("host-owned");
  });

  it("restores a pre-existing exact mount marker after unmount", () => {
    const container = document.createElement("div");
    container.setAttribute("data-h3-context-mount", "host-owned");
    const controller = createMountController({ createRoot });
    controller.mount(container, () => <div>mounted</div>);
    const markerWhileMounted = container.firstElementChild?.getAttribute(
      "data-h3-context-mount",
    );
    controller.unmount();
    expect(markerWhileMounted).toBe("");
    expect(container.getAttribute("data-h3-context-mount")).toBe("host-owned");
  });

  it("continues complete destruction and rethrows the first cleanup failure", () => {
    const container = document.createElement("div");
    const controller = createMountController({ createRoot });
    const laterCleanup = vi.fn();
    controller.mount(container, ({ own }) => {
      own(laterCleanup);
      own(() => {
        throw new Error("cleanup failed");
      });
      return <div>mounted</div>;
    });
    expect(() => controller.unmount()).toThrow(/cleanup failed/);
    expect(laterCleanup).toHaveBeenCalledOnce();
    expect(document.querySelectorAll("style[data-h3-context]")).toHaveLength(0);
    expect(container.childNodes).toHaveLength(0);
  });

  it("releases H3 ownership when the host reuses the shared mount without destroy", async () => {
    const container = document.createElement("div");
    document.body.append(container);
    const cleanup = vi.fn();
    const createRootMock = vi.fn(() => ({
      render: vi.fn(),
      unmount: vi.fn(),
    }));
    const controller = createMountController({
      createRoot: createRootMock as never,
    });

    controller.mount(container, ({ own }) => {
      own(cleanup);
      return <div data-testid="h3-owned">H3</div>;
    });
    expect(document.querySelectorAll("style[data-h3-context]")).toHaveLength(1);

    const replacement = document.createElement("div");
    replacement.dataset.foreignExtension = "";
    replacement.textContent = "foreign";
    container.append(replacement);
    await Promise.resolve();

    expect(cleanup).toHaveBeenCalledOnce();
    expect(container.hasAttribute("data-h3-context-mount")).toBe(false);
    expect(document.querySelectorAll("style[data-h3-context]")).toHaveLength(0);
    expect(container.childNodes).toHaveLength(1);
    expect(container.firstChild).toBe(replacement);

    controller.unmount();
    expect(container.childNodes).toHaveLength(1);
    expect(container.firstChild).toBe(replacement);

    controller.mount(container, () => <div data-testid="remounted">H3</div>);
    expect(createRootMock).toHaveBeenCalledTimes(2);
    expect(
      container.firstElementChild?.hasAttribute("data-h3-context-mount"),
    ).toBe(true);
    controller.unmount();
    expect(container.childNodes).toHaveLength(0);
  });

  it("releases managed successor authority when the host takes over the shared mount", async () => {
    type RegisteredView = {
      mountView(container: HTMLElement): void;
      unmountView(): void;
      disposeExtension(): void;
    };
    let registered: RegisteredView | undefined;
    const session = createShellSession();
    const detachManagedSerialSequence = vi.fn(async () => undefined);
    const captureViewFocus = vi.fn();
    const actions = {
      EntrySidebar: () => <div>H3</div>,
      beginViewFocusClaim: () => ({
        container: session.container!,
        generation: 1,
      }),
      captureViewFocus,
      clearActiveAppModeExecution: vi.fn(),
      clearDeferredAppModeProjections: vi.fn(),
      clearDeferredAppModeTerminals: vi.fn(),
      clearDeferredManagedArtifacts: vi.fn(),
      closeProductionMediaPreview: vi.fn(),
      closeSemanticProposalReview: vi.fn(),
      detachManagedSerialSequence,
      ensureBuildProvenance: vi.fn(),
      ensureProductionWorkspace: vi.fn(),
      ensureProviderProjection: vi.fn(),
      invalidateViewFocusClaim: vi.fn(),
      nleDisposeOverlay: vi.fn(),
      nleStopOwnerRenewal: vi.fn(),
      nleSyncOwnerRenewal: vi.fn(),
      recordPageLocalFocus: vi.fn(),
      releaseMediaRuntime: vi.fn(),
      releaseProviderSession: vi.fn(),
      renderCurrent: vi.fn(),
      resumeProductionSession: vi.fn(),
      retainedAssetsLeave: vi.fn(),
      workspaceStateLeave: vi.fn(),
    } as unknown as ShellActions;
    const mount = createMountController({
      createRoot: vi.fn(() => ({
        render: vi.fn(),
        unmount: vi.fn(),
      })) as never,
    });
    const deps = {
      app: {},
      appModeLifecycle: {
        dispose: vi.fn(),
        synchronizeRunSequence: vi.fn(),
      },
      entrySetup: {
        setup(install: () => void) {
          install();
          return true;
        },
        dispose(cleanup: () => void) {
          cleanup();
          return true;
        },
      },
      host: {
        register(next: RegisteredView) {
          registered = next;
        },
      },
      localeStore: createLocaleStore(),
      mount,
      pageRegistry: {
        getSnapshot: () => ({ selected: "context" as const }),
        subscribe: () => vi.fn(),
      },
      performanceRecorder: {
        measure(_name: string, operation: () => void) {
          operation();
        },
      },
      productionProposalDispatcher: {
        clearSensitive: vi.fn(),
        dispose: vi.fn(),
      },
    } as unknown as ShellDeps;
    const extension = createExtensionRegistration(
      Object.freeze({ session, deps, actions }) satisfies ShellRuntime,
    );
    extension.setup();
    expect(actions.resumeProductionSession).toHaveBeenCalledOnce();
    expect(actions.ensureProductionWorkspace).not.toHaveBeenCalled();
    if (registered === undefined) throw new Error("view was not registered");

    const panel = document.createElement("section");
    panel.className = "side-bar-panel";
    const content = document.createElement("div");
    content.className = "sidebar-content-container";
    const container = document.createElement("div");
    content.append(container);
    panel.append(content);
    document.body.append(panel);
    registered.mountView(container);

    const replacement = document.createElement("section");
    replacement.dataset.foreignExtension = "";
    container.replaceChildren(replacement);
    await Promise.resolve();

    expect(detachManagedSerialSequence).toHaveBeenCalledOnce();
    expect(actions.retainedAssetsLeave).toHaveBeenCalledOnce();
    expect(actions.workspaceStateLeave).toHaveBeenCalledOnce();
    expect(
      detachManagedSerialSequence.mock.invocationCallOrder[0],
    ).toBeLessThan(captureViewFocus.mock.invocationCallOrder[0]!);
    expect(session.container).toBeUndefined();
    expect(container.firstChild).toBe(replacement);

    registered.unmountView();
    expect(detachManagedSerialSequence).toHaveBeenCalledOnce();
    expect(container.firstChild).toBe(replacement);

    registered.mountView(container);
    registered.unmountView();
    expect(detachManagedSerialSequence).toHaveBeenCalledTimes(2);
    expect(actions.retainedAssetsLeave).toHaveBeenCalledTimes(2);
    expect(actions.workspaceStateLeave).toHaveBeenCalledTimes(2);
    expect(container.childNodes).toHaveLength(0);
    registered.disposeExtension();
  });
});
