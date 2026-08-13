#include <core.p4>
#include <v1model.p4>

// MASI-NIDS vNext software target: p4-stateless-firewall/v1.
// This program intentionally contains no policy, authorization, model, or
// durable-control logic.  The only runtime mutation boundary is P4Runtime.

const bit<16> ETHERTYPE_IPV4 = 0x0800;
const bit<16> ETHERTYPE_IPV6 = 0x86dd;
const bit<8> IP_PROTOCOL_TCP = 6;
const bit<8> IP_PROTOCOL_UDP = 17;

const bit<9> CPU_PORT = 510;
const bit<32> PACKET_IN_CLONE_SESSION = 99;
const bit<8> PACKET_IN_FIELD_LIST = 1;

const bit<32> TELEMETRY_CELLS = 256;
const bit<32> TELEMETRY_TOTAL_CELLS = 512;
typedef bit<9> port_t;
typedef bit<48> mac_addr_t;
typedef bit<32> ipv4_addr_t;

enum bit<2> fragment_class_t {
    UNFRAGMENTED = 0,
    FIRST_FRAGMENT = 1,
    NON_INITIAL_FRAGMENT = 2,
    INVALID_FRAGMENT = 3
}

enum bit<8> packet_in_reason_t {
    TELEMETRY_SAMPLE = 1
}

@id(0x04000101)
@controller_header("packet_in")
header packet_in_header_t {
    @id(1) bit<16> ingress_port;
    @id(2) packet_in_reason_t reason;
    @id(3) bit<8> telemetry_bank;
    @id(4) bit<32> telemetry_epoch;
    @id(5) bit<64> telemetry_sequence;
}

header ethernet_t {
    mac_addr_t dst_addr;
    mac_addr_t src_addr;
    bit<16> ether_type;
}

header ipv4_t {
    bit<4> version;
    bit<4> ihl;
    bit<6> dscp;
    bit<2> ecn;
    bit<16> total_len;
    bit<16> identification;
    bit<3> flags;
    bit<13> fragment_offset;
    bit<8> ttl;
    bit<8> protocol;
    bit<16> checksum;
    ipv4_addr_t src_addr;
    ipv4_addr_t dst_addr;
}

header ipv6_t {
    bit<4> version;
    bit<8> traffic_class;
    bit<20> flow_label;
    bit<16> payload_len;
    bit<8> next_header;
    bit<8> hop_limit;
    bit<128> src_addr;
    bit<128> dst_addr;
}

header tcp_t {
    bit<16> src_port;
    bit<16> dst_port;
    bit<32> seq_no;
    bit<32> ack_no;
    bit<4> data_offset;
    bit<3> reserved;
    bit<9> flags;
    bit<16> window;
    bit<16> checksum;
    bit<16> urgent_ptr;
}

header udp_t {
    bit<16> src_port;
    bit<16> dst_port;
    bit<16> length;
    bit<16> checksum;
}

struct headers_t {
    packet_in_header_t packet_in;
    ethernet_t ethernet;
    ipv4_t ipv4;
    ipv6_t ipv6;
    tcp_t tcp;
    udp_t udp;
}

struct metadata_t {
    bit<1> selector_key;
    bit<1> active_policy_bank;
    bit<1> firewall_drop;
    bit<1> ipv4_supported;
    bit<1> l4_present;
    bit<16> l4_src_port;
    bit<16> l4_dst_port;
    fragment_class_t fragment_class;

    bit<1> telemetry_bank;
    bit<32> telemetry_epoch;
    bit<3> telemetry_class;
    bit<32> telemetry_cell;
    bit<64> telemetry_sequence;
    bit<10> telemetry_sample_key;

    @field_list(PACKET_IN_FIELD_LIST)
    bit<16> packet_in_ingress_port;
    @field_list(PACKET_IN_FIELD_LIST)
    packet_in_reason_t packet_in_reason;
    @field_list(PACKET_IN_FIELD_LIST)
    bit<8> packet_in_telemetry_bank;
    @field_list(PACKET_IN_FIELD_LIST)
    bit<32> packet_in_telemetry_epoch;
    @field_list(PACKET_IN_FIELD_LIST)
    bit<64> packet_in_telemetry_sequence;
}

struct telemetry_hint_t {
    bit<1> bank;
    bit<32> epoch;
    bit<64> sequence;
    port_t ingress_port;
}

