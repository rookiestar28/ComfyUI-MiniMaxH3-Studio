export const MAX_NLE_DECORATION_BITMAPS = 64 as const;
export const MAX_NLE_FILMSTRIP_BITMAPS = 16 as const;

export type NleDecorationKind = "thumbnail" | "filmstrip";

export type NleDecorationCacheKey = Readonly<{
  workspaceHandle: string;
  assetFingerprint: string;
  derivativeProfileId: string;
  derivativeKind: NleDecorationKind;
}>;

type ClosableBitmap = Readonly<{ close(): void }>;

type CacheEntry<T extends ClosableBitmap> = Readonly<{
  key: NleDecorationCacheKey;
  value: T;
}>;

const encodeKey = (key: NleDecorationCacheKey): string =>
  JSON.stringify([
    key.workspaceHandle,
    key.assetFingerprint,
    key.derivativeProfileId,
    key.derivativeKind,
  ]);

const closeBitmap = (bitmap: ClosableBitmap): void => {
  try {
    bitmap.close();
  } catch {
    // ImageBitmap.close() is specified as synchronous cleanup and should not throw.
    // Cleanup must continue if a host-provided stand-in violates that contract.
  }
};

export const createNleDecorationCache = <
  T extends ClosableBitmap = ImageBitmap,
>(
  limits: Readonly<Record<NleDecorationKind, number>> = Object.freeze({
    thumbnail: MAX_NLE_DECORATION_BITMAPS,
    filmstrip: MAX_NLE_FILMSTRIP_BITMAPS,
  }),
) => {
  if (
    !Number.isSafeInteger(limits.thumbnail) ||
    limits.thumbnail < 1 ||
    !Number.isSafeInteger(limits.filmstrip) ||
    limits.filmstrip < 1
  )
    throw new RangeError("decoration cache limits must be positive integers");

  const entries = new Map<string, CacheEntry<T>>();

  const remove = (encoded: string): void => {
    const entry = entries.get(encoded);
    if (!entry) return;
    entries.delete(encoded);
    closeBitmap(entry.value);
  };

  return Object.freeze({
    get(key: NleDecorationCacheKey): T | undefined {
      const encoded = encodeKey(key);
      const entry = entries.get(encoded);
      if (!entry) return undefined;
      entries.delete(encoded);
      entries.set(encoded, entry);
      return entry.value;
    },

    set(key: NleDecorationCacheKey, value: T): void {
      const encoded = encodeKey(key);
      const existing = entries.get(encoded);
      if (existing) {
        entries.delete(encoded);
        if (existing.value !== value) closeBitmap(existing.value);
      }
      entries.set(encoded, { key: { ...key }, value });
      const kindSize = () =>
        [...entries.values()].filter(
          (entry) => entry.key.derivativeKind === key.derivativeKind,
        ).length;
      while (kindSize() > limits[key.derivativeKind]) {
        const oldest = [...entries].find(
          ([, entry]) => entry.key.derivativeKind === key.derivativeKind,
        )?.[0];
        if (oldest === undefined) break;
        remove(oldest);
      }
    },

    retainWorkspaceAssets(
      workspaceHandle: string,
      assetFingerprints: ReadonlySet<string>,
    ): void {
      for (const [encoded, entry] of entries) {
        if (
          entry.key.workspaceHandle === workspaceHandle &&
          !assetFingerprints.has(entry.key.assetFingerprint)
        )
          remove(encoded);
      }
    },

    purgeWorkspace(workspaceHandle: string): void {
      for (const [encoded, entry] of entries) {
        if (entry.key.workspaceHandle === workspaceHandle) remove(encoded);
      }
    },

    close(): void {
      for (const encoded of [...entries.keys()]) remove(encoded);
    },

    size(kind?: NleDecorationKind): number {
      return kind === undefined
        ? entries.size
        : [...entries.values()].filter(
            (entry) => entry.key.derivativeKind === kind,
          ).length;
    },
  });
};
