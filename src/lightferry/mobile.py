"""Phone TEST client for Lightferry (PLAN sections 11-12). Test tool only; never part of the real path.

This machine is the SENDER (handshake.Sender over archive.pack(path)). A phone is the RECEIVER's camera,
speaker and buttons; all Receiver logic (decode, keys, unpack) runs here, the phone never holds the key.

    lightferry mobile-serve --path PATH [--key-file F] [--host LAN_IP] [--port 8443] [--out DIR] [--remote-sender]
    python -m lightferry.mobile ...     (same as `lightferry mobile-serve ...`)
    lightferry.mobile.serve(path, key, host=None, port=8443, out_dir=None, backchannel='audio')

Security (PLAN section 12): binds the LAN interface only (``lan_ip()`` unless ``--host``), one WebSocket
client at a time, a fresh self-signed cert per run in a private temp dir, token on every request. Both VERIFY
codes are confirmed by hand on the pages (screen = S, phone = R).

Back-channel
  audio : each receiver reply is a 2-byte note token (handshake.note_token), played by the phone (WebAudio)
          with acoustic.encode_notes (~1.6 s) and heard by the sender's microphone (this machine's, via
          acoustic.NoteListener; or, with --remote-sender, the other machine's `lightferry send`).
  direct: receiver messages go straight to the sender (documented test shortcut).

Remote sender (``--remote-sender``)
  The sender is ANOTHER machine running ``lightferry send --no-camera --backchannel audio``; the phone scans
  that machine's screen. Only the Receiver runs here; its note tokens are what that machine listens for.
"""
from __future__ import annotations

import asyncio
import collections
import datetime as _dt
import hmac
import ipaddress
import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np


STATIC = Path(__file__).parent / "mobile_static"
FS = 48000
MAX_JPEG = 2_000_000


# ---------------------------------------------------------------- helpers

def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))  # UDP connect: no packet is sent
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


