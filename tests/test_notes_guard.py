"""Audio note tokens are 16 bits: S keeps few of them valid at once, and NoteListener rate-limits bad tags
(PLAN section 11)."""
import secrets

import numpy as np

from lightferry import acoustic as ac, archive, handshake as hs
from lightferry.link import LinkConfig, Profile, for_audio

FS = 48000


def _feed(lst, x):
    got = []
    for i in range(0, len(x), FS // 4):
        got += lst.feed(x[i:i + FS // 4])
    return got


def _burst(tok):
    return np.concatenate([ac.encode_notes(tok, FS), np.zeros(FS // 4, np.float32)])


def test_rate_limit_drops_tokens_after_many_bad_tags():
    good = b"\x5a\xc3"
    rng = np.random.default_rng(1)
    bad = [bytes(rng.integers(0, 256, 2, dtype=np.uint8)) for _ in range(24)]
    bad = [b for b in bad if b != good]
    lst = ac.NoteListener(FS, accept=lambda t: t == good)
    assert _feed(lst, np.concatenate([_burst(b) for b in bad] + [_burst(good), np.zeros(FS, np.float32)])) == []
    assert lst.limited and lst.suppressed == 1

    lst = ac.NoteListener(FS, accept=lambda t: t == good)  # a few bad tags: still heard
    assert _feed(lst, np.concatenate([_burst(b) for b in bad[:5]] + [_burst(good), np.zeros(FS, np.float32)])) \
        == [good]
    assert not lst.limited and lst.suppressed == 0


def test_good_tokens_are_not_counted_as_bad():
    toks = [b"\x11\x22", b"\x33\x44", b"\x55\x66"]
    lst = ac.NoteListener(FS, accept=lambda t: t in toks, max_bad_per_min=0)  # any bad tag would mute it
    assert _feed(lst, np.concatenate([_burst(t) for t in toks] + [np.zeros(FS, np.float32)])) == toks


def test_few_tokens_valid_at_once(tmp_path):
    """False-accept odds per random candidate = len(expected) / 2**16. With every recent code's tokens valid
    it was up to 128 (1/512); now only the current state's answers for the latest cycle count."""
    psk = secrets.token_bytes(32)
    src = tmp_path / "big"
    src.write_bytes(secrets.token_bytes(30000))  # ~70 data tiles: a long cycle
    blob, man = archive.pack(src)
    cfg = for_audio(LinkConfig())
    snd = hs.Sender(psk, man, blob, cfg=cfg, proposal=Profile(64, 6, 1), auto_confirm=True, backchannel="audio")
    rcv = hs.Receiver(psk, tmp_path / "dst", cfg=cfg, auto_confirm=True, backchannel="audio", announce=False)
    worst, now, screen, r_on = {}, 0.0, [], True
    while now < 90 and not snd.finished:
        frame = snd.step(now, [])
        screen = frame or screen
        worst[snd.state] = max(worst.get(snd.state, 0), len(snd.expected))
        if r_on:  # R answers until the data starts, then goes quiet: S keeps cycling and re-signing
            for tok in rcv.step(now, list(screen)):
                snd.hear(tok, now)
            r_on = snd.state != hs.DATA
        now += 0.05
    assert snd.state == hs.DATA and snd.progress[1] > 50
    accepted = sum(bytes([a, b]) in snd.expected for a in range(256) for b in range(256))
    assert accepted == len(snd.expected)
    per_tile = 2  # DONE_OK / DONE_FAIL
    window = int(cfg.timeout / cfg.dwell_s) + 2
    assert worst[hs.HELLO] <= 2 and worst.get(hs.VERIFY, 0) == 0
    assert worst[hs.DATA] <= per_tile * window < 128, worst
    assert max(worst.values()) / 2 ** 16 < 1 / 1000, worst
