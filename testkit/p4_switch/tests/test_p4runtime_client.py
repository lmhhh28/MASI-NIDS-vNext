from __future__ import annotations

# P4Runtime messages are descriptor-backed in the pinned protobuf package.
# pyright: reportAttributeAccessIssue=false

import queue
import unittest

from p4.v1 import p4runtime_pb2

from testkit.p4_switch.lib.p4runtime_client import P4RuntimeClient


class SupplementalHintDrainTests(unittest.TestCase):
    @staticmethod
    def client() -> P4RuntimeClient:
        client = P4RuntimeClient.__new__(P4RuntimeClient)
        client.election_id = 100
        client._stream_error = None
        client._requests = queue.Queue(maxsize=128)
        client._responses = queue.Queue(maxsize=4096)
        return client

    @staticmethod
    def digest(digest_id: int, list_id: int):
        response = p4runtime_pb2.StreamMessageResponse()
        response.digest.digest_id = digest_id
        response.digest.list_id = list_id
        return response

    def test_digest_is_acked_and_packet_in_is_consumed(self) -> None:
        client = self.client()
        client._responses.put_nowait(self.digest(17, 23))
        packet = p4runtime_pb2.StreamMessageResponse()
        packet.packet.payload = b"packet"
        client._responses.put_nowait(packet)

        observed = client.drain_supplemental_hints(
            quiet_seconds=0.001,
            max_messages=4,
        )

        self.assertEqual(1, observed["digest_hints"])
        self.assertEqual(1, observed["packet_in_hints"])
        self.assertEqual(1, observed["digest_acks"])
        self.assertEqual(2, observed["total_messages"])
        self.assertEqual(2, observed["response_queue_before"])
        self.assertEqual(0, observed["response_queue_after"])
        ack = client._requests.get_nowait()
        assert ack is not None
        self.assertEqual(17, ack.digest_ack.digest_id)
        self.assertEqual(23, ack.digest_ack.list_id)

    def test_over_limit_burst_fails_closed(self) -> None:
        client = self.client()
        client._responses.put_nowait(self.digest(17, 1))
        client._responses.put_nowait(self.digest(17, 2))

        with self.assertRaisesRegex(RuntimeError, "bounded message limit 1"):
            client.drain_supplemental_hints(
                quiet_seconds=0.001,
                max_messages=1,
            )

    def test_invalid_drain_bounds_are_rejected(self) -> None:
        client = self.client()
        with self.assertRaises(ValueError):
            client.drain_supplemental_hints(max_messages=0)
        with self.assertRaises(ValueError):
            client.drain_supplemental_hints(max_messages=257)
        with self.assertRaises(ValueError):
            client.drain_supplemental_hints(quiet_seconds=0)


if __name__ == "__main__":
    unittest.main()
