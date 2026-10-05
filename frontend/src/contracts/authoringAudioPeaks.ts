export const AUTHORING_AUDIO_PEAKS_VERSION = 1 as const;
export const AUTHORING_AUDIO_PEAKS_PAIR_RATE = 100 as const;
export const AUTHORING_AUDIO_PEAKS_SAMPLE_RATE = 8_000 as const;
export const AUTHORING_AUDIO_PEAKS_MAX_PAIRS = 16_384 as const;
export const AUTHORING_AUDIO_PEAKS_HEADER_BYTES = 20 as const;
export const AUTHORING_AUDIO_PEAKS_MAX_BYTES =
  AUTHORING_AUDIO_PEAKS_HEADER_BYTES + AUTHORING_AUDIO_PEAKS_MAX_PAIRS * 2;
const SAMPLES_PER_PAIR =
  AUTHORING_AUDIO_PEAKS_SAMPLE_RATE / AUTHORING_AUDIO_PEAKS_PAIR_RATE;

export type AuthoringAudioPeaks = Readonly<{
  version: typeof AUTHORING_AUDIO_PEAKS_VERSION;
  pairRate: typeof AUTHORING_AUDIO_PEAKS_PAIR_RATE;
  sampleRate: typeof AUTHORING_AUDIO_PEAKS_SAMPLE_RATE;
  sampleCount: number;
  pairCount: number;
  pairs: Int8Array;
  byteLength: number;
}>;

const invalid = (): never => {
  throw new Error("invalid audio peaks envelope");
};

export function decodeAuthoringAudioPeaks(
  input: Uint8Array | ArrayBuffer,
): AuthoringAudioPeaks {
  const bytes =
    input instanceof Uint8Array
      ? input.slice()
      : input instanceof ArrayBuffer
        ? new Uint8Array(input)
        : invalid();
  if (
    bytes.byteLength < AUTHORING_AUDIO_PEAKS_HEADER_BYTES + 2 ||
    bytes.byteLength > AUTHORING_AUDIO_PEAKS_MAX_BYTES ||
    bytes[0] !== 0x48 ||
    bytes[1] !== 0x33 ||
    bytes[2] !== 0x41 ||
    bytes[3] !== 0x50
  )
    invalid();
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const version = view.getUint16(4, true);
  const pairRate = view.getUint16(6, true);
  const sampleRate = view.getUint32(8, true);
  const sampleCount = view.getUint32(12, true);
  const pairCount = view.getUint32(16, true);
  const expectedPairs = Math.ceil(sampleCount / SAMPLES_PER_PAIR);
  if (
    version !== AUTHORING_AUDIO_PEAKS_VERSION ||
    pairRate !== AUTHORING_AUDIO_PEAKS_PAIR_RATE ||
    sampleRate !== AUTHORING_AUDIO_PEAKS_SAMPLE_RATE ||
    !Number.isSafeInteger(sampleCount) ||
    sampleCount < 1 ||
    sampleCount > AUTHORING_AUDIO_PEAKS_MAX_PAIRS * SAMPLES_PER_PAIR ||
    pairCount !== expectedPairs ||
    pairCount < 1 ||
    pairCount > AUTHORING_AUDIO_PEAKS_MAX_PAIRS ||
    bytes.byteLength !== AUTHORING_AUDIO_PEAKS_HEADER_BYTES + pairCount * 2
  )
    invalid();
  const pairs = new Int8Array(
    bytes.buffer,
    bytes.byteOffset + AUTHORING_AUDIO_PEAKS_HEADER_BYTES,
    pairCount * 2,
  );
  for (let index = 0; index < pairCount; index += 1) {
    const low = view.getInt8(AUTHORING_AUDIO_PEAKS_HEADER_BYTES + index * 2);
    const high = view.getInt8(
      AUTHORING_AUDIO_PEAKS_HEADER_BYTES + index * 2 + 1,
    );
    if (low > high) invalid();
  }
  return Object.freeze({
    version: AUTHORING_AUDIO_PEAKS_VERSION,
    pairRate: AUTHORING_AUDIO_PEAKS_PAIR_RATE,
    sampleRate: AUTHORING_AUDIO_PEAKS_SAMPLE_RATE,
    sampleCount,
    pairCount,
    pairs,
    byteLength: bytes.byteLength,
  });
}

export function disposeAuthoringAudioPeaks(peaks: AuthoringAudioPeaks): void {
  try {
    const start = peaks.pairs.byteOffset - AUTHORING_AUDIO_PEAKS_HEADER_BYTES;
    if (
      start < 0 ||
      peaks.byteLength < AUTHORING_AUDIO_PEAKS_HEADER_BYTES ||
      start + peaks.byteLength > peaks.pairs.buffer.byteLength
    ) {
      peaks.pairs.fill(0);
      return;
    }
    new Uint8Array(peaks.pairs.buffer, start, peaks.byteLength).fill(0);
  } catch {
    // Cleanup is synchronous and best-effort for a host stand-in; never skip pair erasure.
    try {
      peaks.pairs.fill(0);
    } catch {
      // A forged detached view is already inaccessible.
    }
  }
}
