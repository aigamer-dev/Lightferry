"""Blob <-> tiles (PLAN section 8). Pure data; sealing and acks live in link.py.

* ``split(data, chunk, obj_id) -> list[Tile]``   (empty data -> one empty tile)
* ``join(tiles, expect_sha256=None) -> bytes``   (sha256 check, raises ``TilerError``)
* ``Reassembler``                                 dedupes by (obj_id, seq), tracks base/bitmap
* ``pack_frames(items, per_frame)``               group packets/tiles into displayed frames
* ``body_capacity(grid)``                         tile body bytes that fit a code of that grid
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, Sequence, TypeVar

from . import protocol

T = TypeVar("T")


class TilerError(Exception):
    pass


@dataclass(frozen=True)
class Tile:
    obj_id: int
    seq: int
    total: int
    data: bytes


def body_capacity(grid: int) -> int:
    """Tile body size for a grid: code payload capacity minus the 56 B packet overhead."""
    from . import codec  # local import keeps protocol/tiler usable without OpenCV
    n = codec.max_payload_bytes(grid) - protocol.OVERHEAD
    if n <= 0:
        raise TilerError(f"grid {grid} too small for a packet")
    return n


def split(data: bytes, chunk: int, obj_id: int) -> list[Tile]:
    if chunk <= 0:
        raise TilerError("chunk must be positive")
    n = max(1, -(-len(data) // chunk))
    return [Tile(obj_id, i, n, bytes(data[i * chunk:(i + 1) * chunk])) for i in range(n)]


class Reassembler:
    """Collects tiles of one object. ``add`` returns True only for a new (obj_id, seq)."""

    def __init__(self, obj_id: int, expect_sha256: bytes | None = None):
        self.obj_id = obj_id
        self.expect_sha256 = expect_sha256
        self.total: int | None = None
        self._tiles: dict[int, bytes] = {}
        self._base = 0
        self.duplicates = 0

    def add(self, obj_id: int, seq: int, total: int, data: bytes) -> bool:
        if obj_id != self.obj_id or total < 1 or seq >= total:
            raise TilerError("tile does not belong to this object")
        if self.total is None:
            self.total = total
        elif total != self.total:
            raise TilerError("inconsistent tile total")
        if seq in self._tiles:
            self.duplicates += 1
            return False
        self._tiles[seq] = bytes(data)
        while self._base in self._tiles:
            self._base += 1
        return True

    @property
    def received(self) -> int:
        return len(self._tiles)

    @property
    def complete(self) -> bool:
        return self.total is not None and self._base >= self.total

    @property
    def base(self) -> int:
        """Lowest missing seq (== total when complete; 0 before any tile)."""
        return self._base

    def bitmap(self, bits: int = 16) -> int:
        """Bit i set <=> seq base+1+i received."""
        m = 0
        for i in range(bits):
            if (self._base + 1 + i) in self._tiles:
                m |= 1 << i
        return m

    def assemble(self) -> bytes:
        if not self.complete:
            raise TilerError("incomplete")
        data = b"".join(self._tiles[i] for i in range(self.total))
        if self.expect_sha256 is not None and hashlib.sha256(data).digest() != self.expect_sha256:
            raise TilerError("sha256 mismatch")
        return data


def join(tiles: Iterable[Tile], expect_sha256: bytes | None = None) -> bytes:
    """Reassemble tiles (any order, duplicates ignored); verifies sha256 when given."""
    r: Reassembler | None = None
    for t in tiles:
        if r is None:
            r = Reassembler(t.obj_id, expect_sha256)
        r.add(t.obj_id, t.seq, t.total, t.data)
    if r is None:
        raise TilerError("no tiles")
    return r.assemble()


def pack_frames(items: Sequence[T], per_frame: int) -> list[list[T]]:
    """Group items into displayed frames of at most ``per_frame`` tiles (cols*rows)."""
    if per_frame < 1:
        raise TilerError("per_frame must be >= 1")
    return [list(items[i:i + per_frame]) for i in range(0, len(items), per_frame)]