parser ParserImpl(packet_in packet,
                  out headers_t hdr,
                  inout metadata_t meta,
                  inout standard_metadata_t standard_metadata) {
    state start {
        packet.extract(hdr.ethernet);
        transition select(hdr.ethernet.ether_type) {
            ETHERTYPE_IPV4: parse_ipv4;
            ETHERTYPE_IPV6: parse_ipv6;
            default: accept;
        }
    }

    state parse_ipv4 {
        packet.extract(hdr.ipv4);
        transition select(hdr.ipv4.fragment_offset, hdr.ipv4.protocol) {
            (0, IP_PROTOCOL_TCP): parse_tcp;
            (0, IP_PROTOCOL_UDP): parse_udp;
            default: accept;
        }
    }

    state parse_ipv6 {
        packet.extract(hdr.ipv6);
        transition accept;
    }

    state parse_tcp {
        packet.extract(hdr.tcp);
        transition accept;
    }

    state parse_udp {
        packet.extract(hdr.udp);
        transition accept;
    }
}

control VerifyChecksumImpl(inout headers_t hdr, inout metadata_t meta) {
    apply {
        verify_checksum(
            hdr.ipv4.isValid() && hdr.ipv4.ihl == 5,
            {
                hdr.ipv4.version,
                hdr.ipv4.ihl,
                hdr.ipv4.dscp,
                hdr.ipv4.ecn,
                hdr.ipv4.total_len,
                hdr.ipv4.identification,
                hdr.ipv4.flags,
                hdr.ipv4.fragment_offset,
                hdr.ipv4.ttl,
                hdr.ipv4.protocol,
                hdr.ipv4.src_addr,
                hdr.ipv4.dst_addr
            },
            hdr.ipv4.checksum,
            HashAlgorithm.csum16);
    }
}

