"""Regression tests for pinned ComfyUI linked-input pre-validation semantics."""

from __future__ import annotations

import unittest

from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextNativeH3AdapterNode,
    H3ContextPlanNode,
    H3ContextPreviewNode,
    H3ContextValidatorNode,
)


class HostValidationBoundaryTests(unittest.TestCase):
    def test_linked_typed_placeholders_are_deferred_until_execution(self) -> None:
        self.assertIs(H3ContextPlanNode.VALIDATE_INPUTS(None), True)
        self.assertIs(H3ContextCompilerNode.VALIDATE_INPUTS(None), True)
        self.assertIs(H3ContextValidatorNode.VALIDATE_INPUTS(None, None), True)
        self.assertIs(H3ContextPreviewNode.VALIDATE_INPUTS(None), True)
        self.assertIs(H3ContextNativeH3AdapterNode.VALIDATE_INPUTS(None), True)

    def test_direct_invalid_objects_remain_rejected(self) -> None:
        checks = (
            H3ContextPlanNode.VALIDATE_INPUTS(object()),
            H3ContextCompilerNode.VALIDATE_INPUTS(object()),
            H3ContextValidatorNode.VALIDATE_INPUTS(object(), object()),
            H3ContextPreviewNode.VALIDATE_INPUTS(object()),
            H3ContextNativeH3AdapterNode.VALIDATE_INPUTS(object()),
        )
        for result in checks:
            self.assertIsInstance(result, str)
            self.assertNotEqual(result, "")


if __name__ == "__main__":
    unittest.main()
