"""Audio back-channel: R speaks three 2-byte note tokens per transfer (HELLO seen, manifest complete, done) while
the manifest and data cycle on screen; S re-signs its codes to ask again when a token was lost."""
import random
import secrets

import pytest

from lightferry import archive, handshake as hs
from lightferry.link import LinkConfig, Profile, for_audio
from lightferry.protocol import PType

NOTE_S = 0.75  # a token's airtime


def _pair(tmp_path, size=1500):
    psk = secrets.token_bytes(32)
    src = tmp_path / "src" / "demo"
    src.mkdir(parents=True)
    (src / "a.md").write_bytes(secrets.token_bytes(size))  # incompressible: several data tiles
    blob, man = archive.pack(src)
    cfg = for_audio(LinkConfig())
    snd = hs.Sender(psk, man, blob, cfg=cfg, proposal=Profile(64, 6, 3), auto_confirm=True, backchannel="audio")
    rcv = hs.Receiver(psk, tmp_path / "dst", cfg=cfg, auto_confirm=True, backchannel="audio", announce=False)
    return snd, rcv


def _run(tmp_path, loss=0.0, seed=0, max_time=3600.0):
    snd, rcv = _pair(tmp_path)
    now, dt, screen, in_air, tokens, rng = 0.0, 0.2, [], [], [], random.Random(seed)
    while now < max_time and not (snd.finished and rcv.finished):
        for t, tok in [x for x in in_air if x[0] <= now]:
            snd.hear(tok, now)
        in_air = [x for x in in_air if x[0] > now]
        frame = snd.step(now, [])
        if frame:
            screen = frame
        for tok in rcv.step(now, list(screen)):  # the camera sees whatever is on screen, every frame
            assert len(tok) == 2
            tokens.append(tok)
            if rng.random() >= loss:
                in_air.append((now + NOTE_S, tok))
        now += dt
    return snd, rcv, tokens, now


def test_completes_with_one_token_per_step(tmp_path):
    snd, rcv, tokens, t = _run(tmp_path)
    assert snd.state == "CLOSED" and rcv.state == "CLOSED", (snd.abort_reason, rcv.abort_reason)
    assert (tmp_path / "dst" / "demo" / "a.md").read_bytes() == (tmp_path / "src" / "demo" / "a.md").read_bytes()
    assert snd.progress[1] > 1  # several data tiles, still only three replies
    assert len(tokens) == 3, tokens  # HELLO seen, manifest complete, archive done
    assert snd.data_profile.tiles_n == 1
    assert t < 10, t


@pytest.mark.parametrize("seed", range(6))
def test_lost_tokens_are_recovered_by_asking_again(tmp_path, seed):
    snd, rcv, tokens, t = _run(tmp_path, loss=0.35, seed=seed)
    assert snd.state == "CLOSED" and rcv.state == "CLOSED", (snd.abort_reason, rcv.abort_reason, t)


def test_tokens_are_bound_to_session_and_packet(tmp_path):
    snd, rcv = _pair(tmp_path)
    frame = snd.step(0.0, [])
    tok = rcv.step(0.0, frame)[0]
    other, _ = _pair(tmp_path / "x")
    other.step(0.0, [])
    assert not other.hear(tok, 0.1)  # another session's S (other nonce) rejects it
    bad = bytes([tok[0] ^ 1, tok[1]])
    assert not snd.hear(bad, 0.1)
    assert snd.hear(tok, 0.1) and snd.state == "AUTHED"
    assert not snd.hear(tok, 0.2) or snd.state == "AUTHED"  # a replay changes nothing
