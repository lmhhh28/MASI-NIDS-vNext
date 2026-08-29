from __future__ import annotations

import unittest

from masi_analysis.errors import AnalysisError
from masi_analysis.mcp import MCPClient
from masi_analysis.models import ProviderDelegation, ProviderToolRequest
from masi_analysis.peer import PeerClient

from .support import budgets, frozen_input


class ExternalBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_request_overflow_fails_before_transport_initialization(self) -> None:
        client = MCPClient.__new__(MCPClient)
        request = ProviderToolRequest(
            kind="tool",
            name="masi.evidence.get",
            arguments={"evidence_id": "evidence-1"},
        )
        for limit, requests in ((0, [request]), (1, [request, request])):
            input_bundle = frozen_input().model_copy(update={"budgets": budgets(tool_calls=limit)})
            with self.subTest(limit=limit), self.assertRaises(AnalysisError) as caught:
                await client.execute(requests, input_bundle, None)  # type: ignore[arg-type]
            self.assertEqual(caught.exception.code, "MCP_BUDGET_EXHAUSTED")

    async def test_a2a_request_overflow_fails_before_peer_lookup(self) -> None:
        client = PeerClient.__new__(PeerClient)
        request = ProviderDelegation(peer_id="fixture-peer", question="Provide bounded context")
        for limit, requests in ((0, [request]), (1, [request, request])):
            input_bundle = frozen_input().model_copy(update={"budgets": budgets(outbound_delegations=limit)})
            with self.subTest(limit=limit), self.assertRaises(AnalysisError) as caught:
                await client.delegate(
                    requests,
                    input_bundle,
                    allowed_peer_ids={"fixture-peer"},
                    secret_values=(),
                )
            self.assertEqual(caught.exception.code, "A2A_BUDGET_EXHAUSTED")


if __name__ == "__main__":
    unittest.main()
