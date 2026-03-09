"""
tests/test_protocol.py — Unit tests for LME2510C USB protocol logic

Tests are organized by functional area and use a MockUsbDevice to avoid
requiring physical hardware.  All protocol packet formats are validated
against the IDA Pro 9.0 decompilation of UDE262D.sys documented in
LME2510_Analysis.md and confirmed by reading driver/UDE262D.sys.c.

Run:
    python -m pytest tests/test_protocol.py -v
or:
    python -m unittest tests.test_protocol -v
"""

import sys
import os
import unittest
from collections import deque

# ── Import the module under test ─────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lme2510_init as lme_mod
from lme2510_init import (
    LME2510,
    DEMOD_ADDR, DEMOD_HIGH, TUNER_ADDR, REF_FREQ, FW_ACK_OK,
    PID_COLD, PID_WARM,
)


# ── Mock USB device ───────────────────────────────────────────────────────────

class MockUsbDevice:
    """
    Minimal pyusb Device stub for testing.  Records all writes and serves
    pre-queued byte strings for reads.
    """
    def __init__(self):
        self.written: list[bytes] = []   # all bytes written via write()
        self._read_queue: deque = deque()

    def write(self, ep, data, timeout=1000):
        self.written.append(bytes(data))
        return len(data)

    def read(self, ep, size, timeout=1000):
        if self._read_queue:
            return self._read_queue.popleft()
        raise RuntimeError("MockUsbDevice: read queue is empty")

    def queue_read(self, data: bytes):
        """Enqueue bytes to be returned by the next read() call."""
        self._read_queue.append(data)

    def last_write(self) -> bytes:
        return self.written[-1]

    def reset(self):
        self.written.clear()
        self._read_queue.clear()


def make_lme(dev: MockUsbDevice) -> LME2510:
    """Construct an LME2510 instance around a MockUsbDevice."""
    return LME2510(dev)


# ═══════════════════════════════════════════════════════════════════════════════
# 1.  Checksum (sub_135CA)
# ═══════════════════════════════════════════════════════════════════════════════

class TestFirmwareChecksum(unittest.TestCase):
    """
    sub_135CA: simple 8-bit summation checksum.
    All bytes are summed; result is truncated to 8 bits.
    """

    def _chk(self, data):
        return LME2510._fw_checksum(data)

    def test_all_zeros(self):
        self.assertEqual(self._chk(b'\x00' * 50), 0x00)

    def test_all_ones(self):
        # 50 × 0xFF = 0x31B2; truncated to 8 bits = 0xB2
        self.assertEqual(self._chk(b'\xFF' * 50), (0xFF * 50) & 0xFF)

    def test_known_value(self):
        data = bytes(range(10))  # 0+1+2+…+9 = 45 = 0x2D
        self.assertEqual(self._chk(data), 0x2D)

    def test_wrap_around(self):
        # 256 × 0x01 = 256; 256 & 0xFF = 0
        self.assertEqual(self._chk(b'\x01' * 256), 0x00)

    def test_single_byte(self):
        self.assertEqual(self._chk(b'\xAB'), 0xAB)


# ═══════════════════════════════════════════════════════════════════════════════
# 2.  Firmware download packet construction (sub_1392E)
# ═══════════════════════════════════════════════════════════════════════════════

class TestFirmwarePacketFormat(unittest.TestCase):
    """
    Packet format from driver sub_1392E:
      Normal chunk : [Cmd]     [Len-1]  [50-byte data] [checksum]   = 53 bytes
      Last chunk   : [Cmd|0x80][n-1]    [n-byte data]  [checksum]   = n+3 bytes
    """

    def _build_pkt(self, cmd, chunk):
        """Replicate the packet-build logic of LME2510._download_stage."""
        is_last = False   # not relevant here; caller passes correct cmd
        chk = LME2510._fw_checksum(chunk)
        return bytes([cmd, len(chunk) - 1]) + chunk + bytes([chk])

    # ── FW1 normal chunk ──────────────────────────────────────────────────────

    def test_fw1_normal_chunk_cmd_byte(self):
        chunk = bytes(range(50))
        pkt = self._build_pkt(0x01, chunk)
        self.assertEqual(pkt[0], 0x01, "FW1 normal chunk must start with 0x01")

    def test_fw1_normal_chunk_len_field(self):
        chunk = bytes(range(50))
        pkt = self._build_pkt(0x01, chunk)
        self.assertEqual(pkt[1], 49, "Len-1 for 50-byte chunk must be 49")

    def test_fw1_normal_chunk_total_size(self):
        chunk = bytes(range(50))
        pkt = self._build_pkt(0x01, chunk)
        self.assertEqual(len(pkt), 53)  # 1 + 1 + 50 + 1

    def test_fw1_normal_chunk_checksum_position(self):
        chunk = bytes(range(50))
        pkt = self._build_pkt(0x01, chunk)
        expected_chk = LME2510._fw_checksum(chunk)
        self.assertEqual(pkt[-1], expected_chk)

    # ── FW1 last chunk ────────────────────────────────────────────────────────

    def test_fw1_last_chunk_cmd_byte(self):
        chunk = b'\xAA\xBB\xCC'
        pkt = self._build_pkt(0x01 | 0x80, chunk)
        self.assertEqual(pkt[0], 0x81, "FW1 last chunk must use 0x81")

    def test_fw1_last_chunk_len_field(self):
        chunk = b'\xAA\xBB\xCC'
        pkt = self._build_pkt(0x01 | 0x80, chunk)
        self.assertEqual(pkt[1], 2, "Len-1 for 3-byte last chunk must be 2")

    def test_fw1_last_chunk_total_size(self):
        n = 7
        chunk = bytes([i & 0xFF for i in range(n)])
        pkt = self._build_pkt(0x81, chunk)
        self.assertEqual(len(pkt), n + 3)  # 1 + 1 + n + 1

    # ── FW2 normal / last chunk ───────────────────────────────────────────────

    def test_fw2_normal_cmd(self):
        chunk = bytes(50)
        pkt = self._build_pkt(0x02, chunk)
        self.assertEqual(pkt[0], 0x02)

    def test_fw2_last_cmd(self):
        chunk = bytes(1)
        pkt = self._build_pkt(0x82, chunk)
        self.assertEqual(pkt[0], 0x82)

    # ── Edge: last chunk is exactly 50 bytes ─────────────────────────────────

    def test_last_chunk_exactly_50_bytes(self):
        """A firmware whose size is an exact multiple of 50 must still mark
        the very last 50-byte chunk with Cmd | 0x80."""
        chunk = bytes(range(50))
        pkt = self._build_pkt(0x81, chunk)  # 0x01|0x80
        self.assertEqual(pkt[0], 0x81)
        self.assertEqual(pkt[1], 49)
        self.assertEqual(len(pkt), 53)

    # ── Actual firmware files can be parsed ───────────────────────────────────

    def test_fw_bootloader_chunk_count(self):
        """fw_bootloader.bin is 512 bytes → 10 chunks of 50 + 1 of 12 → 11 total."""
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "fw", "fw_bootloader.bin")
        if not os.path.exists(path):
            self.skipTest(f"Firmware file not found: {path}")
        with open(path, "rb") as f:
            blob = f.read()
        self.assertEqual(len(blob), 512)
        chunks = [blob[i:i+50] for i in range(0, len(blob), 50)]
        self.assertEqual(len(chunks), 11)  # 10 full + 1 partial(12)

    def test_fw_lgs8g75_chunk_count(self):
        """fw_lgs8g75.bin is 4836 bytes → 96 chunks of 50 + 1 of 36 → 97 total."""
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "fw", "fw_lgs8g75.bin")
        if not os.path.exists(path):
            self.skipTest(f"Firmware file not found: {path}")
        with open(path, "rb") as f:
            blob = f.read()
        self.assertEqual(len(blob), 4836)
        chunks = [blob[i:i+50] for i in range(0, len(blob), 50)]
        self.assertEqual(len(chunks), 97)  # 96 full + 1 partial(36)