control IngressImpl(inout headers_t hdr,
                    inout metadata_t meta,
                    inout standard_metadata_t standard_metadata) {
    @id(0x13000101)
    direct_counter(CounterType.packets_and_bytes) response_overlay_direct_counter;
    @id(0x13000102)
    direct_counter(CounterType.packets_and_bytes) baseline_bank_0_direct_counter;
    @id(0x13000103)
    direct_counter(CounterType.packets_and_bytes) baseline_bank_1_direct_counter;

    @id(0x12000101)
    counter(1, CounterType.packets_and_bytes) response_overlay_eligible_counter;
    @id(0x12000102)
    counter(2, CounterType.packets_and_bytes) baseline_eligible_counter;

    // The aggregate is deliberately approximate and bounded: two banks of
    // 256 hash cells.  P4Runtime exposes packet/byte counts for every cell;
    // collisions are represented by an explicit quality value in the public
    // contract, never misrepresented as an exact flow map.
    @id(0x12000103)
    counter(TELEMETRY_TOTAL_CELLS, CounterType.packets_and_bytes) telemetry_cell_counter;
    @id(0x12000104)
    counter(2, CounterType.packets_and_bytes) telemetry_bank_counter;
    // Per bank: IPv4, IPv6, non-IP, fragment, parser/checksum/unsupported.
    @id(0x12000105)
    counter(10, CounterType.packets) telemetry_class_counter;

    // One monotonic supplemental-sample sequence slot per telemetry bank.
    // Snapshot consistency uses selector-frozen banks; the exported bank
    // packet counter is the target-qualified snapshot sequence.
    @id(0x16000118)
    register<bit<64>>(2) telemetry_bank_sequence;
    @id(0x01000101)
    action overlay_permit_and_continue() { }

    @id(0x01000102)
    action overlay_drop() {
        meta.firewall_drop = 1;
        mark_to_drop(standard_metadata);
    }

    @id(0x01000103)
    action baseline_permit_and_continue() { }

    @id(0x01000104)
    action baseline_drop() {
        meta.firewall_drop = 1;
        mark_to_drop(standard_metadata);
    }

    @id(0x01000105)
    action select_policy_bank_0() {
        meta.active_policy_bank = 0;
    }

    @id(0x01000106)
    action select_policy_bank_1() {
        meta.active_policy_bank = 1;
    }

    @id(0x01000107)
    action select_telemetry_bank_0(bit<32> epoch) {
        meta.telemetry_bank = 0;
        meta.telemetry_epoch = epoch;
    }

    @id(0x01000108)
    action select_telemetry_bank_1(bit<32> epoch) {
        meta.telemetry_bank = 1;
        meta.telemetry_epoch = epoch;
    }

    @id(0x01000109)
    action forward_to_port(port_t port) {
        standard_metadata.egress_spec = port;
    }

    @id(0x0100010a)
    action forwarding_drop() {
        mark_to_drop(standard_metadata);
    }

    @id(0x02000101)
    table response_overlay {
        key = {
            meta.ipv4_supported: exact;
            meta.l4_present: exact;
            hdr.ipv4.src_addr: exact;
            hdr.ipv4.dst_addr: exact;
            hdr.ipv4.protocol: exact;
            meta.l4_src_port: exact;
            meta.l4_dst_port: exact;
        }
        actions = {
            overlay_permit_and_continue;
            overlay_drop;
            NoAction;
        }
        size = 1024;
        counters = response_overlay_direct_counter;
        default_action = NoAction();
    }

    @id(0x02000102)
    table policy_selector {
        key = {
            meta.selector_key: exact;
        }
        actions = {
            select_policy_bank_0;
            select_policy_bank_1;
        }
        size = 1;
        default_action = select_policy_bank_0();
    }

    @id(0x02000103)
    table baseline_bank_0 {
        key = {
            standard_metadata.ingress_port: ternary;
            hdr.ipv4.src_addr: ternary;
            hdr.ipv4.dst_addr: ternary;
            hdr.ipv4.protocol: ternary;
            meta.l4_present: ternary;
            meta.l4_src_port: ternary;
            meta.l4_dst_port: ternary;
            meta.fragment_class: ternary;
        }
        actions = {
            baseline_permit_and_continue;
            baseline_drop;
        }
        size = 4096;
        counters = baseline_bank_0_direct_counter;
        default_action = baseline_permit_and_continue();
    }

    @id(0x02000104)
    table baseline_bank_1 {
        key = {
            standard_metadata.ingress_port: ternary;
            hdr.ipv4.src_addr: ternary;
            hdr.ipv4.dst_addr: ternary;
            hdr.ipv4.protocol: ternary;
            meta.l4_present: ternary;
            meta.l4_src_port: ternary;
            meta.l4_dst_port: ternary;
            meta.fragment_class: ternary;
        }
        actions = {
            baseline_permit_and_continue;
            baseline_drop;
        }
        size = 4096;
        counters = baseline_bank_1_direct_counter;
        default_action = baseline_permit_and_continue();
    }

    @id(0x02000105)
    table l2_forward {
        key = {
            hdr.ethernet.dst_addr: exact;
        }
        actions = {
            forward_to_port;
            forwarding_drop;
        }
        size = 1024;
        default_action = forwarding_drop();
    }

    @id(0x02000106)
    table telemetry_selector {
        key = {
            meta.selector_key: exact;
        }
        actions = {
            select_telemetry_bank_0;
            select_telemetry_bank_1;
        }
        size = 1;
        default_action = select_telemetry_bank_0(0);
    }

    @id(0x0100010b)
    action accumulate_ipv4() {
        bit<32> bank_index;
        bit<64> sequence;
        bank_index = (bit<32>) meta.telemetry_bank;
        telemetry_bank_counter.count(bank_index);
        telemetry_class_counter.count(bank_index * 5);
        telemetry_cell_counter.count((bank_index * TELEMETRY_CELLS) + meta.telemetry_cell);
        @atomic {
            telemetry_bank_sequence.read(sequence, bank_index);
            sequence = sequence + 1;
            telemetry_bank_sequence.write(bank_index, sequence);
            meta.telemetry_sequence = sequence;
        }
    }

    @id(0x0100010c)
    action accumulate_ipv4_fragment() {
        bit<32> bank_index;
        bit<64> sequence;
        bank_index = (bit<32>) meta.telemetry_bank;
        telemetry_bank_counter.count(bank_index);
        telemetry_class_counter.count(bank_index * 5);
        telemetry_class_counter.count((bank_index * 5) + 3);
        telemetry_cell_counter.count((bank_index * TELEMETRY_CELLS) + meta.telemetry_cell);
        @atomic {
            telemetry_bank_sequence.read(sequence, bank_index);
            sequence = sequence + 1;
            telemetry_bank_sequence.write(bank_index, sequence);
            meta.telemetry_sequence = sequence;
        }
    }

    @id(0x0100010d)
    action accumulate_ipv6() {
        bit<32> bank_index;
        bit<64> sequence;
        bank_index = (bit<32>) meta.telemetry_bank;
        telemetry_bank_counter.count(bank_index);
        telemetry_class_counter.count((bank_index * 5) + 1);
        telemetry_cell_counter.count((bank_index * TELEMETRY_CELLS) + meta.telemetry_cell);
        @atomic {
            telemetry_bank_sequence.read(sequence, bank_index);
            sequence = sequence + 1;
            telemetry_bank_sequence.write(bank_index, sequence);
            meta.telemetry_sequence = sequence;
        }
    }

    @id(0x0100010e)
    action accumulate_non_ip() {
        bit<32> bank_index;
        bit<64> sequence;
        bank_index = (bit<32>) meta.telemetry_bank;
        telemetry_bank_counter.count(bank_index);
        telemetry_class_counter.count((bank_index * 5) + 2);
        @atomic {
            telemetry_bank_sequence.read(sequence, bank_index);
            sequence = sequence + 1;
            telemetry_bank_sequence.write(bank_index, sequence);
            meta.telemetry_sequence = sequence;
        }
    }

    @id(0x0100010f)
    action accumulate_invalid() {
        bit<32> bank_index;
        bit<64> sequence;
        bank_index = (bit<32>) meta.telemetry_bank;
        telemetry_bank_counter.count(bank_index);
        telemetry_class_counter.count((bank_index * 5) + 4);
        @atomic {
            telemetry_bank_sequence.read(sequence, bank_index);
            sequence = sequence + 1;
            telemetry_bank_sequence.write(bank_index, sequence);
            meta.telemetry_sequence = sequence;
        }
    }

    @id(0x01000110)
    action emit_supplemental_samples() {
        telemetry_hint_t hint;
        hint.bank = meta.telemetry_bank;
        hint.epoch = meta.telemetry_epoch;
        hint.sequence = meta.telemetry_sequence;
        hint.ingress_port = standard_metadata.ingress_port;
        digest<telemetry_hint_t>(1, hint);

        meta.packet_in_ingress_port = (bit<16>) standard_metadata.ingress_port;
        meta.packet_in_reason = packet_in_reason_t.TELEMETRY_SAMPLE;
        meta.packet_in_telemetry_bank = (bit<8>) meta.telemetry_bank;
        meta.packet_in_telemetry_epoch = meta.telemetry_epoch;
        meta.packet_in_telemetry_sequence = meta.telemetry_sequence;
        clone_preserving_field_list(CloneType.I2E,
                                    PACKET_IN_CLONE_SESSION,
                                    PACKET_IN_FIELD_LIST);
    }

    @id(0x02000107)
    table telemetry_accumulate {
        key = {
            meta.telemetry_class: exact;
        }
        actions = {
            accumulate_ipv4;
            accumulate_ipv4_fragment;
            accumulate_ipv6;
            accumulate_non_ip;
            accumulate_invalid;
        }
        const entries = {
            0: accumulate_ipv4();
            1: accumulate_ipv4_fragment();
            2: accumulate_ipv6();
            3: accumulate_non_ip();
            4: accumulate_invalid();
        }
        default_action = accumulate_invalid();
    }

    @id(0x02000108)
    table telemetry_sample_gate {
        key = {
            meta.telemetry_sample_key: exact;
        }
        actions = {
            emit_supplemental_samples;
            NoAction;
        }
        const entries = {
            0: emit_supplemental_samples();
        }
        default_action = NoAction();
    }

    apply {
        meta.selector_key = 0;
        meta.active_policy_bank = 0;
        meta.firewall_drop = 0;
        meta.ipv4_supported = 0;
        meta.l4_present = 0;
        meta.l4_src_port = 0;
        meta.l4_dst_port = 0;
        meta.fragment_class = fragment_class_t.INVALID_FRAGMENT;
        meta.telemetry_bank = 0;
        meta.telemetry_epoch = 0;
        meta.telemetry_class = 4;
        meta.telemetry_cell = 0;
        meta.telemetry_sequence = 0;
        meta.telemetry_sample_key = 1;

        if (hdr.ipv4.isValid()) {
            if (hdr.ipv4.fragment_offset != 0) {
                meta.fragment_class = fragment_class_t.NON_INITIAL_FRAGMENT;
            } else if (hdr.ipv4.flags[0:0] == 1) {
                meta.fragment_class = fragment_class_t.FIRST_FRAGMENT;
            } else {
                meta.fragment_class = fragment_class_t.UNFRAGMENTED;
            }
            if (hdr.tcp.isValid()) {
                meta.l4_present = 1;
                meta.l4_src_port = hdr.tcp.src_port;
                meta.l4_dst_port = hdr.tcp.dst_port;
            } else if (hdr.udp.isValid()) {
                meta.l4_present = 1;
                meta.l4_src_port = hdr.udp.src_port;
                meta.l4_dst_port = hdr.udp.dst_port;
            }
            if (hdr.ipv4.version == 4 && hdr.ipv4.ihl == 5 && standard_metadata.checksum_error == 0) {
                meta.ipv4_supported = 1;
            }
        }

        telemetry_selector.apply();

        if (hdr.ipv4.isValid() && meta.ipv4_supported == 1) {
            hash(meta.telemetry_cell,
                 HashAlgorithm.crc32,
                 (bit<32>) 0,
                 {hdr.ipv4.src_addr, hdr.ipv4.dst_addr, hdr.ipv4.protocol,
                  meta.l4_present, meta.l4_src_port, meta.l4_dst_port,
                  meta.fragment_class},
                 TELEMETRY_CELLS);
            if (meta.fragment_class == fragment_class_t.UNFRAGMENTED) {
                meta.telemetry_class = 0;
            } else {
                meta.telemetry_class = 1;
            }
        } else if (hdr.ipv6.isValid() && hdr.ipv6.version == 6 && standard_metadata.parser_error == error.NoError) {
            hash(meta.telemetry_cell,
                 HashAlgorithm.crc32,
                 (bit<32>) 0,
                 {hdr.ipv6.src_addr, hdr.ipv6.dst_addr, hdr.ipv6.next_header},
                 TELEMETRY_CELLS);
            meta.telemetry_class = 2;
        } else if (standard_metadata.parser_error == error.NoError &&
                   standard_metadata.checksum_error == 0 &&
                   !hdr.ipv4.isValid() && !hdr.ipv6.isValid()) {
            meta.telemetry_class = 3;
        }

        telemetry_accumulate.apply();
        meta.telemetry_sample_key = (bit<10>) meta.telemetry_sequence;
        telemetry_sample_gate.apply();

        // Malformed/truncated/parser/checksum/IPv4-options traffic fails closed.
        if (standard_metadata.parser_error != error.NoError ||
            standard_metadata.checksum_error == 1 ||
            (hdr.ipv4.isValid() && meta.ipv4_supported == 0)) {
            meta.firewall_drop = 1;
            mark_to_drop(standard_metadata);
        } else if (hdr.ipv4.isValid()) {
            if (meta.l4_present == 1) {
                response_overlay_eligible_counter.count(0);
                response_overlay.apply();
            }
            if (meta.firewall_drop == 0) {
                policy_selector.apply();
                baseline_eligible_counter.count((bit<32>) meta.active_policy_bank);
                if (meta.active_policy_bank == 0) {
                    baseline_bank_0.apply();
                } else {
                    baseline_bank_1.apply();
                }
            }
        }

        if (meta.firewall_drop == 0) {
            l2_forward.apply();
        }
    }
}