def make_self_signed(host: str, out_dir: Path | None = None) -> tuple[Path, Path, str]:
    """A new self-signed cert (SAN: host, localhost, 127.0.0.1) -> (cert, key, sha256 hex). Without ``out_dir``
    it goes into a fresh private dir (``mkdtemp``, 0700) that the caller removes; the key file is created 0600
    before anything is written to it, and never reused."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    out_dir = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="lightferry-cert-"))
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    cp, kp = out_dir / "cert.pem", out_dir / "key.pem"
    for p in (cp, kp):
        p.unlink(missing_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "lightferry-mobile-test")])
    sans = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    try:
        sans.append(x509.IPAddress(ipaddress.ip_address(host)))
    except ValueError:
        sans.append(x509.DNSName(host))
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - _dt.timedelta(days=1))
            .not_valid_after(now + _dt.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .sign(key, hashes.SHA256()))
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    fd = os.open(kp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    return cp, kp, cert.fingerprint(hashes.SHA256()).hex()


# ---------------------------------------------------------------- microphone

class Mic:
    """Records this machine's mic into a queue of float32 chunks: sounddevice, else arecord."""

    def __init__(self, fs: int = FS):
        self.fs = fs
        self.q: collections.deque = collections.deque(maxlen=2000)
        self.kind = ""
        self._stop = threading.Event()
        self._proc = None
        self._stream = None

    def open(self) -> None:
        try:
            import sounddevice as sd

            def cb(indata, frames, t, status):
                self.q.append(indata[:, 0].copy())

            self._stream = sd.InputStream(samplerate=self.fs, channels=1, dtype="float32", callback=cb)
            self._stream.start()
            self.kind = "sounddevice"
            return
        except Exception as e:  # noqa: BLE001
            err = f"sounddevice: {e}"
        try:
            self._proc = subprocess.Popen(
                ["arecord", "-q", "-f", "S16_LE", "-r", str(self.fs), "-c", "1", "-t", "raw"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"{err}; arecord: {e}") from e
        time.sleep(0.4)
        if self._proc.poll() is not None:
            msg = (self._proc.stderr.read() or b"").decode(errors="replace").strip()
            raise RuntimeError(f"{err}; arecord failed: {msg}")
        self.kind = "arecord"
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        n = 4800 * 2
        while not self._stop.is_set():
            b = self._proc.stdout.read(n)
            if not b:
                break
            self.q.append(np.frombuffer(b[: len(b) // 2 * 2], np.int16).astype(np.float32) / 32768)

    def drain(self) -> np.ndarray | None:
        chunks = []
        while self.q:
            chunks.append(self.q.popleft())
        return np.concatenate(chunks) if chunks else None

    def close(self):
        self._stop.set()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
        if self._proc is not None:
            self._proc.terminate()


# ---------------------------------------------------------------- session

class Session:
    def __init__(self, path, key: bytes, out_dir, backchannel: str, token: str, remote_sender: bool = False):
        import cv2

        self.cv2 = cv2
        self.path = path
        self.token = token
        self.key = key
        self.dest = Path(out_dir) if out_dir else Path("out") / "received"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.t_start = time.time()
        self.remote = remote_sender
        # a remote sender only hears audio: there is no in-process sender to hand messages to directly
        self.backchannel = "audio" if remote_sender or backchannel not in ("audio", "direct") else backchannel
        self.banner = ""
        self.mic: Mic | None = None
        self.sender = None    # created only when the phone user presses Start (see begin())
        self.receiver = None
        self.phase = "IDLE"   # IDLE until the first Start; then RUNNING
        self.gen = 0
        self.rlock = threading.Lock()
        self.s_inbound: collections.deque = collections.deque()
        self.frame_png = b""
        self.frame_v = 0
        self.detected = False
        self.tiles = 0
        self.pending: collections.deque = collections.deque()  # note tokens awaiting playback
        self.busy_until = 0.0
        self.ws = None
        self.msg_seq = 0
        self.listener = None
        self.listen_buf: list = []

    # -- session lifecycle
    def begin(self) -> None:
        """Create a FRESH Sender/Receiver pair (new nonces); timers start now. Cleans stale state."""
        from . import acoustic, archive, handshake
        from .link import LinkConfig, Profile, for_audio

        cfg = LinkConfig()
        if self.backchannel == "audio":  # audio messages take ~9 s: stretch resend timers
            cfg = for_audio(cfg)
        sender = None
        if not self.remote:
            blob, manifest = archive.pack(self.path)
            sender = handshake.Sender(self.key, manifest, blob, backchannel=self.backchannel, cfg=cfg,
                                      proposal=Profile(64, 10, 2))  # VERIFY: the screen page's buttons
        receiver = handshake.Receiver(self.key, self.dest, backchannel=self.backchannel, announce=False,
                                      cfg=cfg)  # VERIFY: the phone's buttons
        with self.rlock:
            self.gen += 1
            self.s_inbound.clear()
            self.pending.clear()
            self.busy_until = 0.0
            self.frame_png = b""
            self.frame_v += 1
            self.detected = False
            self.tiles = 0
            self.msg_seq = 0
            self.listen_buf = []
            if self.mic is not None:
                self.mic.drain()  # drop stale audio
            self.listener = acoustic.NoteListener(FS, accept=lambda t: sender is not None and t in sender.expected)
            self.sender, self.receiver = sender, receiver
            self.phase = "RUNNING"

    def ended(self) -> bool:
        s, r = self.sender, self.receiver
        if self.remote:
            return r is not None and r.finished
        return s is not None and r is not None and (s.finished or r.finished)

    # -- sender side
    def sender_tick(self):
        sender = self.sender
        if sender is None:
            return
        inbound = []
        while self.s_inbound:
            inbound.append(self.s_inbound.popleft())
        out = sender.step(time.monotonic(), inbound)
        if out:
            from . import channels
            img = channels.render_frame(out, sender.profile)
            ok, buf = self.cv2.imencode(".png", img)
            if ok:
                self.frame_png = buf.tobytes()
                self.frame_v += 1

    # -- receiver side
    def receiver_step(self, packets: list[bytes]) -> list[bytes]:
        with self.rlock:
            if self.receiver is None:
                return []
            out = self.receiver.step(time.monotonic(), packets)
        return out

    def handle_jpeg(self, data: bytes) -> list[bytes]:
        from . import vision
        img = self.cv2.imdecode(np.frombuffer(data, np.uint8), self.cv2.IMREAD_COLOR)
        packets: list[bytes] = []
        if self.receiver is None:
            return []
        if img is not None:
            self.nframes = getattr(self, "nframes", 0) + 1
            t0 = time.monotonic()
            warped = vision.detect_frame(img)
            self.detected = warped is not None
            if warped is not None:
                with self.rlock:
                    profiles = self.receiver.decode_profiles if self.receiver else []
                for p in profiles:
                    packets = vision.extract_tiles(warped, p.grid, p.tiles_n)
                    if packets:
                        break
            self.tiles = len(packets)
            if os.environ.get("LIGHTFERRY_DEBUG") == "1":
                from .protocol import PType, peek
                kinds = ",".join(PType(peek(p).ptype).name for p in packets)
                print(f"[phone] frame#{self.nframes} detect={'yes' if warped is not None else 'no'} [{kinds}] "
                      f"R={self.receiver.state if self.receiver else '-'} {time.monotonic()-t0:.2f}s", flush=True)
        return self.receiver_step(packets)

    def dispatch(self, out: list[bytes]) -> None:
        """Receiver outbound messages -> back-channel."""
        for m in out:
            if self.backchannel == "direct":
                self.s_inbound.append(m)
            elif m not in self.pending:
                self.pending.append(m)

    async def pump_audio(self):
        if self.phase != "RUNNING" or self.backchannel != "audio" or self.ws is None \
                or time.monotonic() < self.busy_until:
            return
        from . import acoustic
        if not self.pending:
            return
        token = self.pending.popleft()
        self.msg_seq = (self.msg_seq + 1) & 0xFFFF
        seq = self.msg_seq
        samples = acoustic.encode_notes(token, FS)
        pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
        self.busy_until = time.monotonic() + len(samples) / FS + 0.5
        print(f"[phone] playing reply #{seq} ({len(samples) / FS:.1f}s)", flush=True)
        try:
            await self.ws.send_bytes(b"A" + pcm)
        except Exception:  # noqa: BLE001
            pass

    async def mic_pump(self):
        from . import acoustic
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(0.5)
            lis = self.listener
            if self.backchannel != "audio" or self.mic is None or lis is None or self.phase != "RUNNING":
                if self.mic is not None and self.phase != "RUNNING":
                    self.mic.drain()
                continue
            x = self.mic.drain()
            if x is None:
                continue
            gen = self.gen
            msgs = await loop.run_in_executor(None, lis.feed, x)
            if gen != self.gen:
                continue  # session restarted while decoding: discard stale messages
            s = self.sender
            for t in msgs:
                if s is not None:
                    s.hear(t, time.monotonic())

    def set_backchannel(self, mode: str):
        if self.remote:
            self.banner = "Remote sender: the back-channel is always audio."
            return
        if mode == "audio" and self.mic is None:
            try:
                self.open_mic()
            except RuntimeError as e:
                self.banner = f"Mic unavailable ({e}); using DIRECT back-channel (test shortcut)."
                mode = "direct"
        self.backchannel = mode
        if mode == "direct":  # tokens can't be turned back into packets: the engines must restart
            self.pending.clear()

    def open_mic(self):
        m = Mic()
        m.open()
        self.mic = m

    # -- status
    def files(self) -> list[str]:
        if self.receiver is None:
            return []
        m = self.receiver.manifest or {}
        name = m.get("name", "")
        if m.get("kind") == "dir":
            rels = [f"{name}/{f['path']}" for f in m.get("files", [])]
        else:
            rels = [name] if name else []
        return [r for r in rels if (self.dest / r).is_file()]

    def status(self) -> dict:
        s, r = self.sender, self.receiver
        if r is None or (s is None and not self.remote):
            idle = {"state": "IDLE", "progress": [0, 0], "verify": None, "warning": "", "abort": None}
            return {
                "state": "IDLE", "ended": False, "backchannel": self.backchannel, "banner": self.banner,
                "mic": self.mic.kind if self.mic else None, "frame_v": self.frame_v,
                "message": "Waiting for phone \u2014 scan the QR and press Start",
                "sender": dict(idle),
                "receiver": {**idle, "detected": False, "tiles": 0, "done": False, "files": []},
            }
        done = r.state in ("DONE", "CLOSED")
        return {
            "state": "ENDED" if self.ended() else "RUNNING", "ended": self.ended(), "message": "",
            "backchannel": self.backchannel, "banner": self.banner,
            "mic": self.mic.kind if self.mic else None,
            "frame_v": self.frame_v,
            "sender": {"state": "REMOTE", "progress": [0, 0], "verify": None, "warning": "", "abort": None}
            if s is None else {"state": s.state, "progress": list(s.progress), "verify": s.verify_code,
                               "warning": s.warning, "abort": s.abort_reason},
            "receiver": {"state": r.state, "progress": list(r.progress), "verify": r.verify_code,
                         "warning": r.warning, "abort": r.abort_reason, "detected": self.detected,
                         "tiles": self.tiles, "done": done, "files": self.files() if done else []},
        }


# ---------------------------------------------------------------- web app

def _build_app(sess: Session):
    from aiohttp import web

    def authed(req) -> bool:
        t = req.query.get("t") or req.headers.get("X-Token", "")
        return hmac.compare_digest(t.encode(), sess.token.encode())

    @web.middleware
    async def guard(req, handler):
        if not authed(req):
            return web.Response(status=403, text="forbidden")
        return await handler(req)

    def page(name):
        async def h(req):
            return web.Response(body=(STATIC / name).read_bytes(), content_type="text/html",
                                headers={"Cache-Control": "no-store"})
        return h

    async def root(req):
        raise web.HTTPFound(f"/screen?t={sess.token}")

    async def frame(req):
        return web.Response(body=sess.frame_png, content_type="image/png", headers={"Cache-Control": "no-store"})

    async def qr(req):
        p = Path("out") / "phone-qr.png"
        if not p.is_file():
            raise web.HTTPNotFound()
        return web.Response(body=p.read_bytes(), content_type="image/png", headers={"Cache-Control": "no-store"})

    async def status(req):
        return web.json_response(sess.status())

    async def sender_answer(req):
        d = await req.json()
        if sess.sender is not None:
            sess.sender.operator_answer(bool(d.get("ok")))
        return web.json_response({"ok": True})

    async def set_bc(req):
        d = await req.json()
        sess.set_backchannel("direct" if d.get("mode") == "direct" else "audio")
        return web.json_response({"mode": sess.backchannel, "banner": sess.banner})

    async def restart(req):
        await asyncio.get_running_loop().run_in_executor(None, sess.begin)
        return web.json_response({"ok": True, "state": sess.phase})

    async def files(req):
        rel = req.match_info["p"]
        root_ = sess.dest.resolve()
        p = (root_ / rel).resolve()
        if root_ not in p.parents or not p.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(p)

    async def ws_handler(req):
        if sess.ws is not None and not sess.ws.closed:  # one client: never hand the session to a second one
            return web.Response(status=409, text="another client is connected")
        ws = web.WebSocketResponse(max_msg_size=MAX_JPEG, heartbeat=20)
        sess.ws = ws  # claimed before the handshake's await, so two racing clients can't both get in
        try:
            await ws.prepare(req)
        except Exception:
            sess.ws = None
            raise
        loop = asyncio.get_running_loop()
        busy = False
        push = asyncio.create_task(_push(ws))
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.BINARY:
                    if busy or len(msg.data) > MAX_JPEG:
                        continue
                    busy = True
                    try:
                        out = await loop.run_in_executor(None, sess.handle_jpeg, msg.data)
                        sess.dispatch(out)
                    finally:
                        busy = False
                elif msg.type == web.WSMsgType.TEXT:
                    try:
                        d = json.loads(msg.data)
                    except ValueError:
                        continue
                    if d.get("type") == "answer":
                        if sess.receiver is not None:
                            sess.receiver.operator_answer(bool(d.get("ok")))
                    elif d.get("type") == "start":
                        # phone pressed Start (camera granted) or the WS reconnected: begin if idle/finished
                        if sess.phase == "IDLE" or sess.ended():
                            await loop.run_in_executor(None, sess.begin)
                    elif d.get("type") == "restart":
                        await loop.run_in_executor(None, sess.begin)
        finally:
            push.cancel()
            if sess.ws is ws:
                sess.ws = None
        return ws

    async def _push(ws):
        while True:
            await ws.send_json({"type": "status", **sess.status()})
            await asyncio.sleep(0.3)

    app = web.Application(middlewares=[guard], client_max_size=MAX_JPEG)
    app.add_routes([
        web.get("/", root), web.get("/screen", page("screen.html")), web.get("/m", page("m.html")),
        web.get("/frame.png", frame), web.get("/qr.png", qr), web.get("/status.json", status), web.get("/ws", ws_handler),
        web.post("/api/sender_answer", sender_answer), web.post("/api/backchannel", set_bc),
        web.post("/api/restart", restart),
        web.get("/files/{p:.+}", files),
    ])
    return app


async def _background(sess: Session):
    async def sender_loop():
        while True:
            try:
                sess.sender_tick()
            except Exception as e:  # noqa: BLE001
                print("sender error:", e, file=sys.stderr)
            await asyncio.sleep(0.05)

    async def recv_tick():
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(0.3)
            out = await loop.run_in_executor(None, sess.receiver_step, [])
            sess.dispatch(out)
            await sess.pump_audio()

    await asyncio.gather(sender_loop(), recv_tick(), sess.mic_pump())


def _print_qr(url: str, png: Path) -> None:
    import segno
    q = segno.make(url, error="m")
    png.parent.mkdir(parents=True, exist_ok=True)
    q.save(str(png), scale=10, border=4)
    try:
        q.terminal(compact=os.environ.get("LIGHTFERRY_QR_COMPACT") == "1", border=2)
    except Exception:  # noqa: BLE001
        print("(QR print failed; see PNG)")


async def _main(path, key, host, port, out_dir, backchannel, stop_after, remote_sender=False):
    from aiohttp import web

    token = secrets.token_hex(16)
    sess = Session(path, key, out_dir, backchannel, token, remote_sender=remote_sender)
    if sess.backchannel == "audio" and not sess.remote:  # a remote sender listens with its own mic
        try:
            sess.open_mic()
        except RuntimeError as e:
            sess.backchannel = "direct"
            sess.banner = f"Mic unavailable ({e}); using DIRECT back-channel (test shortcut)."
            print("WARNING:", sess.banner)
    ip = host or lan_ip()  # PLAN 12: the LAN interface only, not 0.0.0.0
    cert, keyf, fp = make_self_signed(ip)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, keyf)
    shutil.rmtree(cert.parent, ignore_errors=True)  # loaded: nothing of the key stays on disk
    runner = web.AppRunner(_build_app(sess))
    await runner.setup()
    await web.TCPSite(runner, ip, port, ssl_context=ctx).start()
    phone = f"https://{ip}:{port}/m?t={token}"
    screen = f"https://{ip}:{port}/screen?t={token}"
    print("\nLightferry phone TEST client (not part of the real air-gapped path)")
    print(f"cert SHA-256: {fp}")
    print(f"back-channel: {sess.backchannel}" + (f" (mic via {sess.mic.kind})" if sess.mic else "")
          + (" (played by the phone to the remote sender's mic)" if sess.remote else ""))
    _print_qr(phone, Path("out") / "phone-qr.png")
    if sess.remote:
        print(f"\nPhone : {phone}\nQR PNG: out/phone-qr.png\nReceived files -> {sess.dest}\n"
              "1. On the sending machine run: lightferry send --path <file-or-folder> --key-file <same key> "
              "--no-camera --backchannel audio\n"
              "2. Open the Phone URL (accept the certificate warning), press Start, and point the phone at that "
              "machine's Lightferry window, speaker towards its microphone.\n", flush=True)
    else:
        print(f"\nPhone : {phone}\nScreen: {screen}\nQR PNG: out/phone-qr.png\nReceived files -> {sess.dest}\n"
              "1. Open the Screen URL in a browser on THIS machine: the codes the phone scans are drawn there.\n"
              "2. Open the Phone URL (accept the certificate warning), press Start, and point the phone at that "
              "page.\n", flush=True)
    bg = asyncio.create_task(_background(sess))  # runs for the whole process; sessions come and go via begin()
    try:
        if stop_after:
            await asyncio.sleep(stop_after)
        else:
            await asyncio.Event().wait()
    finally:
        bg.cancel()
        if sess.mic:
            sess.mic.close()
        await runner.cleanup()


def serve(path, key, host: str | None = None, port: int = 8443, out_dir=None, backchannel: str = "audio",
          _stop_after: float | None = None, remote_sender: bool = False) -> None:
    """Blocking. `key` is the shared secret (bytes). `host` None = this machine's LAN address."""
    try:
        asyncio.run(_main(path, key, host, port, out_dir, backchannel, _stop_after, remote_sender))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":  # `python -m lightferry.mobile ...` == `lightferry mobile-serve ...`
    from .cli import main
    sys.exit(main(["mobile-serve", *sys.argv[1:]]))
