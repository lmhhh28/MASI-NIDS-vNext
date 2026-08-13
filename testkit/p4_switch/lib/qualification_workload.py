"""Bounded real-wire workload helpers for BMv2 qualification.

The sender and both capture oracles run in the isolated runner container which
shares the BMv2 network namespace.  ``masi-p1`` is the test-ingress side of port
1 and ``masi-p2`` is the independent packet-outcome side of port 2.  P4Runtime
readback remains a separate oracle and is never inferred from packet capture.
"""

# The pinned P4Runtime protobuf package exposes generated enum members and
# P4Info descriptor objects without static attribute stubs.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import hashlib
import math
import multiprocessing
import random
import socket
import struct
import threading
import time
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence, TypedDict

from p4.v1 import p4runtime_pb2

from testkit.p4_switch.lib.p4runtime_client import (
    P4InfoIndex,
    P4RuntimeClient,
    default_action_entity,
    readback_selectors,
    selector_entity,
    table_entity,
    table_key_entity,
)


FRAME_BYTES = 256
ETH_P_ALL = 0x0003
PACKET_OUTGOING = 4
SOL_PACKET = 263
PACKET_STATISTICS = 6
MAGIC = b"MASIQV1!"
PAYLOAD_OFFSET = 14 + 20 + 8
TOKEN_OFFSET = PAYLOAD_OFFSET + len(MAGIC)
SEQUENCE_OFFSET = TOKEN_OFFSET + 8
SEND_NS_OFFSET = SEQUENCE_OFFSET + 8
PHASE_OFFSET = SEND_NS_OFFSET + 8
MAC_PORT_1 = bytes.fromhex("000000000101")
MAC_PORT_2 = bytes.fromhex("000000000202")
MAX_WORKLOAD_PACKETS = 4_000_000
MAX_WORKLOAD_SAMPLES = 400
MAX_LATENCY_SAMPLES_PER_PHASE = 65_536
SENDER_EXECUTION_PROFILE = "multiprocessing-spawn/v1"
SENDER_PROGRESS_PUBLISH_PACKETS = 64
SENDER_READY_TIMEOUT_SECONDS = 5.0
SENDER_JOIN_GRACE_SECONDS = 10.0
SENDER_SEND_BUFFER_BYTES = 4 * 1024 * 1024
CAPTURE_EXECUTION_PROFILE = "af-packet-thread/v1"
CAPTURE_RECEIVE_BUFFER_BYTES = 16 * 1024 * 1024
CAPTURE_DRAIN_TIMEOUT_SECONDS = 2.0
CAPTURE_DRAIN_STABLE_INTERVALS = 5
_UINT64_MASK = (1 << 64) - 1


class _SenderState(TypedDict):
    attempted: int
    accepted: int
    accepted_bytes: int
    by_phase: dict[int, int]
    attempted_by_phase: dict[int, int]
    errors: list[str]
    maximum_schedule_lateness_ns: int
    maximum_catch_up_backlog_packets: int
    packets_late_over_interval: int
    maximum_schedule_lateness_ns_by_phase: dict[int, int]
    actual_send_buffer_bytes: int


class _SenderProgressSnapshot(TypedDict):
    attempted: int
    accepted: int
    accepted_bytes: int
    by_phase: dict[int, int]


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def percentile(values: Sequence[int], quantile: float) -> float | None:
    """Return a deterministic nearest-rank percentile in milliseconds."""

    if not values:
        return None
    if quantile < 0 or quantile > 1:
        raise ValueError("quantile must be within [0, 1]")
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index] / 1_000_000


def latency_summary(
    values: Sequence[int], *, exact_max_ns: int | None = None
) -> dict[str, float | None]:
    summary: dict[str, float | None] = {}
    for name, quantile in (("p50", 0.50), ("p95", 0.95), ("p99", 0.99), ("max", 1.0)):
        observed = (
            exact_max_ns / 1_000_000
            if name == "max" and exact_max_ns is not None
            else percentile(values, quantile)
        )
        summary[name] = None if observed is None else round(observed, 6)
    return summary


def _splitmix64(value: int) -> int:
    value = (value + 0x9E3779B97F4A7C15) & _UINT64_MASK
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & _UINT64_MASK
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & _UINT64_MASK
    return value ^ (value >> 31)


