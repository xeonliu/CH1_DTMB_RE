"""
lme2510_stream.py — tune the CH1 (LME2510C + LGS8GL5 + MAX2165) DTMB stick,
enable the transport stream, and forward it to UDP and/or a .ts file.

The stream path is deliberately simple: keep EP 0x88 read continuously and
never let bookkeeping get in the way.  Status decoding and register
telemetry are sampled once per second from the same loop.

Logs (optional):
  --status-log  human-readable phases, EP 0x8A decodes, stream statistics
  --reg-log     every I2C register read/write (demod + MAX2165), plus the
                periodic live telemetry snapshots
  --usb-trace   additionally append raw USB TX/RX packets to the reg log

Examples:
  # forward 618 MHz to UDP 127.0.0.1:1234 (VLC: udp://@1234)
  sudo .venv/bin/python lme2510_stream.py --freq 618

  # forward the raw EP 0x88 bulk frames verbatim to UDP 127.0.0.1:1235
  sudo .venv/bin/python lme2510_stream.py --freq 618 --no-udp \\
      --raw-udp 127.0.0.1:1235

  # save 10 s of 554 MHz to a file with register logging, no UDP
  sudo .venv/bin/python lme2510_stream.py --freq 554 --no-udp \\
      --file ts554.ts --seconds 10 --reg-log logs/regs-554.log
"""

import argparse
import datetime as _dt
import os
import socket
import time

import lme2510_probe as probe
from ts_utils import TS_LEN, TSPacketizer


def now_ms() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class LoggingLME2510(probe.LME2510):
    """
    LME2510 wrapper that logs every I2C register operation.

    Register operations are intercepted at the CMD 0x04/0x05/0x84/0x85 layer,
    so reads/writes performed by init_tuner(), tune() and the LGS8GL5 lock
    training all land in the register log with timestamps.
    """

    TUNER_REG_NAMES = {
        0x00: "N_div", 0x01: "K_frac_hi", 0x02: "K_frac_mid", 0x03: "K_frac_lo",
        0x04: "track_filter", 0x05: "LNA", 0x06: "PLL_CFG", 0x07: "TEST",
        0x08: "SHUTDOWN", 0x09: "VCO_CTRL", 0x0A: "BASEBAND",
        0x0B: "DC_OFFSET_CTRL", 0x0C: "DC_OFFSET_DAC", 0x0D: "ROM_TABLE_ADDR",
        0x10: "ROM_TABLE_DATA", 0x11: "STATUS", 0x12: "AUTOTUNE",
    }

    def __init__(self, dev, reg_log: str | None = None, usb_trace: bool = False):
        super().__init__(dev)
        self.usb_trace = usb_trace
        self._reg_fh = None
        if reg_log:
            os.makedirs(os.path.dirname(reg_log) or ".", exist_ok=True)
            self._reg_fh = open(reg_log, "a", encoding="utf-8")
            self._reg_log_path = reg_log
        else:
            self._reg_log_path = None

    def _log(self, kind: str, detail: str):
        if self._reg_fh:
            self._reg_fh.write(f"{now_ms()} | {kind} | {detail}\n")
            self._reg_fh.flush()

    def close(self):
        if self._reg_fh:
            self._reg_fh.close()
            self._reg_fh = None

    @property
    def reg_log_path(self):
        return self._reg_log_path

    # ── raw USB trace (--usb-trace) ──────────────────────────────────────────

    def _send(self, data):
        if self.usb_trace:
            self._log("USB_TX", bytes(data).hex(" ").upper())
        return super()._send(data)

    def _recv(self, n):
        resp = super()._recv(n)
        if self.usb_trace:
            self._log("USB_RX", resp.hex(" ").upper() if resp else "(none)")
        return resp

    # ── register logging ─────────────────────────────────────────────────────

    def _reg_name(self, dev_addr: int, reg: int) -> str:
        if dev_addr == probe.TUNER_ADDR:
            name = self.TUNER_REG_NAMES.get(reg)
            return f"TUNER reg 0x{reg:02X}{(' [' + name + ']') if name else ''}"
        return f"DEMOD{'H' if reg >= 0xC0 else ''} reg 0x{reg:02X}"

    def cmd_write_single(self, dev_addr, reg_addr, value) -> bool:
        ok = super().cmd_write_single(dev_addr, reg_addr, value)
        self._log("I2C_WRITE", f"{self._reg_name(dev_addr, reg_addr)} = "
                               f"0x{value:02X}  ack_ok={ok}")
        return ok

    def cmd_read_single(self, dev_addr, reg_addr):
        val = super().cmd_read_single(dev_addr, reg_addr)
        self._log("I2C_READ", f"{self._reg_name(dev_addr, reg_addr)} -> "
                              f"{'None' if val is None else f'0x{val:02X}'}")
        return val

    def cmd_write_block(self, dev_addr, reg_addr, data) -> bool:
        ok = super().cmd_write_block(dev_addr, reg_addr, data)
        self._log("I2C_WRITE_BLOCK", f"{self._reg_name(dev_addr, reg_addr)}+ "
                                     f"data={' '.join(f'{b:02X}' for b in data)}")
        return ok

    def cmd_read_block(self, dev_addr, reg_addr, count):
        val = super().cmd_read_block(dev_addr, reg_addr, count)
        if dev_addr == probe.TUNER_ADDR:
            self._log("I2C_READ_BLOCK", f"TUNER reg 0x{reg_addr:02X} n={count} -> "
                                        f"{'None' if val is None else val.hex(' ').upper()}")
        return val

    def send_short_cmd(self, data, ack_len: int = 5, label: str = "CMD") -> bytes:
        resp = super().send_short_cmd(data, ack_len, label)
        self._log("CTRL_CMD", f"{label} packet={bytes(data).hex(' ').upper()} "
                              f"ack={resp.hex(' ').upper() if resp else '(none)'}")
        return resp


