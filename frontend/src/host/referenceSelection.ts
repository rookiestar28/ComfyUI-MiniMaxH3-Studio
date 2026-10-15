/** Pure reference arithmetic shared by the picker, request and splice boundaries. */
export const MAX_REFERENCE_IMAGES = 9;
export const MAX_REFERENCE_VIDEOS = 3;
export const MAX_REFERENCE_AUDIOS = 3;
export const MAX_REFERENCE_FILES = 12;
export const MAX_REFERENCE_AUDIO_TOTAL = 3;

export type ReferenceSelection = Readonly<{
  images: number;
  videos: number;
  audios: number;
  soundtrack: "included" | "excluded" | "unavailable";
}>;
export type ReferenceCapacityReason = "per_kind" | "files" | "audio" | "visual";

function capacityReason(
  selection: ReferenceSelection,
): ReferenceCapacityReason | null {
  const { images, videos, audios, soundtrack } = selection;
  if (
    ![images, videos, audios].every(
      (value) => Number.isInteger(value) && value >= 0,
    ) ||
    images > MAX_REFERENCE_IMAGES ||
    videos > MAX_REFERENCE_VIDEOS ||
    audios > MAX_REFERENCE_AUDIOS
  )
    return "per_kind";
  const paired = soundtrack === "included" ? videos : 0;
  if (images + videos + paired + audios > MAX_REFERENCE_FILES) return "files";
  if (paired + audios > MAX_REFERENCE_AUDIO_TOTAL) return "audio";
  return null;
}

export function referenceCapacity(selection: ReferenceSelection) {
  const paired = selection.soundtrack === "included" ? selection.videos : 0;
  const bounds = capacityReason(selection);
  const reason =
    bounds ?? (selection.images + selection.videos === 0 ? "visual" : null);
  const remaining = { images: 0, videos: 0, audios: 0 };
  for (const [kind, maximum] of [
    ["images", MAX_REFERENCE_IMAGES],
    ["videos", MAX_REFERENCE_VIDEOS],
    ["audios", MAX_REFERENCE_AUDIOS],
  ] as const) {
    for (let added = 1; added <= maximum; added += 1) {
      if (
        capacityReason({ ...selection, [kind]: selection[kind] + added }) !==
        null
      )
        break;
      remaining[kind] = added;
    }
  }
  return {
    files: selection.images + selection.videos + paired + selection.audios,
    audio: paired + selection.audios,
    capacityAllowed: bounds === null,
    allowed: reason === null,
    reason,
    remaining,
    canIncludeSoundtrack:
      capacityReason({ ...selection, soundtrack: "included" }) === null,
  };
}

export function referenceLabels(selection: ReferenceSelection) {
  const paired = selection.soundtrack === "included" ? selection.videos : 0;
  const images = Array.from(
    { length: selection.images },
    (_, i) => `<Picture ${i + 1}>`,
  );
  const videos = Array.from(
    { length: selection.videos },
    (_, i) => `<Video ${i + 1}>`,
  );
  const pairedAudios = Array.from(
    { length: paired },
    (_, i) => `<Audio ${i + 1}>`,
  );
  const audios = Array.from(
    { length: selection.audios },
    (_, i) => `<Audio ${paired + i + 1}>`,
  );
  // IMPORTANT: soundtrack assets precede their own video in Registry/native order.
  // Grouping all videos before all soundtracks changes the compiled label oracle.
  const ordered = [
    ...images,
    ...videos.flatMap((video, i) =>
      paired ? [pairedAudios[i]!, video] : [video],
    ),
    ...audios,
  ];
  return { images, videos, pairedAudios, audios, ordered };
}