class _LatencyReservoir:
    """Fixed-capacity deterministic uniform reservoir with an exact maximum."""

    def __init__(self, capacity: int, seed: int) -> None:
        if not 1 <= capacity <= MAX_LATENCY_SAMPLES_PER_PHASE:
            raise ValueError(
                "latency sample capacity must be within "
                f"[1, {MAX_LATENCY_SAMPLES_PER_PHASE}]"
            )
        if not 0 <= seed <= _UINT64_MASK:
            raise ValueError("latency reservoir seed must fit uint64")
        self.capacity = capacity
        self.seed = seed
        self.population = 0
        self.samples = array("Q")
        self.maximum_ns: int | None = None
        self.replacements = 0

    def observe(self, latency_ns: int) -> None:
        if latency_ns < 0:
            raise ValueError("latency cannot be negative")
        self.population += 1
        self.maximum_ns = (
            latency_ns if self.maximum_ns is None else max(self.maximum_ns, latency_ns)
        )
        if len(self.samples) < self.capacity:
            self.samples.append(latency_ns)
            return
        slot = _splitmix64(self.seed ^ self.population) % self.population
        if slot < self.capacity:
            self.samples[slot] = latency_ns
            self.replacements += 1

    def evidence(self) -> dict[str, object]:
        return {
            "method": "deterministic-reservoir-splitmix64/v1",
            "capacity": self.capacity,
            "population": self.population,
            "retained_samples": len(self.samples),
            "not_retained": self.population - len(self.samples),
            "replacements": self.replacements,
            "seed_hex": f"{self.seed:016x}",
            "exact_max_ns": self.maximum_ns,
        }


def bootstrap_mean_ci(
    values: Sequence[float],
    *,
    confidence: float = 0.95,
    seed: int = 1,
    resamples: int = 10_000,
) -> dict[str, float | int]:
    """Deterministic bootstrap CI; all samples, including outliers, are retained."""

    if not values:
        raise ValueError("bootstrap requires at least one observation")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be within (0, 1)")
    if resamples < 100:
        raise ValueError("resamples must be at least 100")
    generator = random.Random(seed)
    count = len(values)
    means = []
    for _ in range(resamples):
        means.append(
            sum(values[generator.randrange(count)] for _ in range(count)) / count
        )
    means.sort()
    tail = (1 - confidence) / 2
    low = means[max(0, math.floor(tail * resamples))]
    high = means[min(resamples - 1, math.ceil((1 - tail) * resamples) - 1)]
    return {
        "mean": round(sum(values) / count, 6),
        "low": round(low, 6),
        "high": round(high, 6),
        "confidence": confidence,
        "seed": seed,
        "resamples": resamples,
        "observations": count,
    }


def validate_packet_oracle_profile(profile: dict[str, object]) -> None:
    expected: dict[str, object] = {
        "sender_execution": SENDER_EXECUTION_PROFILE,
        "sender_progress_publish_packets": SENDER_PROGRESS_PUBLISH_PACKETS,
        "sender_send_buffer_requested_bytes": SENDER_SEND_BUFFER_BYTES,
        "capture_execution": CAPTURE_EXECUTION_PROFILE,
        "capture_receive_buffer_requested_bytes": CAPTURE_RECEIVE_BUFFER_BYTES,
        "capture_packet_statistics_required": True,
        "capture_drain_timeout_ms": round(CAPTURE_DRAIN_TIMEOUT_SECONDS * 1000),
        "capture_drain_stable_intervals": CAPTURE_DRAIN_STABLE_INTERVALS,
    }
    if profile != expected:
        raise RuntimeError(
            f"packet oracle runtime diverges from the frozen profile: {profile}"
        )


def _internet_checksum(header: bytes) -> int:
    if len(header) % 2:
        header += b"\x00"
    total = sum(struct.unpack(f"!{len(header) // 2}H", header))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def _ipv4_bytes(address: str) -> bytes:
    return socket.inet_aton(address)


class FrameTemplate:
    """Create exact 256-byte Ethernet/IPv4/UDP frames with a stamped payload."""

    def __init__(
        self,
        token: bytes,
        *,
        source_ip: str = "203.0.113.10",
        destination_ip: str = "198.51.100.10",
        source_port: int = 49152,
        destination_port: int = 8080,
    ) -> None:
        if len(token) != 8:
            raise ValueError("traffic token must be exactly eight bytes")
        self.token = token
        payload_length = FRAME_BYTES - PAYLOAD_OFFSET
        udp_length = 8 + payload_length
        ip_total_length = 20 + udp_length
        ethernet = MAC_PORT_2 + MAC_PORT_1 + struct.pack("!H", 0x0800)
        ip_without_checksum = struct.pack(
            "!BBHHHBBH4s4s",
            0x45,
            0,
            ip_total_length,
            0x4D53,
            0x4000,
            64,
            17,
            0,
            _ipv4_bytes(source_ip),
            _ipv4_bytes(destination_ip),
        )
        checksum = _internet_checksum(ip_without_checksum)
        ipv4 = bytearray(ip_without_checksum)
        struct.pack_into("!H", ipv4, 10, checksum)
        # UDP checksum zero is valid for IPv4 and lets the sequence/timestamp
        # payload change without mutating a header checksum on every packet.
        udp = struct.pack("!HHHH", source_port, destination_port, udp_length, 0)
        payload = bytearray(payload_length)
        payload[: len(MAGIC)] = MAGIC
        payload[len(MAGIC) : len(MAGIC) + 8] = token
        payload[len(MAGIC) + 33 :] = b"Q" * (payload_length - len(MAGIC) - 33)
        self._frame = bytearray(ethernet + bytes(ipv4) + udp + payload)
        if len(self._frame) != FRAME_BYTES:
            raise AssertionError(f"frame is {len(self._frame)} bytes, expected 256")

    def build(self, sequence: int, send_ns: int, phase_code: int) -> bytes:
        frame = self._frame.copy()
        struct.pack_into("!Q", frame, SEQUENCE_OFFSET, sequence)
        struct.pack_into("!Q", frame, SEND_NS_OFFSET, send_ns)
        struct.pack_into("!B", frame, PHASE_OFFSET, phase_code)
        return bytes(frame)