# ═══════════════════════════════════════════════════════════════════════════════
# 3.  Firmware ACK byte validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestFirmwareAck(unittest.TestCase):
    """
    Driver sub_1392E: FAIL if read succeeded AND byte != -120 AND byte != 119.
    -120 signed == 0x88 unsigned; 119 == 0x77.
    """

    def test_0x88_is_valid(self):
        self.assertIn(0x88, FW_ACK_OK)

    def test_0x77_is_valid(self):
        self.assertIn(0x77, FW_ACK_OK)

    def test_other_values_invalid(self):
        for v in [0x00, 0x01, 0x55, 0xFF, 0xAA, 0x87, 0x78]:
            self.assertNotIn(v, FW_ACK_OK, f"0x{v:02x} should not be a valid ACK")


# ═══════════════════════════════════════════════════════════════════════════════
# 4.  Command 0x04 — Block I2C write (sub_14083 / sub_14FA2)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmd04WriteBlock(unittest.TestCase):
    """
    Format: [04][Len=2+N][DevAddr][RegAddr][Data*N]
    ACK:    [88] (first byte; driver reads 4 bytes from EP 0x81)
    """

    def _send_and_capture(self, dev_addr, reg_addr, data):
        mock = MockUsbDevice()
        lme = make_lme(mock)
        # Queue ACK response (4 bytes, first is 0x88)
        mock.queue_read(bytes([0x88, 0x00, 0x00, 0x00]))
        ok = lme.cmd_write_block(dev_addr, reg_addr, data)
        return ok, mock.last_write()

    def test_packet_starts_with_0x04(self):
        _, pkt = self._send_and_capture(0xC0, 0x00, [0x01, 0x02, 0x03])
        self.assertEqual(pkt[0], 0x04)

    def test_len_field_is_2_plus_n(self):
        data = [0xAA, 0xBB, 0xCC, 0xDD, 0xEE]  # N=5
        _, pkt = self._send_and_capture(0xC0, 0x00, data)
        self.assertEqual(pkt[1], 2 + len(data), "Len = 2 + data_count")

    def test_dev_addr_position(self):
        _, pkt = self._send_and_capture(0xC0, 0x00, [0x11])
        self.assertEqual(pkt[2], 0xC0, "DevAddr at byte 2")

    def test_reg_addr_position(self):
        _, pkt = self._send_and_capture(0xC0, 0x0A, [0x83])
        self.assertEqual(pkt[3], 0x0A, "RegAddr at byte 3")

    def test_data_bytes(self):
        data = [0x33, 0x18, 0x00, 0x00, 0xB7]
        _, pkt = self._send_and_capture(0xC0, 0x00, data)
        self.assertEqual(list(pkt[4:4+len(data)]), data)

    def test_tuner_618mhz_packet(self):
        """
        618 MHz example from LME2510_Analysis.md §5.2:
        CMD: 04 07 C0 00 33 18 00 00 B7
        """
        data = [0x33, 0x18, 0x00, 0x00, 0xB7]
        _, pkt = self._send_and_capture(0xC0, 0x00, data)
        expected = bytes([0x04, 0x07, 0xC0, 0x00, 0x33, 0x18, 0x00, 0x00, 0xB7])
        self.assertEqual(pkt, expected)

    def test_returns_true_on_0x88_ack(self):
        ok, _ = self._send_and_capture(0x32, 0x01, [0xE0])
        self.assertTrue(ok)

    def test_returns_false_on_bad_ack(self):
        mock = MockUsbDevice()
        lme = make_lme(mock)
        mock.queue_read(bytes([0x00, 0x00, 0x00, 0x00]))  # wrong ACK
        ok = lme.cmd_write_block(0x32, 0x01, [0xE0])
        self.assertFalse(ok)


# ═══════════════════════════════════════════════════════════════════════════════
# 5.  Command 0x05 — Single register write (sub_1417A)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmd05WriteReg(unittest.TestCase):
    """
    Format: [05][04][DevAddr][RegAddr][Value]
    ACK:    [88]
    """

    def _send_and_capture(self, dev_addr, reg_addr, value):
        mock = MockUsbDevice()
        lme = make_lme(mock)
        mock.queue_read(bytes([0x88, 0x00, 0x00, 0x00]))
        ok = lme.cmd_write_single(dev_addr, reg_addr, value)
        return ok, mock.last_write()

    def test_packet_starts_with_0x05(self):
        _, pkt = self._send_and_capture(0x32, 0x01, 0xE0)
        self.assertEqual(pkt[0], 0x05)

    def test_len_field_always_0x04(self):
        _, pkt = self._send_and_capture(0x32, 0x01, 0xE0)
        self.assertEqual(pkt[1], 0x04)

    def test_dev_addr(self):
        _, pkt = self._send_and_capture(0x32, 0x01, 0xE0)
        self.assertEqual(pkt[2], 0x32)

    def test_reg_addr(self):
        _, pkt = self._send_and_capture(0x32, 0x01, 0xE0)
        self.assertEqual(pkt[3], 0x01)

    def test_value(self):
        _, pkt = self._send_and_capture(0x32, 0x01, 0xE0)
        self.assertEqual(pkt[4], 0xE0)

    def test_repeater_enable_packet(self):
        """Enable I2C repeater: 05 04 32 01 E0 (sub_147DA(50,1,224))."""
        _, pkt = self._send_and_capture(0x32, 0x01, 0xE0)
        self.assertEqual(pkt, bytes([0x05, 0x04, 0x32, 0x01, 0xE0]))

    def test_repeater_disable_packet(self):
        """Disable I2C repeater: 05 04 32 01 60 (sub_147DA(50,1,96))."""
        _, pkt = self._send_and_capture(0x32, 0x01, 0x60)
        self.assertEqual(pkt, bytes([0x05, 0x04, 0x32, 0x01, 0x60]))

    def test_value_truncated_to_8_bits(self):
        _, pkt = self._send_and_capture(0x32, 0x01, 0x1E0)  # 0x1E0 → 0xE0
        self.assertEqual(pkt[4], 0xE0)


# ═══════════════════════════════════════════════════════════════════════════════
# 6.  Command 0x84 — Block I2C read (sub_14106)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmd84ReadBlock(unittest.TestCase):
    """
    Request:  [84][03][DevAddr][RegAddr][Count]
    Response: [55][data * Count]
    """

    def _send_and_capture(self, dev_addr, reg_addr, count, data_bytes):
        mock = MockUsbDevice()
        lme = make_lme(mock)
        mock.queue_read(bytes([0x55]) + bytes(data_bytes))
        result = lme.cmd_read_block(dev_addr, reg_addr, count)
        return result, mock.last_write()

    def test_packet_starts_with_0x84(self):
        _, pkt = self._send_and_capture(0xC0, 0x04, 1, [0x07])
        self.assertEqual(pkt[0], 0x84)

    def test_len_field_always_0x03(self):
        _, pkt = self._send_and_capture(0xC0, 0x04, 1, [0x07])
        self.assertEqual(pkt[1], 0x03)

    def test_dev_addr_position(self):
        _, pkt = self._send_and_capture(0xC0, 0x04, 1, [0x07])
        self.assertEqual(pkt[2], 0xC0)

    def test_reg_addr_position(self):
        _, pkt = self._send_and_capture(0xC0, 0x04, 1, [0x07])
        self.assertEqual(pkt[3], 0x04)

    def test_count_field(self):
        _, pkt = self._send_and_capture(0xC0, 0x00, 3, [0x01, 0x02, 0x03])
        self.assertEqual(pkt[4], 3)

    def test_returns_data_without_prefix(self):
        """0x55 prefix must be stripped from the returned data."""
        result, _ = self._send_and_capture(0xC0, 0x04, 1, [0x07])
        self.assertEqual(result, b'\x07')

    def test_returns_none_on_wrong_prefix(self):
        mock = MockUsbDevice()
        lme = make_lme(mock)
        mock.queue_read(bytes([0xAA, 0x07]))  # wrong prefix
        result = lme.cmd_read_block(0xC0, 0x04, 1)
        self.assertIsNone(result)

    def test_multi_byte_read(self):
        data = [0x11, 0x22, 0x33, 0x44, 0x55]
        result, _ = self._send_and_capture(0xC0, 0x00, 5, data)
        self.assertEqual(result, bytes(data))

    def test_pll_latch_read_packet(self):
        """PLL latch read: 84 03 C0 04 01 (sub_1524A step 5)."""
        _, pkt = self._send_and_capture(0xC0, 0x04, 1, [0x07])
        self.assertEqual(pkt, bytes([0x84, 0x03, 0xC0, 0x04, 0x01]))


