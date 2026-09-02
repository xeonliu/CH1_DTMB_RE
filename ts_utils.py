"""
MPEG-TS helpers for the LME2510C EP 0x88 stream.

EP 0x88 delivers a *continuous* MPEG-TS byte stream as a sequence of USB bulk
transfers (typically 4096 B each).  A TS packet is not aligned to a transfer
boundary: it may start in one transfer and complete in the next.

Capture code must therefore treat the EP as one byte stream and must NOT
re-align every 4096-byte chunk independently: doing so drops the packet that
crosses each boundary (about 5-10% of the stream on this hardware).

`TSPacketizer` keeps a carry buffer and emits complete 188-byte packets on one
global phase, re-syncing only after a genuine gap or startup garbage.
`best_sync_offset` / `extract_ts_packets` are single-buffer helpers kept only
for offline analysis of an already-sliced buffer.
"""

TS_LEN = 188
SYNC = 0x47


class TSPacketizer:
    """
    Turn ordered USB bulk frames into a continuous MPEG-TS packet stream.

    ``feed()`` may return fewer bytes than the supplied chunk contains (the
    rest stays buffered as a partial packet that crosses into the next chunk).
    All bytes returned across calls together form the exact TS packet
    sequence, so packets that straddle chunk boundaries are not lost.
    """

    def __init__(self, min_sync: int = 3):
        self._buf = bytearray()
        self._min_sync = min_sync
        self.packets = 0          # total complete packets emitted
        self.resyncs = 0          # times a new phase was found
        self.dropped_bytes = 0    # leading garbage / invalid bytes discarded

    def feed(self, chunk: bytes | bytearray) -> bytes:
        """Append one USB bulk chunk and return any newly completed packets."""
        self._buf += chunk
        out = bytearray()
        while len(self._buf) >= TS_LEN:
            if self._buf[0] == SYNC:
                out += self._buf[:TS_LEN]
                del self._buf[:TS_LEN]
                self.packets += 1
                continue
            if not self._advance_to_sync():
                break
        return bytes(out)

    def _advance_to_sync(self) -> bool:
        """Drop leading bytes until the buffer starts on a credible TS phase."""
        need = TS_LEN * self._min_sync
        if len(self._buf) < need:
            # Not enough data to validate a new phase yet; keep buffering.
            return False
        off, hits = best_sync_offset(self._buf, self._min_sync)
        if hits >= self._min_sync and off:
            self.resyncs += 1
            self.dropped_bytes += off
            del self._buf[:off]
            return True
        # No credible phase in the current window: drop one packet-width so a
        # long garbage run cannot make the buffer grow without bound.
        self.resyncs += 1
        self.dropped_bytes += TS_LEN
        del self._buf[:TS_LEN]
        return True

    def remaining(self) -> bytes:
        """Bytes still buffered (normally the tail of an incomplete packet)."""
        return bytes(self._buf)


def best_sync_offset(frame: bytes, min_sync: int = 3) -> tuple[int, int]:
    """
    Return (offset, hits) for the 188-byte alignment with the most 0x47 sync
    bytes inside *frame*.  Ties prefer the smaller offset.  Offsets that cannot
    hold *min_sync* packets are ignored.
    """
    if len(frame) < TS_LEN * min_sync:
        return 0, 0
    best_off, best_hits = 0, 0
    for off in range(TS_LEN):
        hits = 0
        for start in range(off, len(frame), TS_LEN):
            if frame[start] == SYNC:
                hits += 1
        if hits > best_hits:
            best_hits, best_off = hits, off
    return best_off, best_hits


def extract_ts_packets(frame: bytes, min_sync: int = 3):
    """
    Extract the complete TS packets from one USB bulk frame.

    Single-buffer offline helper: it finds the best phase *inside one already
    sliced buffer only* and deliberately drops packets at buffer edges.  It
    must NOT be used in the streaming hot path.

    Returns (packet_bytes, sync_offset, packet_count, sync_ratio).
    If no reliable alignment is found, returns (b"", offset, 0, 0.0).
    """
    off, hits = best_sync_offset(frame, min_sync)
    if hits < min_sync:
        return b"", off, 0, 0.0

    out = bytearray()
    count = 0
    start = off
    while start + TS_LEN <= len(frame):
        if frame[start] == SYNC:
            out += frame[start:start + TS_LEN]
            count += 1
        start += TS_LEN

    max_full = (len(frame) - off) // TS_LEN if len(frame) > off else 0
    ratio = (count / max_full) if max_full else 0.0
    return bytes(out), off, count, ratio


def packetize_frames(frames) -> bytes:
    """Continuous packetization over ordered frames (USB bulk chunks)."""
    pz = TSPacketizer()
    return b"".join(pz.feed(bytes(frame)) for frame in frames)