@dataclass(frozen=True)
class WorkloadPhase:
    name: str
    duration_seconds: float
    requested_rate_pps: float
    code: int

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("phase name is required")
        if self.duration_seconds <= 0:
            raise ValueError("phase duration must be positive")
        if self.requested_rate_pps <= 0:
            raise ValueError("phase rate must be positive")
        if not 1 <= self.code <= 255:
            raise ValueError("phase code must fit one nonzero byte")


def _empty_sender_state(phases: Sequence[WorkloadPhase]) -> _SenderState:
    return {
        "attempted": 0,
        "accepted": 0,
        "accepted_bytes": 0,
        "by_phase": {phase.code: 0 for phase in phases},
        "attempted_by_phase": {phase.code: 0 for phase in phases},
        "errors": [],
        "maximum_schedule_lateness_ns": 0,
        "maximum_catch_up_backlog_packets": 0,
        "packets_late_over_interval": 0,
        "maximum_schedule_lateness_ns_by_phase": {phase.code: 0 for phase in phases},
        "actual_send_buffer_bytes": 0,
    }


def _publish_sender_progress(
    progress: Any,
    state: _SenderState,
    phase_codes: Sequence[int],
) -> None:
    with progress.get_lock():
        progress[0] = state["attempted"]
        progress[1] = state["accepted"]
        progress[2] = state["accepted_bytes"]
        for index, phase_code in enumerate(phase_codes, start=3):
            progress[index] = state["by_phase"][phase_code]


def _sender_progress_snapshot(
    progress: Any,
    phase_codes: Sequence[int],
) -> _SenderProgressSnapshot:
    with progress.get_lock():
        values = list(progress[:])
    return {
        "attempted": int(values[0]),
        "accepted": int(values[1]),
        "accepted_bytes": int(values[2]),
        "by_phase": {
            phase_code: int(values[index])
            for index, phase_code in enumerate(phase_codes, start=3)
        },
    }


def _integer_snapshot_value(snapshot: dict[str, object], name: str) -> int:
    value = snapshot[name]
    if not isinstance(value, int):
        raise TypeError(f"capture snapshot {name} is not an integer")
    return value