# ── EP 0x8A decoding (LGS8GL5 formulas from LME2510_Analysis.md §5.5) ────────

def gl5_strength(lock: bool, signal: int, hi: int) -> int:
    low3 = signal & 0x07
    word = signal | (hi << 8)
    if not lock:
        return low3 + 8
    if 0x1F00 <= word <= 0x1FFF:
        return low3 + 80
    if 0x0058 <= word <= 0x00FF:
        return low3 + 70
    if 0x0100 <= word <= 0x0180:
        return low3 + 60
    if 0x0180 <= word <= 0x0200:
        return low3 + 50
    return 69


def gl5_quality(lock: bool, snr_raw: int) -> int:
    mod = snr_raw % 15
    linear = (100 * snr_raw) // 255
    if not lock:
        return mod + 5
    if linear == 0:
        return mod + 30
    if linear <= 40:
        return 90 - linear
    if linear < 90:
        return 100 - ((3 * linear // 4) % 100)
    return 30


def decode_status(pkt: dict) -> dict:
    lock = bool(pkt["lock"])
    signal = pkt.get("signal_level", pkt["snr"])
    snr_raw = pkt.get("snr_raw", pkt["ber_h"])
    hi = pkt.get("hi", pkt["ctr"])
    return {
        "raw": pkt["raw"],
        "lock": lock,
        "signal": signal,
        "snr_raw": snr_raw,
        "hi": hi,
        "lo": pkt.get("lo", pkt["ber_l"]),
        "strength_pct": gl5_strength(lock, signal, hi),
        "quality_pct": gl5_quality(lock, snr_raw),
    }


def status_line(dec: dict) -> str:
    return (f"EP8A raw={dec['raw']} lock={int(dec['lock'])} "
            f"signal=0x{dec['signal']:02X} snr_raw=0x{dec['snr_raw']:02X} "
            f"hi=0x{dec['hi']:02X} lo=0x{dec['lo']:02X} "
            f"strength={dec['strength_pct']}% quality={dec['quality_pct']}%")


def status_sample(lme, sink=None, timeout_ms: int = 700,
                  report_empty: bool = True) -> bool:
    """Read one EP 0x8A packet and emit a decoded STATUS line.

    Prints to stdout and, when *sink* is given, appends the same line to the
    status log.  Returns True if a packet arrived.  With report_empty=True a
    short note is still emitted (and False returned) when the endpoint is
    silent, so a missing enable step is visible in the log.
    """
    pkt = lme.read_status_packet(timeout_ms=timeout_ms)
    if pkt is None:
        if report_empty:
            line = f"STATUS EP 0x8A: no packet within {timeout_ms} ms"
            print(line)
            if sink:
                sink.write(f"{now_ms()} | {line}\n")
                sink.flush()
        return False
    line = f"STATUS {status_line(decode_status(pkt))}"
    print(line)
    if sink:
        sink.write(f"{now_ms()} | {line}\n")
        sink.flush()
    return True


def telemetry(lme: LoggingLME2510):
    """Read live registers once and log one combined snapshot line."""
    fmt = lambda v: "--" if v is None else f"0x{v:02X}"
    regs = {r: lme.demod_read(r) for r in (0x02, 0x04, 0x37, 0x4B,
                                           0x7C, 0x7E, 0xA2, 0xA4, 0xC5)}
    t11 = lme.tuner_read(0x11)
    t12 = lme.tuner_read(0x12)
    lme._log("TELEMETRY",
             f"4B={fmt(regs[0x4B])} A4={fmt(regs[0xA4])} "
             f"37={fmt(regs[0x37])} 7C={fmt(regs[0x7C])} "
             f"A2={fmt(regs[0xA2])} tuner11={fmt(t11)} tuner12={fmt(t12)}")


# ── device open + firmware ────────────────────────────────────────────────────

def open_logged(args) -> LoggingLME2510:
    """Open the device, downloading the main firmware if needed."""
    for _ in range(20):
        try:
            dev = probe.open_device()
            break
        except RuntimeError:
            time.sleep(0.25)
    else:
        raise RuntimeError("LME2510C device not found")

    def make(dev):
        return LoggingLME2510(dev, reg_log=args.reg_log, usb_trace=args.usb_trace)

    lme = make(dev)
    if lme.fw_is_loaded():
        print("Firmware: already loaded.")
        return lme

    print("Firmware: downloading stage-1 + stage-2...")
    lme.download_firmware(args.fw1, args.fw2)
    print("Waiting for device re-enumeration...")
    time.sleep(2.0)
    for _ in range(20):
        try:
            dev = probe.open_device()
            break
        except RuntimeError:
            time.sleep(0.5)
    else:
        raise RuntimeError("Device did not re-enumerate after firmware load")
    lme.close()
    lme = make(dev)
    if not lme.fw_is_loaded():
        print("Warning: firmware marker still missing; continuing anyway.")
    return lme


# ── stream loop ───────────────────────────────────────────────────────────────

def run_stream(args, lme: LoggingLME2510, slog_fh):
    """Read EP 0x88 continuously and forward aligned TS packets."""
    udp = None
    udp_target = None
    if not args.no_udp:
        host, _, port = args.udp.rpartition(":")
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        udp_target = (host, int(port))
        print(f"UDP target: {host}:{int(port)}")

    raw_udp = None
    raw_udp_target = None
    if args.raw_udp:
        host, _, port = args.raw_udp.rpartition(":")
        raw_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        raw_udp.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        raw_udp_target = (host, int(port))
        print(f"Raw UDP target: {host}:{int(port)} "
              f"(one datagram per EP 0x88 bulk frame)")

    fout = None
    if args.file:
        fout = open(args.file, "wb")
        print(f"File capture: {args.file}")
    packetizer = TSPacketizer()

    def say(line: str):
        print(line)
        if slog_fh:
            slog_fh.write(f"{now_ms()} | {line}\n")
            slog_fh.flush()

    def sample():
        """Read one EP 0x8A packet + live registers (only while idle/at end)."""
        pkt = lme.read_status_packet(timeout_ms=120)
        if pkt is not None:
            say(f"STATUS {status_line(decode_status(pkt))}")
        if args.telemetry:
            telemetry(lme)

    t0 = time.perf_counter()
    bytes_total = pkts_total = dgrams_total = frames_total = timeouts = 0
    raw_bytes_total = raw_dgrams_total = 0
    last_idle_sample = 0.0

    try:
        while True:
            elapsed = time.perf_counter() - t0
            if args.seconds and elapsed >= args.seconds:
                break

            frame = lme.read_stream_chunk(buf_size=4096, timeout_ms=500)
            if frame:
                frames_total += 1
                if raw_udp:
                    raw_udp.sendto(frame, raw_udp_target)
                    raw_bytes_total += len(frame)
                    raw_dgrams_total += 1
                packets = packetizer.feed(frame)
                count = len(packets) // TS_LEN
                if packets:
                    bytes_total += len(packets)
                    pkts_total += count
                    if fout:
                        fout.write(packets)
                    if udp:
                        for off in range(0, len(packets) - 1315, 1316):
                            udp.sendto(packets[off:off + 1316], udp_target)
                            dgrams_total += 1
                        if (rem := len(packets) % 1316):
                            udp.sendto(packets[-rem:], udp_target)
                            dgrams_total += 1
            else:
                timeouts += 1
                elapsed = time.perf_counter() - t0
                if (args.telemetry and
                        elapsed - last_idle_sample >= args.telemetry):
                    sample()
                    last_idle_sample = elapsed

    except KeyboardInterrupt:
        say("Interrupted by user.")
    finally:
        elapsed = time.perf_counter() - t0
        sample()
        if elapsed > 0:
            mbps = bytes_total * 8 / elapsed / 1e6
            say(f"FINAL elapsed={elapsed:.1f}s bytes={bytes_total} "
                f"pkts={pkts_total} frames={frames_total} "
                f"timeouts={timeouts} resyncs={packetizer.resyncs} "
                f"dropped_bytes={packetizer.dropped_bytes} "
                f"rate={mbps:.2f} Mbit/s")
        if raw_udp:
            say(f"RAW FINAL bytes={raw_bytes_total} datagrams={raw_dgrams_total}")
        if fout:
            fout.close()
        if udp:
            udp.close()
        if raw_udp:
            raw_udp.close()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--freq", type=int, default=618, help="tune frequency in MHz")
    ap.add_argument("--pids", default="",
                    help="comma-separated PID list (hex, e.g. 0x0100,0x0101) to "
                         "program into the bridge filter as an allow-list; "
                         "default 0x1FFF = all PIDs")
    ap.add_argument("--pid-mode", type=int, default=0,
                    help="CMD 0x03 mode for --pids: 0 = keep only listed PIDs "
                         "(default); 2 = clear/0x1FFF semantics (advanced)")
    ap.add_argument("--udp", default="127.0.0.1:1234",
                    help="UDP target host:port (default 127.0.0.1:1234)")
    ap.add_argument("--raw-udp", default="",
                    help="forward each raw EP 0x88 bulk frame unchanged to "
                         "host:port (diagnostic; no TS re-packetization)")
    ap.add_argument("--no-udp", action="store_true", help="disable UDP")
    ap.add_argument("--file", default="", help="also write TS to this .ts file")
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="stop after N seconds (default: until Ctrl-C)")
    ap.add_argument("--telemetry", type=float, default=2.0,
                    help="live register snapshot interval in seconds (0 disables)")
    ap.add_argument("--status-log", default="",
                    help="status/statistics log (default: logs/stream-<time>.log)")
    ap.add_argument("--reg-log", default="",
                    help="register-operation log (default: logs/regs-<time>.log)")
    ap.add_argument("--usb-trace", action="store_true",
                    help="also log raw USB TX/RX packets")
    ap.add_argument("--fw1", default=probe.FW_1_PATH)
    ap.add_argument("--fw2", default=probe.FW_2_PATH)
    args = ap.parse_args()

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    os.makedirs("logs", exist_ok=True)
    args.status_log = args.status_log or f"logs/stream-{stamp}.log"
    args.reg_log = args.reg_log or f"logs/regs-{stamp}.log"
    slog_fh = open(args.status_log, "a", encoding="utf-8")
    print(f"status log : {args.status_log}")
    print(f"register log: {args.reg_log}")

    try:
        lme = open_logged(args)
        chip = lme.identify_demod()
        lme.cmd_select_chip_type(chip)
        if chip == "LGS8GL5":
            lme._demod_soft_reset()
        lme.init_tuner()
        lme._init_demod_after_identify(chip)
        lme.tune(args.freq)
        locked = lme.lock_after_tune(chip)
        pid_list = [int(p.strip(), 0) for p in args.pids.split(",") if p.strip()]
        if pid_list:
            print(f"\nPID filter: {', '.join(f'0x{p:04X}' for p in pid_list)}")
            lme.cmd_pid_filter(pid_list, mode=args.pid_mode)
        else:
            lme.cmd_pid_filter_default_1fff()
        # The PID-filter commit (CMD 0x03 x2 + CMD 0x06) is what makes the
        # bridge start emitting EP 0x8A status packets, so sample right after
        # it — still before forwarding begins.
        status_sample(lme, slog_fh, timeout_ms=700)
        print(f"\nLocked: {locked} — forwarding TS on {args.freq} MHz...")
        run_stream(args, lme, slog_fh)
    finally:
        if "lme" in locals():
            lme.close()
        slog_fh.close()


if __name__ == "__main__":
    main()
