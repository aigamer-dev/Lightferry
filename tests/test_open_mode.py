import os
import secrets

import pytest

from lightferry import archive, cli, handshake as hs
from lightferry.channels import LoopChannel, ManualClock
from lightferry.link import LinkConfig, Profile, run_pair


def _pair(psk_s, psk_r, tmp_path, max_time=600):
    src = tmp_path / "src" / "demo"
    src.mkdir(parents=True)
    (src / "a.txt").write_bytes(b"open mode test\n" * 100)
    blob, manifest = archive.pack(src)
    clock = ManualClock()
    ch = LoopChannel(clock=clock)
    cfg = LinkConfig()
    snd = hs.Sender(psk_s, manifest, blob, cfg=cfg, proposal=Profile(64, 6, 3), auto_confirm=True)
    rcv = hs.Receiver(psk_r, tmp_path / "dst", cfg=cfg, auto_confirm=True)
    run_pair(snd, rcv, ch, clock, max_time=max_time)
    return src, snd, rcv


def test_open_mode_loop_transfer_succeeds_without_prompt(tmp_path, monkeypatch):
    monkeypatch.delenv("LIGHTFERRY_KEY", raising=False)
    psk = cli.load_key(None, None, None, True)
    assert psk == cli.OPEN_PSK
    src, snd, rcv = _pair(psk, psk, tmp_path)
    assert snd.state == "CLOSED" and rcv.state == "CLOSED"
    assert (tmp_path / "dst" / "demo" / "a.txt").read_bytes() == (src / "a.txt").read_bytes()
    assert snd.verify_code == rcv.verify_code and len(snd.verify_code) == 6


def test_open_vs_keyed_is_a_key_mismatch(tmp_path):
    _, snd, rcv = _pair(cli.OPEN_PSK, secrets.token_bytes(32), tmp_path, max_time=120)
    assert rcv.abort_reason == "KEY_MISMATCH" or snd.abort_reason == "KEY_MISMATCH"
    assert not (tmp_path / "dst" / "demo").exists()


def test_open_flag_conflicts_with_keys(tmp_path):
    kf = tmp_path / "k"
    kf.write_bytes(bytes(range(32)))
    with pytest.raises(ValueError, match="--open cannot be combined"):
        cli.load_key(str(kf), None, None, True)
    with pytest.raises(ValueError, match="--open cannot be combined"):
        cli.load_key(None, None, "correct horse battery", True)


def test_env_open_only_without_explicit_key(monkeypatch, tmp_path):
    monkeypatch.setenv("LIGHTFERRY_OPEN", "1")
    monkeypatch.delenv("LIGHTFERRY_KEY", raising=False)
    assert cli.load_key(None, None) == cli.OPEN_PSK
    kf = tmp_path / "k"
    kf.write_bytes(bytes(range(32)))
    assert cli.load_key(str(kf), None) == bytes(range(32))  # an explicit key still wins


def test_open_psk_is_public_and_fixed():
    import hashlib
    assert cli.OPEN_PSK == hashlib.sha256(b"lightferry-open-mode-v1").digest()


def test_verify_auto_only_in_open_mode_unless_verify_flag():
    from argparse import Namespace
    assert cli._auto(Namespace(yes=False, camera="webcam", auto_verify=True))
    assert not cli._auto(Namespace(yes=False, camera="webcam", auto_verify=False))
    assert not cli._auto(Namespace(yes=False, camera="webcam"))