def _sender_process_main(
    ingress_interface: str,
    token: bytes,
    phases: tuple[WorkloadPhase, ...],
    progress: Any,
    control: Any,
) -> None:
    """Run paced sends outside the callback/capture interpreter and its GIL."""

    phase_codes = tuple(phase.code for phase in phases)
    state = _empty_sender_state(phases)
    sender: socket.socket | None = None
    try:
        sender = socket.socket(
            socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL)
        )
        sender.bind((ingress_interface, 0))
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SENDER_SEND_BUFFER_BYTES)
        state["actual_send_buffer_bytes"] = int(
            sender.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
        )
        control.send(
            {
                "kind": "ready",
                "actual_send_buffer_bytes": state["actual_send_buffer_bytes"],
            }
        )
        command = control.recv()
        if not isinstance(command, dict) or command.get("kind") != "start":
            raise RuntimeError("sender received an invalid start command")
        schedule_start_ns = int(command["schedule_start_ns"])
        template = FrameTemplate(token)
        sequence = 0
        phase_start_ns = schedule_start_ns
        for phase in phases:
            phase_duration_ns = int(phase.duration_seconds * 1e9)
            phase_end_ns = phase_start_ns + phase_duration_ns
            packet_interval_ns = max(1, int(1e9 / phase.requested_rate_pps))
            packet_index = 0
            while True:
                target_ns = phase_start_ns + int(
                    packet_index * 1e9 / phase.requested_rate_pps
                )
                if target_ns >= phase_end_ns:
                    break
                while True:
                    remaining_ns = target_ns - time.perf_counter_ns()
                    if remaining_ns <= 0:
                        break
                    if remaining_ns > 200_000:
                        time.sleep((remaining_ns - 100_000) / 1e9)
                sequence += 1
                packet_index += 1
                send_ns = time.perf_counter_ns()
                schedule_lateness_ns = max(0, send_ns - target_ns)
                catch_up_backlog = schedule_lateness_ns // packet_interval_ns
                state["maximum_schedule_lateness_ns"] = max(
                    state["maximum_schedule_lateness_ns"], schedule_lateness_ns
                )
                state["maximum_schedule_lateness_ns_by_phase"][phase.code] = max(
                    state["maximum_schedule_lateness_ns_by_phase"][phase.code],
                    schedule_lateness_ns,
                )
                state["maximum_catch_up_backlog_packets"] = max(
                    state["maximum_catch_up_backlog_packets"], catch_up_backlog
                )
                if catch_up_backlog > 0:
                    state["packets_late_over_interval"] += 1
                frame = template.build(sequence, send_ns, phase.code)
                state["attempted"] += 1
                state["attempted_by_phase"][phase.code] += 1
                sent = sender.send(frame)
                if sent != FRAME_BYTES:
                    state["errors"].append(f"short send {sent}/{FRAME_BYTES}")
                else:
                    state["accepted"] += 1
                    state["accepted_bytes"] += sent
                    state["by_phase"][phase.code] += 1
                if state["attempted"] % SENDER_PROGRESS_PUBLISH_PACKETS == 0:
                    _publish_sender_progress(progress, state, phase_codes)
            phase_start_ns = phase_end_ns
    except BaseException as exc:
        state["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        _publish_sender_progress(progress, state, phase_codes)
        if sender is not None:
            sender.close()
        try:
            control.send({"kind": "result", "state": state})
        except (BrokenPipeError, EOFError, OSError):
            pass
        control.close()


class _Capture:
    def __init__(
        self,
        interface: str,
        token: bytes,
        *,
        max_sequence: int,
        collect_latency: bool,
        latency_sample_capacity_per_phase: int,
        expected_packet_type: int | None,
    ) -> None:
        self.interface = interface
        self.token = token
        self.collect_latency = collect_latency
        self.latency_sample_capacity_per_phase = latency_sample_capacity_per_phase
        self.expected_packet_type = expected_packet_type
        self._seen = bytearray(max_sequence + 1)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._socket: socket.socket | None = None
        self.unique = 0
        self.duplicates = 0
        self.invalid_sequences = 0
        self.by_phase: dict[int, int] = {}
        self.latencies: dict[int, _LatencyReservoir] = {}
        self.error: str | None = None
        self.requested_receive_buffer_bytes = CAPTURE_RECEIVE_BUFFER_BYTES
        self.actual_receive_buffer_bytes = 0
        self.kernel_packets = 0
        self.kernel_drops = 0
        self.packet_statistics_error: str | None = None

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise TimeoutError(f"capture on {self.interface} did not start")
        if self.error is not None:
            raise RuntimeError(self.error)

    def _run(self) -> None:
        try:
            capture = socket.socket(
                socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL)
            )
            capture.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_RCVBUF,
                self.requested_receive_buffer_bytes,
            )
            self.actual_receive_buffer_bytes = int(
                capture.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
            )
            capture.settimeout(0.2)
            capture.bind((self.interface, 0))
            self._socket = capture
            self._ready.set()
            while not self._stop.is_set():
                try:
                    frame, address = capture.recvfrom(2048)
                except socket.timeout:
                    continue
                if self.expected_packet_type is not None and len(address) >= 3:
                    if int(address[2]) != self.expected_packet_type:
                        continue
                if len(frame) != FRAME_BYTES:
                    continue
                if frame[PAYLOAD_OFFSET:TOKEN_OFFSET] != MAGIC:
                    continue
                if frame[TOKEN_OFFSET:SEQUENCE_OFFSET] != self.token:
                    continue
                received_ns = time.perf_counter_ns()
                sequence = struct.unpack_from("!Q", frame, SEQUENCE_OFFSET)[0]
                send_ns = struct.unpack_from("!Q", frame, SEND_NS_OFFSET)[0]
                phase_code = frame[PHASE_OFFSET]
                with self._lock:
                    if sequence <= 0 or sequence >= len(self._seen):
                        self.invalid_sequences += 1
                        continue
                    if self._seen[sequence]:
                        self.duplicates += 1
                        continue
                    self._seen[sequence] = 1
                    self.unique += 1
                    self.by_phase[phase_code] = self.by_phase.get(phase_code, 0) + 1
                    if self.collect_latency:
                        latency_ns = max(0, received_ns - send_ns)
                        reservoir = self.latencies.get(phase_code)
                        if reservoir is None:
                            seed_material = self.token + bytes((phase_code,))
                            seed = int.from_bytes(
                                hashlib.sha256(seed_material).digest()[:8], "big"
                            )
                            reservoir = _LatencyReservoir(
                                self.latency_sample_capacity_per_phase,
                                seed,
                            )
                            self.latencies[phase_code] = reservoir
                        reservoir.observe(latency_ns)
        except BaseException as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
        finally:
            if self._socket is not None:
                try:
                    packet_statistics = self._socket.getsockopt(
                        SOL_PACKET,
                        PACKET_STATISTICS,
                        8,
                    )
                    self.kernel_packets, self.kernel_drops = struct.unpack(
                        "=II", packet_statistics
                    )
                except (OSError, struct.error) as exc:
                    self.packet_statistics_error = f"{type(exc).__name__}: {exc}"
                self._socket.close()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "unique": self.unique,
                "duplicates": self.duplicates,
                "invalid_sequences": self.invalid_sequences,
                "by_phase": dict(self.by_phase),
                "error": self.error,
                "socket_statistics": {
                    "requested_receive_buffer_bytes": self.requested_receive_buffer_bytes,
                    "actual_receive_buffer_bytes": self.actual_receive_buffer_bytes,
                    "kernel_packets": self.kernel_packets,
                    "kernel_drops": self.kernel_drops,
                    "error": self.packet_statistics_error,
                },
            }

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)
        if self._thread.is_alive() and self.error is None:
            self.error = "capture thread did not terminate"


