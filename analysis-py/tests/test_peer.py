from __future__ import annotations

import unittest

from masi_analysis.canonical import canonical_digest, go_json_bytes
from masi_analysis.errors import PeerUnavailable
from masi_analysis.peer import PeerClient

DIGEST_A = "sha256:" + "a" * 64


def peer_response(*, binding_generation: int = 3) -> bytes:
    data = {
        "schema_version": "masi-analysis-peer-artifact/v1",
        "summary": "Bounded untrusted peer context.",
        "source_refs": [],
        "content_digest": DIGEST_A,
    }
    data["content_digest"] = canonical_digest({key: value for key, value in data.items() if key != "content_digest"})
    return go_json_bytes(
        {
            "task": {
                "id": "peer-task-1",
                "contextId": "peer-context-1",
                "status": {"state": "TASK_STATE_COMPLETED", "timestamp": "2033-05-18T03:33:21Z"},
                "artifacts": [
                    {
                        "artifactId": "peer-artifact-1",
                        "name": "Bounded peer context",
                        "description": "Untrusted peer Artifact.",
                        "parts": [{"data": data, "mediaType": "application/json"}],
                        "metadata": {"contentDigest": data["content_digest"], "nonExecutable": True},
                    }
                ],
                "history": [],
                "metadata": {
                    "schemaVersion": "masi-analysis-peer-task/v1",
                    "pluginId": "fixture.peer.agent",
                    "bindingGeneration": binding_generation,
                    "inputDigest": DIGEST_A,
                    "traceId": "trace-peer-1",
                    "reasonCode": "PEER_SUCCEEDED",
                },
            }
        }
    )


class PeerContractTests(unittest.TestCase):
    def test_strict_peer_task_identity(self) -> None:
        task = PeerClient.decode_task_response(
            200,
            {"A2A-Version": "1.0", "Content-Type": "application/a2a+json"},
            peer_response(),
            plugin_id="fixture.peer.agent",
            binding_generation=3,
            input_digest=DIGEST_A,
            trace_id="trace-peer-1",
        )
        self.assertEqual(task.task_id, "peer-task-1")

    def test_wrong_peer_binding_and_unknown_field_fail_closed(self) -> None:
        with self.assertRaises(PeerUnavailable):
            PeerClient.decode_task_response(
                200,
                {"A2A-Version": "1.0", "Content-Type": "application/a2a+json"},
                peer_response(binding_generation=2),
                plugin_id="fixture.peer.agent",
                binding_generation=3,
                input_digest=DIGEST_A,
                trace_id="trace-peer-1",
            )
        malformed = peer_response()[:-1] + b',"unexpected":true}'
        with self.assertRaises(PeerUnavailable):
            PeerClient.decode_task_response(
                200,
                {"A2A-Version": "1.0", "Content-Type": "application/a2a+json"},
                malformed,
                plugin_id="fixture.peer.agent",
                binding_generation=3,
                input_digest=DIGEST_A,
                trace_id="trace-peer-1",
            )


if __name__ == "__main__":
    unittest.main()
