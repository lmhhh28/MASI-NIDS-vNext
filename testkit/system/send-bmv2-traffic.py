#!/usr/bin/env python3
"""Traffic-only sender for the real BMv2/Edge rehearsal."""

from __future__ import annotations

import argparse
import json
import time

from scapy.all import AsyncSniffer, Ether, IP, TCP, sendp


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=64)
    parser.add_argument("--interface", default="masi-p1")
    args = parser.parse_args()
    if args.count < 1 or args.count > 4096:
        raise SystemExit("count must be in 1..4096")
    started = time.time_ns()
    packets = [
        Ether(src="00:00:00:00:01:01", dst="00:00:00:00:02:02")
        / IP(src=f"192.0.2.{(index % 200) + 1}", dst="198.51.100.10")
        / TCP(sport=10000 + index, dport=443, flags="S")
        for index in range(args.count)
    ]
    peer_sniffer = AsyncSniffer(iface="masi-s1", store=True)
    peer_sniffer.start()
    time.sleep(0.05)
    sendp(packets, iface=args.interface, inter=0.001, verbose=False)
    time.sleep(0.1)
    peer_packets = peer_sniffer.stop()
    print(
        json.dumps(
            {
                "schema_version": "bmv2-traffic-sender/v1",
                "interface": args.interface,
                "packet_count": len(packets),
                "peer_packet_count": len(peer_packets),
                "started_at_unix_ns": started,
                "finished_at_unix_ns": time.time_ns(),
                "p4runtime_credentials_present": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
