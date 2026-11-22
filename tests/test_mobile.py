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