class TrafficHarness:
    """Paced raw sender plus independent ingress and egress packet captures."""

    def __init__(
        self,
        run_identity: str,
        *,
        ingress_interface: str = "masi-p1",
        egress_interface: str = "masi-p2",
    ) -> None:
        self.token = hashlib.sha256(run_identity.encode()).digest()[:8]
        self.ingress_interface = ingress_interface
        self.egress_interface = egress_interface

    def run(
        self,
        phases: Sequence[WorkloadPhase],
        *,
        sample_interval_seconds: float | None = None,
        sample_callback: Callable[
            [int, str, dict[str, object]], dict[str, object] | None
        ]
        | None = None,
        latency_sample_capacity_per_phase: int = MAX_LATENCY_SAMPLES_PER_PHASE,
        max_packets_per_run: int = MAX_WORKLOAD_PACKETS,
        max_samples_per_run: int = MAX_WORKLOAD_SAMPLES,
    ) -> dict[str, object]:
        if not phases:
            raise ValueError("at least one workload phase is required")
        codes = [phase.code for phase in phases]
        if len(set(codes)) != len(codes):
            raise ValueError("workload phase codes must be unique")
        if not 1 <= latency_sample_capacity_per_phase <= MAX_LATENCY_SAMPLES_PER_PHASE:
            raise ValueError("latency sample capacity exceeds the hard bound")
        if not 1 <= max_packets_per_run <= MAX_WORKLOAD_PACKETS:
            raise ValueError("packet bound exceeds the hard workload limit")
        if not 1 <= max_samples_per_run <= MAX_WORKLOAD_SAMPLES:
            raise ValueError("sample bound exceeds the hard workload limit")
        max_packets = (
            sum(
                math.ceil(phase.duration_seconds * phase.requested_rate_pps)
                for phase in phases
            )
            + len(phases)
            + 64
        )
        if max_packets > max_packets_per_run:
            raise ValueError(
                f"workload requires {max_packets} packet slots, "
                f"limit is {max_packets_per_run}"
            )
        total_duration_seconds = sum(phase.duration_seconds for phase in phases)
        if sample_interval_seconds is not None:
            if sample_interval_seconds <= 0:
                raise ValueError("sample interval must be positive")
            projected_samples = math.floor(
                total_duration_seconds / sample_interval_seconds + 1e-9
            )
            if projected_samples > max_samples_per_run:
                raise ValueError(
                    f"workload requires {projected_samples} samples, "
                    f"limit is {max_samples_per_run}"
                )
        ingress = _Capture(
            self.ingress_interface,
            self.token,
            max_sequence=max_packets,
            collect_latency=False,
            latency_sample_capacity_per_phase=latency_sample_capacity_per_phase,
            expected_packet_type=PACKET_OUTGOING,
        )
        egress = _Capture(
            self.egress_interface,
            self.token,
            max_sequence=max_packets,
            collect_latency=True,
            latency_sample_capacity_per_phase=latency_sample_capacity_per_phase,
            expected_packet_type=None,
        )
        total_duration_ns = int(total_duration_seconds * 1e9)
        phase_tuple = tuple(phases)
        phase_codes = tuple(phase.code for phase in phases)
        process_context = multiprocessing.get_context("spawn")
        sender_progress = process_context.Array(
            "Q",
            3 + len(phase_codes),
            lock=True,
        )
        parent_control, child_control = process_context.Pipe(duplex=True)
        sender_process = process_context.Process(
            target=_sender_process_main,
            args=(
                self.ingress_interface,
                self.token,
                phase_tuple,
                sender_progress,
                child_control,
            ),
            daemon=True,
        )
        sender_process.start()
        child_control.close()
        if not parent_control.poll(SENDER_READY_TIMEOUT_SECONDS):
            sender_process.terminate()
            sender_process.join(timeout=3)
            parent_control.close()
            raise TimeoutError("sender process did not become ready")
        ready_message = parent_control.recv()
        if not isinstance(ready_message, dict) or ready_message.get("kind") != "ready":
            sender_process.join(timeout=3)
            parent_control.close()
            raise RuntimeError(
                f"sender process failed before readiness: {ready_message}"
            )
        try:
            ingress.start()
            egress.start()
        except BaseException:
            sender_process.terminate()
            sender_process.join(timeout=3)
            parent_control.close()
            ingress.stop()
            egress.stop()
            raise
        schedule_start_ns = time.perf_counter_ns() + 200_000_000
        parent_control.send({"kind": "start", "schedule_start_ns": schedule_start_ns})
        sender_state = _empty_sender_state(phases)
        sender_process_evidence: dict[str, object] = {
            "execution_profile": SENDER_EXECUTION_PROFILE,
            "start_method": "spawn",
            "pid": sender_process.pid,
            "ready": True,
            "actual_send_buffer_bytes": int(ready_message["actual_send_buffer_bytes"]),
        }
        samples: list[dict[str, object]] = []
        callback_errors: list[str] = []
        if sample_interval_seconds is not None:
            sample_index = 1
            while True:
                sample_target_ns = schedule_start_ns + int(
                    sample_index * sample_interval_seconds * 1e9
                )
                if sample_target_ns > schedule_start_ns + total_duration_ns:
                    break
                remaining_ns = sample_target_ns - time.perf_counter_ns()
                if remaining_ns > 0:
                    time.sleep(remaining_ns / 1e9)
                offset_ms = int((sample_target_ns - schedule_start_ns) / 1_000_000)
                elapsed_seconds = offset_ms / 1000
                phase_name = phases[-1].name
                boundary = 0.0
                for phase_index, phase in enumerate(phases):
                    boundary += phase.duration_seconds
                    if elapsed_seconds < boundary or phase_index == len(phases) - 1:
                        phase_name = phase.name
                        break
                traffic_snapshot: dict[str, object] = {
                    "sender": _sender_progress_snapshot(
                        sender_progress,
                        phase_codes,
                    ),
                    "test_ingress": ingress.snapshot(),
                    "outcome": egress.snapshot(),
                }
                module_metrics: dict[str, object] = {}
                if sample_callback is not None:
                    try:
                        returned = sample_callback(
                            offset_ms, phase_name, traffic_snapshot
                        )
                        if returned:
                            module_metrics.update(returned)
                    except BaseException as exc:
                        callback_errors.append(f"{type(exc).__name__}: {exc}")
                samples.append(
                    {
                        "offset_ms": offset_ms,
                        "phase": phase_name,
                        "traffic": traffic_snapshot,
                        "module_metrics": module_metrics,
                    }
                )
                sample_index += 1
        sender_join_timeout = (
            total_duration_seconds + SENDER_JOIN_GRACE_SECONDS
            if sample_interval_seconds is None
            else SENDER_JOIN_GRACE_SECONDS
        )
        sender_process.join(timeout=sender_join_timeout)
        sender_timed_out = sender_process.is_alive()
        if sender_timed_out:
            sender_state["errors"].append("sender process exceeded bounded schedule")
            progress_snapshot = _sender_progress_snapshot(sender_progress, phase_codes)
            sender_state["attempted"] = int(progress_snapshot["attempted"])
            sender_state["accepted"] = int(progress_snapshot["accepted"])
            sender_state["accepted_bytes"] = int(progress_snapshot["accepted_bytes"])
            progress_by_phase = progress_snapshot["by_phase"]
            assert isinstance(progress_by_phase, dict)
            sender_state["by_phase"] = {
                int(code): int(value) for code, value in progress_by_phase.items()
            }
            sender_process.terminate()
            sender_process.join(timeout=3)
            if sender_process.is_alive():
                sender_process.kill()
                sender_process.join(timeout=3)
        elif parent_control.poll(1):
            result_message: Any = parent_control.recv()
            if (
                isinstance(result_message, dict)
                and result_message.get("kind") == "result"
                and isinstance(result_message.get("state"), dict)
            ):
                sender_state = result_message["state"]
            else:
                sender_state["errors"].append(
                    f"invalid sender result message: {result_message}"
                )
        else:
            sender_state["errors"].append("sender process returned no result")
        sender_exit_code = sender_process.exitcode
        sender_process_evidence.update(
            {
                "exit_code": sender_exit_code,
                "terminated_after_timeout": sender_timed_out,
            }
        )
        if sender_exit_code not in (0, None):
            sender_state["errors"].append(
                f"sender process exited with status {sender_exit_code}"
            )
        sender_process.close()
        parent_control.close()
        sender_finished_ns = time.perf_counter_ns()

        drain_started_ns = time.perf_counter_ns()
        drain_deadline = time.perf_counter() + CAPTURE_DRAIN_TIMEOUT_SECONDS
        stable_intervals = 0
        prior_counts = (-1, -1)
        while time.perf_counter() < drain_deadline:
            ingress_drain = ingress.snapshot()
            egress_drain = egress.snapshot()
            counts = (
                _integer_snapshot_value(ingress_drain, "unique"),
                _integer_snapshot_value(egress_drain, "unique"),
            )
            if counts == prior_counts:
                stable_intervals += 1
            else:
                stable_intervals = 0
                prior_counts = counts
            if stable_intervals >= CAPTURE_DRAIN_STABLE_INTERVALS:
                break
            time.sleep(0.1)
        ingress.stop()
        egress.stop()
        capture_drain = {
            "timeout_ms": round(CAPTURE_DRAIN_TIMEOUT_SECONDS * 1000),
            "stable_interval_target": CAPTURE_DRAIN_STABLE_INTERVALS,
            "stable_intervals_observed": stable_intervals,
            "completed": stable_intervals >= CAPTURE_DRAIN_STABLE_INTERVALS,
            "elapsed_ms": round(
                (time.perf_counter_ns() - drain_started_ns) / 1e6,
                6,
            ),
        }

        ingress_snapshot = ingress.snapshot()
        egress_snapshot = egress.snapshot()
        phase_results = []
        sender_by_phase = sender_state["by_phase"]
        sender_attempted_by_phase = sender_state["attempted_by_phase"]
        ingress_by_phase = ingress_snapshot["by_phase"]
        egress_by_phase = egress_snapshot["by_phase"]
        assert isinstance(ingress_by_phase, dict)
        assert isinstance(egress_by_phase, dict)
        for phase in phases:
            attempted = int(sender_attempted_by_phase.get(phase.code, 0))
            accepted = int(sender_by_phase.get(phase.code, 0))
            observed_ingress = int(ingress_by_phase.get(phase.code, 0))
            observed_egress = int(egress_by_phase.get(phase.code, 0))
            reservoir = egress.latencies.get(phase.code)
            latencies: Sequence[int] = (
                reservoir.samples if reservoir is not None else ()
            )
            exact_max_ns = reservoir.maximum_ns if reservoir is not None else None
            latency_sampling = (
                reservoir.evidence()
                if reservoir is not None
                else {
                    "method": "deterministic-reservoir-splitmix64/v1",
                    "capacity": latency_sample_capacity_per_phase,
                    "population": 0,
                    "retained_samples": 0,
                    "not_retained": 0,
                    "replacements": 0,
                    "seed_hex": None,
                    "exact_max_ns": None,
                }
            )
            achieved_pps = observed_egress / phase.duration_seconds
            phase_results.append(
                {
                    "name": phase.name,
                    "code": phase.code,
                    "planned_ms": round(phase.duration_seconds * 1000),
                    "requested_rate_pps": phase.requested_rate_pps,
                    "sender_attempted": attempted,
                    "sender_accepted": accepted,
                    "sender_accepted_bytes": accepted * FRAME_BYTES,
                    "test_ingress_packets": observed_ingress,
                    "dut_egress_packets": observed_egress,
                    "independent_action_outcome_packets": observed_egress,
                    "ingress_capture_gap": max(0, accepted - observed_ingress),
                    "egress_capture_or_outcome_gap": max(0, accepted - observed_egress),
                    "achieved_rate_pps": round(achieved_pps, 6),
                    "achieved_rate_bps": round(achieved_pps * FRAME_BYTES * 8, 3),
                    "latency_ms": latency_summary(
                        latencies,
                        exact_max_ns=exact_max_ns,
                    ),
                    "latency_sampling": latency_sampling,
                }
            )
        errors = list(sender_state["errors"])
        errors.extend(callback_errors)
        if ingress.error:
            errors.append(f"ingress capture: {ingress.error}")
        if egress.error:
            errors.append(f"egress capture: {egress.error}")
        for capture_name, snapshot in (
            ("ingress", ingress_snapshot),
            ("egress", egress_snapshot),
        ):
            socket_statistics = snapshot["socket_statistics"]
            assert isinstance(socket_statistics, dict)
            statistics_error = socket_statistics.get("error")
            if statistics_error:
                errors.append(
                    f"{capture_name} PACKET_STATISTICS unavailable: {statistics_error}"
                )
            kernel_drops = int(socket_statistics.get("kernel_drops", 0))
            if kernel_drops:
                errors.append(
                    f"{capture_name} capture kernel dropped {kernel_drops} packets"
                )
        if not capture_drain["completed"]:
            errors.append("capture drain did not reach a stable bounded interval")
        return {
            "frame_bytes": FRAME_BYTES,
            "token_sha256": sha256_bytes(self.token),
            "monotonic_start_ns": schedule_start_ns,
            "monotonic_scheduled_end_ns": schedule_start_ns + total_duration_ns,
            "monotonic_end_ns": sender_finished_ns,
            "planned_duration_ms": round(total_duration_ns / 1_000_000),
            "sender": sender_state,
            "sender_process": sender_process_evidence,
            "capture_drain": capture_drain,
            "test_ingress": ingress_snapshot,
            "outcome": egress_snapshot,
            "phases": phase_results,
            "samples": samples,
            "errors": errors,
        }


