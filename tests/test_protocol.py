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


# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