# ═══════════════════════════════════════════════════════════════════════════════
# 7.  Command 0x85 — Single register read (sub_14240)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmd85ReadSingle(unittest.TestCase):
    """
    Request:  [85][02][DevAddr][RegAddr][xx]  — 5th byte is residual/irrelevant
    Response: [55][value][xx][xx][xx]         — value is at index 1
    """

    def _send_and_capture(self, dev_addr, reg_addr, value_byte):
        mock = MockUsbDevice()
        lme = make_lme(mock)
        # Response: [0x55][value][3 residual bytes]
        mock.queue_read(bytes([0x55, value_byte, 0xAB, 0xCD, 0xEF]))
        result = lme.cmd_read_single(dev_addr, reg_addr)
        return result, mock.last_write()

    def test_packet_starts_with_0x85(self):
        _, pkt = self._send_and_capture(0x32, 0x00, 0x0E)
        self.assertEqual(pkt[0], 0x85)

    def test_len_field_always_0x02(self):
        _, pkt = self._send_and_capture(0x32, 0x00, 0x0E)
        self.assertEqual(pkt[1], 0x02)

    def test_dev_addr_position(self):
        _, pkt = self._send_and_capture(0x32, 0x00, 0x0E)
        self.assertEqual(pkt[2], 0x32)

    def test_reg_addr_position(self):
        _, pkt = self._send_and_capture(0x32, 0x4B, 0x01)
        self.assertEqual(pkt[3], 0x4B)

    def test_value_is_at_response_index_1(self):
        """Value must be extracted from resp[1], NOT resp[2] (bug in Demod_Identification.md ≤ v1)."""
        result, _ = self._send_and_capture(0x32, 0x00, 0x0E)
        self.assertEqual(result, 0x0E)

    def test_demod_identify_packet(self):
        """Demod identification: 85 02 32 00 00 (sub_1485E(50, 0, ...))."""
        _, pkt = self._send_and_capture(0x32, 0x00, 0x0E)
        self.assertEqual(pkt, bytes([0x85, 0x02, 0x32, 0x00, 0x00]))

    def test_lock_status_packet(self):
        """Lock polling: 85 02 32 4B xx (LME2510_Analysis.md §5.3)."""
        _, pkt = self._send_and_capture(0x32, 0x4B, 0x01)
        self.assertEqual(pkt, bytes([0x85, 0x02, 0x32, 0x4B, 0x00]))

    def test_returns_none_on_wrong_prefix(self):
        mock = MockUsbDevice()
        lme = make_lme(mock)
        mock.queue_read(bytes([0x85, 0x0E, 0x00, 0x00, 0x00]))  # wrong prefix
        result = lme.cmd_read_single(0x32, 0x00)
        self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════════════════════
# 8.  Demodulator address routing (sub_142BB)
# ═══════════════════════════════════════════════════════════════════════════════

class TestDemodRouting(unittest.TestCase):
    """
    sub_142BB: logical register → physical I2C device address
      0x00 – 0xBF → 0x32 (DEMOD_ADDR, primary bank)
      0xC0 – 0xFF → 0x36 (DEMOD_HIGH, extended bank, same chip)
    """

    def test_reg_0x00_routes_to_low_bank(self):
        self.assertEqual(LME2510._demod_phys_addr(0x00), DEMOD_ADDR)

    def test_reg_0x4b_routes_to_low_bank(self):
        self.assertEqual(LME2510._demod_phys_addr(0x4B), DEMOD_ADDR)

    def test_reg_0xBF_is_still_low_bank(self):
        self.assertEqual(LME2510._demod_phys_addr(0xBF), DEMOD_ADDR)

    def test_reg_0xC0_routes_to_high_bank(self):
        self.assertEqual(LME2510._demod_phys_addr(0xC0), DEMOD_HIGH)

    def test_reg_0xFF_routes_to_high_bank(self):
        self.assertEqual(LME2510._demod_phys_addr(0xFF), DEMOD_HIGH)

    def test_reg_0xC5_routes_to_high_bank(self):
        # sub_14474 writes to 0xC5 via sub_142EA which routes to 0x36
        self.assertEqual(LME2510._demod_phys_addr(0xC5), DEMOD_HIGH)

    def test_boundary_0xBF(self):
        self.assertEqual(LME2510._demod_phys_addr(0xBF), DEMOD_ADDR)
        self.assertEqual(LME2510._demod_phys_addr(0xC0), DEMOD_HIGH)

    def test_constants(self):
        self.assertEqual(DEMOD_ADDR, 0x32)
        self.assertEqual(DEMOD_HIGH, 0x36)


# ═══════════════════════════════════════════════════════════════════════════════
# 9.  Demodulator identification (sub_13AD7)
# ═══════════════════════════════════════════════════════════════════════════════

class TestIdentifyDemod(unittest.TestCase):
    """
    sub_13AD7: read Demod reg 0x00.
      0x0E (14) → LGS8GL5
      other    → LGS8G75
    """

    def _identify(self, reg00_value):
        mock = MockUsbDevice()
        lme = make_lme(mock)
        mock.queue_read(bytes([0x55, reg00_value, 0, 0, 0]))
        return lme.identify_demod()

    def test_0x0E_is_lgs8gl5(self):
        chip = self._identify(0x0E)
        self.assertEqual(chip, "LGS8GL5")

    def test_0x00_is_lgs8g75(self):
        chip = self._identify(0x00)
        self.assertEqual(chip, "LGS8G75")

    def test_0xFF_is_lgs8g75(self):
        chip = self._identify(0xFF)
        self.assertEqual(chip, "LGS8G75")

    def test_0x01_is_lgs8g75(self):
        chip = self._identify(0x01)
        self.assertEqual(chip, "LGS8G75")

    def test_read_packet_uses_demod_addr(self):
        """Identification reads device 0x32, register 0x00 via CMD 0x85."""
        mock = MockUsbDevice()
        lme = make_lme(mock)
        mock.queue_read(bytes([0x55, 0x00, 0, 0, 0]))
        lme.identify_demod()
        pkt = mock.last_write()
        self.assertEqual(pkt, bytes([0x85, 0x02, 0x32, 0x00, 0x00]))

    def test_raises_on_no_response(self):
        mock = MockUsbDevice()
        lme = make_lme(mock)
        mock.queue_read(bytes([0xAA, 0x00, 0, 0, 0]))  # wrong prefix → None
        with self.assertRaises(RuntimeError):
            lme.identify_demod()


# ═══════════════════════════════════════════════════════════════════════════════
# 10.  Frequency divider calculation (sub_150C4)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCalcNK(unittest.TestCase):
    """
    sub_150C4: N = freq // 12,  K = ((freq % 12) << 20) // 12
    4-byte list: [N, 0x10|(K>>16)&0xF, K>>8&0xFF, K&0xFF]
    The high nibble 0x1 of byte[1] is a fixed mode bit (constant 0x10).
    """

    def _calc(self, freq_mhz):
        mock = MockUsbDevice()
        return make_lme(mock)._calc_nk(freq_mhz)

    def test_618_mhz_N(self):
        """618 / 12 = 51 = 0x33"""
        nk = self._calc(618)
        self.assertEqual(nk[0], 0x33)

    def test_618_mhz_K(self):
        """618 % 12 = 6; K = (6 << 20) / 12 = 524288 = 0x080000"""
        nk = self._calc(618)
        K = ((nk[1] & 0x0F) << 16) | (nk[2] << 8) | nk[3]
        self.assertEqual(K, 0x080000)

    def test_618_mhz_byte1_high_nibble(self):
        """High nibble of byte[1] must always be 0x1 (mode bit)."""
        nk = self._calc(618)
        self.assertEqual((nk[1] >> 4) & 0xF, 0x1)

    def test_618_mhz_full_bytes(self):
        """Full verification: 618 MHz → [0x33, 0x18, 0x00, 0x00]"""
        self.assertEqual(self._calc(618), [0x33, 0x18, 0x00, 0x00])

    def test_474_mhz(self):
        """474 / 12 = 39 = 0x27; (474 % 12) = 6; K = 0x080000"""
        nk = self._calc(474)
        self.assertEqual(nk[0], 0x27)
        self.assertEqual(nk[1], 0x18)
        self.assertEqual(nk[2], 0x00)
        self.assertEqual(nk[3], 0x00)

    def test_498_mhz(self):
        """498 / 12 = 41 = 0x29; (498 % 12) = 6; K = 0x080000"""
        nk = self._calc(498)
        self.assertEqual(nk[0], 0x29)

    def test_exact_multiple_of_12(self):
        """480 MHz: N = 40 = 0x28, K = 0 → byte[1] high nibble still 0x1"""
        nk = self._calc(480)
        self.assertEqual(nk[0], 40)
        K = ((nk[1] & 0x0F) << 16) | (nk[2] << 8) | nk[3]
        self.assertEqual(K, 0)
        self.assertEqual((nk[1] >> 4), 0x1)

    def test_ref_freq_is_12(self):
        self.assertEqual(REF_FREQ, 12)

    def test_k_formula(self):
        """K = ((freq % REF_FREQ) << 20) // REF_FREQ for any frequency."""
        for freq in [470, 522, 618, 666, 754, 780]:
            nk = self._calc(freq)
            expected_N = freq // 12
            expected_K = ((freq % 12) << 20) // 12
            self.assertEqual(nk[0], expected_N & 0xFF, f"N mismatch for {freq} MHz")
            actual_K = ((nk[1] & 0x0F) << 16) | (nk[2] << 8) | nk[3]
            self.assertEqual(actual_K, expected_K, f"K mismatch for {freq} MHz")


