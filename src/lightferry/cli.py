"""Lightferry command line (PLAN section 2).

    lightferry send    --path=<file-or-folder> [options]
    lightferry receive --path=<destination dir> [options]
    (the older --send / --receive flags still work)
    lightferry keygen [--out FILE] | selftest | bench [...] | mobile-serve --path P [--port N] [--remote-sender]
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
import tempfile
import threading
import time
from pathlib import Path

SUBCOMMANDS = ("keygen", "selftest", "bench", "mobile-serve")
MODES = ("send", "receive")  # `lightferry send ...` == `lightferry --send ...`


def _err(msg: str) -> int:
    print(f"lightferry: error: {msg}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------- keys

def load_key(key_file: str | None, key_hex: str | None) -> bytes:
    from .protocol import MIN_PSK_LEN
    if key_hex:
        print("warning: --key on the command line is visible in the process list; prefer --key-file or "
              "LIGHTFERRY_KEY", file=sys.stderr)
        src = key_hex
    elif key_file:
        src = "@" + key_file
    else:
        src = os.environ.get("LIGHTFERRY_KEY", "")
        if not src:
            raise ValueError("no key: use --key-file FILE, --key HEX or LIGHTFERRY_KEY (create one with `lightferry keygen`)")
    if src.startswith("@"):
        raw = Path(src[1:]).expanduser().read_bytes()
        try:
            key = bytes.fromhex(raw.decode("ascii").strip())
        except (UnicodeDecodeError, ValueError):
            key = raw
    else:
        key = bytes.fromhex(src.strip())
    if len(key) < MIN_PSK_LEN:
        raise ValueError(f"key too short ({len(key)} bytes, need >= {MIN_PSK_LEN})")
    return key


def cmd_keygen(a) -> int:
    out = Path(a.out)
    if out.exists():
        return _err(f"{out} exists; refusing to overwrite")
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(secrets.token_bytes(32))
    print(f"wrote 32-byte key to {out} (mode 600). Move it to the other machine by hand; never commit it.")
    return 0


# ---------------------------------------------------------------- helpers

def parse_tiles(s: str | None) -> int | None:
    if s is None:
        return None
    n = s.lower().split("x")
    if len(set(n)) != 1:
        raise ValueError("--tiles must be square, e.g. 3 or 3x3")
    t = int(n[0])
    if not 1 <= t <= 4:
        raise ValueError("--tiles must be 1..4")
    return t


def _status(eng, last: dict, role: str) -> None:
    done, total = eng.progress
    key = (eng.state, done, total)
    now = time.monotonic()
    if key != last.get("k") and (eng.state != last.get("s") or now - last.get("t", 0) > 0.5):
        pct = f" {done}/{total} tiles ({100 * done // total}%)" if total else ""
        print(f"[{role}] {eng.state}{pct}", end="\r" if total and eng.state == "DATA" else "\n", flush=True)
        last.update(k=key, s=eng.state, t=now)
    if eng.verify_code and eng.verify_code != last.get("v") and eng.auto_confirm:  # test channels: logged only
        last["v"] = eng.verify_code
        print(f"\n[{role}] verify code {eng.verify_code} (confirmed automatically: test channel)", flush=True)
    if eng.warning and eng.warning != last.get("w"):
        last["w"] = eng.warning
        print(f"\n[{role}] WARNING: {eng.warning}", flush=True)


def _finish(eng, role: str) -> int:
    print()
    if eng.state == "CLOSED":
        if role == "S" and (eng.result or {}).get("confirmed") is False:
            print(f"[{role}] transfer complete (unconfirmed: only the receiver's 16-bit audio token says so; "
                  "check the receiver's console).")
        else:
            print(f"[{role}] transfer complete.")
        return 0
    print(f"[{role}] ABORTED: {eng.abort_reason}")
    return 1


def operator_prompt(role: str):
    """VERIFY on real hardware (PLAN section 6 step 4): show the code and ask y/N on the console. Returns a
    ``confirm(code)`` for the engines: it starts the question on a thread and answers via
    ``engine.operator_answer`` so the screen and camera keep running meanwhile. No answer -> VERIFY_TIMEOUT."""
    box: dict = {}

    def confirm(code: str):
        def ask():
            try:
                ans = input(f"\n[{role}] VERIFY: does the other machine show {code}? [y/N] ")
            except EOFError:
                ans = ""
            box["eng"].operator_answer(ans.strip().lower() in ("y", "yes"))
        threading.Thread(target=ask, daemon=True).start()
        return None  # pending
    return confirm, box


def _audio(a, role: str) -> bool:
    if role == "s":
        return a.backchannel == "audio" or (a.backchannel == "auto" and a.no_camera)
    return a.backchannel == "audio"


def build_endpoint(a, role: str, psk: bytes):
    """-> (Endpoint, back, closers). Real hardware or dir:."""
    from . import channels, io_platform as iop
    closers = []
    spec = a.camera
    if spec == "loop":
        raise ValueError("--camera loop is in-process only; use `lightferry selftest`")
    if spec.startswith("dir:"):
        if _audio(a, role):  # dir: carries packets only; nothing would ever deliver R's note tokens
            raise ValueError("--camera dir: works only with the camera back-channel; drop --backchannel audio / "
                             "--no-camera")
        return channels.open_channel(spec, role), None, closers
    if spec != "webcam":
        raise ValueError("--camera must be webcam, dir:<path> or loop")
    audio = _audio(a, role)
    screen = iop.ScreenSink(a.fullscreen)
    closers.append(screen)
    if role == "s":
        if audio:
            back = iop.AudioBack("s")
            closers.append(back)
            return channels.Endpoint("s", sink=screen, back=back), screen, closers  # pumped every tick
        cam = iop.WebcamSource(a.camera_index)
        closers.append(cam)
        back = iop.CameraBack(source=cam)
        return channels.Endpoint("s", sink=screen, back=back), screen, closers
    cam = iop.WebcamSource(a.camera_index)
    closers.append(cam)
    if audio:
        back = iop.AudioBack("r")
        closers.append(back)
    else:
        back = iop.CameraBack(sink=screen)
    return channels.Endpoint("r", source=cam, back=back), screen, closers


def _tick_io(end, screen, now) -> None:
    if hasattr(end.back, "tick"):
        end.back.tick(now)
    if screen is not None:
        screen.poll()


# ---------------------------------------------------------------- send / receive

def _auto(a) -> bool:
    """VERIFY is confirmed automatically only on test channels (dir:, or --yes, which needs one)."""
    return a.yes or a.camera != "webcam"


def run_send(a, psk: bytes) -> int:
    from . import archive, handshake as hs
    from .link import LinkConfig, Profile, for_audio
    blob, manifest = archive.pack(a.path, a.compress)
    print(f"packed {manifest['name']}: {len(manifest['files'])} files, {manifest['raw_size']} B -> "
          f"{len(blob)} B ({manifest['compression']})")
    audio = _audio(a, "s")
    # audio means the receiver is a phone or webcam filming from a distance: one large tile per frame reads
    # reliably, while 3x3 tiles shrink each module below what such a camera resolves
    prop = Profile(a.grid or 64, a.module_px or 6, a.tiles or (1 if audio else 3))
    cfg = LinkConfig(timeout=a.timeout, retries=a.retries)
    confirm, box = operator_prompt("S")
    end, screen, closers = build_endpoint(a, "s", psk)
    snd = box["eng"] = hs.Sender(psk, manifest, blob, cfg=for_audio(cfg) if audio else cfg, proposal=prop,
                                 backchannel="audio" if audio else "camera", auto_confirm=_auto(a),
                                 confirm=confirm)
    notes = hasattr(end.back, "accept")  # audio: R answers with note tokens, checked by the engine
    if notes:
        end.back.accept = lambda t: t in snd.expected
    print("Sender running: point the receiver's camera at this screen. Keys in its window: f fullscreen, q quit.")
    last: dict = {}
    try:
        while True:
            now = time.monotonic()
            inbound = []
            while (m := end.back.recv(0.0)) is not None:
                if not notes:
                    inbound.append(m)
                elif snd.hear(m, now):  # the only sign the phone's audio got through: say so
                    print(f"\n[S] heard the receiver's notes", flush=True)
            frame = snd.step(now, inbound)
            if frame:
                end.sink.show(frame, snd.profile)
            _tick_io(end, screen, now)
            _status(snd, last, "S")
            if snd.finished:
                break
            time.sleep(0.02)
        t_end = time.monotonic() + snd.linger_s
        while time.monotonic() < t_end:  # keep DONE_ACK/ABORT visible
            _tick_io(end, screen, time.monotonic())
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    finally:
        for c in closers:
            getattr(c, "close", lambda: None)()
    return _finish(snd, "S")


def run_receive(a, psk: bytes) -> int:
    from . import handshake as hs
    from .link import LinkConfig, for_audio
    audio = _audio(a, "r")
    cfg = LinkConfig(timeout=a.timeout, retries=a.retries)
    confirm, box = operator_prompt("R")
    end, screen, closers = build_endpoint(a, "r", psk)
    rcv = box["eng"] = hs.Receiver(psk, a.path, cfg=for_audio(cfg) if audio else cfg, announce=not audio,
                                   backchannel="audio" if audio else "camera", overwrite=a.overwrite,
                                   auto_confirm=_auto(a), confirm=confirm)
    print(f"Receiver running: point the camera at the sender's screen. Saving to {a.path}. Keys in its window: "
          "f fullscreen, q quit.")
    last: dict = {}
    try:
        while True:
            now = time.monotonic()
            inbound: list[bytes] = []
            while (f := end.source.read(0.03, rcv.decode_profiles)) is not None:
                inbound.extend(f)
            for m in rcv.step(now, inbound):
                end.back.send(m)
            _tick_io(end, screen, now)
            _status(rcv, last, "R")
            if rcv.finished:
                break
        t_end = time.monotonic() + rcv.linger_s
        while time.monotonic() < t_end:
            _tick_io(end, screen, time.monotonic())
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    finally:
        for c in closers:
            getattr(c, "close", lambda: None)()
    return _finish(rcv, "R")


# ---------------------------------------------------------------- selftest

def cmd_selftest(a) -> int:
    from . import archive, handshake as hs
    from .channels import LoopChannel, ManualClock
    from .link import LinkConfig, Profile, run_pair
    t0 = time.perf_counter()
    psk = secrets.token_bytes(32)
    with tempfile.TemporaryDirectory() as td:
        src, dst = Path(td) / "src" / "demo", Path(td) / "dst"
        (src / "sub").mkdir(parents=True)
        (src / "a.txt").write_bytes(b"hello lightferry\n" * 200)
        (src / "sub" / "b.bin").write_bytes(os.urandom(5000))
        blob, manifest = archive.pack(src)
        clock = ManualClock()
        ch = LoopChannel(clock=clock)
        cfg = LinkConfig()
        snd = hs.Sender(psk, manifest, blob, cfg=cfg, proposal=Profile(64, 6, 3), auto_confirm=True)
        rcv = hs.Receiver(psk, dst, cfg=cfg, auto_confirm=True)
        vt = run_pair(snd, rcv, ch, clock, max_time=600)
        ok = snd.state == "CLOSED" and rcv.state == "CLOSED"
        if ok:
            for p in src.rglob("*"):
                if p.is_file() and (dst / "demo" / p.relative_to(src)).read_bytes() != p.read_bytes():
                    ok = False
        wall = time.perf_counter() - t0
        if ok:
            print(f"selftest OK: {len(blob)} B archive, virtual link time {vt:.1f}s, wall {wall:.2f}s, "
                  f"verify {snd.verify_code}")
            return 0
        print(f"selftest FAILED: sender {snd.state} ({snd.abort_reason}), receiver {rcv.state} ({rcv.abort_reason})")
        return 1


# ---------------------------------------------------------------- argparse

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="lightferry", description="Visual sneakernet: files between air-gapped "
                                 "machines via screen codes scanned by webcam.",
                                 usage="lightferry {send,receive} --path PATH [options]\n"
                                       "       lightferry {keygen,selftest,bench,mobile-serve} [...]")
    m = ap.add_mutually_exclusive_group()
    m.add_argument("--send", action="store_true", help=argparse.SUPPRESS)  # older spelling of `send`
    m.add_argument("--receive", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--path", help="file/folder to send, or destination dir when receiving")
    ap.add_argument("--key-file", help="32-byte key file (or hex text), see `lightferry keygen`")
    ap.add_argument("--key", help="hex key (LEAKS to the process list; prefer --key-file / env LIGHTFERRY_KEY)")
    ap.add_argument("--camera", default="webcam", help="webcam | dir:<path> | loop (loop: selftest only)")
    ap.add_argument("--camera-index", type=int, default=0)
    ap.add_argument("--backchannel", choices=("auto", "camera", "audio"), default="auto")
    ap.add_argument("--no-camera", action="store_true", help="sender has no camera (audio back-channel)")
    ap.add_argument("--grid", type=int, help="sender's data grid proposal (default 64)")
    ap.add_argument("--module-px", type=int, help="pixels per module proposal (default 6)")
    ap.add_argument("--tiles", help="tiles per side, e.g. 3 or 3x3 (default 3; 1 with the audio back-channel)")
    ap.add_argument("--timeout", type=float, default=1.5)
    ap.add_argument("--retries", type=int, default=8)
    ap.add_argument("--overwrite", action="store_true", help="receiver: replace an existing item")
    ap.add_argument("--fullscreen", action="store_true", help="start the display window fullscreen")
    ap.add_argument("--yes", action="store_true", help="test hook: auto-confirm VERIFY (only with --camera dir:)")
    c = ap.add_mutually_exclusive_group()
    c.add_argument("--compress", dest="compress", nargs="?", const="auto", default="auto",
                   choices=("auto", "lzma", "zlib", "none"), help="compression (default auto)")
    c.add_argument("--no-compress", dest="compress", action="store_const", const="none")
    return ap


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in SUBCOMMANDS:
        sub, rest = argv[0], argv[1:]
        if sub == "bench":
            from . import bench
            return bench.main(rest) or 0
        sp = argparse.ArgumentParser(prog=f"lightferry {sub}")
        if sub == "keygen":
            sp.add_argument("--out", default="lightferry.key")
            return cmd_keygen(sp.parse_args(rest))
        if sub == "selftest":
            return cmd_selftest(sp.parse_args(rest))
        sp.add_argument("--path", required=True, help="file/folder this machine sends to the phone; with "
                        "--remote-sender, the destination dir for what the phone receives")
        sp.add_argument("--port", type=int, default=8443)
        sp.add_argument("--host", default=None, help="address to bind (default: this machine's LAN IP)")
        sp.add_argument("--backchannel", choices=("audio", "direct"), default="audio")
        sp.add_argument("--remote-sender", action="store_true",
                        help="the phone receives from another machine running `lightferry send --backchannel "
                             "audio`; its replies are played in that sender's audio format")
        sp.add_argument("--out", help="where received files go (default out/received; --remote-sender: --path)")
        sp.add_argument("--key-file")
        sp.add_argument("--key")
        a = sp.parse_args(rest)
        try:
            key = load_key(a.key_file, a.key)
            from . import mobile
        except ImportError as e:
            return _err(f"mobile-serve needs the 'mobile' extra / mobile module: {e}")
        except (ValueError, OSError) as e:
            return _err(str(e))
        if a.remote_sender:
            mobile.serve(None, key, host=a.host, port=a.port, out_dir=a.out or a.path, remote_sender=True)
        else:
            mobile.serve(a.path, key, host=a.host, port=a.port, out_dir=a.out, backchannel=a.backchannel)
        return 0

    if argv and argv[0] in MODES:
        argv = ["--" + argv[0], *argv[1:]]
    a = build_parser().parse_args(argv)
    if not (a.send or a.receive):
        build_parser().print_help()
        return 2
    if not a.path:
        return _err("--path is required")
    try:
        a.tiles = parse_tiles(a.tiles)
        if a.yes and a.camera == "webcam":
            return _err("--yes is a test hook: only with --camera dir: (real hardware always asks VERIFY)")
        psk = load_key(a.key_file, a.key)
        if a.receive:
            if a.no_camera:
                return _err("--receive needs a camera")
            if a.backchannel == "auto":
                a.backchannel = "camera"
        return run_send(a, psk) if a.send else run_receive(a, psk)
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # clear message, no traceback for expected failures
        from .channels import ChannelError
        from .archive import ArchiveError
        if isinstance(e, (ChannelError, ArchiveError, ValueError, OSError)):
            return _err(str(e))
        raise


if __name__ == "__main__":
    sys.exit(main())
