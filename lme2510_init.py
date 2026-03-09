"""
lme2510_init.py — LME2510C DTMB USB stick initialization tool
Based on reverse engineering of UDE262D.sys (IDA Pro 9.0)

Usage:
    python lme2510_init.py              # tune to 618 MHz, print status
    python lme2510_init.py --freq 498   # tune to 498 MHz
    python lme2510_init.py --freq 618 --stream  # dump raw TS to stdout

Requires: pyusb  (pip install pyusb)
Windows:  also install Zadig and switch the device to WinUSB/libusb driver
"""

import usb.core
import usb.util
import time
import sys
import os
import argparse

# ─── Constants ────────────────────────────────────────────────────────────────

VID         = 0x3344   # LME2510C USB Vendor ID
PID         = 0x1120   # LME2510C USB Product ID (cold-boot / warm-boot share this VID:PID)

EP_CMD_OUT  = 0x01     # Bulk OUT  64 B  → commands
EP_CMD_IN   = 0x81     # Bulk IN   64 B  ← command responses / ACK
EP_STREAM   = 0x88     # Bulk IN  512 B  ← MPEG-TS (High Speed mode only; EP 0x87 for Full Speed)
EP_STATUS   = 0x8A     # Interrupt IN 64 B ← signal status packets (~128 ms)

DEMOD_ADDR  = 0x32     # LGS8GL5 / LGS8G75 primary I2C address (regs 0x00–0xBF)
DEMOD_HIGH  = 0x36     # LGS8GL5 / LGS8G75 extended bank (regs 0xC0–0xFF, same chip)
TUNER_ADDR  = 0xC0     # MAX2165 I2C address

REF_FREQ    = 12       # MAX2165 reference clock (MHz)

# Firmware paths relative to this script's directory (extracted from UDE262D.sys)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FW_1_PATH   = os.path.join(_SCRIPT_DIR, "fw", "fw_bootloader.bin")   # Firmware stage 1 (USB controller patch / bootloader)
FW_2_PATH   = os.path.join(_SCRIPT_DIR, "fw", "fw_lgs8g75.bin")      # Firmware stage 2 (default: LGS8G75; use fw_lgs8gl5.bin for LGS8GL5)

# Valid firmware-download ACK bytes (driver sub_1392E: byte != -120 AND byte != 119 → error)
# -120 signed == 0x88 unsigned; 119 == 0x77 unsigned
FW_ACK_OK   = {0x88, 0x77}

TIMEOUT_MS  = 1000


# ─── LME2510 device class ─────────────────────────────────────────────────────

