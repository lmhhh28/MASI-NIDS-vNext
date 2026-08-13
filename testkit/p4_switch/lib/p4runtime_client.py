"""Small, bounded P4Runtime 1.4 test controller.

This adapter exists only inside the isolated module testkit.  It deliberately
does not implement assignment, scheduling, journaling, retry, or any Edge
Agent responsibility.
"""

# The pinned P4Runtime protobuf package builds message classes and enum members
# dynamically, so Pyright cannot use them as annotation forms or resolve their
# descriptor-backed attributes.  Keep the exception scoped to this adapter.
# pyright: reportInvalidTypeForm=false, reportAttributeAccessIssue=false

from __future__ import annotations

import hashlib
import ipaddress
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence

import grpc
from google.protobuf import text_format
from p4.config.v1 import p4info_pb2
from p4.v1 import p4data_pb2
from p4.v1 import p4runtime_pb2
from p4.v1 import p4runtime_pb2_grpc


MAX_UPDATES_PER_WRITE = 256
MAX_ENTITIES_PER_READ = 256
MAX_SUPPLEMENTAL_HINTS_PER_DRAIN = 256


def _bytes_for_width(value: int, bitwidth: int) -> bytes:
    if value < 0 or value >= 1 << bitwidth:
        raise ValueError(f"value {value} does not fit bit<{bitwidth}>")
    return value.to_bytes(max(1, (bitwidth + 7) // 8), "big")


def _varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint cannot encode a negative value")
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def bmv2_device_config(json_bytes: bytes) -> bytes:
    """Encode p4.tmp.P4DeviceConfig{device_data: json_bytes}.

    The BMv2 wrapper is a target-specific three-field protobuf that is not
    distributed by the P4Runtime PyPI package.  Encoding field 3 (wire type 2)
    directly keeps the test image independent from a second p4c installation.
    """

    return b"\x1a" + _varint(len(json_bytes)) + json_bytes


@dataclass(frozen=True)
class Ternary:
    value: int
    mask: int


@dataclass(frozen=True)
class ReadbackKey:
    table_id: int
    priority: int
    fields: tuple[tuple[int, str, int, int], ...]
    action_id: int
    params: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class PipelineIdentity:
    p4info_digest: str
    device_config_digest: str
    cookie: int


class PipelineIdentityMismatch(RuntimeError):
    reason_code = "P4INFO_PIPELINE_DRIFT"


class CompiledPlanConflict(ValueError):
    reason_code = "EQUAL_PRIORITY_CONFLICT"


class P4InfoIndex:
    """Validated P4Info name/id and protobuf entity builder."""

    def __init__(self, path: str | Path) -> None:
        self.p4info = p4info_pb2.P4Info()
        text_format.Merge(Path(path).read_text(encoding="utf-8"), self.p4info)
        self._tables = self._index(self.p4info.tables)
        self._actions = self._index(self.p4info.actions)
        self._counters = self._index(self.p4info.counters)
        self._direct_counters = self._index(self.p4info.direct_counters)
        self._registers = self._index(self.p4info.registers)
        self._digests = self._index(self.p4info.digests)

    @staticmethod
    def _index(objects: Iterable[object]) -> dict[str, object]:
        result: dict[str, object] = {}
        for obj in objects:
            result[obj.preamble.name] = obj
            result[obj.preamble.alias] = obj
        return result

    def table(self, name: str):
        return self._tables[name]

    def action_info(self, name: str):
        return self._actions[name]

    def counter_id(self, name: str) -> int:
        return self._counters[name].preamble.id

    def direct_counter_id(self, name: str) -> int:
        return self._direct_counters[name].preamble.id

    def register_id(self, name: str) -> int:
        return self._registers[name].preamble.id

    def digest_id(self, name: str) -> int:
        return self._digests[name].preamble.id

    def action(self, name: str, **params: int) -> p4runtime_pb2.Action:
        info = self.action_info(name)
        expected = {param.name for param in info.params}
        if set(params) != expected:
            raise ValueError(
                f"action {name} expected params {sorted(expected)}, got {sorted(params)}"
            )
        result = p4runtime_pb2.Action(action_id=info.preamble.id)
        for param in info.params:
            encoded = result.params.add()
            encoded.param_id = param.id
            encoded.value = _bytes_for_width(params[param.name], param.bitwidth)
        return result

    def table_entry(
        self,
        table_name: str,
        matches: Mapping[str, int | Ternary],
        action_name: str | None = None,
        action_params: Mapping[str, int] | None = None,
        *,
        priority: int = 0,
        is_default_action: bool = False,
    ) -> p4runtime_pb2.TableEntry:
        table = self.table(table_name)
        result = p4runtime_pb2.TableEntry(
            table_id=table.preamble.id,
            priority=priority,
            is_default_action=is_default_action,
        )
        fields = {field.name: field for field in table.match_fields}
        unknown = set(matches) - set(fields)
        if unknown:
            raise ValueError(f"unknown fields for {table_name}: {sorted(unknown)}")
        if not is_default_action:
            for name, value in matches.items():
                info = fields[name]
                match = result.match.add(field_id=info.id)
                if info.match_type == p4info_pb2.MatchField.EXACT:
                    if isinstance(value, Ternary):
                        raise ValueError(f"exact field {name} received a mask")
                    match.exact.value = _bytes_for_width(value, info.bitwidth)
                elif info.match_type == p4info_pb2.MatchField.TERNARY:
                    if not isinstance(value, Ternary):
                        raise ValueError(f"ternary field {name} requires Ternary")
                    if value.mask == 0:
                        result.match.pop()
                        continue
                    match.ternary.value = _bytes_for_width(
                        value.value & value.mask, info.bitwidth
                    )
                    match.ternary.mask = _bytes_for_width(value.mask, info.bitwidth)
                else:
                    raise ValueError(f"unsupported match type for {name}")
            exact_names = {
                field.name
                for field in table.match_fields
                if field.match_type == p4info_pb2.MatchField.EXACT
            }
            if not exact_names.issubset(matches):
                missing = sorted(exact_names - set(matches))
                raise ValueError(f"missing exact fields for {table_name}: {missing}")
        if action_name is not None:
            result.action.action.CopyFrom(
                self.action(action_name, **dict(action_params or {}))
            )
        return result

    def baseline_entry(
        self,
        bank: int,
        *,
        src_ipv4: int,
        action: str,
        priority: int,
        ingress_port: int | None = None,
        dst_ipv4: int | None = None,
        protocol: int | None = None,
        l4_present: int | None = None,
        src_port: int | None = None,
        dst_port: int | None = None,
        fragment_class: int | None = None,
    ) -> p4runtime_pb2.TableEntry:
        def masked(value: int | None, width: int) -> Ternary:
            return Ternary(0, 0) if value is None else Ternary(value, (1 << width) - 1)

        return self.table_entry(
            f"baseline_bank_{bank}",
            {
                "standard_metadata.ingress_port": masked(ingress_port, 9),
                "hdr.ipv4.src_addr": masked(src_ipv4, 32),
                "hdr.ipv4.dst_addr": masked(dst_ipv4, 32),
                "hdr.ipv4.protocol": masked(protocol, 8),
                "meta.l4_present": masked(l4_present, 1),
                "meta.l4_src_port": masked(src_port, 16),
                "meta.l4_dst_port": masked(dst_port, 16),
                "meta.fragment_class": masked(fragment_class, 2),
            },
            "baseline_drop" if action == "drop" else "baseline_permit_and_continue",
            priority=priority,
        )

    def overlay_entry(
        self,
        *,
        src_ipv4: str,
        dst_ipv4: str,
        protocol: int,
        src_port: int,
        dst_port: int,
        action: str,
    ) -> p4runtime_pb2.TableEntry:
        return self.table_entry(
            "response_overlay",
            {
                "meta.ipv4_supported": 1,
                "meta.l4_present": 1,
                "hdr.ipv4.src_addr": int(ipaddress.IPv4Address(src_ipv4)),
                "hdr.ipv4.dst_addr": int(ipaddress.IPv4Address(dst_ipv4)),
                "hdr.ipv4.protocol": protocol,
                "meta.l4_src_port": src_port,
                "meta.l4_dst_port": dst_port,
            },
            "overlay_drop" if action == "drop" else "overlay_permit_and_continue",
        )

    @staticmethod
    def readback_key(entry: p4runtime_pb2.TableEntry) -> ReadbackKey:
        fields: list[tuple[int, str, int, int]] = []
        for match in entry.match:
            kind = match.WhichOneof("field_match_type")
            if kind == "exact":
                fields.append(
                    (match.field_id, kind, int.from_bytes(match.exact.value, "big"), 0)
                )
            elif kind == "ternary":
                fields.append(
                    (
                        match.field_id,
                        kind,
                        int.from_bytes(match.ternary.value, "big"),
                        int.from_bytes(match.ternary.mask, "big"),
                    )
                )
            else:
                raise ValueError(f"unsupported readback match {kind}")
        action = entry.action.action
        return ReadbackKey(
            table_id=entry.table_id,
            priority=entry.priority,
            fields=tuple(sorted(fields)),
            action_id=action.action_id,
            params=tuple(
                sorted(
                    (param.param_id, int.from_bytes(param.value, "big"))
                    for param in action.params
                )
            ),
        )


class P4RuntimeClient:
    """One P4Runtime StreamChannel and bounded unary RPC client."""

    def __init__(
        self,
        address: str,
        *,
        device_id: int,
        election_id: int,
        ca_path: str | Path,
        cert_path: str | Path,
        key_path: str | Path,
        server_name: str = "masi-switch",
        rpc_timeout: float = 10.0,
    ) -> None:
        self.device_id = device_id
        self.election_id = election_id
        self.rpc_timeout = rpc_timeout
        credentials = grpc.ssl_channel_credentials(
            root_certificates=Path(ca_path).read_bytes(),
            private_key=Path(key_path).read_bytes(),
            certificate_chain=Path(cert_path).read_bytes(),
        )
        self.channel = grpc.secure_channel(
            address,
            credentials,
            options=(("grpc.ssl_target_name_override", server_name),),
        )
        grpc.channel_ready_future(self.channel).result(timeout=rpc_timeout)
        self.stub = p4runtime_pb2_grpc.P4RuntimeStub(self.channel)
        self._requests: queue.Queue[p4runtime_pb2.StreamMessageRequest | None] = (
            queue.Queue(maxsize=128)
        )
        self._responses: queue.Queue[p4runtime_pb2.StreamMessageResponse] = queue.Queue(
            maxsize=4096
        )
        self._stream_error: BaseException | None = None
        self._thread = threading.Thread(target=self._consume_stream, daemon=True)
        self._thread.start()
        self.arbitration = self._arbitrate()
        self.expected_pipeline_identity: PipelineIdentity | None = None

    def _request_iterator(self) -> Iterator[p4runtime_pb2.StreamMessageRequest]:
        while True:
            request = self._requests.get()
            if request is None:
                return
            yield request

    def _consume_stream(self) -> None:
        try:
            for response in self.stub.StreamChannel(self._request_iterator()):
                self._responses.put(response, timeout=5)
        except BaseException as exc:  # retained and surfaced by wait_for
            self._stream_error = exc

    def _arbitrate(self) -> p4runtime_pb2.MasterArbitrationUpdate:
        request = p4runtime_pb2.StreamMessageRequest()
        request.arbitration.device_id = self.device_id
        request.arbitration.election_id.high = 0
        request.arbitration.election_id.low = self.election_id
        self._requests.put(request, timeout=1)
        response = self.wait_for("arbitration", timeout=self.rpc_timeout)
        return response.arbitration

    def wait_for(
        self, kind: str, *, timeout: float
    ) -> p4runtime_pb2.StreamMessageResponse:
        deadline = time.monotonic() + timeout
        deferred: list[p4runtime_pb2.StreamMessageResponse] = []
        try:
            while time.monotonic() < deadline:
                if self._stream_error is not None and self._responses.empty():
                    raise RuntimeError(
                        "P4Runtime stream failed"
                    ) from self._stream_error
                try:
                    response = self._responses.get(
                        timeout=min(0.1, deadline - time.monotonic())
                    )
                except queue.Empty:
                    continue
                if response.WhichOneof("update") == kind:
                    return response
                deferred.append(response)
            raise TimeoutError(f"timed out waiting for StreamChannel {kind}")
        finally:
            for response in deferred:
                self._responses.put_nowait(response)

    def drain_stream(self, *, quiet_seconds: float = 0.1) -> dict[str, int]:
        """Drain stale stream responses until the bounded quiet interval elapses."""

        if quiet_seconds <= 0 or quiet_seconds > 1:
            raise ValueError("quiet_seconds must be in (0, 1]")
        counts: dict[str, int] = {}
        deadline = time.monotonic() + quiet_seconds
        while time.monotonic() < deadline:
            try:
                response = self._responses.get(
                    timeout=min(0.02, deadline - time.monotonic())
                )
            except queue.Empty:
                continue
            kind = response.WhichOneof("update") or "unknown"
            counts[kind] = counts.get(kind, 0) + 1
            deadline = time.monotonic() + quiet_seconds
        return counts

    def drain_supplemental_hints(
        self,
        *,
        quiet_seconds: float = 0.05,
        max_messages: int = MAX_SUPPLEMENTAL_HINTS_PER_DRAIN,
    ) -> dict[str, int]:
        """Consume bounded Digest/PacketIn hints and ACK every DigestList.

        Qualification workloads periodically call this while traffic is active.
        Queue depths before and after the drain are returned so resource evidence
        includes the transient pressure instead of observing only the empty queue.
        Unexpected StreamChannel messages and an over-limit burst fail closed.
        """

        if quiet_seconds <= 0 or quiet_seconds > 1:
            raise ValueError("quiet_seconds must be in (0, 1]")
        if not 1 <= max_messages <= MAX_SUPPLEMENTAL_HINTS_PER_DRAIN:
            raise ValueError(
                f"max_messages must be in [1, {MAX_SUPPLEMENTAL_HINTS_PER_DRAIN}]"
            )
        counts = {
            "digest_hints": 0,
            "packet_in_hints": 0,
            "digest_acks": 0,
            "primary_arbitration_updates": 0,
            "total_messages": 0,
            "request_queue_before": self._requests.qsize(),
            "response_queue_before": self._responses.qsize(),
        }
        deadline = time.monotonic() + quiet_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if self._stream_error is not None and self._responses.empty():
                raise RuntimeError("P4Runtime stream failed") from self._stream_error
            try:
                response = self._responses.get(timeout=min(0.02, remaining))
            except queue.Empty:
                continue
            if counts["total_messages"] >= max_messages:
                raise RuntimeError(
                    "supplemental hint drain exceeded bounded message limit "
                    f"{max_messages}"
                )
            kind = response.WhichOneof("update") or "unknown"
            counts["total_messages"] += 1
            if kind == "digest":
                self.send_digest_ack(
                    response.digest.digest_id,
                    response.digest.list_id,
                )
                counts["digest_hints"] += 1
                counts["digest_acks"] += 1
            elif kind == "packet":
                counts["packet_in_hints"] += 1
            elif kind == "arbitration":
                arbitration = response.arbitration
                if not (
                    arbitration.status.code == 0
                    and arbitration.election_id.high == 0
                    and arbitration.election_id.low == self.election_id
                ):
                    raise RuntimeError("unexpected non-primary arbitration update")
                counts["primary_arbitration_updates"] += 1
            else:
                raise RuntimeError(f"unexpected StreamChannel message: {kind}")
            deadline = time.monotonic() + quiet_seconds
        counts["request_queue_after"] = self._requests.qsize()
        counts["response_queue_after"] = self._responses.qsize()
        return counts

    @property
    def is_primary(self) -> bool:
        return (
            self.arbitration.status.code == 0
            and self.arbitration.election_id.high == 0
            and self.arbitration.election_id.low == self.election_id
        )

    def queue_depths(self) -> dict[str, int]:
        """Expose bounded local adapter queues for qualification sampling."""

        return {
            "request": self._requests.qsize(),
            "response": self._responses.qsize(),
            "request_limit": self._requests.maxsize,
            "response_limit": self._responses.maxsize,
        }

    @staticmethod
    def _p4info_digest(p4info: p4info_pb2.P4Info) -> str:
        payload = p4info.SerializeToString(deterministic=True)
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _bytes_digest(payload: bytes) -> str:
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def set_pipeline(
        self, p4info: p4info_pb2.P4Info, bmv2_json: bytes
    ) -> PipelineIdentity:
        device_config = bmv2_device_config(bmv2_json)
        identity_material = p4info.SerializeToString(deterministic=True) + device_config
        cookie = (
            int.from_bytes(hashlib.sha256(identity_material).digest()[:8], "big") or 1
        )
        request = p4runtime_pb2.SetForwardingPipelineConfigRequest(
            device_id=self.device_id,
            election_id=p4runtime_pb2.Uint128(low=self.election_id),
            action=p4runtime_pb2.SetForwardingPipelineConfigRequest.VERIFY_AND_COMMIT,
        )
        request.config.p4info.CopyFrom(p4info)
        request.config.p4_device_config = device_config
        request.config.cookie.cookie = cookie
        self.stub.SetForwardingPipelineConfig(request, timeout=self.rpc_timeout)
        identity = PipelineIdentity(
            p4info_digest=self._p4info_digest(p4info),
            device_config_digest=self._bytes_digest(device_config),
            cookie=cookie,
        )
        self.expected_pipeline_identity = identity
        return identity

    def get_pipeline_identity(self) -> PipelineIdentity:
        request = p4runtime_pb2.GetForwardingPipelineConfigRequest(
            device_id=self.device_id,
            response_type=p4runtime_pb2.GetForwardingPipelineConfigRequest.ALL,
        )
        response = self.stub.GetForwardingPipelineConfig(
            request, timeout=self.rpc_timeout
        )
        if not response.config.HasField("p4info"):
            raise PipelineIdentityMismatch("target omitted P4Info readback")
        if not response.config.HasField("cookie"):
            raise PipelineIdentityMismatch("target omitted pipeline cookie readback")
        return PipelineIdentity(
            p4info_digest=self._p4info_digest(response.config.p4info),
            device_config_digest=self._bytes_digest(response.config.p4_device_config),
            cookie=response.config.cookie.cookie,
        )

    def verify_pipeline_identity(
        self, expected: PipelineIdentity | None = None
    ) -> PipelineIdentity:
        expected_identity = expected or self.expected_pipeline_identity
        if expected_identity is None:
            raise PipelineIdentityMismatch("no expected pipeline identity")
        observed = self.get_pipeline_identity()
        if observed != expected_identity:
            raise PipelineIdentityMismatch(
                f"expected {expected_identity}, observed {observed}"
            )
        return observed

    def _write_request(
        self, update_type: int, entities: Sequence[p4runtime_pb2.Entity]
    ) -> p4runtime_pb2.WriteRequest:
        if len(entities) > MAX_UPDATES_PER_WRITE:
            raise ValueError("write exceeds 256-update bound")
        request = p4runtime_pb2.WriteRequest(
            device_id=self.device_id,
            election_id=p4runtime_pb2.Uint128(low=self.election_id),
            atomicity=p4runtime_pb2.WriteRequest.CONTINUE_ON_ERROR,
        )
        for entity in entities:
            update = request.updates.add(type=update_type)
            update.entity.CopyFrom(entity)
        return request

    def write(
        self,
        update_type: int,
        entities: Sequence[p4runtime_pb2.Entity],
        *,
        timeout: float | None = None,
    ) -> None:
        request = self._write_request(update_type, entities)
        self.stub.Write(request, timeout=timeout or self.rpc_timeout)

    def write_if_pipeline_matches(
        self,
        expected: PipelineIdentity,
        update_type: int,
        entities: Sequence[p4runtime_pb2.Entity],
        *,
        timeout: float | None = None,
    ) -> None:
        self.verify_pipeline_identity(expected)
        self.write(update_type, entities, timeout=timeout)

    def write_without_observing_response(
        self,
        update_type: int,
        entities: Sequence[p4runtime_pb2.Entity],
        *,
        timeout: float | None = None,
    ):
        """Issue a real Write but intentionally discard its unary response.

        The isolated fixture retains the future only long enough for independent
        P4Runtime Read reconciliation.  This models a controller-side response
        loss without replacing the BMv2 target or its public boundary.
        """

        request = self._write_request(update_type, entities)
        return self.stub.Write.future(request, timeout=timeout or self.rpc_timeout)

    def write_batched(
        self,
        update_type: int,
        entities: Sequence[p4runtime_pb2.Entity],
        *,
        timeout: float | None = None,
    ) -> None:
        for offset in range(0, len(entities), MAX_UPDATES_PER_WRITE):
            self.write(
                update_type,
                entities[offset : offset + MAX_UPDATES_PER_WRITE],
                timeout=timeout,
            )

    def read(
        self,
        entities: Sequence[p4runtime_pb2.Entity],
        *,
        timeout: float | None = None,
    ) -> list[p4runtime_pb2.Entity]:
        if len(entities) > MAX_ENTITIES_PER_READ:
            raise ValueError("read exceeds 256-entity bound")
        request = p4runtime_pb2.ReadRequest(device_id=self.device_id)
        request.entities.extend(entities)
        result: list[p4runtime_pb2.Entity] = []
        for response in self.stub.Read(request, timeout=timeout or self.rpc_timeout):
            result.extend(response.entities)
        return result

    def read_batched(
        self,
        entities: Sequence[p4runtime_pb2.Entity],
        *,
        timeout: float | None = None,
    ) -> list[p4runtime_pb2.Entity]:
        result: list[p4runtime_pb2.Entity] = []
        for offset in range(0, len(entities), MAX_ENTITIES_PER_READ):
            result.extend(
                self.read(
                    entities[offset : offset + MAX_ENTITIES_PER_READ], timeout=timeout
                )
            )
        return result

    def send_digest_ack(self, digest_id: int, list_id: int) -> None:
        request = p4runtime_pb2.StreamMessageRequest()
        request.digest_ack.digest_id = digest_id
        request.digest_ack.list_id = list_id
        self._requests.put(request, timeout=1)

    def close(self) -> None:
        try:
            self._requests.put_nowait(None)
        except queue.Full:
            pass
        self.channel.close()
        self._thread.join(timeout=2)

    def __enter__(self) -> "P4RuntimeClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def table_entity(entry: p4runtime_pb2.TableEntry) -> p4runtime_pb2.Entity:
    entity = p4runtime_pb2.Entity()
    entity.table_entry.CopyFrom(entry)
    return entity


def table_key(entry: p4runtime_pb2.TableEntry) -> p4runtime_pb2.TableEntry:
    key = p4runtime_pb2.TableEntry(
        table_id=entry.table_id,
        priority=entry.priority,
        is_default_action=entry.is_default_action,
    )
    key.match.extend(entry.match)
    return key


def table_key_entity(entry: p4runtime_pb2.TableEntry) -> p4runtime_pb2.Entity:
    return table_entity(table_key(entry))


def direct_counter_entity(
    entry: p4runtime_pb2.TableEntry,
    *,
    packets: int | None = None,
    bytes_: int | None = None,
) -> p4runtime_pb2.Entity:
    entity = p4runtime_pb2.Entity()
    entity.direct_counter_entry.table_entry.CopyFrom(table_key(entry))
    if packets is not None:
        entity.direct_counter_entry.data.packet_count = packets
    if bytes_ is not None:
        entity.direct_counter_entry.data.byte_count = bytes_
    return entity


def counter_entity(
    counter_id: int,
    index: int,
    *,
    packets: int | None = None,
    bytes_: int | None = None,
) -> p4runtime_pb2.Entity:
    entity = p4runtime_pb2.Entity()
    entity.counter_entry.counter_id = counter_id
    entity.counter_entry.index.index = index
    if packets is not None:
        entity.counter_entry.data.packet_count = packets
    if bytes_ is not None:
        entity.counter_entry.data.byte_count = bytes_
    return entity


def register_entity(
    register_id: int, index: int, *, value: int | None = None
) -> p4runtime_pb2.Entity:
    entity = p4runtime_pb2.Entity()
    entity.register_entry.register_id = register_id
    entity.register_entry.index.index = index
    if value is not None:
        entity.register_entry.data.CopyFrom(
            p4data_pb2.P4Data(bitstring=_bytes_for_width(value, 64))
        )
    return entity


def digest_entity(digest_id: int) -> p4runtime_pb2.Entity:
    entity = p4runtime_pb2.Entity()
    entity.digest_entry.digest_id = digest_id
    entity.digest_entry.config.max_timeout_ns = 100_000_000
    entity.digest_entry.config.max_list_size = 16
    entity.digest_entry.config.ack_timeout_ns = 1_000_000_000
    return entity


def clone_session_entity(session_id: int, cpu_port: int) -> p4runtime_pb2.Entity:
    entity = p4runtime_pb2.Entity()
    clone = entity.packet_replication_engine_entry.clone_session_entry
    clone.session_id = session_id
    clone.class_of_service = 0
    # BMv2 does not implement clone truncation; zero means no truncation.
    clone.packet_length_bytes = 0
    replica = clone.replicas.add()
    replica.egress_port = cpu_port
    replica.instance = 1
    return entity


def selector_entity(
    index: P4InfoIndex, table: str, action: str, *, epoch: int | None = None
) -> p4runtime_pb2.Entity:
    params = {} if epoch is None else {"epoch": epoch}
    return table_entity(
        index.table_entry(table, {"meta.selector_key": 0}, action, params)
    )


def default_action_entity(
    index: P4InfoIndex, bank: int, action: str
) -> p4runtime_pb2.Entity:
    return table_entity(
        index.table_entry(
            f"baseline_bank_{bank}",
            {},
            "baseline_drop" if action == "drop" else "baseline_permit_and_continue",
            is_default_action=True,
        )
    )


def readback_selectors(
    entries: Sequence[p4runtime_pb2.TableEntry],
) -> list[p4runtime_pb2.Entity]:
    return [table_key_entity(entry) for entry in entries]


def reject_equal_priority_conflicts(
    entries: Sequence[p4runtime_pb2.TableEntry],
) -> None:
    seen: dict[tuple[int, int, tuple[tuple[int, str, int, int], ...]], ReadbackKey] = {}
    for entry in entries:
        key = P4InfoIndex.readback_key(entry)
        match_identity = (key.table_id, key.priority, key.fields)
        previous = seen.get(match_identity)
        if previous is not None and (
            previous.action_id != key.action_id or previous.params != key.params
        ):
            raise CompiledPlanConflict(
                "equal-priority overlapping entries have different actions"
            )
        seen[match_identity] = key
