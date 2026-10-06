"""Bounded user direction for the existing server-owned prose drafting action."""

MAX_REVISION_INSTRUCTION_SCALARS = 2_048
MAX_REVISION_INSTRUCTION_BYTES = 8_192


def validate_revision_instruction(value: object) -> str:
    """Preserve valid Unicode verbatim; refusals never echo supplied content."""

    if (
        type(value) is not str
        or not value.strip()
        or len(value) > MAX_REVISION_INSTRUCTION_SCALARS
        or "\x00" in value
        or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
        or len(value.encode("utf-8")) > MAX_REVISION_INSTRUCTION_BYTES
    ):
        raise ValueError("revision instruction is invalid")
    return value
