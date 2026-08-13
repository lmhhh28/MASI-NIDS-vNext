#!/usr/bin/env sh
set -eu

ip link add masi-s1 type veth peer name masi-p1
ip link add masi-s2 type veth peer name masi-p2
for interface_name in masi-s1 masi-p1 masi-s2 masi-p2; do
  ip link set dev "$interface_name" mtu 9500
  ip link set dev "$interface_name" up
done

ip -details link show masi-s1
ip -details link show masi-s2