class LME2510:
    """
    Wraps all communication with the LME2510C USB bridge.

    Protocol summary (from driver sub_14083 / sub_14106 / sub_1417A / sub_14240):
      Write block : [04][Len=2+n][DevAddr][RegAddr][data*n]  ACK: [88]
      Write single: [05][04][DevAddr][RegAddr][Value]         ACK: [88]
      Read block  : [84][03][DevAddr][RegAddr][n]             Resp: [55][data*n]
      Read single : [85][02][DevAddr][RegAddr][xx]            Resp: [55][value][...]
    """

    def __init__(self, dev):
        self.dev = dev
        self.cal = {}          # Tuner calibration: filled by read_calibration()
        self._r0a_lo = 0x03   # Low nibble of tuner reg 0x0A (initial from driver 0xC3 & 0xF)

    # ── Raw USB I/O ───────────────────────────────────────────────────────────

    def _send(self, data: bytes | list):
        self.dev.write(EP_CMD_OUT, bytes(data), TIMEOUT_MS)

    def _recv(self, n: int) -> bytes:
        return bytes(self.dev.read(EP_CMD_IN, n, TIMEOUT_MS))

    # ── Protocol commands ─────────────────────────────────────────────────────

    def cmd_write_block(self, dev_addr: int, reg_addr: int, data: list) -> bool:
        """
        CMD 0x04 — I2C multi-byte write (sub_14083 / sub_14FA2).
        Packet: [04][Len=2+n][DevAddr][RegAddr][data*n]
        """
        pkt = [0x04, 2 + len(data), dev_addr, reg_addr] + list(data)
        self._send(pkt)
        ack = self._recv(4)
        return bool(ack and ack[0] == 0x88)

    def cmd_write_single(self, dev_addr: int, reg_addr: int, value: int) -> bool:
        """
        CMD 0x05 — I2C single-register write (sub_1417A).
        Packet: [05][04][DevAddr][RegAddr][Value]
        """
        self._send([0x05, 0x04, dev_addr, reg_addr, value & 0xFF])
        ack = self._recv(4)
        return bool(ack and ack[0] == 0x88)

    def cmd_read_block(self, dev_addr: int, reg_addr: int, count: int) -> bytes | None:
        """
        CMD 0x84 — I2C multi-byte read (sub_14106 / sub_14F36).
        Packet: [84][03][DevAddr][RegAddr][Count]
        Response: [55][data*count]
        """
        self._send([0x84, 0x03, dev_addr, reg_addr, count])
        resp = self._recv(count + 1)
        if resp and resp[0] == 0x55:
            return resp[1:]
        return None

    def cmd_read_single(self, dev_addr: int, reg_addr: int) -> int | None:
        """
        CMD 0x85 — I2C single-register read (sub_14240).
        Packet: [85][02][DevAddr][RegAddr][00]
        Response: [55][value][3 residual bytes]  — data is at resp[1]
        """
        self._send([0x85, 0x02, dev_addr, reg_addr, 0x00])
        resp = self._recv(5)
        if resp and resp[0] == 0x55:
            return resp[1]
        return None

    # ── Demodulator register access (with I2C address routing) ───────────────

    @staticmethod
    def _demod_phys_addr(reg: int) -> int:
        """sub_142BB: map logical reg → physical I2C device address."""
        return DEMOD_HIGH if reg >= 0xC0 else DEMOD_ADDR

    def demod_write(self, reg: int, value: int) -> bool:
        return self.cmd_write_single(self._demod_phys_addr(reg), reg, value)

    def demod_read(self, reg: int) -> int | None:
        return self.cmd_read_single(self._demod_phys_addr(reg), reg)

    # ── I2C repeater gate (Tuner access) ──────────────────────────────────────

    def _repeater_enable(self):
        """Write 0xE0 to Demod reg 0x01 — opens I2C gate to MAX2165 (sub_147DA)."""
        self.cmd_write_single(DEMOD_ADDR, 0x01, 0xE0)

    def _repeater_disable(self):
        """Write 0x60 to Demod reg 0x01 — closes I2C gate (sub_147DA)."""
        self.cmd_write_single(DEMOD_ADDR, 0x01, 0x60)

    # ── Tuner register access (always via repeater) ───────────────────────────

    def tuner_write(self, reg: int, data: int | list) -> bool:
        if isinstance(data, int):
            data = [data]
        self._repeater_enable()
        ok = self.cmd_write_block(TUNER_ADDR, reg, data)
        self._repeater_disable()
        return ok

    def tuner_read(self, reg: int, count: int = 1):
        self._repeater_enable()
        val = (self.cmd_read_single(TUNER_ADDR, reg) if count == 1
               else self.cmd_read_block(TUNER_ADDR, reg, count))
        self._repeater_disable()
        return val

    # ── Firmware download ─────────────────────────────────────────────────────

    @staticmethod
    def _fw_checksum(data: bytes) -> int:
        return sum(data) & 0xFF

    def _download_stage(self, path: str, fw_id: int):
        """
        Download one firmware stage in 50-byte chunks (sub_1392E).
        Packet: [Cmd][Len-1][50 bytes data][checksum]
        Last chunk sets bit 7 of Cmd.
        """
        if not os.path.exists(path):
            raise FileNotFoundError(f"Firmware file not found: {path}")

        with open(path, "rb") as f:
            blob = f.read()

        print(f"  [{fw_id}] {os.path.basename(path)}  ({len(blob)} bytes)")
        base_cmd = fw_id & 0x7F  # 0x01 or 0x02

        for offset in range(0, len(blob), 50):
            chunk = blob[offset:offset + 50]
            is_last = (offset + 50 >= len(blob))
            cmd = base_cmd | (0x80 if is_last else 0x00)
            pkt = bytes([cmd, len(chunk) - 1]) + chunk + bytes([self._fw_checksum(chunk)])
            self._send(pkt)
            ack = self._recv(1)
            # Driver (sub_1392E) accepts both 0x88 (-120 signed) and 0x77 (119) as success
            if not ack or ack[0] not in FW_ACK_OK:
                raise RuntimeError(f"FW{fw_id} upload failed at offset {offset}: ack={list(ack)}")

        print(f"     → done")

    def fw_is_loaded(self) -> bool:
        """Check String Descriptor index 2 for 'GGG' (post-FW marker)."""
        try:
            s = usb.util.get_string(self.dev, 2)
            return "GGG" in s
        except Exception:
            return False

    def download_firmware(self, fw1: str = FW_1_PATH, fw2: str = FW_2_PATH):
        print("Firmware download:")
        self._download_stage(fw1, 1)
        time.sleep(0.1)
        self._download_stage(fw2, 2)
        time.sleep(0.5)

    # ── Demodulator identification ────────────────────────────────────────────

    def identify_demod(self) -> str:
        """
        sub_13AD7: read Demod reg 0x00.
        0x0E → LGS8GL5, else → LGS8G75.
        """
        val = self.demod_read(0x00)
        if val is None:
            raise RuntimeError("Cannot read Demod reg 0x00")
        chip = "LGS8GL5" if val == 0x0E else "LGS8G75"
        print(f"  Demod reg[0x00] = {val:#04x}  →  {chip}")
        return chip

    # ── Tuner calibration (sub_14FFE) ─────────────────────────────────────────

    def read_calibration(self):
        """
        Read MAX2165 built-in calibration data (sub_14FFE).

        Writes 1–5 to tuner reg 0x0D in sequence, reads reg 0x10 each time.
        Extracts nibbles:
          read[0] (v3): low_band_gain (bit 3:0), high_band_gain (bit 7:4)
          read[1] (v4): bw_min (bit 3:0), bw_max (bit 7:4)
          read[2] (v5): reg_0a_cal (bit 7:4)
        """
        reads = []
        for i in range(1, 6):
            self.tuner_write(0x0D, i)
            v = self.tuner_read(0x10)
            reads.append(v if v is not None else 0)
        self.tuner_write(0x0D, 0)  # Reset reg 0x0D

        v3, v4, v5 = reads[0], reads[1], reads[2]
        self.cal = {
            'low_band_gain':  v3 & 0x0F,   # byte_2E051 (used when freq >= 725 MHz)
            'high_band_gain': (v3 >> 4),    # byte_2E052 (used when freq < 725 MHz)
            'bw_min':         v4 & 0x0F,   # byte_2E055
            'bw_max':         (v4 >> 4),   # byte_2E054
            'reg_0a_cal':     (v5 >> 4),   # byte_2E053
        }
        print(f"  Calibration: {self.cal}")
        return self.cal

    # ── Tuner frequency math ──────────────────────────────────────────────────

    def _calc_nk(self, freq_mhz: int) -> list:
        """
        sub_150C4: N = freq // 12,  K = ((freq % 12) << 20) // 12
        Returns 4-byte list: [N, 0x10|(K>>16)&0xF, K>>8&0xFF, K&0xFF]
        High nibble of byte[1] is always 0x1 (mode bit, constant).
        """
        N = freq_mhz // REF_FREQ
        K = ((freq_mhz % REF_FREQ) << 20) // REF_FREQ
        return [
            N & 0xFF,
            0x10 | ((K >> 16) & 0x0F),
            (K >> 8) & 0xFF,
            K & 0xFF,
        ]

    def _calc_bw_byte(self, freq_mhz: int, force_max_gain: bool = False) -> int:
        """
        sub_15114: Bandwidth/Gain control byte (tuner reg 0x04).
        gain nibble: selects low/high band LNA gain from calibration.
        bw nibble  : linear interpolation across 470–780 MHz range.
        Result: (bw & 0xF) | (gain << 4)
        """
        if force_max_gain or not self.cal:
            gain = 0xF
            bw = 0
        elif freq_mhz >= 725:
            gain = self.cal['low_band_gain']
            bw_min = self.cal['bw_min']
            bw_max = self.cal['bw_max']
            bw = bw_min + (freq_mhz - 470) * (bw_max - bw_min) // 310
        else:
            gain = self.cal['high_band_gain']
            bw_min = self.cal['bw_min']
            bw_max = self.cal['bw_max']
            bw = bw_min + (freq_mhz - 470) * (bw_max - bw_min) // 310
        return (max(0, min(15, bw)) & 0x0F) | (gain << 4)

    def _calc_reg_0a(self) -> int:
        """
        sub_1517F: tuner reg 0x0A.
        High nibble = clamp(byte_2E053 - 2, 0, 15).
        Low nibble  = preserved self._r0a_lo.
        """
        hi = max(0, min(15, self.cal.get('reg_0a_cal', 0) - 2)) if self.cal else 0
        return (hi << 4) | (self._r0a_lo & 0x0F)

    # ── Tuner initialization (sub_151B1) ──────────────────────────────────────

    def init_tuner(self):
        """
        sub_151B1: write the full 15-byte config to MAX2165 reg 0x00.
        Base frequency 474 MHz; calibration is read first.
        """
        print("  Initializing tuner (MAX2165)...")
        BASE = 474

        self.read_calibration()

        nk   = self._calc_nk(BASE)
        bw   = self._calc_bw_byte(BASE, force_max_gain=True)  # ref=0 in driver init call
        r0a  = self._calc_reg_0a()

        init_regs = nk + [
            bw,    # reg 0x04: BW/Gain
            0x01,  # reg 0x05
            0x0A,  # reg 0x06
            0x08,  # reg 0x07
            0x02,  # reg 0x08
            0x54,  # reg 0x09 (84 dec)
            r0a,   # reg 0x0A
            0x75,  # reg 0x0B (117 dec)
            0x00,  # reg 0x0C
            0x00,  # reg 0x0D
            0x00,  # reg 0x0E
        ]
        assert len(init_regs) == 15, f"Expected 15, got {len(init_regs)}"

        self._repeater_enable()
        ok = self.cmd_write_block(TUNER_ADDR, 0x00, init_regs)
        self._repeater_disable()

        if not ok:
            raise RuntimeError("Tuner init block write failed")
        print(f"  15 regs written (base={BASE} MHz, BW={bw:#04x}, reg0A={r0a:#04x})")

    # ── Tune to frequency (sub_1524A) ─────────────────────────────────────────

    def tune(self, freq_mhz: int):
        """
        Full tuning sequence (sub_13C03 → sub_1524A):

        1. Demod soft reset (read/clear/set reg 0x02)
        2. Enable I2C repeater
        3. Write [N, K_hi, K_mid, K_lo, BW] to Tuner reg 0x00  CMD: 04 07 C0 00 ...
        4. Write reg 0x0A                                        CMD: 04 03 C0 0A [val]
        5. Read-Modify-Write reg 0x04 |= 0xF0  (PLL latch)      CMD: 84 03 C0 04 01 → write back
        6. Disable I2C repeater
        """
        print(f"\n{'─'*50}")
        print(f"Tuning to {freq_mhz} MHz")
        print(f"{'─'*50}")

        # Pre-tune: demod soft reset (sub_143B5)
        reg2 = self.demod_read(0x02)
        if reg2 is not None:
            self.demod_write(0x02, reg2 & 0xFE)
            self.demod_write(0x02, reg2 | 0x01)

        nk  = self._calc_nk(freq_mhz)
        bw  = self._calc_bw_byte(freq_mhz)
        r0a = self._calc_reg_0a()
        N, K = nk[0], (nk[1] & 0x0F) << 16 | nk[2] << 8 | nk[3]

        print(f"  N={N:#04x} ({N})  K={K:#08x}  BW={bw:#04x}  reg0A={r0a:#04x}")
        print(f"  CMD 0x04: 04 07 C0 00 {nk[0]:02x} {nk[1]:02x} {nk[2]:02x} {nk[3]:02x} {bw:02x}")

        # Step 1: enable repeater
        self._repeater_enable()

        # Step 2: write 5 bytes N/K/BW
        self.cmd_write_block(TUNER_ADDR, 0x00, nk + [bw])

        # Step 3: write reg 0x0A
        self.cmd_write_block(TUNER_ADDR, 0x0A, [r0a])

        # Step 4: R-M-W reg 0x04 |= 0xF0  (PLL latch, NOT |= 0x40)
        val4 = self.cmd_read_block(TUNER_ADDR, 0x04, 1)
        if val4:
            new4 = val4[0] | 0xF0
            self.cmd_write_block(TUNER_ADDR, 0x04, [new4])
            print(f"  PLL latch: reg[0x04] {val4[0]:#04x} → {new4:#04x}")
        else:
            print("  Warning: could not read reg[0x04] for PLL latch")

        # Step 5: disable repeater
        self._repeater_disable()

        print("  Tune complete.")

    # ── Lock status polling ───────────────────────────────────────────────────

    def poll_lock_reg(self, timeout_s: float = 5.0, interval_s: float = 0.1) -> bool:
        """
        Poll Demod reg 0x4B at ~100 ms intervals (sub_13C03 loop).
        Bit 0 set = locked.
        """
        print(f"\nPolling lock via reg 0x4B (timeout {timeout_s}s)...")
        t_end = time.time() + timeout_s
        while time.time() < t_end:
            st = self.demod_read(0x4B)
            if st is not None:
                locked = bool(st & 0x01)
                tag = "LOCKED ✓" if locked else "unlocked"
                print(f"  reg[0x4B] = {st:#04x}  {tag}")
                if locked:
                    return True
            time.sleep(interval_s)
        print("  Timed out — no lock.")
        return False

    def read_status_packet(self, timeout_ms: int = 700) -> dict | None:
        """
        Read one 8-byte status packet from EP 0x8A (Interrupt IN).
        Format: BB 05 [LOCK] [SNR] [BER_H] [CTR] [BER_L] 00

        Valid lock:  LOCK=1, SNR stable high, BER_L=0x00
        False lock:  LOCK=1, SNR jumps erratically, BER_L=0xFF
        No signal:   LOCK=0
        """
        try:
            raw = bytes(self.dev.read(EP_STATUS, 64, timeout_ms))
            if len(raw) >= 8 and raw[0] == 0xBB and raw[1] == 0x05:
                return {
                    'lock':  raw[2],
                    'snr':   raw[3],
                    'ber_h': raw[4],
                    'ctr':   raw[5],
                    'ber_l': raw[6],
                    'raw':   raw[:8].hex(' ').upper(),
                }
        except usb.core.USBTimeoutError:
            pass
        except Exception as e:
            print(f"  EP 0x8A error: {e}")
        return None

    @staticmethod
    def interpret_status(s: dict) -> str:
        if s['lock'] and s['ber_l'] == 0x00:
            return "GOOD SIGNAL ✓"
        if s['lock']:
            return "false-lock / noise"
        return "no signal"

    def print_status(self, s: dict | None):
        if s is None:
            print("  EP 0x8A: (no packet)")
            return
        print(f"  EP 0x8A: [{s['raw']}]  "
              f"lock={s['lock']}  SNR={s['snr']:#04x}  "
              f"BER={s['ber_h']:02X}{s['ber_l']:02X}  "
              f"→ {self.interpret_status(s)}")

    # ── TS stream ─────────────────────────────────────────────────────────────

    def read_stream_chunk(self, buf_size: int = 4096, timeout_ms: int = 500) -> bytes:
        """Read one Bulk IN transfer from EP 0x88 (MPEG-TS, High Speed mode)."""
        try:
            return bytes(self.dev.read(EP_STREAM, buf_size, timeout_ms))
        except usb.core.USBTimeoutError:
            return b""


