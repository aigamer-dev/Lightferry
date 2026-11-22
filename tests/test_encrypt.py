import secrets
from argparse import Namespace

import pytest

from lightferry import archive, cli, handshake as hs, protocol as P
from lightferry.channels import LoopChannel, ManualClock
from lightferry.link import LinkConfig, Profile, run_pair


def _ns(**kw):
    d = dict(key_file=None, key=None, passphrase=None, encrypt=False, open=False)
    d.update(kw)
    return Namespace(**d)


def test_default_is_keyless_and_unencrypted():
    psk, enc = cli.resolve_mode(_ns())
    assert psk == cli.OPEN_PSK and enc is False
    assert cli.resolve_mode(_ns(open=True)) == (cli.OPEN_PSK, False)  # --open: accepted old name of the default


def test_key_without_encrypt_is_an_error(tmp_path):
    kf = tmp_path / "k"
    kf.write_bytes(bytes(range(32)))
    with pytest.raises(ValueError, match="without --encrypt"):
        cli.resolve_mode(_ns(key_file=str(kf)))


def test_encrypt_needs_a_key(monkeypatch):
    for v in ("LIGHTFERRY_KEY", "LIGHTFERRY_PASSPHRASE", "LIGHTFERRY_OPEN"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(ValueError, match="--encrypt needs a key"):
        cli.resolve_mode(_ns(encrypt=True))


def test_encrypt_with_key_file(tmp_path):
    kf = tmp_path / "k"
    kf.write_bytes(bytes(range(32)))
    psk, enc = cli.resolve_mode(_ns(encrypt=True, key_file=str(kf)))
    assert psk == bytes(range(32)) and enc is True


def test_body_is_encrypted_on_the_wire_and_round_trips():
    keys = P.derive_keys(secrets.token_bytes(32), b"n" * 16, b"m" * 16, encrypt=True)
    s = P.Sealer(keys.k_s2r, keys.session_id, enc_key=keys.enc_tx("s"))
    plain = b"SECRET FILE CONTENT " * 8
    raw = s.seal(P.PType.DATA, plain, obj_id=1, seq=1, total=2)
    assert plain not in raw and b"SECRET" not in raw
    pkt = P.open_packet(keys.k_s2r, raw, P.ReplayWindow(), keys.enc_rx("r"))
    assert pkt.body == plain and pkt.flags & P.FLAG_ENC
    assert P.plain_body(raw, keys.enc_tx("s")) == plain


def test_encrypted_packet_needs_the_key_and_tamper_fails():
    keys = P.derive_keys(secrets.token_bytes(32), b"n" * 16, b"m" * 16, encrypt=True)
    raw = P.Sealer(keys.k_s2r, keys.session_id, enc_key=keys.enc_tx("s")).seal(P.PType.DATA, b"x" * 40, obj_id=1)
    with pytest.raises(P.AuthError):  # a side without --encrypt cannot read it
        P.open_packet(keys.k_s2r, raw, P.ReplayWindow(), None)
    bad = bytearray(raw)
    bad[P.HEADER_LEN + 3] ^= 1
    with pytest.raises(P.AuthError):
        P.open_packet(keys.k_s2r, bytes(bad), P.ReplayWindow(), keys.enc_rx("r"))
    plain = P.Sealer(keys.k_s2r, keys.session_id).seal(P.PType.DATA, b"x" * 40, obj_id=1)
    with pytest.raises(P.AuthError):  # an unencrypted packet is refused when --encrypt is on
        P.open_packet(keys.k_s2r, plain, P.ReplayWindow(), keys.enc_rx("r"))


def _pair(psk_s, psk_r, enc_s, enc_r, tmp_path, max_time=600):
    src = tmp_path / "src" / "demo"
    src.mkdir(parents=True)
    (src / "a.txt").write_bytes(b"encrypt test\n" * 200)
    blob, manifest = archive.pack(src)
    clock = ManualClock()
    ch = LoopChannel(clock=clock)
    cfg = LinkConfig()
    snd = hs.Sender(psk_s, manifest, blob, cfg=cfg, proposal=Profile(64, 6, 3), auto_confirm=True, encrypt=enc_s)
    rcv = hs.Receiver(psk_r, tmp_path / "dst", cfg=cfg, auto_confirm=True, encrypt=enc_r)
    run_pair(snd, rcv, ch, clock, max_time=max_time)
    return src, snd, rcv


def test_encrypted_transfer_over_loop_channel(tmp_path):
    key = secrets.token_bytes(32)
    src, snd, rcv = _pair(key, key, True, True, tmp_path)
    assert snd.state == "CLOSED" and rcv.state == "CLOSED"
    assert (tmp_path / "dst" / "demo" / "a.txt").read_bytes() == (src / "a.txt").read_bytes()


def test_encrypted_vs_keyless_fails(tmp_path):
    _, snd, rcv = _pair(secrets.token_bytes(32), cli.OPEN_PSK, True, False, tmp_path, max_time=120)
    assert "KEY_MISMATCH" in (snd.abort_reason, rcv.abort_reason)
    assert not (tmp_path / "dst" / "demo").exists()


def test_keyless_default_transfer_over_loop_channel(tmp_path):
    src, snd, rcv = _pair(cli.OPEN_PSK, cli.OPEN_PSK, False, False, tmp_path)
    assert snd.state == "CLOSED" and rcv.state == "CLOSED"
