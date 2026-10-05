import {
  disposeAuthoringAudioPeaks,
  type AuthoringAudioPeaks,
} from "../contracts/authoringAudioPeaks";

export const MAX_NLE_AUDIO_PEAKS_ENVELOPES = 32 as const;
export const MAX_NLE_AUDIO_PEAKS_BYTES = 2 * 1024 * 1024;

export type NleAudioPeaksCacheKey = Readonly<{
  workspaceHandle: string;
  assetFingerprint: string;
  derivativeProfileId: string;
  derivativeKind: "audio_peaks";
}>;

type CacheLimits = Readonly<{ entries: number; bytes: number }>;
type CacheEntry = Readonly<{
  key: NleAudioPeaksCacheKey;
  value: AuthoringAudioPeaks;
}>;

const encodeKey = (key: NleAudioPeaksCacheKey): string =>
  JSON.stringify([
    key.workspaceHandle,
    key.assetFingerprint,
    key.derivativeProfileId,
    key.derivativeKind,
  ]);

const clearPeaks = (peaks: AuthoringAudioPeaks): void => {
  disposeAuthoringAudioPeaks(peaks);
};

export const createNleAudioPeaksCache = (
  limits: CacheLimits = Object.freeze({
    entries: MAX_NLE_AUDIO_PEAKS_ENVELOPES,
    bytes: MAX_NLE_AUDIO_PEAKS_BYTES,
  }),
) => {
  if (
    !Number.isSafeInteger(limits.entries) ||
    limits.entries < 1 ||
    !Number.isSafeInteger(limits.bytes) ||
    limits.bytes < 1
  )
    throw new RangeError("audio peaks cache limits must be positive integers");

  const entries = new Map<string, CacheEntry>();
  let retainedBytes = 0;

  const remove = (encoded: string): void => {
    const entry = entries.get(encoded);
    if (!entry) return;
    entries.delete(encoded);
    retainedBytes -= entry.value.byteLength;
    clearPeaks(entry.value);
  };

  return Object.freeze({
    get(key: NleAudioPeaksCacheKey): AuthoringAudioPeaks | undefined {
      const encoded = encodeKey(key);
      const entry = entries.get(encoded);
      if (!entry) return undefined;
      entries.delete(encoded);
      entries.set(encoded, entry);
      return entry.value;
    },

    set(key: NleAudioPeaksCacheKey, value: AuthoringAudioPeaks): void {
      if (value.byteLength > limits.bytes) {
        clearPeaks(value);
        throw new RangeError("audio peaks entry exceeds cache byte limit");
      }
      const encoded = encodeKey(key);
      const existing = entries.get(encoded);
      if (existing) {
        entries.delete(encoded);
        retainedBytes -= existing.value.byteLength;
        if (existing.value !== value) clearPeaks(existing.value);
      }
      entries.set(encoded, { key: { ...key }, value });
      retainedBytes += value.byteLength;
      while (entries.size > limits.entries || retainedBytes > limits.bytes) {
        const oldest = entries.keys().next().value as string | undefined;
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

    size(): number {
      return entries.size;
    },

    bytes(): number {
      return retainedBytes;
    },
  });
};
