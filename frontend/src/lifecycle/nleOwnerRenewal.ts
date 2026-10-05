// One visible Production/editor owner may stay live while the user works for longer than the
// backend's inactivity limit. The scheduler holds only opaque owner identity, never media.

export const NLE_OWNER_RENEWAL_INTERVAL_MS = 120_000;

export type NleRenewalOwner = Readonly<{
  productionHandle: string;
  productionId: string;
  editorHandle: string | null;
}>;

export function createNleOwnerRenewal(
  input: Readonly<{
    currentOwner: () => NleRenewalOwner | null;
    renewProduction: () => Promise<void>;
    renewEditor: () => Promise<void>;
  }>,
) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let activeKey: string | null = null;
  let epoch = 0;
  let running = false;
  const productionReads = new Map<string, number>();
  const editorReads = new Map<string, number>();

  function productionKey(owner: NleRenewalOwner): string {
    return `${owner.productionHandle}\u0000${owner.productionId}`;
  }

  function key(owner: NleRenewalOwner): string {
    return `${owner.productionHandle}\u0000${owner.productionId}\u0000${owner.editorHandle ?? ""}`;
  }

  /**
   * Record a completed Production route read or action for this exact project.
   * IMPORTANT: every such exchange already refreshed the backend inactivity clock. Without this
   * record a reconcile after navigation re-reads a project answered moments earlier, adds a
   * request the page never made and can replace the visible projection mid-interaction.
   */
  function noteProductionExchange(
    productionHandle: string,
    productionId: string,
  ): void {
    productionReads.set(`${productionHandle}\u0000${productionId}`, Date.now());
  }

  function stop(): void {
    epoch += 1;
    if (timer !== undefined) clearTimeout(timer);
    timer = undefined;
    activeKey = null;
  }

  function remaining(
    last: number | undefined,
    now: number,
    reconcile: boolean,
  ): number {
    return last === undefined
      ? reconcile
        ? 0
        : NLE_OWNER_RENEWAL_INTERVAL_MS
      : Math.max(0, NLE_OWNER_RENEWAL_INTERVAL_MS - (now - last));
  }

  function pruneReads(now: number, owner: NleRenewalOwner): void {
    for (const [identity, last] of productionReads)
      if (
        identity !== productionKey(owner) &&
        now - last >= 2 * NLE_OWNER_RENEWAL_INTERVAL_MS
      )
        productionReads.delete(identity);
    for (const [identity, last] of editorReads)
      if (
        identity !== owner.editorHandle &&
        now - last >= 2 * NLE_OWNER_RENEWAL_INTERVAL_MS
      )
        editorReads.delete(identity);
  }

  async function tick(
    expectedEpoch: number,
    expectedKey: string,
  ): Promise<void> {
    timer = undefined;
    const ownerAtStart = input.currentOwner();
    if (
      running ||
      epoch !== expectedEpoch ||
      ownerAtStart === null ||
      key(ownerAtStart) !== expectedKey
    ) {
      sync();
      return;
    }
    running = true;
    try {
      const now = Date.now();
      const projectKey = productionKey(ownerAtStart);
      if (remaining(productionReads.get(projectKey), now, true) === 0) {
        productionReads.set(projectKey, now);
        await input.renewProduction();
      }
      const owner = input.currentOwner();
      if (
        epoch === expectedEpoch &&
        owner !== null &&
        key(owner) === expectedKey &&
        owner.editorHandle !== null &&
        remaining(editorReads.get(owner.editorHandle), Date.now(), true) === 0
      ) {
        editorReads.set(owner.editorHandle, Date.now());
        await input.renewEditor();
      }
    } catch {
      // A failed read is not proof of release; the owning session reports its own error state.
    } finally {
      running = false;
      sync();
    }
  }

  function sync(reconcile = false): void {
    const owner = input.currentOwner();
    if (owner === null) {
      stop();
      return;
    }
    const nextKey = key(owner);
    if (activeKey !== nextKey) {
      stop();
      activeKey = nextKey;
    }
    if (running) return;
    const now = Date.now();
    pruneReads(now, owner);
    if (timer !== undefined) {
      if (!reconcile) return;
      clearTimeout(timer);
      timer = undefined;
    }
    const capturedEpoch = epoch;
    const delay = Math.min(
      remaining(productionReads.get(productionKey(owner)), now, reconcile),
      owner.editorHandle === null
        ? NLE_OWNER_RENEWAL_INTERVAL_MS
        : remaining(editorReads.get(owner.editorHandle), now, reconcile),
    );
    timer = setTimeout(() => void tick(capturedEpoch, nextKey), delay);
  }

  return { sync, stop, noteProductionExchange };
}