def single_packet_outcome(
    run_identity: str,
    *,
    expect_forward: bool,
    source_ip: str,
    source_port: int,
    destination_port: int = 8080,
    timeout_seconds: float = 0.25,
) -> dict[str, object]:
    """Send one exact frame and independently observe its port-2 outcome."""

    token = hashlib.sha256(run_identity.encode()).digest()[:8]
    template = FrameTemplate(
        token,
        source_ip=source_ip,
        source_port=source_port,
        destination_port=destination_port,
    )
    capture = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    capture.settimeout(0.02)
    capture.bind(("masi-p2", 0))
    sender = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    sender.bind(("masi-p1", 0))
    send_ns = time.perf_counter_ns()
    frame = template.build(1, send_ns, 250)
    sent = sender.send(frame)
    observed = False
    latency_ms: float | None = None
    deadline = time.perf_counter() + timeout_seconds
    try:
        while time.perf_counter() < deadline:
            try:
                candidate = capture.recv(2048)
            except socket.timeout:
                continue
            if (
                len(candidate) == FRAME_BYTES
                and candidate[PAYLOAD_OFFSET:TOKEN_OFFSET] == MAGIC
                and candidate[TOKEN_OFFSET:SEQUENCE_OFFSET] == token
            ):
                observed = True
                latency_ms = (time.perf_counter_ns() - send_ns) / 1e6
                break
    finally:
        sender.close()
        capture.close()
    return {
        "sender_attempted": 1,
        "sender_accepted": int(sent == FRAME_BYTES),
        "sender_accepted_bytes": sent,
        "expected_forward": expect_forward,
        "outcome_observed": observed,
        "outcome_matches": observed == expect_forward,
        "latency_ms": round(latency_ms, 6) if latency_ms is not None else None,
    }


