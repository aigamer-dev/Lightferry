import pytest

from lightferry import protocol as P

PSK = bytes(range(32))
NS, NR = b"S" * 16, b"R" * 16


def keys():
    return P.derive_keys(PSK, NS, NR)


def test_header_size_and_roundtrip():
    assert P.HEADER_LEN == 24 and P.OVERHEAD == 56
    k = keys()
    sl = P.Sealer(k.k_s2r, k.session_id)
    raw = sl.seal(P.PType.DATA, b"hello", obj_id=1, seq=3, total=9, flags=P.FLAG_LAST)
    assert len(raw) == 56 + 5
    pkt = P.open_packet(k.k_s2r, raw, P.ReplayWindow())
    assert (pkt.ptype, pkt.obj_id, pkt.seq, pkt.total, pkt.body, pkt.last) == (7, 1, 3, 9, b"hello", True)
    assert pkt.session_id == k.session_id and pkt.counter == 0 and not pkt.duplicate


def test_flipped_byte_and_wrong_key():
    k = keys()
    raw = P.Sealer(k.k_s2r, k.session_id).seal(P.PType.DATA, b"abc")
    for i in range(len(raw)):
        bad = bytearray(raw)
        bad[i] ^= 0x01
        with pytest.raises(P.AuthError):
            P.open_packet(k.k_s2r, bytes(bad), P.ReplayWindow())
    with pytest.raises(P.AuthError):
        P.open_packet(P.derive_keys(bytes(32), NS, NR).k_s2r, raw)
    with pytest.raises(P.AuthError):
        P.open_packet(k.k_s2r, raw[:50])


def test_reflected_packet_rejected():
    k = keys()
    raw = P.Sealer(k.k_s2r, k.session_id).seal(P.PType.ACK, b"x")
    with pytest.raises(P.AuthError):
        P.open_packet(k.k_r2s, raw)  # S->R packet reflected back to S
    assert len({k.k_s2r, k.k_r2s, k.k_sess, k.k_audio}) == 4
    assert P.hello_key(PSK, "s") != P.hello_key(PSK, "r")
    h = P.Sealer(P.hello_key(PSK, "s")).seal(P.PType.HELLO, b"{}")
    with pytest.raises(P.AuthError):
        P.open_packet(P.hello_key(PSK, "r"), h)


def test_session_a_into_b_rejected():
    ka = P.derive_keys(PSK, b"1" * 16, b"2" * 16)
    kb = P.derive_keys(PSK, b"3" * 16, b"4" * 16)
    assert ka.session_id != kb.session_id
    raw = P.Sealer(ka.k_s2r, ka.session_id).seal(P.PType.DATA, b"old")
    with pytest.raises(P.AuthError):
        P.open_packet(kb.k_s2r, raw, P.ReplayWindow())


def test_retransmission_is_duplicate_not_reapplied():
    k = keys()
    raw = P.Sealer(k.k_s2r, k.session_id).seal(P.PType.DATA, b"abc")
    w = P.ReplayWindow()
    assert not P.open_packet(k.k_s2r, raw, w).duplicate
    assert P.open_packet(k.k_s2r, raw, w).duplicate


def test_window_semantics():
    w = P.ReplayWindow()
    assert w.check(10) == "new"
    w.commit(10)
    assert w.check(10) == "dup"
    assert w.check(5) == "new"  # in-window reorder accepted once
    w.commit(5)
    assert w.check(5) == "dup"
    w.commit(10 + 4096)  # slide: 10 is now exactly W below -> out
    assert w.check(10) == "replay"
    assert w.check(11) == "new"
    assert w.check(10 + 4096) == "dup"
    assert w.check(10 + 4096 - 4095) == "new"


def test_below_window_is_replay_error():
    k = keys()
    sl = P.Sealer(k.k_s2r, k.session_id)
    pk = [sl.seal(P.PType.DATA, b"x") for _ in range(5000)]
    w = P.ReplayWindow()
    P.open_packet(k.k_s2r, pk[4999], w)
    P.open_packet(k.k_s2r, pk[4999 - 4095], w)  # oldest in-window: accepted
    with pytest.raises(P.ReplayError):
        P.open_packet(k.k_s2r, pk[0], w)
    P.open_packet(k.k_s2r, pk[4500], w)
    assert P.open_packet(k.k_s2r, pk[4500], w).duplicate


def test_keys_deterministic_and_nonce_bound():
    assert keys() == keys()
    assert keys().k_s2r != P.derive_keys(PSK, NS, b"X" * 16).k_s2r
    with pytest.raises(ValueError):
        P.derive_keys(b"short", NS, NR)


def test_verify_code():
    k = keys()
    a = P.verify_code(k, "file", b"m" * 32, NS, NR)
    assert len(a) == 6 and a.isdigit()
    assert a == P.verify_code(k, "file", b"m" * 32, NS, NR)
    assert a != P.verify_code(k, "file", b"n" * 32, NS, NR)
    c = P.verify_code(k, "cmd", None, NS, NR)
    assert len(c) == 6 and c != P.verify_code(k, "cmd", None, NS, b"Z" * 16)


def test_peek():
    k = keys()
    raw = P.Sealer(k.k_s2r, k.session_id).seal(P.PType.START, b"", obj_id=P.OBJ_CTRL)
    h = P.peek(raw)
    assert h.ptype == P.PType.START and h.obj_id == P.OBJ_CTRL
    assert P.peek(b"short") is None
    assert P.peek(bytes([99]) + raw[1:]) is None
