"""Stop-and-wait ARQ pieces, profiles, config and in-process drivers (PLAN section 8).

Engines (handshake.Sender / handshake.Receiver) are transport agnostic: ``step(now, inbound) -> outbound``.
This module gives them the reliable-tile machinery and glue to channels:

* ``Profile(grid, module_px, tiles_n)``, ``BOOTSTRAP`` (64, 6, 1), ``LinkConfig``
* ``TileTx``  sender side: frames of pending tiles, retransmit on timeout, apply ACK state
* ``TileRx``  receiver side: dedupe, reassemble, build ACK bodies
* ``encode_ack`` / ``decode_ack``  ACK body: obj_id u16, base u32 (lowest missing seq), top_counter u32,
  bitmap u16 (bit i <=> seq base+1+i received)
* ``pump_sender`` / ``pump_receiver`` / ``run_pair``  drive engines over channels.Endpoints
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, replace
from typing import Callable, Sequence

from . import protocol, tiler
from .channels import Endpoint, ManualClock

ACK_FMT = ">HIIH"
ACK_BITS = 16


@dataclass(frozen=True)
class Profile:
    """Visual profile of a displayed frame: code grid, pixels per module, tiles per side (NxN)."""
    grid: int
    module_px: int
    tiles_n: int

    @property
    def per_frame(self) -> int:
        return self.tiles_n * self.tiles_n

    @property
    def body_capacity(self) -> int:
        return tiler.body_capacity(self.grid)

    def to_json(self) -> dict:
        return {"grid": self.grid, "module_px": self.module_px, "tiles": self.tiles_n}

    @classmethod
    def from_json(cls, d: dict) -> "Profile":
        try:
            return cls(int(d["grid"]), int(d["module_px"]), int(d["tiles"]))
        except (KeyError, TypeError, ValueError) as e:
            raise protocol.ProtocolError("bad profile") from e


BOOTSTRAP = Profile(64, 6, 1)


@dataclass
class LinkConfig:
    """Timers (seconds) and limits shared by both engines."""
    timeout: float = 1.5  # re-show the last message this often
    retries: int = 8  # consecutive timeouts without progress before ABORT
    hello_s: float = 120.0  # HELLO phase limit
    sync_s: float = 30.0  # SYNC phase limit
    verify_s: float = 120.0  # VERIFY/READY phase limit
    idle_s: float = 60.0  # receiver: no valid frame during DATA
    key_mismatch_s: float = 10.0  # MAC failures with no valid frame before KEY_MISMATCH abort
    linger_s: float = 3.0  # how long a driver should keep the final ABORT/DONE_ACK shown
    dwell_s: float = 0.25  # audio: how long each code of a cycling (unacknowledged) object stays on screen


def for_audio(cfg: LinkConfig) -> LinkConfig:
    """Timers for the acoustic back-channel: a note reply takes ~0.7 s to play plus the phone's camera and
    decode latency, so S waits 5 s before asking again (see Sender.step). Never shortens."""
    return replace(cfg, timeout=max(cfg.timeout, 5.0), idle_s=max(cfg.idle_s, 120.0),
                   hello_s=max(cfg.hello_s, 300.0), sync_s=max(cfg.sync_s, 300.0), verify_s=max(cfg.verify_s, 300.0))


def encode_ack(obj_id: int, base: int, top_counter: int, bitmap: int) -> bytes:
    return struct.pack(ACK_FMT, obj_id, base, top_counter, bitmap & 0xFFFF)


def decode_ack(body: bytes) -> tuple[int, int, int, int]:
    """-> (obj_id, base, top_counter, bitmap). Raises ProtocolError on bad length."""
    if len(body) != struct.calcsize(ACK_FMT):
        raise protocol.ProtocolError("bad ack length")
    return struct.unpack(ACK_FMT, body)


class TileTx:
    """Sender side of one object. ``seal(tile) -> bytes`` is called once per tile (first show); the sealed
    bytes are cached, so retransmissions are byte-identical (same counter and tag).

    ``poll(now)`` returns the packets of a frame to (re)show, or None. ``on_ack`` applies receiver state.
    ``done`` once every tile is acknowledged; ``failed`` after ``retries`` timeouts without an ACK.
    """

    def __init__(self, tiles: Sequence[tiler.Tile], seal: Callable[[tiler.Tile], bytes], per_frame: int,
                 timeout: float, retries: int):
        self.tiles = list(tiles)
        self.total = len(self.tiles)
        self._seal = seal
        self.per_frame = per_frame
        self.timeout, self.retries = timeout, retries
        self.base = 0
        self._acked: set[int] = set()
        self._sealed: dict[int, bytes] = {}
        self._counters: dict[int, int] = {}
        self._frame: list[int] | None = None
        self._shown_at = 0.0
        self._tries = 0
        self.frames = 0
        self.retransmitted = 0
        self.failed = False

    @property
    def done(self) -> bool:
        return self.base >= self.total

    def _compose(self) -> list[int]:
        out: list[int] = []
        first = self.base
        for s in range(self.base, self.total):
            if len(out) >= self.per_frame or s > first + ACK_BITS:
                break
            if s not in self._acked:
                out.append(s)
        return out

    def _packets(self, frame: list[int]) -> list[bytes]:
        pk = []
        for s in frame:
            if s not in self._sealed:
                raw = self._seal(self.tiles[s])
                self._sealed[s] = raw
                self._counters[s] = protocol.peek(raw).counter
            pk.append(self._sealed[s])
        return pk

    def poll(self, now: float) -> list[bytes] | None:
        if self.done or self.failed:
            return None
        if self._frame is None:
            self._frame = self._compose()
            self.frames += 1
            self._shown_at, self._tries = now, 0
            return self._packets(self._frame)
        if now - self._shown_at >= self.timeout:
            self._tries += 1
            if self._tries > self.retries:
                self.failed = True
                return None
            self._shown_at = now
            self.retransmitted += len(self._frame)
            return self._packets(self._frame)
        return None

    def rearm(self) -> None:
        """Clear a ``failed`` state so polling resumes (caller's own deadline decides when to give up)."""
        self.failed = False
        self._tries = 0

    def on_ack(self, base: int, top_counter: int, bitmap: int) -> None:
        """Apply receiver state (idempotent; stale ACKs only ever under-report)."""
        base = min(base, self.total)
        if base > self.base:
            self.base = base
            self._acked = {s for s in self._acked if s >= base}
        for i in range(ACK_BITS):
            if (bitmap >> i) & 1 and base + 1 + i < self.total:
                self._acked.add(base + 1 + i)
        while self.base in self._acked:
            self._acked.discard(self.base)
            self.base += 1
        for s in [s for s in self._sealed if s < self.base]:  # free cache
            del self._sealed[s]
        if self._frame is not None:
            in_frame = any(self._counters.get(s) == top_counter for s in self._frame)
            if in_frame or all(s < self.base or s in self._acked for s in self._frame):
                self._frame = None  # frame answered: compose the next one on the next poll


class TileRx:
    """Receiver side of one object: dedupe by (obj_id, seq), reassemble, answer with ACK bodies."""

    def __init__(self, obj_id: int, expect_sha256: bytes | None = None):
        self.obj_id = obj_id
        self.r = tiler.Reassembler(obj_id, expect_sha256)
        self.top_counter = 0

    def add(self, pkt: protocol.Packet) -> bool:
        """True if the tile was new. Bad/inconsistent tiles are ignored (False)."""
        try:
            return self.r.add(pkt.obj_id, pkt.seq, pkt.total, pkt.body)
        except tiler.TilerError:
            return False

    def note_counter(self, counter: int) -> None:
        """Record the highest counter in the latest batch of packets (new or duplicate): ACKs echo it so
        the sender can tell which frame was answered."""
        self.top_counter = counter

    @property
    def complete(self) -> bool:
        return self.r.complete

    def ack_body(self) -> bytes:
        return encode_ack(self.obj_id, self.r.base, self.top_counter, self.r.bitmap(ACK_BITS))

    def assemble(self) -> bytes:
        return self.r.assemble()


# ---------------------------------------------------------------- drivers

def pump_sender(sender, end: Endpoint, now: float) -> bool:
    """One tick for a Sender over an Endpoint: read back-channel, step, show any new frame.
    Returns ``sender.finished``."""
    inbound = []
    while (m := end.back.recv(0.0)) is not None:
        inbound.append(m)
    frame = sender.step(now, inbound)
    if frame:
        end.sink.show(frame, sender.profile)
    return sender.finished


def pump_receiver(receiver, end: Endpoint, now: float) -> bool:
    """One tick for a Receiver over an Endpoint: read frames, step, send replies on the back channel."""
    inbound: list[bytes] = []
    while (f := end.source.read(0.0, receiver.decode_profiles)) is not None:
        inbound.extend(f)
    for m in receiver.step(now, inbound):
        end.back.send(m)
    return receiver.finished


def run_pair(sender, receiver, channel, clock: ManualClock, *, max_time: float = 600.0, tick: float = 0.05,
             stop_on_finish: bool = True) -> float:
    """Drive both engines over a ``LoopChannel`` built with ``clock`` in virtual time. Returns the final
    virtual time. Stops when both engines are finished (or at ``max_time``); a finished Sender keeps
    being stepped until the receiver finishes so it can re-answer a repeated DONE."""
    while clock.now < max_time:
        pump_sender(sender, channel.s_end, clock.now)
        pump_receiver(receiver, channel.r_end, clock.now)
        if stop_on_finish and sender.finished and receiver.finished:
            break
        clock.advance(tick)
    return clock.now
