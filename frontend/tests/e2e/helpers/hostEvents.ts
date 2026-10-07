import type { Page } from "@playwright/test";

type HostControlAction = "drop_socket" | "restore_socket" | "install_wrappers";

function installHostNoiseAgent(initiallyEnabled: boolean): void {
  type NoisyGraph = Record<string, any> & {
    serialize?: (...args: unknown[]) => unknown;
  };
  type NoiseRuntime = Window &
    typeof globalThis & {
      app?: { graph?: NoisyGraph };
      comfyAPI?: { app?: { app?: { graph?: NoisyGraph } } };
      __H3_CONTEXT_E2E_NOISE__?: {
        enabled: boolean;
        revision: number;
        wrappedGraph?: NoisyGraph;
      };
    };
  const runtime = window as NoiseRuntime;
  const noise = (runtime.__H3_CONTEXT_E2E_NOISE__ ??= {
    enabled: initiallyEnabled,
    revision: 0,
  });
  noise.enabled = initiallyEnabled;

  const decorate = (serialized: unknown): unknown => {
    if (
      !noise.enabled ||
      serialized === null ||
      typeof serialized !== "object" ||
      Array.isArray(serialized)
    ) {
      return serialized;
    }
    noise.revision += 1;
    const workflow = structuredClone(serialized) as Record<string, any>;
    const extra =
      workflow.extra !== null &&
      typeof workflow.extra === "object" &&
      !Array.isArray(workflow.extra)
        ? workflow.extra
        : {};
    workflow.extra = {
      ...extra,
      ds: {
        scale: 1 + noise.revision / 100,
        offset: [noise.revision, noise.revision],
      },
      m23_26_noisy_host: { revision: noise.revision },
    };
    const nodes = Array.isArray(workflow.nodes) ? workflow.nodes : [];
    const movable = nodes.find(
      (node: unknown): node is Record<string, any> =>
        node !== null && typeof node === "object" && !Array.isArray(node),
    );
    if (movable !== undefined) {
      const properties =
        movable.properties !== null &&
        typeof movable.properties === "object" &&
        !Array.isArray(movable.properties)
          ? movable.properties
          : {};
      movable.properties = { ...properties, cnr_id: "m23-26-noisy-host" };
      const position = Array.isArray(movable.pos) ? movable.pos : [0, 0];
      movable.pos = [
        Number(position[0] ?? 0) + noise.revision,
        Number(position[1] ?? 0),
      ];
    }
    const seed = nodes.find(
      (node: unknown): node is Record<string, any> =>
        node !== null &&
        typeof node === "object" &&
        !Array.isArray(node) &&
        /seed|sampler/i.test(
          String((node as Record<string, unknown>).type ?? ""),
        ) &&
        Array.isArray((node as Record<string, unknown>).widgets_values) &&
        typeof (node as Record<string, any>).widgets_values[0] === "number",
    );
    if (seed === undefined) {
      workflow.widget_idx_map = {};
      return workflow;
    }
    const seedId = String(seed.id);
    workflow.widget_idx_map = { [seedId]: { seed: 0 } };
    seed.widgets_values = [
      seed.widgets_values[0] + noise.revision,
      ...seed.widgets_values.slice(1),
    ];
    return workflow;
  };

  const wrapGraph = (): void => {
    const graph = runtime.app?.graph ?? runtime.comfyAPI?.app?.app?.graph;
    if (
      graph === undefined ||
      graph === noise.wrappedGraph ||
      typeof graph.serialize !== "function"
    ) {
      return;
    }
    const serialize = graph.serialize.bind(graph);
    graph.serialize = (...args: unknown[]) => decorate(serialize(...args));
    noise.wrappedGraph = graph;
  };
  const scheduleWrap = (): void => queueMicrotask(wrapGraph);
  window.addEventListener("click", scheduleWrap, true);
  window.addEventListener("change", scheduleWrap, true);
  window.addEventListener("h3-context-e2e-host-noise", (event) => {
    noise.enabled = Boolean((event as CustomEvent<unknown>).detail);
    scheduleWrap();
  });
  window.addEventListener("DOMContentLoaded", scheduleWrap, { once: true });
  scheduleWrap();
}

async function hostControl(
  page: Page,
  action: HostControlAction,
): Promise<void> {
  await page.evaluate((requested) => {
    const controls = (
      window as unknown as {
        __H3_CONTEXT_E2E_HOST__?: Record<HostControlAction, () => unknown>;
      }
    ).__H3_CONTEXT_E2E_HOST__;
    const operation = controls?.[requested];
    if (typeof operation !== "function")
      throw new Error(`hermetic host control unavailable: ${requested}`);
    operation();
  }, action);
}

export type HostDoubleControls = Readonly<{
  installWrappers(): Promise<void>;
  dropSocket(): Promise<void>;
  restoreSocket(): Promise<void>;
  emit(type: string, detail?: unknown): Promise<void>;
  noise(enabled: boolean): Promise<void>;
}>;

export function hostDoubleControls(page: Page): HostDoubleControls {
  return Object.freeze({
    installWrappers: () => hostControl(page, "install_wrappers"),
    dropSocket: () => hostControl(page, "drop_socket"),
    restoreSocket: () => hostControl(page, "restore_socket"),
    async emit(type, detail) {
      await page.evaluate(
        ({ eventType, eventDetail }) => {
          window.dispatchEvent(
            new CustomEvent(eventType, { detail: eventDetail }),
          );
        },
        { eventType: type, eventDetail: detail },
      );
    },
    async noise(enabled) {
      await page.addInitScript(installHostNoiseAgent, enabled);
      await page.evaluate((active) => {
        document.documentElement.toggleAttribute("data-h3-host-noise", active);
        window.dispatchEvent(
          new CustomEvent("h3-context-e2e-host-noise", { detail: active }),
        );
      }, enabled);
    },
  });
}
