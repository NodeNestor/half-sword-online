"""Forward Error Correction using XOR-based parity.

This module provides a simple, fast FEC scheme suitable for real-time
streaming.  It is *not* a full Reed-Solomon code — it uses XOR parity
which can recover at most one lost packet per parity group (or, with
interleaved parity, one loss per sub-group).

Math
----
Given data packets D_0 .. D_{k-1}, a single XOR parity packet P is:

    P = D_0 ^ D_1 ^ ... ^ D_{k-1}

If any single D_i is lost, it can be recovered as:

    D_i = P ^ D_0 ^ ... ^ D_{i-1} ^ D_{i+1} ^ ... ^ D_{k-1}

For *n* parity packets (n >= 2) we partition the group into *n*
interleaved sub-groups.  Sub-group *j* contains packets whose index
satisfies ``i % n == j``.  Each sub-group gets its own parity packet,
allowing recovery of up to one loss per sub-group (so up to *n*
losses in the best case, if they fall in different sub-groups).

Packet layout
-------------
Every parity packet carries a small header:

    [1 byte]  parity_index   — which sub-group (0 .. n-1)
    [2 bytes] group_seq_base — sequence number of the first data packet
    [1 byte]  group_size     — number of data packets in the group
    [2 bytes] max_payload_len — original max payload length (for un-padding)
    [rest]    XOR payload

All multi-byte integers are big-endian.
"""

import logging
import struct
from typing import Optional

log = logging.getLogger(__name__)

_HDR_FMT = "!BHBH"  # parity_index(u8), group_seq_base(u16), group_size(u8), max_len(u16)
_HDR_SIZE = struct.calcsize(_HDR_FMT)  # 6 bytes


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _xor_bytes(a: bytes, b: bytes) -> bytes:
    """XOR two byte strings, padding the shorter one with zeros."""
    if len(a) < len(b):
        a = a + b'\x00' * (len(b) - len(a))
    elif len(b) < len(a):
        b = b + b'\x00' * (len(a) - len(b))
    return bytes(x ^ y for x, y in zip(a, b))


def _xor_bytes_fast(a: bytes, b: bytes) -> bytes:
    """XOR using int conversion — much faster for large payloads."""
    la, lb = len(a), len(b)
    if la < lb:
        a = a + b'\x00' * (lb - la)
    elif lb < la:
        b = b + b'\x00' * (la - lb)
    ia = int.from_bytes(a, "big")
    ib = int.from_bytes(b, "big")
    return (ia ^ ib).to_bytes(len(a), "big")


def _pad(data: bytes, length: int) -> bytes:
    """Zero-pad *data* to exactly *length* bytes."""
    if len(data) >= length:
        return data
    return data + b'\x00' * (length - len(data))


# ------------------------------------------------------------------
# Encoder
# ------------------------------------------------------------------

