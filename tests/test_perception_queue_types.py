"""Typed queue admission owns every delegated linked socket, without executing media."""

from __future__ import annotations

import inspect
import unittest
from typing import Any

from oracles.host_queue_validation import validation_call

from comfyui_h3_context.timeline_producer_nodes import H3FullReferenceTimelineProducerNode

NODE = H3FullReferenceTimelineProducerNode


def queue_arguments(scenario: str = "all_links_connected") -> dict[str, Any]:
    return validation_call(
        NODE.INPUT_TYPES(), set(inspect.signature(NODE.VALIDATE_INPUTS).parameters), scenario
    )


def validate_types(linked: object, *, direct_visual: object = None) -> bool | str:
    # A pre-fix hook cannot receive host-delegated types. Preserve its real signature for RED
    # instead of causing a TypeError, so failures describe wrong queue admission/refusal.
    arguments: dict[str, Any] = {"visual_result": direct_visual}
    if "input_types" in inspect.signature(NODE.VALIDATE_INPUTS).parameters:
        arguments["input_types"] = linked
    return NODE.VALIDATE_INPUTS(**arguments)


class PerceptionQueueTypeTests(unittest.TestCase):
    def test_correct_connected_visual_placeholder_is_admitted(self) -> None:
        self.assertIs(
            NODE.VALIDATE_INPUTS(**queue_arguments()), True, "correct_linked_visual_was_refused"
        )

    def test_required_links_without_visual_remain_refused(self) -> None:
        result = NODE.VALIDATE_INPUTS(**queue_arguments("required_links_only"))
        self.assertIsInstance(result, str)
        self.assertTrue(result)

    def test_every_socket_rejects_the_wrong_link_type(self) -> None:
        schema = NODE.INPUT_TYPES()
        correct = {
            name: declaration[0]
            for section in ("required", "optional")
            for name, declaration in schema[section].items()
        }
        for name in correct:
            with self.subTest(socket=name):
                linked = dict(correct, **{name: "WRONG_SOCKET_TYPE"})
                self.assertEqual(
                    validate_types(linked, direct_visual=object()), "linked_input_type_mismatch"
                )

    def test_unknown_or_malformed_type_maps_are_content_free_refusals(self) -> None:
        private_marker = "untrusted_operator_value"
        malformed: tuple[object, ...] = (
            [],
            "invalid",
            False,
            {private_marker: "H3_VISUAL_PRODUCER_RESULT"},
            {"visual_result": []},
            {"visual_result": True},
            {"visual_result": None},
            {1: "H3_VISUAL_PRODUCER_RESULT"},
        )
        for linked in malformed:
            with self.subTest(kind=type(linked).__name__):
                result = validate_types(linked, direct_visual=object())
                self.assertEqual(result, "linked_input_type_mismatch")
                self.assertNotIn(private_marker, str(result))

    def test_correct_optional_visual_type_admits_the_unresolved_value(self) -> None:
        self.assertIs(validate_types({"visual_result": "H3_VISUAL_PRODUCER_RESULT"}), True)

    def test_map_subclass_is_refused_without_invoking_its_reader(self) -> None:
        class UntrustedMap(dict[str, str]):
            def items(self) -> Any:
                raise AssertionError("untrusted_map_reader_was_invoked")

        self.assertEqual(
            validate_types(UntrustedMap(), direct_visual=object()), "linked_input_type_mismatch"
        )

    def test_direct_visual_structural_validation_preserves_execution_authority_boundary(
        self,
    ) -> None:
        self.assertIs(NODE.VALIDATE_INPUTS(visual_result=object()), True)
        self.assertIsInstance(NODE.VALIDATE_INPUTS(), str)


if __name__ == "__main__":
    unittest.main()