def install_infrastructure(index: P4InfoIndex, client: P4RuntimeClient) -> None:
    entities = [
        selector_entity(index, "policy_selector", "select_policy_bank_0"),
        selector_entity(
            index,
            "telemetry_selector",
            "select_telemetry_bank_0",
            epoch=1,
        ),
        table_entity(
            index.table_entry(
                "l2_forward",
                {"hdr.ethernet.dst_addr": int.from_bytes(MAC_PORT_2, "big")},
                "forward_to_port",
                {"port": 2},
            )
        ),
        table_entity(
            index.table_entry(
                "l2_forward",
                {"hdr.ethernet.dst_addr": int.from_bytes(MAC_PORT_1, "big")},
                "forward_to_port",
                {"port": 1},
            )
        ),
    ]
    client.write(p4runtime_pb2.Update.INSERT, entities)
    client.write(
        p4runtime_pb2.Update.MODIFY,
        [
            default_action_entity(index, 0, "permit"),
            default_action_entity(index, 1, "permit"),
        ],
    )


def make_baseline_rules(
    index: P4InfoIndex,
    bank: int,
    count: int,
    *,
    generation: int,
) -> list[Any]:
    if count < 0 or count > 4096:
        raise ValueError("baseline rule count must be within [0, 4096]")
    rules = []
    if count:
        rules.append(
            index.baseline_entry(
                bank,
                src_ipv4=int.from_bytes(_ipv4_bytes("192.0.2.250"), "big"),
                action="drop",
                priority=2_000_000 + generation,
                ingress_port=1,
                dst_ipv4=int.from_bytes(_ipv4_bytes("198.51.100.10"), "big"),
                protocol=17,
                l4_present=1,
                src_port=55000,
                dst_port=8080,
                fragment_class=0,
            )
        )
    for offset in range(max(0, count - 1)):
        rules.append(
            index.baseline_entry(
                bank,
                src_ipv4=0x0A000001 + offset,
                action="permit",
                priority=1_500_000 - offset,
            )
        )
    return rules