# ─── Device open ──────────────────────────────────────────────────────────────

def open_device() -> usb.core.Device:
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        raise RuntimeError(f"Device {VID:#06x}:{PID:#06x} not found.\n"
                           "  Windows: run Zadig and switch to WinUSB/libusb-win32.")

    # Detach kernel driver (Linux / macOS)
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except (NotImplementedError, usb.core.USBError):
        pass  # Windows: no-op

    dev.set_configuration(1)
    usb.util.claim_interface(dev, 0)
    # Switch to Alt Setting 1 to activate all 7 endpoints
    dev.set_interface_altsetting(interface=0, alternate_setting=1)
    print(f"Device opened: {VID:#06x}:{PID:#06x}  (Alt Setting 1)")
    return dev


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="LME2510C DTMB USB initialization & tuning tool")
    parser.add_argument("--freq",   type=int,   default=618,
                        help="Tune frequency in MHz (default: 618)")
    parser.add_argument("--stream", action="store_true",
                        help="Dump raw TS bytes to stdout after tuning")
    parser.add_argument("--fw1",    default=FW_1_PATH,
                        help=f"Firmware stage 1 path (default: {FW_1_PATH})")
    parser.add_argument("--fw2",    default=FW_2_PATH,
                        help=f"Firmware stage 2 path (default: {FW_2_PATH})")
    parser.add_argument("--status-only", action="store_true",
                        help="Only print EP 0x8A status packets, no tuning")
    args = parser.parse_args()

    # ── 1. Open device ────────────────────────────────────────────────────────
    dev = open_device()
    lme = LME2510(dev)

    # ── 2. Firmware check / download ─────────────────────────────────────────
    if lme.fw_is_loaded():
        print("Firmware: already loaded.")
    else:
        print("Firmware: not loaded — starting download...")
        lme.download_firmware(args.fw1, args.fw2)
        print("Waiting for device re-enumeration...")
        time.sleep(2.0)
        dev = open_device()
        lme = LME2510(dev)

    if args.status_only:
        print("\n[EP 0x8A status packets — Ctrl-C to stop]")
        while True:
            lme.print_status(lme.read_status_packet(2000))
        return

    # ── 3. Identify demodulator ───────────────────────────────────────────────
    print("\n[Demodulator identification]")
    chip = lme.identify_demod()

    # ── 4. Initialize tuner ───────────────────────────────────────────────────
    print("\n[Tuner initialization]")
    lme.init_tuner()

    # ── 5. Tune ───────────────────────────────────────────────────────────────
    lme.tune(args.freq)

    # ── 6. Lock polling via demod register ───────────────────────────────────
    locked = lme.poll_lock_reg(timeout_s=5.0)

    # ── 7. Signal status via EP 0x8A ─────────────────────────────────────────
    print("\n[EP 0x8A signal status (5 packets)]")
    for _ in range(5):
        lme.print_status(lme.read_status_packet(700))

    # ── 8. Optional TS stream dump ────────────────────────────────────────────
    if args.stream:
        if not locked:
            print("\nWarning: no lock — stream will likely be noise.")
        print("\n[TS stream → stdout, Ctrl-C to stop]")
        out = sys.stdout.buffer if hasattr(sys.stdout, "buffer") else sys.stdout
        try:
            while True:
                chunk = lme.read_stream_chunk()
                if chunk:
                    out.write(chunk)
                    out.flush()
        except KeyboardInterrupt:
            print("\nStream stopped.", file=sys.stderr)


if __name__ == "__main__":
    main()