control EgressImpl(inout headers_t hdr,
                   inout metadata_t meta,
                   inout standard_metadata_t standard_metadata) {
    apply {
        if (standard_metadata.egress_port == CPU_PORT) {
            hdr.packet_in.setValid();
            hdr.packet_in.ingress_port = meta.packet_in_ingress_port;
            hdr.packet_in.reason = meta.packet_in_reason;
            hdr.packet_in.telemetry_bank = meta.packet_in_telemetry_bank;
            hdr.packet_in.telemetry_epoch = meta.packet_in_telemetry_epoch;
            hdr.packet_in.telemetry_sequence = meta.packet_in_telemetry_sequence;
        }
    }
}

control ComputeChecksumImpl(inout headers_t hdr, inout metadata_t meta) {
    apply {
        update_checksum(
            hdr.ipv4.isValid() && hdr.ipv4.ihl == 5,
            {
                hdr.ipv4.version,
                hdr.ipv4.ihl,
                hdr.ipv4.dscp,
                hdr.ipv4.ecn,
                hdr.ipv4.total_len,
                hdr.ipv4.identification,
                hdr.ipv4.flags,
                hdr.ipv4.fragment_offset,
                hdr.ipv4.ttl,
                hdr.ipv4.protocol,
                hdr.ipv4.src_addr,
                hdr.ipv4.dst_addr
            },
            hdr.ipv4.checksum,
            HashAlgorithm.csum16);
    }
}

control DeparserImpl(packet_out packet, in headers_t hdr) {
    apply {
        packet.emit(hdr.packet_in);
        packet.emit(hdr.ethernet);
        packet.emit(hdr.ipv4);
        packet.emit(hdr.ipv6);
        packet.emit(hdr.tcp);
        packet.emit(hdr.udp);
    }
}

V1Switch(
    ParserImpl(),
    VerifyChecksumImpl(),
    IngressImpl(),
    EgressImpl(),
    ComputeChecksumImpl(),
    DeparserImpl()
) main;
