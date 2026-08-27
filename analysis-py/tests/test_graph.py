from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from masi_analysis.canonical import compute_input_digest, compute_provider_response_digest
from masi_analysis.config import BindingState, load_runtime_material
from masi_analysis.errors import ProviderUnavailable
from masi_analysis.graph import TOPOLOGY_DIGEST, AnalysisGraph
from masi_analysis.mcp import ToolResult
from masi_analysis.models import (
    GroundedClaim,
    ProviderDelegation,
    ProviderRequest,
    ProviderResponse,
    ProviderToolRequest,
)

from .support import DIGEST_A, DIGEST_B, frozen_input, write_runtime

ZERO = "sha256:" + "0" * 64


def response_for(request: ProviderRequest, *, request_tool: bool) -> ProviderResponse:
    response = ProviderResponse(
        schema_version="masi-analysis-provider-response/v1",
        request_id=request.request_id,
        input_digest=request.input_digest,
        outcome_hint="succeeded",
        observed_claims=[GroundedClaim(claim="The authorized evidence was observed.", evidence_refs=["evidence-1"])],
        model_result_facts=[GroundedClaim(claim="The frozen model result is an input fact.", evidence_refs=["evidence-1"])],
        model_explanation_facts=[GroundedClaim(claim="TreeSHAP shows a qualified association.", evidence_refs=["explanation-1"])],
        inferred_claims=["Additional investigation may be useful."],
        uncertainties=["The available evidence is bounded."],
        limitations=["Association is not causality."],
        missing_evidence=["Independent outcome evidence remains desirable."],
        recommendations=["Review current Go-owned facts before preparing any separate proposal."],
        generated_content=["Bounded incident analysis draft."],
        tool_requests=([ProviderToolRequest(kind="tool", name="masi.evidence.get", arguments={"evidence_id": "explanation-1"})] if request_tool else []),
        delegations=[] if not request_tool else [ProviderDelegation(peer_id="peer-unused", question="Provide bounded context")],
        response_digest=ZERO,
    )
    return response.model_copy(update={"response_digest": compute_provider_response_digest(response)})


class ProviderStub:
    provider_id = "fixture-provider"
    model_id = "fixture-model"
    profile_digest = DIGEST_A
    secret_values: tuple[str, ...] = ()

    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def analyze(self, request: ProviderRequest, *, timeout_ms: int, response_bytes: int) -> ProviderResponse:
        del timeout_ms, response_bytes
        self.calls += 1
        if self.fail:
            raise ProviderUnavailable()
        return response_for(request, request_tool=request.phase == "hypothesis")


class MCPStub:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, requests: list[ProviderToolRequest], input_bundle: Any, binding: Any, *, round_number: int = 1) -> list[ToolResult]:
        del input_bundle, binding, round_number
        self.calls += len(requests)
        return [
            ToolResult(
                "tool-1",
                "tool",
                "masi.evidence.get",
                "explanation-1",
                "succeeded",
                DIGEST_A,
                1,
                "valid",
                False,
                {
                    "explanation": {
                        "evidence_id": "explanation-1",
                        "method": "tree-shap",
                        "background_digest": DIGEST_A,
                        "model_digest": DIGEST_A,
                        "scaler_digest": DIGEST_B,
                        "sample_digest": DIGEST_B,
                        "coverage": 1.0,
                        "truncated": False,
                        "limitations": ["Association is not causality."],
                    }
                },
            )
        ]


class PeerStub:
    async def delegate(self, requests: list[Any], input_bundle: Any, *, allowed_peer_ids: set[str], secret_values: tuple[str, ...]) -> list[Any]:
        del requests, input_bundle, allowed_peer_ids, secret_values
        return []


class GraphTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        contract_root = Path(__file__).resolve().parents[2] / "contracts"
        config_path = write_runtime(root, contract_root)
        self.material = load_runtime_material(str(config_path))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def input_for_material(self, *, quality: str = "valid") -> Any:
        value = frozen_input(quality=quality)
        value = value.model_copy(update={"config_digest": self.material.config_digest, "input_digest": DIGEST_A})
        return value.model_copy(update={"input_digest": compute_input_digest(value)})

    async def test_real_langgraph_bounded_grounding_and_explanation(self) -> None:
        provider = ProviderStub()
        mcp = MCPStub()
        graph = AnalysisGraph(BindingState(self.material), provider, mcp, PeerStub())  # type: ignore[arg-type]
        artifact, trace = await graph.run(self.input_for_material())
        self.assertEqual(artifact.analysis_outcome, "succeeded")
        self.assertTrue(artifact.non_executable)
        self.assertFalse(artifact.deployment_eligible)
        self.assertEqual(artifact.topology_digest, TOPOLOGY_DIGEST)
        self.assertEqual(len(artifact.model_explanation_facts), 1)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(mcp.calls, 1)
        self.assertIn("GROUNDING_VALIDATED", trace)

    async def test_provider_failure_returns_stable_insufficient_artifact(self) -> None:
        graph = AnalysisGraph(BindingState(self.material), ProviderStub(fail=True), MCPStub(), PeerStub())  # type: ignore[arg-type]
        artifact, _ = await graph.run(self.input_for_material())
        self.assertEqual(artifact.analysis_outcome, "limited")
        self.assertTrue(artifact.limitations)

    async def test_low_quality_never_claims_full_success(self) -> None:
        graph = AnalysisGraph(BindingState(self.material), ProviderStub(), MCPStub(), PeerStub())  # type: ignore[arg-type]
        artifact, _ = await graph.run(self.input_for_material(quality="low"))
        self.assertEqual(artifact.analysis_outcome, "limited")
        self.assertEqual(artifact.quality, "low")


if __name__ == "__main__":
    unittest.main()