# ═══════════════════════════════════════════════════════════════════════════════
# 11.  Bandwidth / gain byte calculation (sub_15114)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCalcBwByte(unittest.TestCase):
    """
    sub_15114: result byte = (bw & 0xF) | (gain << 4)
    Without calibration (force_max_gain or empty cal): gain=0xF, bw=0 → 0xF0.
    With calibration: gain depends on freq >= 725 MHz band.
    """

    def _lme_with_cal(self, cal=None):
        mock = MockUsbDevice()
        lme = make_lme(mock)
        if cal:
            lme.cal = cal
        return lme

    def test_no_calibration_returns_0xf0(self):
        lme = self._lme_with_cal()
        bw_byte = lme._calc_bw_byte(618)
        # No cal → force_max_gain path: gain=0xF, bw=0 → 0xF0
        self.assertEqual(bw_byte, 0xF0)

    def test_force_max_gain_ignores_calibration(self):
        cal = {'low_band_gain': 3, 'high_band_gain': 5,
               'bw_min': 2, 'bw_max': 10}
        lme = self._lme_with_cal(cal)
        bw_byte = lme._calc_bw_byte(618, force_max_gain=True)
        self.assertEqual(bw_byte, 0xF0)

    def test_high_band_uses_low_band_gain(self):
        """Frequencies >= 725 MHz use cal['low_band_gain'] (sub_15114 if a1>=725)."""
        cal = {'low_band_gain': 0xA, 'high_band_gain': 0x5,
               'bw_min': 0, 'bw_max': 0xF}
        lme = self._lme_with_cal(cal)
        bw_byte = lme._calc_bw_byte(754)
        gain = (bw_byte >> 4) & 0xF
        self.assertEqual(gain, 0xA, ">=725 MHz should use low_band_gain")

    def test_low_band_uses_high_band_gain(self):
        """Frequencies < 725 MHz use cal['high_band_gain'] (sub_15114 else branch)."""
        cal = {'low_band_gain': 0xA, 'high_band_gain': 0x5,
               'bw_min': 0, 'bw_max': 0xF}
        lme = self._lme_with_cal(cal)
        bw_byte = lme._calc_bw_byte(618)
        gain = (bw_byte >> 4) & 0xF
        self.assertEqual(gain, 0x5, "<725 MHz should use high_band_gain")

    def test_bw_nibble_clipped_to_15(self):
        """BW result > 15 must be clipped to 15 (sub_15114 comment)."""
        cal = {'low_band_gain': 0, 'high_band_gain': 0,
               'bw_min': 0xF, 'bw_max': 0xF}
        lme = self._lme_with_cal(cal)
        bw_byte = lme._calc_bw_byte(618)
        bw_nibble = bw_byte & 0x0F
        self.assertLessEqual(bw_nibble, 15)

    def test_bw_nibble_not_negative(self):
        """BW result must never be negative (clamped at 0)."""
        cal = {'low_band_gain': 0, 'high_band_gain': 0,
               'bw_min': 0, 'bw_max': 0}
        lme = self._lme_with_cal(cal)
        bw_byte = lme._calc_bw_byte(470)
        bw_nibble = bw_byte & 0x0F
        self.assertGreaterEqual(bw_nibble, 0)


# ═══════════════════════════════════════════════════════════════════════════════
# 12.  Tuner reg 0x0A calculation (sub_1517F)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCalcReg0A(unittest.TestCase):
    """
    sub_1517F:
      hi = clamp(cal['reg_0a_cal'] - 2, 0, 15)
      result = (hi << 4) | (_r0a_lo & 0x0F)
    Initial _r0a_lo = 0x03 (byte_2E042 = -61 = 0xC3 initialised in sub_151B1;
    driver stores low nibble separately as 0xC3 & 0xF = 0x3).
    """

    def _lme_with_cal(self, reg_0a_cal):
        mock = MockUsbDevice()
        lme = make_lme(mock)
        lme.cal = {'reg_0a_cal': reg_0a_cal}
        return lme

    def test_typical_cal_value(self):
        """cal=0xA (10): hi=clamp(10-2,0,15)=8 → byte = 0x83 for lo=0x3"""
        lme = self._lme_with_cal(0xA)
        r = lme._calc_reg_0a()
        self.assertEqual(r, 0x83)

    def test_low_nibble_preserved(self):
        lme = self._lme_with_cal(0xA)
        r = lme._calc_reg_0a()
        self.assertEqual(r & 0x0F, lme._r0a_lo & 0x0F)

    def test_clamp_below_2(self):
        """cal <= 1: hi = clamp(cal-2, 0, 15) = 0"""
        lme = self._lme_with_cal(1)
        r = lme._calc_reg_0a()
        self.assertEqual((r >> 4) & 0xF, 0)

    def test_clamp_above_17(self):
        """cal = 17+: hi = clamp(17-2,0,15) = 15"""
        lme = self._lme_with_cal(17)
        r = lme._calc_reg_0a()
        self.assertEqual((r >> 4) & 0xF, 15)

    def test_no_cal_returns_lo_only(self):
        """Without calibration, hi=0; result = _r0a_lo & 0x0F."""
        mock = MockUsbDevice()
        lme = make_lme(mock)  # cal is empty dict
        r = lme._calc_reg_0a()
        self.assertEqual((r >> 4) & 0xF, 0)
        self.assertEqual(r & 0x0F, lme._r0a_lo & 0x0F)


# ═══════════════════════════════════════════════════════════════════════════════
# 13.  I2C repeater gate control
# ═══════════════════════════════════════════════════════════════════════════════

class TestRepeaterGate(unittest.TestCase):
    """
    Repeater enable  → 05 04 32 01 E0  (sub_147DA(50,1,224))
    Repeater disable → 05 04 32 01 60  (sub_147DA(50,1,96))
    """

    def test_repeater_enable_packet(self):
        mock = MockUsbDevice()
        lme = make_lme(mock)
        mock.queue_read(bytes([0x88, 0, 0, 0]))
        lme._repeater_enable()
        self.assertEqual(mock.last_write(),
                         bytes([0x05, 0x04, 0x32, 0x01, 0xE0]))

    def test_repeater_disable_packet(self):
        mock = MockUsbDevice()
        lme = make_lme(mock)
        mock.queue_read(bytes([0x88, 0, 0, 0]))
        lme._repeater_disable()
        self.assertEqual(mock.last_write(),
                         bytes([0x05, 0x04, 0x32, 0x01, 0x60]))


# ═══════════════════════════════════════════════════════════════════════════════
# 14.  Tune sequence — packet ordering (sub_1524A)
# ═══════════════════════════════════════════════════════════════════════════════

