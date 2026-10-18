"""Shared native reference capacities, independent of registry and request imports."""

# IMPORTANT: paired soundtracks and standalone audio consume the same native pool.
# Separate three-file allowances admit six files that native preflight must reject.
H3_BASE_MAX_AUDIO_REFERENCES = 3
