import type { Page } from "@playwright/test";

export type GraphLoader = Readonly<{
  load(graph: Readonly<Record<string, unknown>>): Promise<void>;
  serialized(): Promise<Record<string, unknown>>;
}>;

export function graphLoader(page: Page): GraphLoader {
  return Object.freeze({
    async load(graph) {
      await page.evaluate(async (candidate) => {
        const app = (
          window as unknown as {
            app?: {
              loadGraphData?: (
                graph: unknown,
                clean?: boolean,
                restore?: boolean,
                workflow?: unknown,
              ) => unknown;
            };
          }
        ).app;
        if (typeof app?.loadGraphData !== "function")
          throw new Error("host graph loader is unavailable");
        await Promise.resolve(app.loadGraphData(candidate, true, true, null));
      }, graph);
    },
    async serialized() {
      return page.evaluate(() => {
        const graph = (
          window as unknown as {
            app?: { graph?: { serialize?: () => unknown } };
          }
        ).app?.graph;
        if (typeof graph?.serialize !== "function")
          throw new Error("host graph serializer is unavailable");
        return structuredClone(graph.serialize()) as Record<string, unknown>;
      });
    },
  });
}
