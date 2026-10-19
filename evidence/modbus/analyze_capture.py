#!/usr/bin/env python3
"""Read-only Modbus analysis for this project's Windows loopback pcapng.

Python 3.10+, standard library only. No sockets, device access, or writes to
the capture. Supports LINKTYPE_NULL/IPv4/TCP, in-order TCP reassembly, and
FC02/03/06/16. Unsupported formats, gaps, overlaps and malformed ADUs fail
explicitly. This is a scoped evidence analyzer, not a Wireshark replacement.
"""
import argparse
import csv
import hashlib
import json
import struct
from collections import Counter
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path


def require(ok, message):
    if not ok:
        raise ValueError(message)


def options(data, endian):
    result = {}
    pos = 0
    while pos + 4 <= len(data):
        code, size = struct.unpack_from(endian + 'HH', data, pos)
        pos += 4
        if code == 0:
            require(size == 0, 'Invalid end-of-options')
            break
        require(pos + size <= len(data), 'Truncated pcapng option')
        result[code] = data[pos:pos + size]
        pos += (size + 3) // 4 * 4
    return result


def load_packets(path):
    data = Path(path).read_bytes()
    pos, section, endian = 0, -1, None
    interfaces, local_interfaces, packets, statistics = [], [], [], []
    blocks = Counter()
    while pos < len(data):
        require(pos + 12 <= len(data), 'Truncated pcapng block header')
        if data[pos:pos + 4] == b'\x0a\x0d\x0d\x0a':
            magic = data[pos + 8:pos + 12]
            require(magic in (b'\x4d\x3c\x2b\x1a', b'\x1a\x2b\x3c\x4d'), 'Bad section byte-order magic')
            endian = '<' if magic[0] == 0x4d else '>'
            section += 1
            local_interfaces = []
        require(endian is not None, 'Missing section header')
        kind, size = struct.unpack_from(endian + 'II', data, pos)
        require(size >= 12 and size % 4 == 0 and pos + size <= len(data), 'Invalid pcapng block length')
        require(struct.unpack_from(endian + 'I', data, pos + size - 4)[0] == size, 'Mismatched pcapng trailing length')
        body = data[pos + 8:pos + size - 4]
        blocks[hex(kind)] += 1
        if kind == 1:
            require(len(body) >= 8, 'Truncated interface block')
            link, _, snaplen = struct.unpack_from(endian + 'HHI', body)
            opt = options(body[8:], endian)
            resol = opt.get(9, b'\x06')[0]
            divisor = (2 ** (resol & 127)) if resol & 128 else 10 ** resol
            offset = struct.unpack(endian + 'q', opt.get(14, b'\0' * 8))[0]
            interface = dict(section=section, interface=len(local_interfaces), linktype=link,
                             snaplen=snaplen, timestamp_units_per_second=divisor,
                             timestamp_offset_seconds=offset,
                             name=opt.get(2, b'').decode('utf-8', 'replace'))
            local_interfaces.append(interface)
            interfaces.append(interface)
        elif kind == 6:
            require(len(body) >= 20, 'Truncated enhanced packet block')
            iid, hi, lo, caplen, origlen = struct.unpack_from(endian + 'IIIII', body)
            require(iid < len(local_interfaces), 'Unknown interface')
            interface = local_interfaces[iid]
            require(caplen == origlen and 20 + caplen <= len(body), 'Truncated captured packet')
            require(interface['linktype'] == 0, 'Only LINKTYPE_NULL is supported')
            raw = body[20:20 + caplen]
            require(len(raw) >= 24 and raw[:4] in (b'\x02\0\0\0', b'\0\0\0\x02'), 'Only IPv4 loopback frames are supported')
            ip = raw[4:]
            ihl, total = (ip[0] & 15) * 4, int.from_bytes(ip[2:4], 'big')
            require(ip[0] >> 4 == 4 and 20 <= ihl <= total <= len(ip), 'Invalid IPv4 header')
            require(ip[9] == 6 and int.from_bytes(ip[6:8], 'big') & 0x3fff == 0, 'Only unfragmented TCP is supported')
            require(ip[12:16] == ip[16:20] == b'\x7f\0\0\x01', 'Expected localhost traffic')
            tcp = ip[ihl:total]
            require(len(tcp) >= 20, 'Truncated TCP header')
            src, dst, seq, ack = struct.unpack_from('>HHII', tcp)
            thl = (tcp[12] >> 4) * 4
            require(20 <= thl <= len(tcp) and (src == 1502) != (dst == 1502), 'Expected TCP port 1502 traffic')
            tick = (hi << 32) | lo
            ts = Fraction(tick, interface['timestamp_units_per_second']) + interface['timestamp_offset_seconds']
            packets.append(dict(frame=len(packets) + 1, section=section, time_ns=round(ts * 10**9),
                                src_port=src, dst_port=dst, seq=seq, ack=ack, flags=tcp[13],
                                payload=tcp[thl:]))
        elif kind == 5:
            require(len(body) >= 12, 'Truncated interface statistics')
            iid = struct.unpack_from(endian + 'I', body)[0]
            opt = options(body[12:], endian)
            statistics.append(dict(section=section, interface=iid, **{
                name: struct.unpack(endian + 'Q', opt[code])[0]
                for code, name in [(4, 'received'), (5, 'interface_dropped'), (6, 'filter_accepted'),
                                   (7, 'os_dropped'), (8, 'delivered')]
                if code in opt}))
        elif kind in (2, 3):
            raise ValueError('Obsolete/simple packet blocks are unsupported; timestamps are required')
        pos += size
    require(packets, 'Capture has no packets')
    return packets, dict(interfaces=interfaces, blocks=dict(blocks), interface_statistics=statistics)


