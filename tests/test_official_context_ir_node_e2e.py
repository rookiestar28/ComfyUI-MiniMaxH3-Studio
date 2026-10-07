"""M4-06 mocked host-boundary path for the optional official Context-IR node."""

from __future__ import annotations

import unittest

from comfyui_h3_context.core import (
    OfficialAspectRatio,
    OfficialContextIRTransportResponse,
    ProviderIdentity,
    RawContextRequest,
    ResolvedCredential,
    TaskMode,
)
from comfyui_h3_context.nodes import OFFICIAL_CONTEXT_IR_NODE_ID
from comfyui_h3_context.registration import NODE_CLASS_MAPPINGS


class MockHostResolver:
    def resolve(self, reference: str) -> ResolvedCredential:
        return ResolvedCredential(reference=reference, value="host-runtime-secret")


class MockHostTransport:
    def __init__(self) -> None:
        self.created = False

    def create(
        self,
        payload: dict[str, object],
        credential: ResolvedCredential,
    ) -> OfficialContextIRTransportResponse:
        assert payload["model"] == "MiniMax-H3"
        assert credential.value == "host-runtime-secret"
        self.created = True
        return OfficialContextIRTransportResponse(202, {"task_id": "host-task-1"})

    def query(
        self,
        task_id: str,
        credential: ResolvedCredential,
    ) -> OfficialContextIRTransportResponse:
        assert task_id == "host-task-1"
        assert credential.value == "host-runtime-secret"
        return OfficialContextIRTransportResponse(
            200,
            {
                "task": {
                    "status": "succeeded",
                    "task_type": "h3_context_ir",
                    "content": {"prompt": "mock host official prompt"},
                }
            },
        )


class OfficialContextIRMockHostE2ETests(unittest.TestCase):
    def test_registered_node_executes_through_mock_host_without_native_generation(self) -> None:
        node_class = NODE_CLASS_MAPPINGS[OFFICIAL_CONTEXT_IR_NODE_ID]
        transport = MockHostTransport()
        node = node_class(transport=transport, resolver=MockHostResolver())  # type: ignore[call-arg]
        request = RawContextRequest(
            mode=TaskMode.T2VA,
            user_intent="A host-boundary fixture prompt.",
            duration_seconds=4,
        )
        prompt, receipt, notice = node.execute(  # type: ignore[attr-defined]
            request,
            ratio=OfficialAspectRatio.RATIO_16_9.value,
            upload_consent=True,
            network_allowed=True,
            credential_reference="env.official_minimax",
        )
        self.assertEqual(prompt, "mock host official prompt")
        self.assertEqual(receipt.provider, ProviderIdentity.OFFICIAL_MINIMAX)
        self.assertTrue(notice.consent_granted)
        self.assertTrue(transport.created)
        self.assertNotIn("MiniMaxH3ImageToVideo", prompt)


if __name__ == "__main__":
    unittest.main()
