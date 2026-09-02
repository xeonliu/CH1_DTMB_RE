"""
lme2510_probe.py — experimental LME2510C DTMB USB probing tool
Based on reverse engineering of UDE262D.sys (IDA Pro 9.0)

Usage:
    python lme2510_probe.py              # tune to 618 MHz, print status
    python lme2510_probe.py --freq 554   # tune to 554 MHz
    python lme2510_probe.py --freq 618 --stream  # dump raw TS to stdout

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
# Device USB Product IDs (from fw_bootloader.bin USB descriptors at offsets 0x00 / 0x100):
PID_COLD    = 0x1111   # Cold-boot PID — USB controller only, no firmware loaded yet
PID_WARM    = 0x1120   # Warm-boot PID — after fw_bootloader.bin (stage 1) loads
PID         = PID_WARM # Default PID used across the codebase

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


# ─── USB string helpers ───────────────────────────────────────────────────────

def read_string_descriptor_raw(dev, index: int, langid: int = 0x0409) -> bytes:
    """
    Read a USB string descriptor as raw bytes (no UTF-16 decoding).

    The LME2510C's firmware puts an ASCII marker in string descriptor 2
    without proper UTF-16LE encoding, so pyusb's get_string() mangles it
    (\"GGGG\" becomes CJK-looking garbage).  Read raw descriptor bytes and
    inspect the payload instead.
    """
    try:
        raw = bytes(dev.ctrl_transfer(
            bmRequestType=0x80, bRequest=0x06,
            wValue=(0x03 << 8) | index, wIndex=langid,
            data_or_wLength=255, timeout=1000,
        ))
    except Exception:
        return b""
    # Descriptor: [len][type=0x03][UTF-16LE-ish payload...]
    return raw[2:]


def firmware_marker_ok(payload: bytes, min_g: int = 3) -> bool:
    """
    Stage-2 (main) firmware marks string descriptor 2 with 0x47 ('G') bytes:

      bootloader / stage-1 only: DEFG  -> one 'G'
      main firmware active:      GGGG  -> four 'G's

    Accept the descriptor only when at least *min_g* ASCII 'G' bytes are
    present so a plain bootloader can never be mistaken for a loaded device.
    """
    return payload.count(0x47) >= min_g


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
        try:
            return bytes(self.dev.read(EP_CMD_IN, n, TIMEOUT_MS))
        except usb.core.USBError as exc:
            print(f"  [USB] _recv({n}): {exc}", file=sys.stderr)
            return b""

    def send_short_cmd(self, data: bytes | list, ack_len: int = 5, label: str = "CMD") -> bytes:
        pkt = bytes(data)
        print(f"  {label}: {pkt.hex(' ').upper()}")
        self._send(pkt)
        resp = self._recv(ack_len)
        print(f"    ACK: {resp.hex(' ').upper() if resp else '(none)'}")
        return resp

    def dump_usb_descriptors(self):
        print("\n[USB descriptors]")
        print(f"  VID:PID = {self.dev.idVendor:#06x}:{self.dev.idProduct:#06x}")
        cfg = self.dev.get_active_configuration()
        print(f"  active configuration = {cfg.bConfigurationValue}")
        for intf in cfg:
            print(f"  interface {intf.bInterfaceNumber}, alt {intf.bAlternateSetting}, "
                  f"class={intf.bInterfaceClass:#04x}, endpoints={intf.bNumEndpoints}")
            for ep in intf:
                attrs = ep.bmAttributes & 0x03
                typ = {0: "control", 1: "iso", 2: "bulk", 3: "interrupt"}.get(attrs, "?")
                direction = "IN" if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN else "OUT"
                print(f"    ep {ep.bEndpointAddress:#04x} {direction:<3} {typ:<9} "
                      f"maxpkt={ep.wMaxPacketSize} interval={ep.bInterval}")

    def read_ep_raw(self, ep: int, size: int, timeout_ms: int) -> bytes | None:
        try:
            data = bytes(self.dev.read(ep, size, timeout_ms))
            return data
        except usb.core.USBTimeoutError:
            return None
        except usb.core.USBError as exc:
            print(f"  EP {ep:#04x}: USBError {exc}")
            return None

    def probe_endpoint(self, ep: int, size: int = 64, count: int = 20,
                       timeout_ms: int = 500, parse_status: bool = False):
        print(f"\n[Raw endpoint probe EP {ep:#04x}, size={size}, count={count}, timeout={timeout_ms}ms]")
        for i in range(count):
            data = self.read_ep_raw(ep, size, timeout_ms)
            if data is None:
                print(f"  {i:02d}: timeout")
                continue
            print(f"  {i:02d}: {len(data)} bytes  {data.hex(' ').upper()}")
            if parse_status:
                packets = self.parse_status_packets(data, require_type=None)
                for pkt in packets:
                    self.print_status(pkt)

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
        """Check raw String Descriptor 2 bytes for the 'GGGG' post-FW marker."""
        return firmware_marker_ok(read_string_descriptor_raw(self.dev, 2))

    def download_firmware(self, fw1: str = FW_1_PATH, fw2: str = FW_2_PATH):
        print("Firmware download:")
        self._download_stage(fw1, 1)
        time.sleep(0.1)
        self._download_stage(fw2, 2)
        time.sleep(0.5)
        self._cmd_post_fw()

    def _cmd_post_fw(self):
        """
        sub_13EC8: post-firmware activation command.

        Sends [0x8A, 0x00] after both firmware stages are downloaded.
        This matches driver sub_13EC8 → sub_14046([0x8A, 0x00], 2) which is
        called as the last step of sub_13A95 (the full firmware-load sequence).
        After receiving this command the device applies the uploaded firmware
        and will typically re-enumerate on the USB bus.  USB errors during the
        5-byte response read are silently ignored because the device may reset
        before it can reply.
        """
        try:
            self._send([0x8A, 0x00])
            self._recv(5)   # ACK; may fail if device resets immediately
        except Exception:
            pass            # device reset / re-enumeration is expected here

    # ── Demodulator identification ────────────────────────────────────────────

    def identify_demod(self, retries: int = 5, retry_delay: float = 0.5) -> str:
        """
        sub_13AD7: read Demod reg 0x00.
        0x0E → LGS8GL5, else → LGS8G75.

        Retries up to *retries* times (with *retry_delay* seconds between
        attempts) because the demodulator may not respond immediately after
        firmware activation or device re-enumeration.
        """
        val = None
        for attempt in range(retries):
            val = self.demod_read(0x00)
            if val is not None:
                break
            if attempt < retries - 1:
                time.sleep(retry_delay)
        if val is None:
            raise RuntimeError(
                "Cannot read Demod reg 0x00 after retries — "
                "check USB connection and that firmware is loaded"
            )
        chip = "LGS8GL5" if val == 0x0E else "LGS8G75"
        print(f"  Demod reg[0x00] = {val:#04x}  →  {chip}")
        return chip

    def diag_demod(self, id_reads: int = 8) -> str:
        """
        Print demodulator ID stability and key DTMB status/control registers.

        This is intentionally read-only.  It helps distinguish a real LGS8GL5
        ID (stable reg 0x00 == 0x0E) from a transient/garbled I2C read.
        """
        print("\n[Demodulator diagnostic]")
        ids = []
        for _ in range(id_reads):
            ids.append(self.demod_read(0x00))
            time.sleep(0.02)

        print("  reg[0x00] reads: " + " ".join(
            "--" if v is None else f"{v:#04x}" for v in ids))
        valid = [v for v in ids if v is not None]
        stable = bool(valid) and all(v == valid[0] for v in valid)
        chip = "UNKNOWN"
        if stable:
            chip = "LGS8GL5" if valid[0] == 0x0E else "LGS8G75"
        print(f"  ID stability: {'stable' if stable else 'unstable'}  →  {chip}")

        regs = (0x02, 0x03, 0x04, 0x07, 0x37, 0x4B, 0x7C, 0x7E, 0xA4, 0xC5)
        for reg in regs:
            val = self.demod_read(reg)
            print(f"  reg[{reg:#04x}] = {'--' if val is None else f'{val:#04x}'}")
        return chip

    # ── Chip-type selection CMD 0x16 (sub_13F00) ─────────────────────────────

    def cmd_select_chip_type(self, chip: str) -> bool:
        """
        sub_13F00: tell the USB bridge firmware which demodulator is connected.

        This command **must** be sent after demodulator identification and
        before reading EP 0x8A signal status packets.  The bridge uses the
        chip-type byte to determine which demodulator I2C registers to poll
        when building the status packets.  Without this command the bridge
        firmware never generates EP 0x8A interrupt packets.

        Packet: [0x16, 0x01, chip_type]  → response: 5 bytes (ACK)
          chip_type = 0x00  for LGS8GL5 (sub_13F00(a1, 0))
          chip_type = 0x01  for LGS8G75 (sub_13F00(a1, 1))
        """
        chip_type = 0x00 if chip == "LGS8GL5" else 0x01
        self._send([0x16, 0x01, chip_type])
        resp = self._recv(5)
        return bool(resp and len(resp) >= 1)

    def cmd_pid_filter_default_1fff(self) -> bool:
        """
        Windows path sub_10654(..., a4=0) -> lme_cmd03_pid_filter_program:
        program one fallback PID 0x1FFF, send the same CMD03 twice, then
        commit/reset with CMD06 [06 00].

        Built from IDA only. This is useful for checking whether the bridge
        firmware only starts periodic interrupt/status traffic after the same
        post-tune PID-filter commit the Windows driver performs.
        """
        return self.cmd_pid_filter([0x1FFF], mode=2)

    def cmd_pid_filter(self, pids: list[int], mode: int = 0) -> bool:
        """
        Program CMD 0x03 with the given PIDs, send it twice, then commit with
        CMD 0x06 (sub_13F76 -> sub_13E90).

        Packet layout (sub_13F76):
          [03] [4n+2] [2k] [pid_lo] [2k+1] [pid_hi] ... [20] [terminal]
        Terminal byte: mode 0 -> 0x80 + 2*(n-1); mode 2 -> 0x81 + 2*(n-1).

        Live-verified semantics (LGS8GL5 + MAX2165, EP 0x88):
          mode 0 = allow-list: only the listed PIDs are forwarded.
          mode 2 + PID 0x1FFF = clear/reset: all PIDs are forwarded
            (the Windows post-tune default commit).
          mode 2 + real PIDs is NOT an allow-list on this hardware: it drops
            the listed PIDs and pads the output with 0x1FFF null packets.
        """
        n = len(pids)
        if not 1 <= n <= 16:
            raise ValueError("PID count must be 1..16")
        if any(not 0 <= pid <= 0x1FFF for pid in pids):
            raise ValueError("PID values must be 0x0000..0x1FFF")

        cmd03 = [0x03, 4 * n + 2]
        for k, pid in enumerate(pids):
            cmd03 += [2 * k, pid & 0xFF, 2 * k + 1, pid >> 8]
        terminal = (0x81 if mode == 2 else 0x80) + 2 * (n - 1)
        cmd03 += [0x20, terminal]

        ack1 = self.send_short_cmd(cmd03, label="CMD03 PID filter pass 1")
        ack2 = self.send_short_cmd(cmd03, label="CMD03 PID filter pass 2")
        ack3 = self.send_short_cmd([0x06, 0x00], label="CMD06 commit")
        return bool(ack1 and ack2 and ack3)

    # ── Post-identify demod init (sub_145A2 + sub_1440D, LGS8GL5 path) ────────

    def _init_demod_after_identify(self, chip: str):
        """
        Demodulator register initialization performed by sub_13AD7 right after
        tuner init, for both chip types.

        LGS8GL5 (sub_145A2(1) then sub_1440D(0)):
          1. Read demod reg 0x07
          2. Write reg 0x07 |= 0x0C   (set bits [3:2])
          3. Write reg 0x09 = 0x00
          4. Write reg 0x0A = 0x00
          5. Write reg 0x0B = 0x00
          6. Write reg 0x0C = 0x00
          7. Read demod reg 0x07 again
          8. Write reg 0x07 &= 0x7C   (clear bits [7,1,0])

        LGS8G75: the equivalent (sub_14D78) sets up a large demod calibration
        table and is a very long operation (~3678 iterations); it writes to
        regs 0xC6, 0x18, 0x3D, 0x39, 0x3A, 0x38, 0x3B and a lookup-table
        batch.  In practice, lock is still achieved without it on warm-restart
        so we apply only the same essential reg 0x07/0x09–0x0C sequence here.
        """
        reg7 = self.demod_read(0x07)
        if reg7 is None:
            return
        if chip == "LGS8GL5":
            # sub_145A2(1): set bits [3:2]
            self.demod_write(0x07, reg7 | 0x0C)
        else:
            # LGS8G75 uses same enable-bits
            self.demod_write(0x07, reg7 | 0x0C)
        # Zero out DTMB sync parameters regs 0x09–0x0C (sub_145A2, both paths)
        for r in (0x09, 0x0A, 0x0B, 0x0C):
            self.demod_write(r, 0x00)
        # sub_1440D(0): clear bits [7,1,0]
        reg7b = self.demod_read(0x07)
        if reg7b is not None:
            self.demod_write(0x07, reg7b & 0x7C)

    # ── Post-tune demod init (sub_14C72 + sub_14957 + sub_14C16) ─────────────

    def _init_demod_post_tune(self, chip: str):
        """
        Demodulator register configuration applied by sub_13C03 after tuning.

        Called for both chip types (the LGS8G75 path also calls sub_14D6E
        which is a no-op stub in the driver).  Prepares the demod for signal
        measurement that drives EP 0x8A status packets.

        Sequence (mirrors sub_14C72(0) + sub_14957(0) + sub_14C16()):
          sub_14C72(0):
            Read  reg 0x07
            Write reg 0x07 |= 0x0C    (set bits [3:2])
            Write reg 0x08 = 0x00
            Write reg 0x09 = 0x00
            Write reg 0x0A = 0x00
            Write reg 0x0B = 0x00
          sub_14957(_, _, 0):
            Read  reg 0x07
            Write reg 0x07 &= 0x7F    (clear bit 7)
          sub_14C16():
            Read  reg 0x0C
            Write reg 0x0C = (old & 0x7B) | 0x80  (clear bit 2, set bit 7)
            Write reg 0x39 = 0x00
            Write reg 0x3D = 0x04
        """
        # sub_14C72(0) ─────────────────────────────────
        reg7 = self.demod_read(0x07)
        if reg7 is not None:
            self.demod_write(0x07, reg7 | 0x0C)
        for r in (0x08, 0x09, 0x0A, 0x0B):
            self.demod_write(r, 0x00)

        # sub_14957(_, _, 0) ───────────────────────────
        reg7 = self.demod_read(0x07)
        if reg7 is not None:
            self.demod_write(0x07, reg7 & 0x7F)

        # sub_14C16() ──────────────────────────────────
        reg_c = self.demod_read(0x0C)
        if reg_c is not None:
            self.demod_write(0x0C, (reg_c & 0x7B) | 0x80)
        self.demod_write(0x39, 0x00)
        self.demod_write(0x3D, 0x04)

    # ── LGS8GL5 lock preparation/training (sub_14527 + sub_14640) ────────────

    def _demod_soft_reset(self) -> bool:
        """
        Demod soft reset (sub_143B5): read reg 0x00, then read reg 0x02,
        clear bit0, set bit0, sleep 5 ms.
        """
        if self.demod_read(0x00) is None:
            return False
        reg2 = self.demod_read(0x02)
        if reg2 is None:
            return False
        if not self.demod_write(0x02, reg2 & 0xFE):
            return False
        if not self.demod_write(0x02, reg2 | 0x01):
            return False
        time.sleep(0.005)
        return True

    def _set_lgs8gl5_regc5_lowbits_and_reset(self) -> bool:
        """
        sub_14474: clear reg 0x7E bit0, set reg 0xC5 low bits to 0x06,
        then soft-reset the demod.
        """
        reg7e = self.demod_read(0x7E)
        if reg7e is None or not self.demod_write(0x7E, reg7e & 0xFE):
            return False

        regc5 = self.demod_read(0xC5)
        if regc5 is None or not self.demod_write(0xC5, (regc5 & 0xE0) | 0x06):
            return False

        return self._demod_soft_reset()

    def _write_lgs8gl5_reg7d_and_reset(self, value: int) -> bool:
        """sub_1444F: write reg 0x7D, then soft-reset the demod."""
        if not self.demod_write(0x7D, value & 0xFF):
            return False
        return self._demod_soft_reset()

    def _prepare_lgs8gl5_for_lock(self) -> bool:
        """
        sub_14527: LGS8GL5 lock preparation.
          reg 0x03 &= ~0x01
          reg 0x7E |=  0x01
          reg 0xC5 &=  0xE0
        """
        reg3 = self.demod_read(0x03)
        if reg3 is None or not self.demod_write(0x03, reg3 & 0xFE):
            return False

        reg7e = self.demod_read(0x7E)
        if reg7e is None or not self.demod_write(0x7E, reg7e | 0x01):
            return False

        regc5 = self.demod_read(0xC5)
        if regc5 is None or not self.demod_write(0xC5, regc5 & 0xE0):
            return False

        return True

    def _poll_demod_mask_eq(self, reg: int, mask: int, expected: int,
                            attempts: int, interval_s: float) -> bool:
        for _ in range(attempts):
            val = self.demod_read(reg)
            if val is not None:
                ok = (val & mask) == expected
                print(f"  reg[{reg:#04x}] = {val:#04x}  "
                      f"mask {mask:#04x} → {'ok' if ok else 'wait'}")
                if ok:
                    return True
            time.sleep(interval_s)
        return False

    def train_lgs8gl5_lock(self, max_rounds: int = 5) -> bool:
        """
        Practical userspace version of IDA sub_14640.

        The driver tries several reg 0x04/0x37 combinations, soft-resets the
        demod, waits for reg 0x4B bit7, then checks reg 0xA4[1:0] == 1.
        """
        print("\n[LGS8GL5 lock training]")
        for round_idx in range(max_rounds):
            mode = round_idx % 5
            if mode in (1, 3):
                continue

            reg4 = self.demod_read(0x04)
            reg37 = self.demod_read(0x37)
            if reg4 is None or reg37 is None:
                print("  training aborted: could not read reg 0x04/0x37")
                return False

            new4 = reg4 & 0xFC
            new37 = reg37
            if mode == 0:
                new4 |= 0x02
                new37 &= 0x7F
            elif mode == 2:
                new37 &= 0x7F
            elif mode == 4:
                new4 |= 0x01
                new37 |= 0x80

            print(f"  round {round_idx + 1}: mode={mode}  "
                  f"reg04 {reg4:#04x}->{new4:#04x}  "
                  f"reg37 {reg37:#04x}->{new37:#04x}")
            if not self.demod_write(0x04, new4):
                return False
            if not self.demod_write(0x37, new37):
                return False
            if not self._demod_soft_reset():
                return False

            if (self._poll_demod_mask_eq(0x4B, 0x80, 0x80, 30, 0.02) and
                    self._poll_demod_mask_eq(0xA4, 0x03, 0x01, 20, 0.01)):
                rega2 = self.demod_read(0xA2)
                if rega2 is None:
                    return False
                if not self._set_lgs8gl5_regc5_lowbits_and_reset():
                    return False
                if not self._write_lgs8gl5_reg7d_and_reset(rega2):
                    return False
                print("  LGS8GL5 lock training: LOCKED")
                return True

            reg7c = self.demod_read(0x7C)
            if reg7c is not None:
                self.demod_write(0x7C, reg7c ^ 0x80)

        print("  LGS8GL5 lock training: no lock")
        return False

    def lock_after_tune(self, chip: str) -> bool:
        if chip == "LGS8GL5":
            if not self._prepare_lgs8gl5_for_lock():
                print("  Warning: LGS8GL5 lock preparation failed")
            self._demod_soft_reset()
            locked = self.train_lgs8gl5_lock()
            if locked:
                return True
            print("\nFallback: polling simple reg 0x4B bit0 lock...")
            return self.poll_lock_reg(timeout_s=5.0)

        self._init_demod_post_tune(chip)
        return self.poll_lock_reg(timeout_s=5.0)

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

    @staticmethod
    def parse_status_packets(raw: bytes, require_type: int | None = 0x05) -> list[dict]:
        packets = []
        for offset in range(0, max(0, len(raw) - 7), 8):
            pkt = raw[offset:offset + 8]
            if len(pkt) >= 8 and pkt[0] == 0xBB and (require_type is None or pkt[1] == require_type):
                packets.append({
                    'type':  pkt[1],
                    'lock':  pkt[2],
                    'snr':   pkt[3],
                    'ber_h': pkt[4],
                    'ctr':   pkt[5],
                    'ber_l': pkt[6],
                    # Documented driver field names (see LME2510_Analysis.md):
                    'signal_level': pkt[3],
                    'snr_raw':      pkt[4],
                    'hi':           pkt[5],
                    'lo':           pkt[6],
                    'raw':   pkt[:8].hex(' ').upper(),
                    'offset': offset,
                })
        return packets

    @staticmethod
    def parse_status_packet(raw: bytes) -> dict | None:
        packets = LME2510.parse_status_packets(raw, require_type=None)
        return packets[0] if packets else None

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
            return self.parse_status_packet(raw)
        except usb.core.USBTimeoutError:
            pass
        except Exception as e:
            # After a long uninterrupted TS run the firmware may have queued
            # many 8-byte packets; a 64-byte read then overflows.  Drain a
            # larger transfer and parse the first status packet from it.
            try:
                raw = bytes(self.dev.read(EP_STATUS, 512, timeout_ms))
                return self.parse_status_packet(raw)
            except Exception:
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
            print("  EP 0x8A: (status endpoint no packet)")
            return
        print(f"  EP 0x8A: [{s['raw']}]  "
              f"type={s.get('type', 0):#04x}  lock={s['lock']}  "
              f"signal={s['signal_level']:#04x}  snr_raw={s['snr_raw']:#04x}  "
              f"hi={s['hi']:02X}  lo={s['lo']:02X}  "
              f"→ {'LOCKED ✓' if s['lock'] else 'not locked'}")

    # ── TS stream ─────────────────────────────────────────────────────────────

    def read_stream_chunk(self, buf_size: int = 4096, timeout_ms: int = 500) -> bytes:
        """Read one Bulk IN transfer from EP 0x88 (MPEG-TS, High Speed mode)."""
        try:
            return bytes(self.dev.read(EP_STREAM, buf_size, timeout_ms))
        except usb.core.USBTimeoutError:
            return b""


# ─── Device open ──────────────────────────────────────────────────────────────

def open_device() -> usb.core.Device:
    # Try warm-boot PID first (most common); fall back to cold-boot PID
    dev = usb.core.find(idVendor=VID, idProduct=PID_WARM)
    if dev is None:
        dev = usb.core.find(idVendor=VID, idProduct=PID_COLD)
    if dev is None:
        raise RuntimeError(
            f"Device not found (VID={VID:#06x}, tried PID={PID_WARM:#06x} and {PID_COLD:#06x}).\n"
            "  Windows: run Zadig and switch to WinUSB/libusb-win32.\n"
            "  Linux/macOS: ensure you have permission to access USB devices."
        )

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
    print(f"Device opened: {VID:#06x}:{dev.idProduct:#06x}  (Alt Setting 1)")
    return dev


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Experimental LME2510C DTMB USB probing tool")
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
    parser.add_argument("--diag-demod", action="store_true",
                        help="Read demod ID and key registers, then exit")
    parser.add_argument("--dump-usb", action="store_true",
                        help="Print active USB configuration/endpoints")
    parser.add_argument("--probe-status", action="store_true",
                        help="Raw-read EP 0x8A after optional tune/commands")
    parser.add_argument("--probe-stream", action="store_true",
                        help="Raw-read EP 0x88 after optional tune/commands")
    parser.add_argument("--probe-count", type=int, default=20,
                        help="Raw probe transfer count (default: 20)")
    parser.add_argument("--probe-timeout", type=int, default=500,
                        help="Raw probe timeout in ms (default: 500)")
    parser.add_argument("--probe-size", type=int, default=64,
                        help="Raw status probe size in bytes (default: 64)")
    parser.add_argument("--no-tune", action="store_true",
                        help="Skip identify/tuner/tune; useful for endpoint-only probing")
    parser.add_argument("--no-pid-filter", action="store_true",
                        help="Skip the post-tune PID 0x1FFF filter commit. "
                             "Without the commit the bridge never emits EP 0x8A "
                             "status packets or EP 0x88 TS data.")
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
        if not lme.fw_is_loaded():
            print("Warning: firmware may not be fully active yet "
                  "(String Descriptor 2 does not contain the 'GGG' warm-boot marker). "
                  "Continuing anyway — identify_demod() will retry if needed.")

    if args.dump_usb:
        lme.dump_usb_descriptors()

    if args.status_only:
        print("\n[EP 0x8A status packets — Ctrl-C to stop]")
        while True:
            lme.print_status(lme.read_status_packet(2000))
        return

    if args.diag_demod:
        lme.diag_demod()
        return

    if args.no_tune:
        if args.probe_status:
            lme.probe_endpoint(EP_STATUS, size=args.probe_size, count=args.probe_count,
                               timeout_ms=args.probe_timeout, parse_status=True)
        if args.probe_stream:
            lme.probe_endpoint(EP_STREAM, size=4096, count=args.probe_count,
                               timeout_ms=args.probe_timeout)
        return

    # ── 3. Identify demodulator ───────────────────────────────────────────────
    print("\n[Demodulator identification]")
    chip = lme.identify_demod()

    # ── 3a. Tell USB bridge which demodulator is connected (sub_13F00) ────────
    #   CMD [0x16, 0x01, chip_type] enables EP 0x8A status packet generation.
    #   Without this the bridge firmware does not know which demodulator
    #   registers to poll, so EP 0x8A never sends interrupt packets.
    lme.cmd_select_chip_type(chip)

    if chip == "LGS8GL5":
        # sub_147BF in the original driver: soft-reset immediately after
        # chip-type selection and before opening the tuner repeater.
        lme._demod_soft_reset()

    # ── 4. Initialize tuner ───────────────────────────────────────────────────
    print("\n[Tuner initialization]")
    lme.init_tuner()

    # ── 4a. Post-identify demodulator register init (sub_145A2 + sub_1440D) ───
    lme._init_demod_after_identify(chip)

    # ── 5. Tune ───────────────────────────────────────────────────────────────
    lme.tune(args.freq)

    # ── 5a/6. Chip-specific post-tune lock path ──────────────────────────────
    locked = lme.lock_after_tune(chip)

    if not args.no_pid_filter:
        print("\n[Windows-derived default PID-filter commit]")
        lme.cmd_pid_filter_default_1fff()

    if args.probe_status:
        lme.probe_endpoint(EP_STATUS, size=args.probe_size, count=args.probe_count,
                           timeout_ms=args.probe_timeout, parse_status=True)

    if args.probe_stream:
        lme.probe_endpoint(EP_STREAM, size=4096, count=args.probe_count,
                           timeout_ms=args.probe_timeout)

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
