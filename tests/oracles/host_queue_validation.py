"""Queue-time input projection restated from the anchored host execution protocol.

No upstream module is imported. Links are unresolved during validation, optional sockets
that are not connected are absent, and list consumers receive an unresolved one-item tuple.
Declaring input_types delegates linked-type checks to the node's hook.
"""

from __future__ import annotations

from typing import Any

SOURCE_ANCHORS = ("host.queue.projection@b323a345", "host.queue.validation@b323a345")

WIDGET_TYPES = frozenset({"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"})
SCENARIOS = ("all_links_connected", "required_links_only")


def widget_default(kind: object, options: dict[str, Any]) -> object:
    if "default" in options:
        return options["default"]
    choices = kind if isinstance(kind, (tuple, list)) else options.get("options", ())
    if choices:
        return choices[0]
    defaults: dict[str, object] = {"INT": 0, "FLOAT": 0.0, "STRING": "", "BOOLEAN": False}
    return defaults.get(kind) if isinstance(kind, str) else None


def validation_call(
    schema: dict[str, Any],
    parameters: set[str],
    scenario: str,
    *,
    input_is_list: bool = False,
    accepts_kwargs: bool = False,
) -> dict[str, Any]:
    if scenario not in SCENARIOS:
        raise ValueError("unknown_queue_scenario")
    projected: dict[str, Any] = {}
    linked_types: dict[str, str] = {}
    for section in ("required", "optional"):
        for name, declaration in schema.get(section, {}).items():
            kind = declaration[0]
            options = declaration[1] if len(declaration) > 1 else {}
            widget = isinstance(kind, (list, tuple)) or kind in WIDGET_TYPES
            if widget:
                default = widget_default(kind, options)
                projected[name] = [default] if input_is_list else default
            elif section == "required" or scenario == "all_links_connected":
                projected[name] = (None,) if input_is_list else None
                linked_types[name] = kind
    if "input_types" in parameters:
        projected["input_types"] = linked_types
    return {key: value for key, value in projected.items() if accepts_kwargs or key in parameters}
