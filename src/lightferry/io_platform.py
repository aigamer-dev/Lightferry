"""Platform I/O: webcam source, on-screen sink, acoustic back-channel adapters (PLAN sections 2, 11).

* ``WebcamSource(FrameSource)``  cv2.VideoCapture with a reader thread (always the latest frame, stale
  frames dropped). ``read()`` yields at most one decoded frame per pump (every second call returns None) so a
  ``while read() is not None`` loop terminates.
* ``ScreenSink(FrameSink)``      cv2.imshow window; ``f`` toggles fullscreen, ``q``/Esc raises KeyboardInterrupt.
  ``poll()`` must run every tick: a window that is not pumped is flagged "not responding" by the desktop.
* ``CameraBack(BackChannel)``    camera back-channel: R shows its replies on a screen, S reads them with its webcam.
* ``AudioBack(BackChannel)``     acoustic back-channel adapter (R speaker -> S microphone) over ``acoustic``.

Audio: R never sends packets over audio. Each reply is a 2-byte note token (handshake.note_token: keyed with
the session's R->S key, it names the S packet it answers), played as 4 notes after a chirp, twice (~1.6 s).
"""
from __future__ import annotations

import os
import queue
import sys
from pathlib import Path
import shutil
import subprocess
import threading
import time

import numpy as np

from .channels import BackChannel, ChannelError, FrameSink, FrameSource, parse_frame, render_frame

WIN = "Lightferry"


class NoDisplayError(ChannelError):
    pass


# ---------------------------------------------------------------- webcam

class WebcamSource(FrameSource):
    def __init__(self, index: int = 0, width: int = 1280, height: int = 720, fps: int = 30):
        import cv2
        self._cv2 = cv2
        cap = cv2.VideoCapture(index)
        if not cap.isOpened():
            cap.release()
            raise ChannelError(f"no camera available at index {index} (try --camera-index N, or "
                               "--camera dir:<path>). Is another program using it?")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, fps)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        self._cap = cap
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._img = None
        self._n = 0
        self._seen = 0
        self._stop = False
        self._skip = False
        self._fails = 0
        self.last_img, self.frames_read, self.detected = None, 0, False
        self.detections, self.packets_decoded = 0, 0
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        t0 = time.monotonic()
        while self._n == 0 and time.monotonic() - t0 < 5.0:
            time.sleep(0.05)
        if self._n == 0:
            self.close()
            raise ChannelError("camera opened but delivered no frames")

    def _run(self) -> None:
        while not self._stop:
            ok, img = self._cap.read()
            if not ok:
                self._fails += 1
                time.sleep(0.02)
                continue
            with self._cv:
                self._img, self._n = img, self._n + 1
                self._cv.notify_all()

    def latest(self, timeout_s: float = 0.0):
        """Newest frame not yet returned (or None)."""
        with self._cv:
            if self._n == self._seen and timeout_s > 0:
                self._cv.wait(timeout_s)
            if self._n == self._seen:
                return None
            self._seen = self._n
            return self._img

    def read(self, timeout_s: float = 0.0, profiles=None):
        if self._skip:  # yield control after each decoded frame
            self._skip = False
            return None
        img = self.latest(timeout_s)
        if img is None:
            return None
        from . import vision
        self.last_img, self.frames_read = img, self.frames_read + 1
        warped = vision.detect_frame(img)
        self.detected = warped is not None
        pk = []
        if warped is not None:
            self.detections += 1
            self.last_warped = warped
            for p in profiles or ():
                pk = vision.extract_tiles(warped, p.grid, p.tiles_n)
                if pk:
                    break
        if pk:
            self.packets_decoded += len(pk)
            self._skip = True
            return pk
        if warped is not None and os.environ.get("LIGHTFERRY_DEBUG") == "1":
            self._dump_failed(img, warped)
        return None

    def _dump_failed(self, img, warped) -> None:
        """LIGHTFERRY_DEBUG=1: save a camera frame and its warped code whenever a code was found but no packet
        decoded (at most one pair every 2 s, 20 pairs in total) into ./lightferry-debug/."""
        now = time.monotonic()
        if now - getattr(self, "_dump_t", -9.0) < 2.0 or getattr(self, "_dump_n", 0) >= 20:
            return
        self._dump_t, self._dump_n = now, getattr(self, "_dump_n", 0) + 1
        d = Path("lightferry-debug")
        d.mkdir(exist_ok=True)
        self._cv2.imwrite(str(d / f"cam-{self._dump_n:02d}.png"), img)
        self._cv2.imwrite(str(d / f"warped-{self._dump_n:02d}.png"), warped)

    def diagnostics(self) -> str:
        """One line for the CLI: delivered resolution and how sharp the last detected code was."""
        cv2 = self._cv2
        w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        s = f"camera {w}x{h}"
        wp = getattr(self, "last_warped", None)
        if wp is not None:
            g = wp if wp.ndim == 2 else cv2.cvtColor(wp, cv2.COLOR_BGR2GRAY)
            s += (f", code {g.shape[1]}x{g.shape[0]}px, sharpness {cv2.Laplacian(g, cv2.CV_64F).var():.0f}, "
                  f"contrast {int(g.max()) - int(g.min())}")
        return s

    def close(self) -> None:
        self._stop = True
        try:
            self._t.join(1.0)
        except Exception:
            pass
        self._cap.release()