def decode_pdu(pdu, request):
    require(pdu, 'Empty Modbus PDU')
    fc = pdu[0]
    result = {'function': fc}
    if fc & 128:
        require(not request and len(pdu) == 2, 'Invalid exception PDU')
        result['exception'] = pdu[1]
    elif request and fc in (2, 3, 6, 16):
        require(len(pdu) >= 5, 'Short request')
        result['address'], value = struct.unpack_from('>HH', pdu, 1)
        if fc == 6:
            require(len(pdu) == 5, 'Invalid FC06 length')
            result['value'] = value
        else:
            result['quantity'] = value
            maximum = {2: 2000, 3: 125, 16: 123}[fc]
            require(1 <= value <= maximum and result['address'] + value <= 65536, 'Invalid register/bit quantity')
            if fc == 16:
                require(len(pdu) == 6 + 2 * value and pdu[5] == 2 * value, 'Invalid FC16 byte count')
                result['values'] = list(struct.unpack('>' + 'H' * value, pdu[6:]))
            else:
                require(len(pdu) == 5, 'Invalid read request length')
    elif not request and fc in (2, 3):
        require(len(pdu) >= 2 and len(pdu) == pdu[1] + 2, 'Invalid read response byte count')
        if fc == 3:
            require(pdu[1] % 2 == 0, 'Odd register response byte count')
            result['values'] = list(struct.unpack('>' + 'H' * (pdu[1] // 2), pdu[2:]))
        else:
            result['packed_bits'] = pdu[2:].hex()
    elif not request and fc in (6, 16):
        require(len(pdu) == 5, 'Invalid write response length')
        result['address'], value = struct.unpack_from('>HH', pdu, 1)
        result['value' if fc == 6 else 'quantity'] = value
    else:
        raise ValueError('Unsupported Modbus function %d' % fc)
    return result


def decode_messages(packets):
    streams, messages = {}, []
    duplicates = 0
    for packet in packets:
        key = packet['section'], packet['src_port'], packet['dst_port']
        if packet['flags'] & 2 and not packet['flags'] & 16:
            require(key not in streams, 'Reused TCP client port/connection is unsupported')
        data = packet['payload']
        if not data:
            continue
        seq = packet['seq'] + bool(packet['flags'] & 2)
        stream = streams.setdefault(key, dict(next_seq=seq, buf=bytearray(), owners=[], segments={}))
        if seq in stream['segments']:
            require(stream['segments'][seq] == data, 'Conflicting retransmission')
            duplicates += 1
            continue
        require(seq == stream['next_seq'], 'TCP gap/out-of-order/overlap; use Wireshark reassembly for this file')
        stream['segments'][seq] = data
        stream['next_seq'] = (seq + len(data)) % (2**32)
        stream['buf'].extend(data)
        stream['owners'].extend([packet] * len(data))
        buf, owners = stream['buf'], stream['owners']
        while len(buf) >= 7:
            tid, proto, length, unit = struct.unpack_from('>HHHB', buf)
            require(proto == 0 and 2 <= length <= 254, 'Invalid MBAP protocol/length')
            size = 6 + length
            if len(buf) < size:
                break
            first, last = owners[0], owners[size - 1]
            request = packet['dst_port'] == 1502
            pdu = bytes(buf[7:size])
            msg = dict(frame=last['frame'], first_frame=first['frame'], time_ns=last['time_ns'],
                       client_port=packet['src_port'] if request else packet['dst_port'],
                       section=packet['section'], transaction_id=tid, unit=unit, request=request,
                       pdu_hex=pdu.hex(), **decode_pdu(pdu, request))
            messages.append(msg)
            del buf[:size]
            del owners[:size]
    require(all(not s['buf'] for s in streams.values()), 'Incomplete Modbus ADU at capture boundary')
    messages.sort(key=lambda m: (m['frame'], m['first_frame']))
    return messages, duplicates


def pair_messages(messages):
    pending, pairs, orphan_responses = {}, [], []
    for msg in messages:
        key = msg['section'], msg['client_port'], msg['transaction_id'], msg['unit']
        if msg['request']:
            require(key not in pending, 'Transaction ID reused before response')
            pending[key] = msg
            continue
        req = pending.pop(key, None)
        if req is None:
            orphan_responses.append(msg)
            continue
        require((msg['function'] & 127) == req['function'], 'Response function mismatch')
        if 'exception' not in msg:
            if req['function'] in (6, 16):
                require(msg['pdu_hex'] == req['pdu_hex'][:10], 'Write response echo mismatch')
            elif req['function'] == 3:
                require(len(msg['values']) == req['quantity'], 'Read register count mismatch')
            elif req['function'] == 2:
                require(len(bytes.fromhex(msg['packed_bits'])) == (req['quantity'] + 7) // 8, 'Read bit count mismatch')
        pairs.append(dict(request=req, response=msg, round_trip_ms=(msg['time_ns'] - req['time_ns']) / 1e6))
    return pairs, list(pending.values()), orphan_responses


def analyze(path):
    packets, metadata = load_packets(path)
    messages, duplicates = decode_messages(packets)
    pairs, pending, orphans = pair_messages(messages)
    first = packets[0]['time_ns']
    for message in messages:
        message['relative_seconds'] = (message['time_ns'] - first) / 1e9
    holdings, discretes = [], []
    for pair in pairs:
        req, res = pair['request'], pair['response']
        if 'exception' in res:
            continue
        if req['function'] == 3 and req['address'] == 0 and req['quantity'] >= 117:
            v = res['values']
            holdings.append(dict(frame=res['frame'], time_ns=res['time_ns'],
                relative_seconds=res['relative_seconds'], heartbeat=v[1], grid_kw=v[4] if v[4] < 32768 else v[4] - 65536,
                pv_kw=v[13], bus_kv=round(v[2] / 100, 2), t1_loading_pct=round(v[8] / 10, 1),
                served_load_kw=v[6], model_state=v[18], command_status=v[19], reason=v[20], ack_sequence=v[21]))
        if req['function'] == 2 and req['address'] == 0 and req['quantity'] >= 9:
            packed = bytes.fromhex(res['packed_bits'])
            values = [(packed[n // 8] >> (n % 8)) & 1 for n in range(9)]
            discretes.append(dict(frame=res['frame'], time_ns=res['time_ns'], relative_seconds=res['relative_seconds'],
                **dict(zip(['cb1_closed', 'trip_latched', 'pv_running', 'grid_available', 'model_valid',
                            'bus_energized', 'remote_mode', 'fault_active', 'pv_tripped'], values))))
    writes = [p for p in pairs if p['request']['function'] in (6, 16)]
    commands, staged = [], None
    for pair in writes:
        req = pair['request']
        require('exception' not in pair['response'], 'Command write returned a Modbus exception')
        require(req['function'] == 6, 'Command report expects individual FC06 writes')
        if req['address'] == 100:
            require(staged is None, 'Command payload overwritten before commit')
            staged = pair
        elif req['address'] == 101:
            require(staged is not None, 'Sequence committed without captured payload')
            code = staged['request']['value']
            seq = req['value']
            matches = [p for p in pairs if p['request']['function'] == 3 and
                p['request'].get('address') == 19 and p['request'].get('quantity') == 3 and
                p['request']['frame'] > req['frame'] and p['response'].get('values') == [2, 0, seq]]
            feedback = matches[0] if matches else None
            commands.append(dict(command={4: 'START PV1', 5: 'STOP PV1'}.get(code, 'CODE %d' % code), code=code, sequence=seq,
                code_request_frame=staged['request']['frame'], code_response_frame=staged['response']['frame'],
                commit_request_frame=req['frame'], commit_response_frame=pair['response']['frame'],
                code_before_sequence=staged['response']['frame'] < req['frame'],
                commit_time_ns=req['time_ns'], commit_relative_seconds=req['relative_seconds'],
                feedback_request_frame=feedback['request']['frame'] if feedback else None,
                feedback_response_frame=feedback['response']['frame'] if feedback else None,
                feedback=feedback['response']['values'] if feedback else None,
                commit_to_applied_observed_ms=(feedback['response']['time_ns'] - req['time_ns']) / 1e6 if feedback else None))
            staged = None
    require(staged is None, 'Uncommitted payload at capture boundary')
    result = dict(capture=Path(path).name, sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        capture_bytes=Path(path).stat().st_size, packets=len(packets),
        first_packet_utc=datetime.fromtimestamp(first / 1e9, timezone.utc).isoformat(),
        duration_seconds=(packets[-1]['time_ns'] - first) / 1e9,
        last_packet_frame=packets[-1]['frame'], unit_ids=sorted(set(m['unit'] for m in messages)),
        client_ports=sorted(set(m['client_port'] for m in messages)), metadata=metadata,
        modbus_messages=len(messages), request_count=sum(m['request'] for m in messages), matched_transactions=len(pairs),
        function_counts=dict(Counter(str(m['function']) + (' request' if m['request'] else ' response') for m in messages)),
        duplicate_tcp_payload_segments=duplicates, tcp_reset_frames=[p['frame'] for p in packets if p['flags'] & 4],
        exception_messages=[m for m in messages if 'exception' in m],
        unmatched_requests=pending, orphan_responses=orphans, commands=commands,
        holding_samples=holdings, discrete_samples=discretes, messages=messages)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.capture)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'analysis.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    for name, rows in [('commands', result['commands']), ('holding_telemetry', result['holding_samples']),
                       ('discrete_telemetry', result['discrete_samples']), ('modbus_messages', result['messages'])]:
        columns = list(dict.fromkeys(k for row in rows for k in row))
        with (args.output / (name + '.csv')).open('w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
    print(json.dumps({k: result[k] for k in ['packets', 'duration_seconds', 'matched_transactions', 'commands',
                                             'unmatched_requests', 'duplicate_tcp_payload_segments']}, indent=2))


if __name__ == '__main__':
    main()
