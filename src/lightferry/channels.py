"""Transports between Sender and Receiver (PLAN section 9).

Data direction (S -> R) carries *frames*: ``list[bytes]`` of packet bytes, one list per displayed frame.
Back direction (R -> S) carries small packet ``bytes`` (acks etc.), so it can later be backed by acoustic.py.

* ``FrameSink.show(packets, profile=None)``; ``FrameSource.read(timeout_s, profiles=None) -> list[bytes] | None``
* ``BackChannel.send(body)``; ``BackChannel.recv(timeout_s) -> bytes | None``
* ``Endpoint(role, sink, source, back)`` the pieces one side holds
* ``LoopChannel`` in-process, bytes mode (default) or image mode (through vision), with ``Impairment`` hooks
* ``DirChannel`` two processes, atomic PNG frame files + ``.bin`` back-channel files
* ``ManualClock`` virtual time for deterministic tests; ``open_channel(spec, role)``

``profiles`` / ``profile`` are objects with ``grid``, ``tiles_n``, ``module_px`` (``link.Profile``); they are
only needed for image mode and are ignored by bytes mode.
"""
from __future__ import annotations

import heapq
import itertools
import os
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence


class ChannelError(Exception):
    pass


class FrameSink(ABC):
    @abstractmethod
    def show(self, packets: Sequence[bytes], profile=None) -> None:
        """Display one frame holding these packets (replaces the previous frame)."""


class FrameSource(ABC):
    @abstractmethod
    def read(self, timeout_s: float = 0.0, profiles=None) -> list[bytes] | None:
        """Next observed frame's packets (CRC-good payloads), or None if nothing arrived."""


class BackChannel(ABC):
    @abstractmethod
    def send(self, body: bytes) -> None: ...

    @abstractmethod
    def recv(self, timeout_s: float = 0.0) -> bytes | None: ...


@dataclass
class Endpoint:
    """What one side holds: S uses ``sink`` + ``back.recv``; R uses ``source`` + ``back.send``."""
    role: str
    sink: FrameSink | None = None
    source: FrameSource | None = None
    back: BackChannel | None = None


class ManualClock:
    """Virtual clock: ``clock()`` returns ``now``; the driver advances it."""

    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


# ---------------------------------------------------------------- image adapters (thin, lazy)

def render_frame(packets: Sequence[bytes], profile):
    """packets -> grayscale frame image via vision.build_frame."""
    if profile is None:
        raise ChannelError("image mode needs a profile")
    from . import vision
    img, _ = vision.build_frame(list(packets), profile.grid, profile.tiles_n, profile.module_px)
    return img


def parse_frame(img, profiles) -> list[bytes]:
    """Frame image -> packets via vision.detect_frame / extract_tiles, trying each profile."""
    from . import vision
    warped = vision.detect_frame(img)
    if warped is None:
        return []
    for p in profiles or ():
        out = vision.extract_tiles(warped, p.grid, p.tiles_n)
        if out:
            return out
    return []


# ---------------------------------------------------------------- impairments

@dataclass
class Impairment:
    """Seeded channel damage. ``drop``/``corrupt``/``dup`` are per packet (frames) or per message (back);
    ``frame_drop`` loses a whole frame; delivery time = now + ``delay`` + U(0, ``jitter``) so a non-zero
    jitter reorders. Corruption flips random bytes (the packet then fails its MAC, as a CRC miss would)."""
    drop: float = 0.0
    corrupt: float = 0.0
    dup: float = 0.0
    frame_drop: float = 0.0
    delay: float = 0.0
    jitter: float = 0.0
    seed: int = 0
    _rng: random.Random = field(init=False, repr=False, default=None)

    def __post_init__(self):
        self._rng = random.Random(self.seed)

    def _chance(self, p: float) -> bool:
        return p > 0 and self._rng.random() < p

    def mangle(self, data: bytes) -> bytes | None:
        """Apply drop/corrupt to one packet/message. None = dropped."""
        if self._chance(self.drop):
            return None
        if self._chance(self.corrupt) and data:
            b = bytearray(data)
            for _ in range(self._rng.randint(1, 4)):
                b[self._rng.randrange(len(b))] ^= 1 << self._rng.randrange(8)
            return bytes(b)
        return data

    def frame(self, packets: Sequence[bytes]) -> list[bytes] | None:
        if self._chance(self.frame_drop):
            return None
        out = [m for m in (self.mangle(p) for p in packets) if m is not None]
        return out or None

    def latency(self) -> float:
        return self.delay + (self._rng.uniform(0, self.jitter) if self.jitter else 0.0)

    def copies(self) -> int:
        return 2 if self._chance(self.dup) else 1


