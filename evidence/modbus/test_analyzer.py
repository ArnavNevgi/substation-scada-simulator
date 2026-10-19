"""Known-byte protocol fixtures and capture-specific acceptance checks."""
import json
import struct
import tempfile
import unittest
from pathlib import Path

from analyze_capture import analyze, decode_messages, decode_pdu, load_packets, pair_messages

CAPTURE = Path(__file__).parent / 'modbus_control_cycle.pcapng'
if not CAPTURE.exists():
    CAPTURE = Path(__file__).parent / 'package' / 'modbus_control_cycle.pcapng'


def adu(tid=1, pdu=bytes.fromhex('0300130003'), unit=1):
    return struct.pack('>HHHB', tid, 0, len(pdu) + 1, unit) + pdu


def segment(payload, seq=1000, frame=1, src=60145, dst=1502):
    return dict(frame=frame, time_ns=frame * 1_000_000, section=0,
                src_port=src, dst_port=dst, seq=seq, ack=0, flags=24, payload=payload)


class DecoderTests(unittest.TestCase):
    def test_known_fc03_feedback(self):
        self.assertEqual(decode_pdu(bytes.fromhex('0306000200000001'), False)['values'], [2, 0, 1])
        self.assertEqual(decode_pdu(bytes.fromhex('0300130003'), True)['address'], 19)

    def test_fc06_address_and_value(self):
        value = decode_pdu(bytes.fromhex('0600640005'), True)
        self.assertEqual((value['address'], value['value']), (100, 5))

    def test_split_tcp_message(self):
        raw = adu()
        parts = [raw[:3], raw[3:8], raw[8:]]
        packets, pos = [], 1000
        for frame, part in enumerate(parts, 1):
            packets.append(segment(part, pos, frame)); pos += len(part)
        messages, duplicates = decode_messages(packets)
        self.assertEqual((len(messages), duplicates), (1, 0))
        self.assertEqual((messages[0]['first_frame'], messages[0]['frame']), (1, 3))

    def test_coalesced_tcp_messages(self):
        messages, _ = decode_messages([segment(adu(1) + adu(2))])
        self.assertEqual([m['transaction_id'] for m in messages], [1, 2])

    def test_identical_retransmission_is_not_second_command(self):
        messages, duplicates = decode_messages([segment(adu()), segment(adu(), frame=2)])
        self.assertEqual((len(messages), duplicates), (1, 1))

    def test_gaps_and_conflicting_overlap_fail(self):
        with self.assertRaises(ValueError):
            decode_messages([segment(adu()), segment(adu(2), seq=1013, frame=2)])
        with self.assertRaises(ValueError):
            decode_messages([segment(adu()), segment(adu(2), frame=2)])

    def test_incomplete_and_bad_mbap_fail(self):
        with self.assertRaises(ValueError):
            decode_messages([segment(adu()[:-1])])
        bad = bytearray(adu()); bad[3] = 1
        with self.assertRaises(ValueError):
            decode_messages([segment(bytes(bad))])

    def test_response_byte_counts(self):
        for raw in ['03040002', '0303000200']:
            with self.assertRaises(ValueError):
                decode_pdu(bytes.fromhex(raw), False)

    def test_wrong_unit_is_not_paired(self):
        req = decode_messages([segment(adu())])[0][0]
        res = decode_messages([segment(adu(pdu=bytes.fromhex('0306000200000001'), unit=2), src=1502, dst=60145)])[0][0]
        pairs, pending, orphans = pair_messages([req, res])
        self.assertEqual((len(pairs), len(pending), len(orphans)), (0, 1, 1))

    def test_bad_write_echo_fails(self):
        req = decode_messages([segment(adu(pdu=bytes.fromhex('0600640005')))])[0][0]
        res = decode_messages([segment(adu(pdu=bytes.fromhex('0600640004')), src=1502, dst=60145)])[0][0]
        with self.assertRaises(ValueError):
            pair_messages([req, res])


class CaptureAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = analyze(CAPTURE)

    def test_complete_capture_and_timestamps(self):
        r = self.result
        self.assertEqual((r['packets'], r['modbus_messages'], r['matched_transactions']), (335, 161, 80))
        self.assertEqual(r['metadata']['interfaces'][0]['timestamp_units_per_second'], 1_000_000_000)
        self.assertAlmostEqual(r['duration_seconds'], 36.0000147)
        self.assertEqual(r['unit_ids'], [1])
        self.assertEqual((r['exception_messages'], r['tcp_reset_frames'], r['orphan_responses']), ([], [], []))
        self.assertEqual(r['duplicate_tcp_payload_segments'], 0)

    def test_commands_and_exact_wire_values(self):
        r = self.result
        stop, start = r['commands']
        self.assertEqual((stop['code'], stop['sequence'], start['code'], start['sequence']), (5, 1, 4, 2))
        self.assertTrue(stop['code_before_sequence'] and start['code_before_sequence'])
        self.assertEqual((stop['feedback'], start['feedback']), ([2, 0, 1], [2, 0, 2]))
        values = {m['frame']: m['pdu_hex'] for m in r['messages']}
        self.assertEqual([values[n] for n in [176, 180, 186, 247, 251, 257]],
                         ['0600640005', '0600650001', '0306000200000001',
                          '0600640004', '0600650002', '0306000200000002'])
        self.assertAlmostEqual(stop['commit_to_applied_observed_ms'], 92.6087)
        self.assertAlmostEqual(start['commit_to_applied_observed_ms'], 120.2726)

    def test_independent_status_and_measurement_feedback(self):
        r = self.result
        h = {x['frame']: x for x in r['holding_samples']}
        d = {x['frame']: x for x in r['discrete_samples']}
        self.assertEqual([h[n]['pv_kw'] for n in [3, 194, 273]], [2000, 0, 2000])
        self.assertEqual([h[n]['grid_kw'] for n in [3, 194, 273]], [8056, 10053, 8056])
        self.assertEqual([d[n]['pv_running'] for n in [7, 198, 269]], [1, 0, 1])
        self.assertTrue(all(x['cb1_closed'] and x['bus_energized'] and x['remote_mode'] for x in r['discrete_samples']))
        self.assertGreater(r['holding_samples'][-1]['heartbeat'], r['holding_samples'][0]['heartbeat'])
        self.assertEqual(h[265]['model_state'], 0)
        self.assertEqual(h[273]['model_state'], 1)

    def test_trailing_poll_is_explicit_boundary(self):
        r = self.result
        self.assertEqual(len(r['unmatched_requests']), 1)
        req = r['unmatched_requests'][0]
        self.assertEqual((req['frame'], req['function'], req['address'], req['quantity']), (335, 3, 0, 117))
        self.assertEqual(req['frame'], r['last_packet_frame'])

    def test_corrupt_pcapng_trailer_is_rejected(self):
        raw = bytearray(CAPTURE.read_bytes()); raw[-1] ^= 1
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'corrupt.pcapng'; p.write_bytes(raw)
            with self.assertRaises(ValueError):
                load_packets(p)


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = dict(tests_run=result.testsRun, passed=result.wasSuccessful(),
                  failures=len(result.failures), errors=len(result.errors),
                  coverage='Known-byte protocol fixtures, TCP split/coalesced/retransmitted segments, malformed data, and the supplied capture acceptance.')
    output = CAPTURE.parent / 'results' / 'verification.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