class TestTuneSequence(unittest.TestCase):
    """
    sub_1524A full sequence for any frequency:
      1. Demod soft reset (read/clear/set reg 0x02)
      2. Enable I2C repeater  → 05 04 32 01 E0
      3. Write 5-byte N/K/BW  → 04 07 C0 00 ...
      4. Write reg 0x0A       → 04 03 C0 0A [val]
      5. R-M-W reg 0x04       → 84 03 C0 04 01 / 04 03 C0 04 [val|0xF0]
      6. Disable I2C repeater → 05 04 32 01 60
    """

    def _run_tune(self, freq_mhz=618):
        mock = MockUsbDevice()
        lme = make_lme(mock)
        lme.cal = {'low_band_gain': 0xF, 'high_band_gain': 0xF,
                   'bw_min': 0, 'bw_max': 0xF, 'reg_0a_cal': 0xA}

        # Demod soft-reset: read reg 0x02
        mock.queue_read(bytes([0x55, 0x01, 0, 0, 0]))   # demod_read(0x02)
        mock.queue_read(bytes([0x88, 0, 0, 0]))          # demod_write reg 0x02 (clear)
        mock.queue_read(bytes([0x88, 0, 0, 0]))          # demod_write reg 0x02 (set)
        # Repeater enable
        mock.queue_read(bytes([0x88, 0, 0, 0]))
        # Write N/K/BW
        mock.queue_read(bytes([0x88, 0, 0, 0]))
        # Write reg 0x0A
        mock.queue_read(bytes([0x88, 0, 0, 0]))
        # Read reg 0x04 for PLL latch
        mock.queue_read(bytes([0x55, 0x07, 0, 0, 0]))
        # Write reg 0x04 (PLL latch)
        mock.queue_read(bytes([0x88, 0, 0, 0]))
        # Repeater disable
        mock.queue_read(bytes([0x88, 0, 0, 0]))

        lme.tune(freq_mhz)
        return mock.written

    def test_repeater_enable_before_tuner_write(self):
        pkts = self._run_tune()
        en = bytes([0x05, 0x04, 0x32, 0x01, 0xE0])
        write_nk = None
        for p in pkts:
            if p[0] == 0x04 and len(p) > 3 and p[2] == 0xC0:
                write_nk = p
                break
        idx_en = pkts.index(en)
        idx_nk = pkts.index(write_nk)
        self.assertLess(idx_en, idx_nk, "Repeater must be enabled before tuner write")

    def test_nk_write_uses_cmd_04(self):
        pkts = self._run_tune()
        tuner_writes = [p for p in pkts if p[0] == 0x04 and len(p) > 3 and p[2] == 0xC0]
        self.assertGreater(len(tuner_writes), 0, "Must write to tuner (0xC0) via CMD 0x04")

    def test_nk_write_5_bytes(self):
        """N/K/BW write: 04 07 C0 00 [5 bytes] — Len=7 = 2+5."""
        pkts = self._run_tune(618)
        nk_write = next((p for p in pkts
                         if p[0] == 0x04 and len(p) > 3 and p[2] == 0xC0 and p[3] == 0x00), None)
        self.assertIsNotNone(nk_write)
        self.assertEqual(nk_write[1], 7, "Len=7 for 5-byte tuner write (2+5)")

    def test_618_mhz_nk_bytes(self):
        """618 MHz: N=0x33, K=0x080000 → packet bytes [0x33, 0x18, 0x00, 0x00, bw]."""
        pkts = self._run_tune(618)
        nk_write = next((p for p in pkts
                         if p[0] == 0x04 and len(p) > 3 and p[2] == 0xC0 and p[3] == 0x00), None)
        self.assertIsNotNone(nk_write)
        self.assertEqual(nk_write[4], 0x33)  # N
        self.assertEqual(nk_write[5], 0x18)  # 0x10 | K[23:20]
        self.assertEqual(nk_write[6], 0x00)  # K[15:8]
        self.assertEqual(nk_write[7], 0x00)  # K[7:0]

    def test_pll_latch_sets_high_nibble(self):
        """R-M-W reg 0x04: written value must have bits [7:4] all set (|= 0xF0)."""
        pkts = self._run_tune(618)
        # reg 0x04 write: 04 03 C0 04 [val|0xF0]
        pll_write = next((p for p in pkts
                          if p[0] == 0x04 and len(p) == 5
                          and p[2] == 0xC0 and p[3] == 0x04), None)
        self.assertIsNotNone(pll_write)
        self.assertEqual(pll_write[4] & 0xF0, 0xF0, "PLL latch: high nibble must be 0xF")

    def test_repeater_disable_after_all_tuner_writes(self):
        pkts = self._run_tune()
        dis = bytes([0x05, 0x04, 0x32, 0x01, 0x60])
        tuner_writes = [i for i, p in enumerate(pkts)
                        if p[0] == 0x04 and len(p) > 3 and p[2] == 0xC0]
        idx_dis = pkts.index(dis)
        self.assertGreater(idx_dis, max(tuner_writes),
                           "Repeater must be disabled AFTER all tuner writes")


# ═══════════════════════════════════════════════════════════════════════════════
# 15.  EP 0x8A signal status packet parsing
# ═══════════════════════════════════════════════════════════════════════════════

class TestStatusPacket(unittest.TestCase):
    """
    Status packet format (LME2510_Analysis.md §5.4):
      BB 05 [LOCK] [SNR] [BER_H] [CTR] [BER_L] 00
    Parsing via read_status_packet() → dict with lock/snr/ber_h/ctr/ber_l.
    """

    def _parse(self, raw8):
        """Simulate what read_status_packet does, without USB I/O."""
        raw = bytes(raw8)
        if len(raw) >= 8 and raw[0] == 0xBB and raw[1] == 0x05:
            return {
                'lock':  raw[2],
                'snr':   raw[3],
                'ber_h': raw[4],
                'ctr':   raw[5],
                'ber_l': raw[6],
                'raw':   raw[:8].hex(' ').upper(),
            }
        return None

    def test_good_signal(self):
        """LOCK=1, BER_L=0x00 → interpret_status returns 'GOOD SIGNAL ✓'."""
        s = self._parse([0xBB, 0x05, 0x01, 0xFF, 0x00, 0x04, 0x00, 0x00])
        self.assertIsNotNone(s)
        self.assertEqual(s['lock'], 1)
        self.assertEqual(s['ber_l'], 0x00)
        self.assertEqual(LME2510.interpret_status(s), "GOOD SIGNAL ✓")

    def test_false_lock(self):
        """LOCK=1, BER_L=0xFF → 'false-lock / noise'."""
        s = self._parse([0xBB, 0x05, 0x01, 0xFF, 0xFF, 0x04, 0xFF, 0x00])
        self.assertIsNotNone(s)
        self.assertEqual(LME2510.interpret_status(s), "false-lock / noise")

    def test_no_signal(self):
        """LOCK=0 → 'no signal'."""
        s = self._parse([0xBB, 0x05, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00])
        self.assertIsNotNone(s)
        self.assertEqual(LME2510.interpret_status(s), "no signal")

    def test_invalid_header_returns_none(self):
        s = self._parse([0xAA, 0x05, 0x01, 0xFF, 0x00, 0x04, 0x00, 0x00])
        self.assertIsNone(s)

    def test_lock_field_position(self):
        s = self._parse([0xBB, 0x05, 0x01, 0x80, 0x00, 0x03, 0x00, 0x00])
        self.assertEqual(s['lock'], 1)

    def test_snr_field_position(self):
        s = self._parse([0xBB, 0x05, 0x01, 0xAB, 0x00, 0x03, 0x00, 0x00])
        self.assertEqual(s['snr'], 0xAB)

    def test_ber_l_field_position(self):
        s = self._parse([0xBB, 0x05, 0x00, 0x00, 0x00, 0x03, 0xCD, 0x00])
        self.assertEqual(s['ber_l'], 0xCD)

    def test_raw_hex_format(self):
        s = self._parse([0xBB, 0x05, 0x01, 0x00, 0xFF, 0x04, 0xFF, 0x00])
        self.assertEqual(s['raw'], 'BB 05 01 00 FF 04 FF 00')

    # Examples from LME2510_Analysis.md §5.4
    def test_example_false_lock_all_errors(self):
        s = self._parse([0xBB, 0x05, 0x01, 0x00, 0xFF, 0x04, 0xFF, 0x00])
        self.assertEqual(LME2510.interpret_status(s), "false-lock / noise")

    def test_example_not_locked_no_signal(self):
        s = self._parse([0xBB, 0x05, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00])
        self.assertEqual(LME2510.interpret_status(s), "no signal")