class _Timed:
    """Delivery queue ordered by ready time (ties by insertion)."""

    def __init__(self, clock: Callable[[], float]):
        self.clock = clock
        self._h: list = []
        self._n = itertools.count()

    def put(self, ready: float, item) -> None:
        heapq.heappush(self._h, (ready, next(self._n), item))

    def get(self, timeout_s: float):
        end = self.clock() + timeout_s
        while True:
            if self._h and self._h[0][0] <= self.clock():
                return heapq.heappop(self._h)[2]
            if self.clock is not time.monotonic or self.clock() >= end:
                return None
            time.sleep(0.002)

    def __len__(self) -> int:
        return len(self._h)


class _LoopSink(FrameSink):
    def __init__(self, ch: "LoopChannel"):
        self.ch = ch

    def show(self, packets, profile=None) -> None:
        ch = self.ch
        ch.frames_shown += 1
        pk = ch.imp_fwd.frame(packets)
        if pk is None:
            return
        item = render_frame(pk, profile) if ch.image_mode else pk
        for _ in range(ch.imp_fwd.copies()):
            ch._fwd.put(ch.clock() + ch.imp_fwd.latency(), item)


class _LoopSource(FrameSource):
    def __init__(self, ch: "LoopChannel"):
        self.ch = ch

    def read(self, timeout_s: float = 0.0, profiles=None):
        item = self.ch._fwd.get(timeout_s)
        if item is None:
            return None
        return parse_frame(item, profiles) if self.ch.image_mode else list(item)


class _LoopBack(BackChannel):
    def __init__(self, ch: "LoopChannel", role: str):
        self.ch, self.role = ch, role

    def send(self, body: bytes) -> None:
        ch = self.ch
        m = ch.imp_back.mangle(bytes(body))
        if m is None:
            return
        for _ in range(ch.imp_back.copies()):
            ch._back.put(ch.clock() + ch.imp_back.latency(), m)

    def recv(self, timeout_s: float = 0.0):
        return self.ch._back.get(timeout_s)


class LoopChannel:
    """In-process channel pair. ``s_end`` / ``r_end`` are the two ``Endpoint``s.

    ``image_mode=True`` renders every frame to an image with vision.build_frame and decodes it with
    vision.detect_frame/extract_tiles (the reader must pass ``profiles``); default bytes mode carries
    the packet lists directly. Reads never block under a ``ManualClock``.
    """

    def __init__(self, *, image_mode: bool = False, fwd: Impairment | None = None,
                 back: Impairment | None = None, clock: Callable[[], float] = time.monotonic):
        self.image_mode = image_mode
        self.imp_fwd = fwd or Impairment()
        self.imp_back = back or Impairment(seed=1)
        self.clock = clock
        self._fwd = _Timed(clock)
        self._back = _Timed(clock)
        self.frames_shown = 0
        self.s_end = Endpoint("s", sink=_LoopSink(self), back=_LoopBack(self, "s"))
        self.r_end = Endpoint("r", source=_LoopSource(self), back=_LoopBack(self, "r"))


# ---------------------------------------------------------------- directory channel (two processes)

