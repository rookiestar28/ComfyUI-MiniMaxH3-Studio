"""Content-free MiniMax-H3 model-card facts at public revision d21241f0.

Limits and labels are interface facts, restated here without explanatory model-card prose.
"""

SOURCE_ANCHORS = ("model.reference.interface@d21241f0",)

MAX_IMAGES = 9
MAX_VIDEOS = 3
MAX_AUDIOS = 3
MAX_FILES = 12
MIN_REFERENCE_SECONDS = 2
MAX_REFERENCE_SECONDS = 15
MIN_OUTPUT_SECONDS = 4
MAX_OUTPUT_SECONDS = 15
VIDEO_FPS = 24
AUDIO_SAMPLE_RATE = 32_000
AUDIO_CHANNELS = 2
STABLE_DIALOGUE_LANGUAGES = frozenset(
    {
        "Arabic",
        "Chinese",
        "English",
        "French",
        "German",
        "Italian",
        "Japanese",
        "Korean",
        "Portuguese",
        "Russian",
        "Spanish",
    }
)


def reference_counts_admissible(images: int, videos: int, audios: int) -> bool:
    counts = (images, videos, audios)
    limits = (MAX_IMAGES, MAX_VIDEOS, MAX_AUDIOS)
    return (
        all(
            type(count) is int and 0 <= count <= limit
            for count, limit in zip(counts, limits, strict=True)
        )
        and sum(counts) <= MAX_FILES
        and (images + videos > 0 or audios == 0)
    )