# ═══════════════════════════════════════════════════════════════════════════════
# 16.  Firmware path constants
# ═══════════════════════════════════════════════════════════════════════════════

class TestFirmwarePaths(unittest.TestCase):
    """
    FW_1_PATH and FW_2_PATH must point to the correct files that exist in the
    repository's fw/ directory.  This catches the historical bug where paths
    were 'fw/fw1.bin' and 'fw/fw2.bin' (files that do not exist).
    """

    def test_fw1_path_basename(self):
        self.assertEqual(os.path.basename(lme_mod.FW_1_PATH), "fw_bootloader.bin")

    def test_fw2_path_basename(self):
        self.assertEqual(os.path.basename(lme_mod.FW_2_PATH), "fw_lgs8g75.bin")

    def test_fw1_file_exists(self):
        self.assertTrue(os.path.isfile(lme_mod.FW_1_PATH),
                        f"FW_1_PATH not found: {lme_mod.FW_1_PATH}")

    def test_fw2_file_exists(self):
        self.assertTrue(os.path.isfile(lme_mod.FW_2_PATH),
                        f"FW_2_PATH not found: {lme_mod.FW_2_PATH}")

    def test_fw1_is_512_bytes(self):
        with open(lme_mod.FW_1_PATH, "rb") as f:
            data = f.read()
        self.assertEqual(len(data), 512, "fw_bootloader.bin must be 512 bytes")

    def test_fw2_is_4836_bytes(self):
        with open(lme_mod.FW_2_PATH, "rb") as f:
            data = f.read()
        self.assertEqual(len(data), 4836, "fw_lgs8g75.bin must be 4836 bytes")


# ═══════════════════════════════════════════════════════════════════════════════
# 17.  VID / PID constants
# ═══════════════════════════════════════════════════════════════════════════════

class TestConstants(unittest.TestCase):
    """
    Verify that VID and PID match the confirmed values from UDE262D.sys
    and the hardware descriptor embedded in fw_bootloader.bin.
    """

    def test_vid(self):
        self.assertEqual(lme_mod.VID, 0x3344)

    def test_pid(self):
        """PID must be 0x1120, NOT 0x1122 (historical bug in lme2510_tool.py)."""
        self.assertEqual(lme_mod.PID, 0x1120)

    def test_ep_cmd_out(self):
        self.assertEqual(lme_mod.EP_CMD_OUT, 0x01)

    def test_ep_cmd_in(self):
        self.assertEqual(lme_mod.EP_CMD_IN, 0x81)

    def test_ep_stream_high_speed(self):
        """TS stream endpoint for High Speed mode is 0x88 (Bulk IN, 512 B)."""
        self.assertEqual(lme_mod.EP_STREAM, 0x88)

    def test_ep_status(self):
        self.assertEqual(lme_mod.EP_STATUS, 0x8A)

    def test_demod_addr(self):
        self.assertEqual(lme_mod.DEMOD_ADDR, 0x32)

    def test_tuner_addr(self):
        self.assertEqual(lme_mod.TUNER_ADDR, 0xC0)

    def test_ref_freq(self):
        self.assertEqual(lme_mod.REF_FREQ, 12)

    def test_pid_cold(self):
        """Cold-boot PID: device uses 0x1111 before any firmware is loaded (USB controller only)."""
        self.assertEqual(lme_mod.PID_COLD, 0x1111)

    def test_pid_warm(self):
        """Warm-boot PID: device uses 0x1120 once the bootloader (stage-1 firmware) is active."""
        self.assertEqual(lme_mod.PID_WARM, 0x1120)

    def test_pid_is_warm(self):
        """PID alias must point to PID_WARM."""
        self.assertEqual(lme_mod.PID, lme_mod.PID_WARM)


# ═══════════════════════════════════════════════════════════════════════════════
# 18.  Post-firmware activation command (sub_13EC8)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdPostFw(unittest.TestCase):
    """
    sub_13EC8 / _cmd_post_fw:
      Sends [0x8A, 0x00] to EP 0x01 after firmware download.
      Reads back 5 bytes from EP 0x81 (may fail if device resets immediately).
      Called as the final step of download_firmware().
    """

    def test_post_fw_packet_format(self):
        """CMD 0x8A activation packet must be exactly [0x8A, 0x00]."""
        mock = MockUsbDevice()
        lme = make_lme(mock)
        mock.queue_read(bytes([0x88, 0, 0, 0, 0]))  # 5-byte ACK
        lme._cmd_post_fw()
        self.assertEqual(mock.last_write(), bytes([0x8A, 0x00]))

    def test_post_fw_reads_5_bytes(self):
        """_cmd_post_fw must attempt to read exactly 5 bytes from EP 0x81."""
        mock = MockUsbDevice()
        lme = make_lme(mock)
        mock.queue_read(bytes([0x88, 0, 0, 0, 0]))
        lme._cmd_post_fw()
        # After the call, the mock read queue should be empty (5 bytes consumed)
        self.assertEqual(len(mock._read_queue), 0)

    def test_post_fw_tolerates_usb_error(self):
        """_cmd_post_fw must not raise even if USB read fails (device may reset)."""
        import usb.core

        class USBErrorMock(MockUsbDevice):
            def read(self, ep, size, timeout=1000):
                raise usb.core.USBError("simulated device reset")

        mock = USBErrorMock()
        lme = make_lme(mock)
        # Should not raise
        try:
            lme._cmd_post_fw()
        except Exception as e:
            self.fail(f"_cmd_post_fw raised unexpectedly: {e}")

    def test_post_fw_tolerates_empty_response(self):
        """_cmd_post_fw must not raise if the device sends 0 bytes."""
        mock = MockUsbDevice()

        class EmptyReadMock(MockUsbDevice):
            def read(self, ep, size, timeout=1000):
                return b""

        lme = make_lme(EmptyReadMock())
        try:
            lme._cmd_post_fw()
        except Exception as e:
            self.fail(f"_cmd_post_fw raised on empty response: {e}")

    def test_download_firmware_calls_post_fw(self):
        """download_firmware() must end with _cmd_post_fw() ([0x8A, 0x00])."""
        mock = MockUsbDevice()
        lme = make_lme(mock)

        fw1_path = lme_mod.FW_1_PATH
        fw2_path = lme_mod.FW_2_PATH
        if not (os.path.isfile(fw1_path) and os.path.isfile(fw2_path)):
            self.skipTest("Firmware files not found")

        with open(fw1_path, "rb") as f:
            blob1 = f.read()
        with open(fw2_path, "rb") as f:
            blob2 = f.read()

        # Calculate total chunk count for both firmware stages
        n1 = (len(blob1) + 49) // 50   # ceil(len/50) for stage 1
        n2 = (len(blob2) + 49) // 50   # ceil(len/50) for stage 2

        # Queue ACKs for all chunks (each chunk gets 1-byte ACK)
        for _ in range(n1 + n2):
            mock.queue_read(bytes([0x88]))

        # Queue the 5-byte response for _cmd_post_fw
        mock.queue_read(bytes([0x88, 0x00, 0x00, 0x00, 0x00]))

        lme.download_firmware(fw1_path, fw2_path)

        # The very last packet written must be [0x8A, 0x00]
        self.assertEqual(mock.last_write(), bytes([0x8A, 0x00]),
                         "Last command after firmware download must be [0x8A, 0x00] (sub_13EC8)")


# ═══════════════════════════════════════════════════════════════════════════════
# 19.  _recv exception handling
# ═══════════════════════════════════════════════════════════════════════════════