def _retry_fs(fn, total_s: float = 1.0):
    """Windows can raise PermissionError while the peer holds the file: retry 10 ms doubling."""
    delay, spent = 0.01, 0.0
    while True:
        try:
            return fn()
        except PermissionError:
            if spent >= total_s:
                raise
            time.sleep(delay)
            spent += delay
            delay *= 2


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".tmp-{os.getpid()}-{time.time_ns()}-{next(_SEQ)}")
    tmp.write_bytes(data)
    _retry_fs(lambda: os.replace(tmp, path))


_SEQ = itertools.count()


def _seq_name(ext: str) -> str:
    return f"{time.time_ns():020d}-{os.getpid()}-{next(_SEQ):08d}{ext}"


class _DirSink(FrameSink):
    def __init__(self, folder: Path):
        self.folder = folder

    def show(self, packets, profile=None) -> None:
        import cv2
        ok, png = cv2.imencode(".png", render_frame(packets, profile))
        if not ok:
            raise ChannelError("png encode failed")
        _atomic_write(self.folder / _seq_name(".png"), png.tobytes())


class _DirSource(FrameSource):
    """Takes the newest frame file (a screen shows only the latest), deletes the consumed ones."""

    def __init__(self, folder: Path, poll_s: float = 0.01):
        self.folder, self.poll_s = folder, poll_s

    def read(self, timeout_s: float = 0.0, profiles=None):
        import cv2
        import numpy as np
        end = time.monotonic() + timeout_s
        while True:
            files = sorted(p for p in self.folder.glob("*.png") if not p.name.startswith("."))
            if files:
                newest = files[-1]
                data = _retry_fs(newest.read_bytes)
                for p in files:
                    try:
                        _retry_fs(p.unlink)
                    except FileNotFoundError:
                        pass
                img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
                return parse_frame(img, profiles) if img is not None else []
            if time.monotonic() >= end:
                return None
            time.sleep(self.poll_s)


class _DirBack(BackChannel):
    """send -> ``out_dir``; recv <- ``in_dir`` (oldest first, each message consumed once)."""

    def __init__(self, out_dir: Path, in_dir: Path, poll_s: float = 0.01):
        self.out_dir, self.in_dir, self.poll_s = out_dir, in_dir, poll_s

    def send(self, body: bytes) -> None:
        _atomic_write(self.out_dir / _seq_name(".bin"), bytes(body))

    def recv(self, timeout_s: float = 0.0):
        end = time.monotonic() + timeout_s
        while True:
            files = sorted(p for p in self.in_dir.glob("*.bin") if not p.name.startswith("."))
            if files:
                data = _retry_fs(files[0].read_bytes)
                try:
                    _retry_fs(files[0].unlink)
                except FileNotFoundError:
                    pass
                return data
            if time.monotonic() >= end:
                return None
            time.sleep(self.poll_s)


def DirChannel(root: str | os.PathLike, role: str) -> Endpoint:
    """Endpoint for one process of a two-process dir channel: ``<root>/s/*.png`` carries S's frames,
    ``<root>/r/*.bin`` carries R's back-channel messages. Image based, so it needs OpenCV + vision."""
    if role not in ("s", "r"):
        raise ChannelError("role must be 's' or 'r'")
    root = Path(root)
    s_dir, r_dir = root / "s", root / "r"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for d in (s_dir, r_dir):
        d.mkdir(exist_ok=True, mode=0o700)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    if role == "s":
        return Endpoint("s", sink=_DirSink(s_dir), back=_DirBack(s_dir, r_dir))
    return Endpoint("r", source=_DirSource(s_dir), back=_DirBack(r_dir, s_dir))


def open_channel(spec: str, role: str) -> Endpoint:
    """From a ``--camera`` spec. ``loop`` is in-process only (use LoopChannel directly)."""
    if spec.startswith("dir:"):
        return DirChannel(spec[4:], role)
    if spec == "loop":
        raise ChannelError("'loop' is in-process only; construct LoopChannel directly")
    raise NotImplementedError(f"channel {spec!r} not implemented yet (webcam/screen backend pending)")
