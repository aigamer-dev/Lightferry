"""Remote-sender mode of the phone test server: its audio must decode on a real `lightferry send` (AudioBack)."""
import asyncio
import secrets

import numpy as np
import pytest

pytest.importorskip("cv2")

from lightferry import acoustic, io_platform
from lightferry.mobile import FS, Session


class _NoIO:
    def capture(self, put, stop):
        stop.wait()


class _WS:
    def __init__(self):
        self.sent = []

    async def send_bytes(self, b):
        self.sent.append(b)


def _session(tmp_path, psk):
    s = Session(None, psk, tmp_path / "in", "direct", "tok", remote_sender=True)
    s.begin()
    return s


def test_remote_session_runs_only_a_receiver(tmp_path):
    s = _session(tmp_path, secrets.token_bytes(32))
    assert s.sender is None and s.receiver is not None
    assert s.backchannel == "audio"  # forced: a remote sender only hears audio
    st = s.status()
    assert st["state"] == "RUNNING" and st["sender"]["state"] == "REMOTE"
    assert not s.ended()


def test_remote_notes_are_heard_by_the_sender(tmp_path):
    from lightferry import archive, handshake as hs

    psk = secrets.token_bytes(32)
    src = tmp_path / "demo"
    src.mkdir()
    (src / "a.md").write_bytes(b"x" * 100)
    snd = hs.Sender(psk, *archive.pack(src)[::-1], backchannel="audio")  # the `lightferry send` machine's engine
    hello = snd.step(0.0, [])

    s = _session(tmp_path, psk)
    out = s.receiver_step(hello)
    s.dispatch(out + out)  # the same token while still queued is not played twice
    assert len(s.pending) == 1 and len(s.pending[0]) == 2
    s.ws = _WS()
    asyncio.run(s.pump_audio())
    assert not s.pending
    pcm = np.frombuffer(s.ws.sent[-1][1:], "<i2").astype(np.float32) / 32768
    assert len(pcm) / FS <= 0.8  # the whole reply: 4 notes, twice

    back = io_platform.AudioBack("s", io=_NoIO())  # the sender's side of `lightferry send`
    back.accept = lambda t: t in snd.expected
    try:
        lst = acoustic.NoteListener(FS, accept=back.accept)
        silence = np.zeros(FS // 2, np.float32)
        heard = []
        for chunk in (silence, pcm, silence, silence, silence):
            heard += lst.feed(chunk)
        assert len(heard) == 1
        assert snd.hear(heard[0], 1.0) and snd.state == "AUTHED"
    finally:
        back.close()


# ---------------------------------------------------------------- server security (PLAN section 12)

def _with_client(tmp_path, fn):
    aiohttp = pytest.importorskip("aiohttp")
    from aiohttp.test_utils import TestClient, TestServer
    from lightferry import mobile

    sess = Session(None, secrets.token_bytes(32), tmp_path / "in", "direct", "tok", remote_sender=True)

    async def run():
        async with TestClient(TestServer(mobile._build_app(sess))) as c:
            await fn(c, sess, aiohttp)
    asyncio.run(run())
    return sess


def test_token_required_on_http_and_ws(tmp_path):
    async def fn(c, sess, aiohttp):
        for path in ("/screen", "/m", "/status.json", "/frame.png", "/ws"):
            r = await c.get(path)
            assert r.status == 403, path
            r = await c.get(path + "?t=wrong")
            assert r.status == 403, path
        assert (await c.get("/status.json?t=tok")).status == 200
        with pytest.raises(aiohttp.WSServerHandshakeError) as e:
            await c.ws_connect("/ws?t=nope")
        assert e.value.status == 403
    _with_client(tmp_path, fn)


def test_second_websocket_client_refused(tmp_path):
    async def fn(c, sess, aiohttp):
        ws1 = await c.ws_connect("/ws?t=tok")
        with pytest.raises(aiohttp.WSServerHandshakeError) as e:
            await c.ws_connect("/ws?t=tok")
        assert e.value.status == 409 and sess.ws is not None and not sess.ws.closed
        await ws1.close()
        for _ in range(50):  # the server notices the close, then a new client may connect
            if sess.ws is None:
                break
            await asyncio.sleep(0.02)
        ws2 = await c.ws_connect("/ws?t=tok")
        await ws2.close()
    _with_client(tmp_path, fn)


def test_oversize_frame_dropped(tmp_path, monkeypatch):
    from lightferry import mobile
    monkeypatch.setattr(mobile, "MAX_JPEG", 1000)
    seen = []
    monkeypatch.setattr(Session, "handle_jpeg", lambda self, data: seen.append(len(data)) or [])

    async def fn(c, sess, aiohttp):
        ws = await c.ws_connect("/ws?t=tok")
        await ws.send_bytes(b"x" * 500)
        await ws.send_bytes(b"x" * 5000)  # over the cap: never decoded, the socket is closed
        for _ in range(50):
            msg = await ws.receive()
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
        assert ws.closed or msg.type != aiohttp.WSMsgType.TEXT
    _with_client(tmp_path, fn)
    assert seen == [500]


def test_cert_is_private_and_fresh(tmp_path):
    pytest.importorskip("cryptography")
    import os
    import stat
    from lightferry import mobile
    cert, key, fp = mobile.make_self_signed("192.168.1.20")
    try:
        d = key.parent
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(key).st_mode) == 0o600
        assert d.name.startswith("lightferry-cert-") and len(fp) == 64
        cert2, key2, fp2 = mobile.make_self_signed("192.168.1.20")
        assert key2.parent != d and fp2 != fp  # per run, never a cached key
    finally:
        import shutil
        shutil.rmtree(key.parent, ignore_errors=True)
        shutil.rmtree(key2.parent, ignore_errors=True)