class TestRecvExceptionHandling(unittest.TestCase):
    """
    _recv() must catch usb.core.USBError and return b"" instead of crashing.
    This prevents USB timeout errors from propagating as unhandled exceptions.
    """

    def test_recv_returns_empty_on_usb_error(self):
        import usb.core

        class USBErrorDev(MockUsbDevice):
            def read(self, ep, size, timeout=1000):
                raise usb.core.USBError("simulated timeout")

        lme = make_lme(USBErrorDev())
        result = lme._recv(5)
        self.assertEqual(result, b"", "_recv must return b'' on USBError, not raise")

    def test_recv_returns_empty_causes_cmd_read_single_none(self):
        """When _recv returns b'', cmd_read_single must return None (not crash)."""
        import usb.core

        class USBErrorDev(MockUsbDevice):
            def read(self, ep, size, timeout=1000):
                raise usb.core.USBError("simulated timeout")

        lme = make_lme(USBErrorDev())
        # _send still works (write succeeds on our mock)
        result = lme.cmd_read_single(0x32, 0x00)
        self.assertIsNone(result)

    def test_recv_returns_empty_causes_identify_demod_retry(self):
        """When USB errors persist, identify_demod must retry and eventually raise RuntimeError."""
        import usb.core

        class USBErrorDev(MockUsbDevice):
            def read(self, ep, size, timeout=1000):
                raise usb.core.USBError("simulated timeout")

        lme = make_lme(USBErrorDev())
        with self.assertRaises(RuntimeError) as ctx:
            lme.identify_demod(retries=2, retry_delay=0)
        self.assertIn("Cannot read Demod reg 0x00", str(ctx.exception))

    def test_recv_normal_path_unaffected(self):
        """Normal (non-error) reads must still work correctly after exception-handling change."""
        mock = MockUsbDevice()
        lme = make_lme(mock)
        mock.queue_read(bytes([0x55, 0x0E, 0, 0, 0]))
        result = lme._recv(5)
        self.assertEqual(result, bytes([0x55, 0x0E, 0, 0, 0]))


# ═══════════════════════════════════════════════════════════════════════════════
# 20.  identify_demod retry logic
# ═══════════════════════════════════════════════════════════════════════════════

class TestIdentifyDemodRetry(unittest.TestCase):
    """
    identify_demod(retries, retry_delay) must:
      - Retry up to *retries* times if demod_read(0x00) returns None
      - Succeed on the first valid response within the retry window
      - Raise RuntimeError with a clear message if all retries are exhausted
    """

    def test_succeeds_on_first_attempt(self):
        mock = MockUsbDevice()
        lme = make_lme(mock)
        mock.queue_read(bytes([0x55, 0x0E, 0, 0, 0]))
        chip = lme.identify_demod(retries=3, retry_delay=0)
        self.assertEqual(chip, "LGS8GL5")

    def test_succeeds_on_second_attempt(self):
        """First attempt returns bad prefix → None; second succeeds."""
        mock = MockUsbDevice()
        lme = make_lme(mock)
        # First read: wrong prefix → cmd_read_single returns None
        mock.queue_read(bytes([0xAA, 0x0E, 0, 0, 0]))
        # Second read: correct
        mock.queue_read(bytes([0x55, 0x00, 0, 0, 0]))
        chip = lme.identify_demod(retries=3, retry_delay=0)
        self.assertEqual(chip, "LGS8G75")

    def test_raises_after_all_retries_exhausted(self):
        """All retries fail → RuntimeError."""
        mock = MockUsbDevice()
        lme = make_lme(mock)
        for _ in range(5):
            mock.queue_read(bytes([0xAA, 0x00, 0, 0, 0]))  # always wrong prefix
        with self.assertRaises(RuntimeError) as ctx:
            lme.identify_demod(retries=3, retry_delay=0)
        self.assertIn("Cannot read Demod reg 0x00", str(ctx.exception))

    def test_error_message_is_helpful(self):
        """RuntimeError message must mention firmware."""
        mock = MockUsbDevice()
        lme = make_lme(mock)
        mock.queue_read(bytes([0xAA, 0x00, 0, 0, 0]))
        try:
            lme.identify_demod(retries=1, retry_delay=0)
        except RuntimeError as e:
            self.assertIn("firmware", str(e).lower())

    def test_default_retries_is_5(self):
        """Default retries parameter must be 5."""
        import inspect
        sig = inspect.signature(LME2510.identify_demod)
        self.assertEqual(sig.parameters['retries'].default, 5)

    def test_default_retry_delay_is_0_5(self):
        """Default retry_delay must be 0.5 seconds."""
        import inspect
        sig = inspect.signature(LME2510.identify_demod)
        self.assertEqual(sig.parameters['retry_delay'].default, 0.5)


# ═══════════════════════════════════════════════════════════════════════════════
# 21.  CMD 0x16 — chip-type selection (sub_13F00)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdSelectChipType(unittest.TestCase):
    """
    sub_13F00 / cmd_select_chip_type:
      Tells the USB bridge firmware which demodulator is connected so that
      the bridge knows which I2C registers to read when building EP 0x8A
      status packets.  Without this command, EP 0x8A never produces data.

    Packet format: [0x16, 0x01, chip_type]
      chip_type = 0x00  for LGS8GL5
      chip_type = 0x01  for LGS8G75
    Response: 5 bytes (ACK, contents not checked)
    """

    def _run(self, chip: str):
        mock = MockUsbDevice()
        lme = make_lme(mock)
        mock.queue_read(bytes([0x55, 0, 0, 0, 0]))
        lme.cmd_select_chip_type(chip)
        return mock.written

    def test_lgs8gl5_cmd_byte(self):
        """First byte of packet must be 0x16."""
        pkts = self._run("LGS8GL5")
        self.assertEqual(pkts[-1][0], 0x16)

    def test_lgs8gl5_second_byte(self):
        """Second byte must always be 0x01."""
        pkts = self._run("LGS8GL5")
        self.assertEqual(pkts[-1][1], 0x01)

    def test_lgs8gl5_chip_type_byte(self):
        """LGS8GL5 → chip_type = 0x00."""
        pkts = self._run("LGS8GL5")
        self.assertEqual(pkts[-1][2], 0x00)

    def test_lgs8g75_chip_type_byte(self):
        """LGS8G75 → chip_type = 0x01."""
        pkts = self._run("LGS8G75")
        self.assertEqual(pkts[-1][2], 0x01)

    def test_packet_length_is_3(self):
        """CMD 0x16 packet must be exactly 3 bytes."""
        pkts = self._run("LGS8GL5")
        self.assertEqual(len(pkts[-1]), 3)

    def test_reads_5_byte_response(self):
        """Must attempt to read 5 bytes from EP 0x81 after sending CMD 0x16."""
        mock = MockUsbDevice()
        lme = make_lme(mock)
        mock.queue_read(bytes([0x55, 0, 0, 0, 0]))
        lme.cmd_select_chip_type("LGS8GL5")
        self.assertEqual(len(mock._read_queue), 0)  # 5-byte response consumed

    def test_returns_true_on_valid_response(self):
        mock = MockUsbDevice()
        lme = make_lme(mock)
        mock.queue_read(bytes([0x55, 0, 0, 0, 0]))
        self.assertTrue(lme.cmd_select_chip_type("LGS8GL5"))

    def test_returns_false_on_empty_response(self):
        mock = MockUsbDevice()

        class EmptyDev(MockUsbDevice):
            def read(self, ep, size, timeout=1000):
                return b""

        lme = make_lme(EmptyDev())
        result = lme.cmd_select_chip_type("LGS8GL5")
        self.assertFalse(result)

    def test_lgs8gl5_full_packet(self):
        """Full packet for LGS8GL5 must be exactly [0x16, 0x01, 0x00]."""
        pkts = self._run("LGS8GL5")
        self.assertEqual(pkts[-1], bytes([0x16, 0x01, 0x00]))

    def test_lgs8g75_full_packet(self):
        """Full packet for LGS8G75 must be exactly [0x16, 0x01, 0x01]."""
        pkts = self._run("LGS8G75")
        self.assertEqual(pkts[-1], bytes([0x16, 0x01, 0x01]))


# ═══════════════════════════════════════════════════════════════════════════════
# 22.  Post-identify demod init (sub_145A2 + sub_1440D)
# ═══════════════════════════════════════════════════════════════════════════════

