"""Low-cardinality in-process operational metrics."""

from __future__ import annotations

import threading
import time
from collections import Counter, defaultdict


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Counter[tuple[str, str]] = Counter()
        self._latency_sum_ms: dict[str, float] = defaultdict(float)
        self._latency_count: Counter[str] = Counter()
        self._started = time.monotonic()

    def increment(self, metric: str, label: str = "total", amount: int = 1) -> None:
        with self._lock:
            self._counters[(metric, label)] += amount

    def observe_ms(self, operation: str, milliseconds: float) -> None:
        with self._lock:
            self._latency_sum_ms[operation] += milliseconds
            self._latency_count[operation] += 1

    def render(self, *, queue_depth: int, in_flight: int) -> str:
        lines = [
            "# TYPE masi_analysis_uptime_seconds gauge",
            f"masi_analysis_uptime_seconds {time.monotonic() - self._started:.3f}",
            "# TYPE masi_analysis_queue_depth gauge",
            f"masi_analysis_queue_depth {queue_depth}",
            "# TYPE masi_analysis_in_flight gauge",
            f"masi_analysis_in_flight {in_flight}",
        ]
        with self._lock:
            for (metric, label), value in sorted(self._counters.items()):
                safe_metric = "".join(character if character.isalnum() or character == "_" else "_" for character in metric)
                safe_label = "".join(character if character.isalnum() or character in "_-" else "_" for character in label)
                lines.append(f'masi_analysis_{safe_metric}_total{{result="{safe_label}"}} {value}')
            for operation in sorted(self._latency_count):
                count = self._latency_count[operation]
                total = self._latency_sum_ms[operation]
                lines.append(f'masi_analysis_operation_latency_ms_sum{{operation="{operation}"}} {total}')
                lines.append(f'masi_analysis_operation_latency_ms_count{{operation="{operation}"}} {count}')
        return "\n".join(lines) + "\n"