# ---------------------------------------------------------------- screen

class ScreenSink(FrameSink):
    def __init__(self, fullscreen: bool = False, title: str = WIN):
        import cv2
        self._cv2 = cv2
        self.title = title
        self.full = fullscreen
        self._img = None
        try:
            cv2.namedWindow(title, cv2.WINDOW_NORMAL)
            self._set_full()
            cv2.waitKey(1)
        except cv2.error as e:
            raise NoDisplayError(f"cannot open a display window ({str(e).splitlines()[0]}). "
                                 "Is a display available (DISPLAY/WAYLAND_DISPLAY set)? "
                                 "headless opencv builds cannot show windows.") from e

    def _set_full(self) -> None:
        c = self._cv2
        c.setWindowProperty(self.title, c.WND_PROP_FULLSCREEN, c.WINDOW_FULLSCREEN if self.full else c.WINDOW_NORMAL)

    def show(self, packets, profile=None) -> None:
        self._img = render_frame(packets, profile)
        self._cv2.imshow(self.title, self._img)
        self.poll()

    def preview(self, img, text: str) -> None:
        """Show the camera image with a status line (receiver, audio mode: the window is otherwise blank)."""
        c = self._cv2
        v = img.copy()
        h, w = v.shape[:2]
        c.rectangle(v, (0, 0), (w, 28), (0, 0, 0), -1)
        c.putText(v, text, (6, 20), c.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 1, c.LINE_AA)
        c.imshow(self.title, v)

    def poll(self) -> None:
        """Pump the GUI event loop; handle keys. Call every tick."""
        k = self._cv2.waitKey(1) & 0xFF
        if k in (ord("q"), 27):
            raise KeyboardInterrupt
        if k == ord("f"):
            self.full = not self.full
            self._set_full()

    def close(self) -> None:
        try:
            self._cv2.destroyWindow(self.title)
        except Exception:
            pass


# ---------------------------------------------------------------- camera back-channel

class CameraBack(BackChannel):
    """R side: pass ``sink`` (replies are shown, several cycle every ``cycle_s``). S side: pass ``source``
    (replies are read from webcam frames with the bootstrap profile). Call ``tick(now)`` every loop."""

    def __init__(self, sink: FrameSink | None = None, source: FrameSource | None = None, profile=None,
                 cycle_s: float = 0.4):
        from .link import BOOTSTRAP
        self.sink, self.source = sink, source
        self.profile = profile or BOOTSTRAP
        self.cycle_s = cycle_s
        self._pending: list[bytes] = []
        self._q: list[bytes] = []
        self._i = 0
        self._t = 0.0

    def send(self, body: bytes) -> None:
        self._pending.append(body)

    def recv(self, timeout_s: float = 0.0):
        if self.source is None:
            return None
        pk = self.source.read(timeout_s, [self.profile])
        return pk[0] if pk else None

    def tick(self, now: float) -> None:
        if self.sink is None:
            return
        if self._pending:
            self._q, self._pending, self._i, self._t = self._pending, [], 0, now
            self.sink.show([self._q[0]], self.profile)
        elif len(self._q) > 1 and now - self._t >= self.cycle_s:
            self._i, self._t = (self._i + 1) % len(self._q), now
            self.sink.show([self._q[self._i]], self.profile)


# ---------------------------------------------------------------- audio I/O