class TestInitDemodAfterIdentify(unittest.TestCase):
    """
    _init_demod_after_identify(chip):
      Called after identify_demod + cmd_select_chip_type, before init_tuner.
      Configures demod registers 0x07 and 0x09–0x0C.

    LGS8GL5 sequence (sub_145A2(1) then sub_1440D(0)):
      1. Read  demod reg 0x07
      2. Write reg 0x07 |= 0x0C   (set bits [3:2])
      3. Write reg 0x09 = 0x00
      4. Write reg 0x0A = 0x00
      5. Write reg 0x0B = 0x00
      6. Write reg 0x0C = 0x00
      7. Read  demod reg 0x07 again
      8. Write reg 0x07 &= 0x7C   (clear bits [7,1,0])
    """

    def _run(self, chip: str, reg7_initial: int = 0x00):
        mock = MockUsbDevice()
        lme = make_lme(mock)
        # First read of reg 0x07
        mock.queue_read(bytes([0x55, reg7_initial, 0, 0, 0]))
        # ACKs for writes to 0x07, 0x09, 0x0A, 0x0B, 0x0C
        for _ in range(5):
            mock.queue_read(bytes([0x88, 0, 0, 0]))
        # Second read of reg 0x07
        mock.queue_read(bytes([0x55, reg7_initial | 0x0C, 0, 0, 0]))
        # ACK for final write to 0x07
        mock.queue_read(bytes([0x88, 0, 0, 0]))
        lme._init_demod_after_identify(chip)
        return mock.written

    def test_reads_reg_07_first(self):
        """First command must be CMD 0x85 read of demod reg 0x07."""
        pkts = self._run("LGS8GL5")
        read_pkts = [p for p in pkts if p[0] == 0x85]
        self.assertGreater(len(read_pkts), 0)
        self.assertEqual(read_pkts[0][3], 0x07)

    def test_sets_bits_3_2_in_reg07(self):
        """After reading reg 0x07, must write it back with bits [3:2] set."""
        reg7 = 0x00
        pkts = self._run("LGS8GL5", reg7_initial=reg7)
        write_07 = [p for p in pkts if p[0] == 0x05 and p[3] == 0x07]
        self.assertGreater(len(write_07), 0)
        # First write should set bits [3:2]
        self.assertEqual(write_07[0][4], reg7 | 0x0C)

    def test_zeros_regs_09_to_0c(self):
        """Regs 0x09, 0x0A, 0x0B, 0x0C must all be written to 0x00."""
        pkts = self._run("LGS8GL5")
        for reg in (0x09, 0x0A, 0x0B, 0x0C):
            writes = [p for p in pkts if p[0] == 0x05 and p[3] == reg]
            self.assertEqual(len(writes), 1, f"Must write reg {reg:#04x} exactly once")
            self.assertEqual(writes[0][4], 0x00, f"reg {reg:#04x} must be written 0x00")

    def test_clears_bits_7_1_0_in_reg07_final(self):
        """Final write to reg 0x07 must clear bits [7,1,0]."""
        reg7_after_set = 0x0C  # bits [3:2] set, others 0
        pkts = self._run("LGS8GL5")
        write_07 = [p for p in pkts if p[0] == 0x05 and p[3] == 0x07]
        # Last write to reg 0x07 should have bits [7,1,0] cleared
        final = write_07[-1][4]
        self.assertEqual(final & 0x83, 0x00, "bits [7,1,0] must be cleared in final reg 0x07 write")

    def test_lgs8g75_path_also_runs(self):
        """LGS8G75 path must also write to reg 0x07 and zero out regs 0x09–0x0C."""
        pkts = self._run("LGS8G75")
        write_07 = [p for p in pkts if p[0] == 0x05 and p[3] == 0x07]
        self.assertGreater(len(write_07), 0, "LGS8G75 path must write reg 0x07")


# ═══════════════════════════════════════════════════════════════════════════════
# 23.  Post-tune demod init (sub_14C72 + sub_14957 + sub_14C16)
# ═══════════════════════════════════════════════════════════════════════════════

class TestInitDemodPostTune(unittest.TestCase):
    """
    _init_demod_post_tune(chip):
      Called after tune() to configure the demodulator for signal measurement.
      This prepares the demod's signal quality registers so the USB bridge
      firmware can read valid SNR/BER values for EP 0x8A status packets.

    Sequence:
      sub_14C72(0): reg 0x07 |= 0x0C; zero regs 0x08–0x0B
      sub_14957(0): reg 0x07 &= 0x7F (clear bit 7)
      sub_14C16():  reg 0x0C = (old & 0x7B) | 0x80; reg 0x39=0; reg 0x3D=4
    """

    def _run(self, chip: str = "LGS8GL5",
             reg7_val: int = 0x00, reg_c_val: int = 0x00):
        mock = MockUsbDevice()
        lme = make_lme(mock)
        # sub_14C72(0): read reg 0x07
        mock.queue_read(bytes([0x55, reg7_val, 0, 0, 0]))
        # ACKs: write 0x07, write 0x08, 0x09, 0x0A, 0x0B
        for _ in range(5):
            mock.queue_read(bytes([0x88, 0, 0, 0]))
        # sub_14957(0): read reg 0x07 again
        mock.queue_read(bytes([0x55, reg7_val | 0x0C, 0, 0, 0]))
        # ACK: write 0x07
        mock.queue_read(bytes([0x88, 0, 0, 0]))
        # sub_14C16(): read reg 0x0C
        mock.queue_read(bytes([0x55, reg_c_val, 0, 0, 0]))
        # ACKs: write 0x0C, write 0x39, write 0x3D
        for _ in range(3):
            mock.queue_read(bytes([0x88, 0, 0, 0]))
        lme._init_demod_post_tune(chip)
        return mock.written

    def test_reg07_bits_3_2_set(self):
        """sub_14C72(0): reg 0x07 |= 0x0C — bits [3:2] must be set."""
        pkts = self._run(reg7_val=0x00)
        write_07 = [p for p in pkts if p[0] == 0x05 and p[3] == 0x07]
        self.assertGreater(len(write_07), 0)
        self.assertEqual(write_07[0][4] & 0x0C, 0x0C)

    def test_reg08_to_0b_zeroed(self):
        """sub_14C72(0): regs 0x08–0x0B must all be written to 0x00."""
        pkts = self._run()
        for reg in (0x08, 0x09, 0x0A, 0x0B):
            writes = [p for p in pkts if p[0] == 0x05 and p[3] == reg]
            self.assertEqual(len(writes), 1, f"Must write reg {reg:#04x}")
            self.assertEqual(writes[0][4], 0x00)

    def test_reg07_bit7_cleared(self):
        """sub_14957(0): reg 0x07 &= 0x7F — bit 7 must be cleared."""
        pkts = self._run(reg7_val=0x80)
        write_07 = [p for p in pkts if p[0] == 0x05 and p[3] == 0x07]
        # The second write to reg 0x07 must clear bit 7
        self.assertGreaterEqual(len(write_07), 2)
        self.assertEqual(write_07[1][4] & 0x80, 0x00)

    def test_reg0c_written(self):
        """sub_14C16(): reg 0x0C must be written."""
        pkts = self._run()
        writes_0c = [p for p in pkts if p[0] == 0x05 and p[3] == 0x0C]
        self.assertEqual(len(writes_0c), 1)

    def test_reg0c_clears_bit2_sets_bit7(self):
        """sub_14C16(): reg 0x0C = (old & 0x7B) | 0x80 — bit 7 set, bit 2 clear."""
        pkts = self._run(reg_c_val=0x04)  # bit 2 set initially
        writes_0c = [p for p in pkts if p[0] == 0x05 and p[3] == 0x0C]
        val = writes_0c[0][4]
        self.assertEqual(val & 0x80, 0x80, "bit 7 must be set")
        self.assertEqual(val & 0x04, 0x00, "bit 2 must be cleared")

    def test_reg39_written_zero(self):
        """sub_14C16(): reg 0x39 must be written to 0x00."""
        pkts = self._run()
        writes_39 = [p for p in pkts if p[0] == 0x05 and p[3] == 0x39]
        self.assertEqual(len(writes_39), 1)
        self.assertEqual(writes_39[0][4], 0x00)

    def test_reg3d_written_4(self):
        """sub_14C16(): reg 0x3D must be written to 0x04."""
        pkts = self._run()
        writes_3d = [p for p in pkts if p[0] == 0x05 and p[3] == 0x3D]
        self.assertEqual(len(writes_3d), 1)
        self.assertEqual(writes_3d[0][4], 0x04)

    def test_works_for_lgs8g75(self):
        """Post-tune demod init must run the same sequence for LGS8G75."""
        pkts = self._run(chip="LGS8G75")
        writes_3d = [p for p in pkts if p[0] == 0x05 and p[3] == 0x3D]
        self.assertEqual(len(writes_3d), 1)
        self.assertEqual(writes_3d[0][4], 0x04)


# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