class FECEncoder:
    """Accumulates data packets and emits XOR parity packets.

    Parameters
    ----------
    group_size:
        Number of data packets per FEC group (default 10).
    fec_percentage:
        Percentage of parity packets relative to group size.
        20 means 2 parity packets per 10 data packets.
    """

    def __init__(self, group_size: int = 10, fec_percentage: int = 20) -> None:
        self.group_size = group_size
        self.num_parity = max(1, (group_size * fec_percentage) // 100)
        self._buffer: list[tuple[bytes, int]] = []  # (data, seq)
        self._group_seq_base: int = -1

    def add_packet(self, data: bytes, seq: int) -> list[bytes]:
        """Add a data packet and return any FEC parity packets to send.

        Returns an empty list most of the time; when the group is complete
        it returns ``num_parity`` parity packets ready for transmission.
        """
        if not self._buffer:
            self._group_seq_base = seq

        self._buffer.append((data, seq))

        if len(self._buffer) < self.group_size:
            return []

        # Group complete — compute parity packets
        parity_packets = self._compute_parity()
        self._buffer.clear()
        return parity_packets

    def flush(self) -> list[bytes]:
        """Force-emit parity for whatever is buffered (partial group)."""
        if not self._buffer:
            return []
        parity_packets = self._compute_parity()
        self._buffer.clear()
        return parity_packets

    # ---- internal ----

    def _compute_parity(self) -> list[bytes]:
        """Compute interleaved XOR parity packets for the current group.

        Sub-group *j* (0-indexed) contains data packets at buffer indices
        where ``index % num_parity == j``.  The parity for that sub-group
        is the XOR of all its members (zero-padded to the max packet length
        in the full group).
        """
        payloads = [d for d, _ in self._buffer]
        max_len = max(len(p) for p in payloads)
        group_size = len(self._buffer)
        base_seq = self._group_seq_base

        result: list[bytes] = []
        for j in range(self.num_parity):
            # Collect sub-group members
            members = [payloads[i] for i in range(group_size) if i % self.num_parity == j]
            if not members:
                continue

            parity = _pad(members[0], max_len)
            for m in members[1:]:
                parity = _xor_bytes_fast(parity, _pad(m, max_len))

            header = struct.pack(_HDR_FMT, j, base_seq & 0xFFFF, group_size, max_len)
            result.append(header + parity)

        log.debug(
            "FEC group seq_base=%d size=%d parity_packets=%d",
            base_seq, group_size, len(result),
        )
        return result


# ------------------------------------------------------------------
# Decoder
# ------------------------------------------------------------------

class FECDecoder:
    """Collects data and parity packets, recovers losses via XOR.

    Parameters
    ----------
    group_size:
        Expected data packets per group (must match the encoder).
    fec_percentage:
        Parity percentage (must match the encoder).
    """

    def __init__(self, group_size: int = 10, fec_percentage: int = 20) -> None:
        self.group_size = group_size
        self.num_parity = max(1, (group_size * fec_percentage) // 100)

        # group_base_seq -> {index_in_group: data}
        self._groups: dict[int, dict[int, bytes]] = {}
        # group_base_seq -> {parity_index: parity_payload (without header)}
        self._parity: dict[int, dict[int, tuple[bytes, int, int]]] = {}
        # Sequences we already recovered (avoid duplicate work)
        self._recovered: set[int] = set()

    def add_packet(
        self, data: bytes, seq: int, is_fec: bool
    ) -> Optional[bytes]:
        """Ingest a data or FEC parity packet.

        Parameters
        ----------
        data:
            Raw packet bytes (for FEC packets this includes the 6-byte header).
        seq:
            Sequence number.  For data packets this is the real sequence
            number; for FEC parity packets it can be anything (the header
            carries the group information).
        is_fec:
            ``True`` if this is a parity packet produced by :class:`FECEncoder`.

        Returns
        -------
        Optional[bytes]
            If a previously-lost data packet was recovered, return it.
            Otherwise ``None``.
        """
        if is_fec:
            return self._ingest_fec(data)
        else:
            return self._ingest_data(data, seq)

    # ---- internal ----

    def _ingest_data(self, data: bytes, seq: int) -> Optional[bytes]:
        """Store a received data packet and attempt recovery."""
        # Determine which group this belongs to.  We need to figure out the
        # base_seq.  Without explicit signalling from the encoder we infer
        # it: base = seq - (seq - base) where base is the nearest multiple
        # of group_size below seq.  This only works when seq numbers are
        # contiguous per-group, which the encoder guarantees.
        base = (seq // self.group_size) * self.group_size
        idx = seq - base

        group = self._groups.setdefault(base, {})
        group[idx] = data

        recovered = self._try_recover(base)
        self._maybe_gc(base)
        return recovered

    def _ingest_fec(self, raw: bytes) -> Optional[bytes]:
        """Parse and store a parity packet, then try recovery."""
        if len(raw) < _HDR_SIZE:
            log.warning("FEC packet too short (%d bytes)", len(raw))
            return None

        parity_idx, base_seq, group_size, max_len = struct.unpack(
            _HDR_FMT, raw[:_HDR_SIZE]
        )
        payload = raw[_HDR_SIZE:]

        parity_group = self._parity.setdefault(base_seq, {})
        parity_group[parity_idx] = (payload, group_size, max_len)

        recovered = self._try_recover(base_seq)
        self._maybe_gc(base_seq)
        return recovered

    def _try_recover(self, base_seq: int) -> Optional[bytes]:
        """Attempt to recover a missing packet in the group at *base_seq*.

        For each sub-group that has parity, check if exactly one data
        packet is missing.  If so, XOR the parity with all present data
        packets to reconstruct the missing one.
        """
        parity_map = self._parity.get(base_seq)
        data_map = self._groups.get(base_seq, {})

        if parity_map is None:
            return None

        for par_idx, (par_payload, group_size, max_len) in parity_map.items():
            # Indices in this sub-group
            sub_indices = [i for i in range(group_size) if i % self.num_parity == par_idx]
            present = [i for i in sub_indices if i in data_map]
            missing = [i for i in sub_indices if i not in data_map]

            if len(missing) != 1:
                continue  # Can only recover exactly one loss per sub-group

            lost_idx = missing[0]
            lost_seq = base_seq + lost_idx

            if lost_seq in self._recovered:
                continue

            # Recover: XOR parity with all present members
            recovered = _pad(par_payload, max_len)
            for i in present:
                recovered = _xor_bytes_fast(recovered, _pad(data_map[i], max_len))

            # Store so we don't recover twice, and add to group
            self._recovered.add(lost_seq)
            data_map[lost_idx] = recovered

            log.debug("FEC recovered seq=%d (group base=%d)", lost_seq, base_seq)
            return recovered

        return None

    def _maybe_gc(self, base_seq: int) -> None:
        """Garbage-collect old groups to avoid unbounded memory use."""
        # Keep at most 5 groups in memory
        max_groups = 5
        all_bases = sorted(set(list(self._groups.keys()) + list(self._parity.keys())))
        while len(all_bases) > max_groups:
            old = all_bases.pop(0)
            self._groups.pop(old, None)
            self._parity.pop(old, None)
