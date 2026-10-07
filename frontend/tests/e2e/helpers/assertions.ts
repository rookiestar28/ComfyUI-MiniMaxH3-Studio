import { expect, type Locator, type Page } from "@playwright/test";

export async function expectCount(
  locator: Locator,
  count: number,
): Promise<void> {
  await expect(locator).toHaveCount(count);
}

export async function expectQueueCount(
  page: Page,
  count: number,
): Promise<void> {
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              __H3_CONTEXT_E2E_QUEUE_COUNT__?: number;
            }
          ).__H3_CONTEXT_E2E_QUEUE_COUNT__ ?? 0,
      ),
    )
    .toBe(count);
}

export async function armNextPaintSample(button: Locator): Promise<void> {
  await button.evaluate((node) => {
    const measured = window as typeof window & {
      __h3OwnedInteractionSample?: number;
    };
    measured.__h3OwnedInteractionSample = -1;
    node.addEventListener(
      "click",
      () => {
        const started = performance.now();
        requestAnimationFrame(() => {
          measured.__h3OwnedInteractionSample = performance.now() - started;
        });
      },
      { capture: true, once: true },
    );
  });
}

export async function armRevisionSample(
  button: Locator,
  expectedRevision: number,
): Promise<void> {
  await button.evaluate((node, revision) => {
    const measured = window as typeof window & {
      __h3OwnedInteractionSample?: number;
    };
    measured.__h3OwnedInteractionSample = -1;
    node.addEventListener(
      "click",
      () => {
        const started = performance.now();
        const root = document.querySelector("#production-sidebar-container");
        if (root === null) return;
        const finish = () => {
          if (
            root.querySelector(`[aria-label="revision ${revision}"]`) === null
          )
            return;
          measured.__h3OwnedInteractionSample = performance.now() - started;
          observer.disconnect();
        };
        const observer = new MutationObserver(finish);
        observer.observe(root, {
          attributes: true,
          attributeFilter: ["aria-label"],
          childList: true,
          subtree: true,
        });
        queueMicrotask(finish);
      },
      { capture: true, once: true },
    );
  }, expectedRevision);
}

export async function readOwnedInteractionSample(page: Page): Promise<number> {
  const read = () =>
    page.evaluate(
      () =>
        (window as typeof window & { __h3OwnedInteractionSample?: number })
          .__h3OwnedInteractionSample ?? -1,
    );
  await expect.poll(read).toBeGreaterThanOrEqual(0);
  return read();
}

export function ownedProjection(
  graph: Readonly<Record<string, unknown>>,
  ownedNodeIds: readonly string[],
): Readonly<Record<string, unknown>> {
  const nodes = Array.isArray(graph.nodes)
    ? graph.nodes.filter((node) =>
        ownedNodeIds.includes(
          String((node as Record<string, unknown> | undefined)?.id),
        ),
      )
    : [];
  const owned = new Set(ownedNodeIds);
  const links = Array.isArray(graph.links)
    ? graph.links.filter(
        (link) =>
          Array.isArray(link) &&
          owned.has(String(link[1])) &&
          owned.has(String(link[3])),
      )
    : [];
  return Object.freeze({
    nodes: structuredClone(nodes),
    links: structuredClone(links),
  });
}