def write_rules(client: P4RuntimeClient, rules: Sequence[Any]) -> None:
    client.write_batched(
        p4runtime_pb2.Update.INSERT,
        [table_entity(rule) for rule in rules],
        timeout=30,
    )


def delete_rules(client: P4RuntimeClient, rules: Sequence[Any]) -> None:
    client.write_batched(
        p4runtime_pb2.Update.DELETE,
        [table_key_entity(rule) for rule in rules],
        timeout=30,
    )


def exact_rule_readback(
    index: P4InfoIndex,
    client: P4RuntimeClient,
    rules: Sequence[Any],
) -> bool:
    received = client.read_batched(readback_selectors(rules), timeout=30)
    actual = {
        index.readback_key(entity.table_entry)
        for entity in received
        if entity.HasField("table_entry")
    }
    expected = {index.readback_key(rule) for rule in rules}
    return actual == expected


def read_policy_bank(index: P4InfoIndex, client: P4RuntimeClient) -> int:
    selector = selector_entity(index, "policy_selector", "select_policy_bank_0")
    response = client.read([table_key_entity(selector.table_entry)])
    if len(response) != 1:
        raise RuntimeError(f"expected one selector entry, received {len(response)}")
    action_id = response[0].table_entry.action.action.action_id
    if action_id == index.action_info("select_policy_bank_0").preamble.id:
        return 0
    if action_id == index.action_info("select_policy_bank_1").preamble.id:
        return 1
    raise RuntimeError(f"unexpected policy selector action {action_id}")


def table_entry_count(
    index: P4InfoIndex,
    client: P4RuntimeClient,
    table_name: str,
) -> int:
    wildcard = p4runtime_pb2.Entity()
    wildcard.table_entry.table_id = index.table(table_name).preamble.id
    return len(client.read([wildcard], timeout=30))


def artifact_identity(artifacts: Path) -> dict[str, str]:
    p4info = (artifacts / "masi_switch.p4info.txtpb").read_bytes()
    program = (artifacts / "masi_switch.json").read_bytes()
    return {
        "program_digest": sha256_bytes(program),
        "p4info_digest": sha256_bytes(p4info),
    }


def all_zero(values: Iterable[int]) -> bool:
    return all(value == 0 for value in values)