class _SoundDeviceIO:
    name = "sounddevice"

    def __init__(self, fs: int, device=None):
        import sounddevice as sd
        self.sd, self.fs, self.device = sd, fs, device

    def play(self, samples: np.ndarray) -> None:
        self.sd.play(samples, self.fs, device=self.device, blocking=True)

    def capture(self, put, stop: threading.Event) -> None:
        with self.sd.InputStream(samplerate=self.fs, channels=1, dtype="float32", device=self.device,
                                 callback=lambda d, n, t, s: put(d[:, 0].copy())):
            while not stop.is_set():
                time.sleep(0.1)


class _AlsaIO:
    name = "arecord/aplay"

    def __init__(self, fs: int, device=None):
        self.fs, self.device = fs, device

    def _dev(self) -> list[str]:
        return ["-D", self.device] if self.device else []

    def play(self, samples: np.ndarray) -> None:
        pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
        subprocess.run(["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(self.fs), "-c", "1", *self._dev(), "-"],
                       input=pcm, check=False)

    def capture(self, put, stop: threading.Event) -> None:
        p = subprocess.Popen(["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(self.fs), "-c", "1",
                              *self._dev(), "-"], stdout=subprocess.PIPE)
        n = self.fs // 4 * 2
        try:
            while not stop.is_set():
                b = p.stdout.read(n)
                if not b:
                    break
                put(np.frombuffer(b[: len(b) // 2 * 2], "<i2").astype(np.float32) / 32768.0)
        finally:
            p.kill()


def open_audio_io(fs: int = 48000, device=None):
    try:
        return _SoundDeviceIO(fs, device)
    except Exception:
        pass
    if shutil.which("arecord") and shutil.which("aplay"):
        return _AlsaIO(fs, device)
    raise ChannelError("no audio backend: install sounddevice (pip install 'lightferry[audio]') or, on Linux, "
                       "alsa-utils (arecord/aplay)")


class AudioBack(BackChannel):
    """Audio back-channel for note tokens (handshake.note_token, 2 bytes = 4 notes, ~1.6 s).

    role 'r': ``send(token)`` plays it (worker thread; a token already waiting is not queued twice).
    role 's': ``recv()`` returns tokens heard on the microphone that ``accept(token)`` approves (set it to
    ``lambda t: t in sender.expected``; that check is also what rejects noise). ``io`` is any object with
    play(samples)/capture(put, stop)."""

    def __init__(self, role: str, io=None, fs: int = 48000, repeat: int = 2):
        from . import acoustic
        self.ac, self.role, self.fs, self.repeat = acoustic, role, fs, repeat
        self.accept = lambda t: False
        self.io = io or open_audio_io(fs)
        self._stop = threading.Event()
        self._out: queue.Queue = queue.Queue()
        self._in: queue.Queue = queue.Queue()
        self._audio: queue.Queue = queue.Queue()
        self._queued: set[bytes] = set()
        self._qlock = threading.Lock()
        self._threads = []
        if role == "r":
            self._threads.append(threading.Thread(target=self._play_loop, daemon=True))
        else:
            self._threads.append(threading.Thread(target=lambda: self.io.capture(self._audio.put, self._stop), daemon=True))
            self._threads.append(threading.Thread(target=self._listen_loop, daemon=True))
        for t in self._threads:
            t.start()

    # R
    def send(self, token: bytes) -> None:
        if self.role != "r":
            return
        with self._qlock:
            if token in self._queued:
                return
            self._queued.add(token)
        self._out.put(token)

    def _play_loop(self) -> None:
        while not self._stop.is_set():
            try:
                token = self._out.get(timeout=0.2)
            except queue.Empty:
                continue
            self.io.play(self.ac.encode_notes(token, self.fs, self.repeat))
            with self._qlock:
                self._queued.discard(token)

    # S
    def _listen_loop(self) -> None:
        lst = self.ac.NoteListener(self.fs, repeat=self.repeat, accept=lambda t: self.accept(t))
        limited = False
        while not self._stop.is_set():
            try:
                x = self._audio.get(timeout=0.2)
            except queue.Empty:
                continue
            for t in lst.feed(x):
                self._in.put(t)
            if lst.limited != limited:  # PLAN 11: log the rate limit, ignore tokens while it lasts
                limited = lst.limited
                print("\nwarning: many unexpected audio tokens in the last minute; ignoring audio for now"
                      if limited else "\naudio rate limit lifted", file=sys.stderr, flush=True)

    def recv(self, timeout_s: float = 0.0):
        try:
            return self._in.get(timeout=timeout_s) if timeout_s > 0 else self._in.get_nowait()
        except queue.Empty:
            return None

    def close(self) -> None:
        self._stop.set()
