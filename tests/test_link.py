import hashlib
import random

import pytest

from lightferry import handshake as H
from lightferry import protocol as P
from lightferry.channels import DirChannel, Impairment, LoopChannel, ManualClock
from lightferry.link import BOOTSTRAP, LinkConfig, Profile, pump_receiver, pump_sender, run_pair

PSK = bytes(range(32))
BADPSK = bytes(range(1, 33))
FAST = LinkConfig(timeout=1.0, retries=8)


def make(size=20000, seed=1):
    blob = random.Random(seed).randbytes(size)
    sha = hashlib.sha256(blob).hexdigest()
    manifest = {"name": "x.bin", "kind": "file", "files": [{"path": "x.bin", "size": size, "sha256": sha}],
                "dirs": [], "skipped": [], "archive_size": size, "archive_sha256": sha,
                "compression": "none", "raw_size": size}
    return blob, manifest


class Rec:
    """Recording extract adapter."""
    def __init__(self):
        self.calls = []

    def __call__(self, blob, dest, manifest, overwrite=False):
        self.calls.append(blob)
        return ["ok"]


def setup(blob=None, manifest=None, *, psk_r=PSK, fwd=None, back=None, cfg=FAST, s_kw=None, r_kw=None,
          r_cls=H.Receiver, image_mode=False, proposal=Profile(64, 6, 3)):
    if blob is None:
        blob, manifest = make()
    clock = ManualClock()
    ch = LoopChannel(clock=clock, fwd=fwd, back=back, image_mode=image_mode)
    rec = Rec()
    s = H.Sender(PSK, manifest, blob, cfg=cfg, proposal=proposal, auto_confirm=True, **(s_kw or {}))
    rkw = dict(extract=rec, check=lambda m: "OK", auto_confirm=True)
    rkw.update(r_kw or {})
    r = r_cls(psk_r, "/unused", cfg=cfg, **rkw)
    return blob, s, r, ch, clock, rec


def types_shown(ch):
    seen = []
    orig = ch.s_end.sink.show

    def show(packets, profile=None):
        seen.extend(P.peek(p).ptype for p in packets)
        return orig(packets, profile)
    ch.s_end.sink.show = show
    return seen


def test_clean_transfer():
    blob, s, r, ch, clock, rec = setup()
    run_pair(s, r, ch, clock)
    assert s.state == H.CLOSED and r.state == H.CLOSED
    assert rec.calls == [blob]
    assert r.result["ok"] and s.result["status"] == "OK" and s.result["sha256_ok"]
    assert s.verify_code == r.verify_code and len(s.verify_code) == 6
    assert s.profile == Profile(64, 6, 3) == r.data_profile
    for st in (H.HELLO, H.AUTHED, H.VERIFY, H.START, H.DATA, H.CLOSED):
        assert st in s.history
    for st in (H.HELLO_ACKED, H.AUTHED, H.VERIFY, H.READY, H.DATA, H.DONE):
        assert st in r.history


@pytest.mark.parametrize("size", [0, 1, 435, 20000])
def test_sizes(size):
    blob, man = make(size)
    blob, s, r, ch, clock, rec = setup(blob, man)
    run_pair(s, r, ch, clock)
    assert r.state == H.CLOSED and rec.calls == [blob]


def test_large_manifest_tiled():
    blob, man = make(2000)
    man["files"] = [{"path": f"dir/file-{i:04d}.txt", "size": 1} for i in range(120)]
    assert len(H.manifest_bytes(man)) > 435 * 3
    blob, s, r, ch, clock, rec = setup(blob, man)
    run_pair(s, r, ch, clock)
    assert r.state == H.CLOSED and r.manifest == man


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_impaired_transfer(seed):
    fwd = Impairment(drop=0.25, corrupt=0.08, dup=0.2, frame_drop=0.05, delay=0.1, jitter=0.9, seed=seed)
    back = Impairment(drop=0.35, corrupt=0.05, dup=0.2, delay=0.1, jitter=0.9, seed=seed + 100)
    blob, s, r, ch, clock, rec = setup(fwd=fwd, back=back, cfg=LinkConfig(timeout=1.0, retries=30))
    run_pair(s, r, ch, clock, max_time=3000)
    assert s.state == H.CLOSED and r.state == H.CLOSED, (s.abort_reason, r.abort_reason)
    assert rec.calls == [blob]
    assert s._tx_d.retransmitted > 0


def test_duplicate_frames_not_double_processed():
    blob, s, r, ch, clock, rec = setup(fwd=Impairment(dup=0.8, seed=5), back=Impairment(dup=0.8, seed=6))
    run_pair(s, r, ch, clock)
    assert r.state == H.CLOSED
    assert len(rec.calls) == 1
    assert r.stats["duplicate"] > 0
    assert r._rx_d.r.received == r._rx_d.r.total


def test_ack_loss_only():
    blob, s, r, ch, clock, rec = setup(back=Impairment(drop=0.6, seed=9), cfg=LinkConfig(timeout=1.0, retries=40))
    run_pair(s, r, ch, clock, max_time=3000)
    assert r.state == H.CLOSED and rec.calls == [blob]


class DropFirst(Impairment):
    """Drop the first ``n`` transmissions of packets of one ptype (retransmissions then get through)."""
    def __init__(self, ptype, n=2):
        super().__init__()
        self.ptype, self.left = ptype, n

    def mangle(self, data):
        h = P.peek(data)
        if h and h.ptype == self.ptype and self.left > 0:
            self.left -= 1
            return None
        return data


@pytest.mark.parametrize("pt", [P.PType.HELLO, P.PType.HELLO_ACK, P.PType.SYNC, P.PType.SYNC_ACK, P.PType.READY,
                                P.PType.START, P.PType.ACK, P.PType.DONE, P.PType.DONE_ACK, P.PType.DATA])
def test_drop_at_every_step_recovers(pt):
    # the first two transmissions of that packet type are lost in each direction
    blob, s, r, ch, clock, rec = setup(fwd=DropFirst(pt), back=DropFirst(pt))
    run_pair(s, r, ch, clock, max_time=300)
    assert s.state == H.CLOSED and r.state == H.CLOSED, (pt, s.state, r.state, s.abort_reason, r.abort_reason)
    assert rec.calls == [blob]


def test_wrong_key_warns_then_aborts_after_10s_no_data_sent():
    blob, s, r, ch, clock, rec = setup(psk_r=BADPSK, cfg=LinkConfig(timeout=1.0))
    seen = types_shown(ch)
    warned_at = None
    while clock.now < 30 and not (s.finished and r.finished):
        pump_sender(s, ch.s_end, clock.now)
        pump_receiver(r, ch.r_end, clock.now)
        if warned_at is None and r.warning == H.KEY_WARNING:
            warned_at = clock.now
        clock.advance(0.05)
    assert warned_at is not None and warned_at < 1
    assert r.state == H.ABORTED and r.abort_reason == "KEY_MISMATCH"
    assert s.abort_reason == "KEY_MISMATCH"
    assert 9.9 <= [e for e in r.events if e[1] == "abort"][0][0] <= 11
    assert set(seen) == {P.PType.HELLO, P.PType.ABORT} and not rec.calls


def test_valid_frame_resets_key_mismatch_timer():
    cfg = LinkConfig(key_mismatch_s=10.0)
    r = H.Receiver(PSK, "/u", cfg=cfg, auto_confirm=True)
    stray = P.Sealer(P.hello_key(BADPSK, "s")).seal(P.PType.HELLO, b"{}")
    good = P.Sealer(P.hello_key(PSK, "s")).seal(P.PType.HELLO, H.protocol.pack_json(
        {"role": "s", "v": 1, "nonce": ("00" * 16), "mode": "file", "caps": H.Caps().to_json(),
         "proposal": Profile(64, 6, 1).to_json()}))
    for t in range(0, 9):
        r.step(float(t), [stray])
    assert r.warning == H.KEY_WARNING and not r.finished
    r.step(9.0, [good])  # valid frame: counter resets, warning clears
    assert r.warning is None and r.state == H.HELLO_ACKED
    for t in range(10, 18):
        r.step(float(t), [stray])
    assert not r.finished  # only 8 s since the reset
    r.step(20.0, [stray])
    assert r.abort_reason == "KEY_MISMATCH"


def test_role_collision():
    a = H.Sender(PSK, *reversed(make(10)), cfg=FAST)
    b = H.Sender(PSK, make(10)[1], b"0123456789", cfg=FAST)
    fa, fb = a.step(0, []), b.step(0, [])
    a.step(0.1, fb)
    b.step(0.1, fa)
    assert a.abort_reason == b.abort_reason == "ROLE_COLLISION"
    r1, r2 = H.Receiver(PSK, "/u", cfg=FAST), H.Receiver(PSK, "/u", cfg=FAST)
    o1, o2 = r1.step(0, []), r2.step(0, [])
    r1.step(0.1, o2)
    r2.step(0.1, o1)
    assert r1.abort_reason == r2.abort_reason == "ROLE_COLLISION"


def test_mode_mismatch():
    blob, man = make(10)
    s = H.Sender(PSK, man, blob, cfg=FAST)
    r = H.Receiver(PSK, "/u", cfg=FAST, mode="cmd")
    f, o = s.step(0, []), r.step(0, [])
    s.step(0.1, o)
    r.step(0.1, f)
    assert s.abort_reason == "MODE_MISMATCH" and r.abort_reason == "MODE_MISMATCH"


@pytest.mark.parametrize("status", ["DEST_NOT_WRITABLE", "NO_SPACE", "EXISTS", "CASE_COLLISION",
                                    "PATH_TOO_LONG", "BAD_MANIFEST"])
def test_sync_ack_statuses_abort(status):
    blob, s, r, ch, clock, rec = setup(r_kw={"check": lambda m: status})
    run_pair(s, r, ch, clock, max_time=60)
    assert s.state == H.ABORTED and s.abort_reason == status
    assert r.abort_reason == status and not rec.calls
    assert s.verify_code is None


class LyingReceiver(H.Receiver):
    """Answers SYNC with the hash of a different manifest."""
    def _sess_seal(self, ptype, body=b"", **kw):
        if ptype == P.PType.SYNC_ACK:
            d = P.unpack_json(body)
            d["sha"] = hashlib.sha256(b"other").hexdigest()
            body = P.pack_json(d)
        return super()._sess_seal(ptype, body, **kw)


def test_mismatched_manifest_hash_aborts():
    blob, s, r, ch, clock, rec = setup(r_cls=LyingReceiver)
    run_pair(s, r, ch, clock, max_time=60)
    assert s.abort_reason == "MANIFEST_MISMATCH" and s.state == H.ABORTED and not rec.calls


def test_verify_code_depends_on_manifest():
    blob, s, r, ch, clock, rec = setup()
    run_pair(s, r, ch, clock)
    b2, m2 = make(20000, seed=2)
    assert s.verify_code != P.verify_code(s.keys, "file", hashlib.sha256(H.manifest_bytes(m2)).digest(),
                                          s.nonce, s._nonce_r)


@pytest.mark.parametrize("who", ["s", "r"])
def test_operator_no_aborts(who):
    blob, man = make()
    clock = ManualClock()
    ch = LoopChannel(clock=clock)
    seen = types_shown(ch)
    codes = []
    s = H.Sender(PSK, man, blob, cfg=FAST, auto_confirm=(who != "s"),
                 confirm=lambda c: codes.append(c) or False)
    r = H.Receiver(PSK, "/u", cfg=FAST, auto_confirm=(who != "r"), check=lambda m: "OK",
                   confirm=lambda c: codes.append(c) or False)
    run_pair(s, r, ch, clock, max_time=60)
    assert codes and len(codes[0]) == 6
    bad, other = (s, r) if who == "s" else (r, s)
    assert bad.abort_reason == "OPERATOR_REJECT"
    assert other.state == H.ABORTED and other.abort_reason == "PEER:OPERATOR_REJECT"
    assert P.PType.DATA not in seen and P.PType.START not in seen


def test_operator_answer_method_and_pending():
    blob, man = make(5000)
    clock = ManualClock()
    ch = LoopChannel(clock=clock)
    s = H.Sender(PSK, man, blob, cfg=FAST, confirm=lambda c: None)  # pending until operator_answer
    r = H.Receiver(PSK, "/u", cfg=FAST, check=lambda m: "OK", extract=Rec(), confirm=lambda c: None)
    run_pair(s, r, ch, clock, max_time=20)
    assert s.state == H.VERIFY and r.state == H.VERIFY and s.verify_code == r.verify_code
    s.operator_answer(True)
    r.operator_answer(True)
    run_pair(s, r, ch, clock, max_time=clock.now + 60)
    assert s.state == H.CLOSED and r.state == H.CLOSED


def test_replay_of_old_session_rejected():
    blob, s, r, ch, clock, rec = setup()
    log = []
    orig = ch.s_end.sink.show

    def show(packets, profile=None):
        log.append(list(packets))
        return orig(packets, profile)
    ch.s_end.sink.show = show
    run_pair(s, r, ch, clock)
    assert r.state == H.CLOSED
    old = [p for f in log for p in f]
    # fresh session B: old packets (incl. SYNC/DATA) must never authenticate R or touch its state
    blob2, man2 = make(3000, seed=7)
    s2 = H.Sender(PSK, man2, blob2, cfg=FAST)
    r2 = H.Receiver(PSK, "/u", cfg=FAST, extract=Rec(), check=lambda m: "OK", auto_confirm=True)
    f = s2.step(0, [])
    r2.step(0, f)
    assert r2.state == H.HELLO_ACKED
    r2.step(0.1, old)
    assert r2.state == H.HELLO_ACKED and r2.stats["mac_fail"] > 0 and r2.manifest is None
    # a HELLO_ACK answering a different session's nonce is ignored by the new sender
    stale = r.step(0.2, [])  # finished engines send nothing; use a fresh receiver for session A's nonce
    ra = H.Receiver(PSK, "/u", cfg=FAST)
    ack_a = [m for m in ra.step(0, s.step(0.0, [])) if P.peek(m).ptype == P.PType.HELLO_ACK]
    s2.step(0.2, ack_a)
    assert s2.state == H.HELLO and not stale


def test_within_session_replay_below_window():
    r = H.Receiver(PSK, "/u", cfg=FAST, extract=Rec(), check=lambda m: "OK")
    s = H.Sender(PSK, *reversed(make(3000)), cfg=FAST)
    s.step(0, [])
    r.step(0, [s._hello_raw])
    assert r.state == H.HELLO_ACKED
    sl = P.Sealer(r.keys.k_s2r, r.keys.session_id, start=5000)
    r.step(1, [sl.seal(P.PType.SYNC, b"x", obj_id=0, seq=0, total=9)])
    assert r.state == H.AUTHED
    old = P.Sealer(r.keys.k_s2r, r.keys.session_id, start=1).seal(P.PType.SYNC, b"y", obj_id=0, seq=1, total=9)
    r.step(2, [old])
    assert r.stats["replay"] == 1 and any(e[1] == "replay" for e in r.events)
    assert r._rx_m.r.received == 1


def test_receiver_not_authed_until_session_packet():
    s = H.Sender(PSK, *reversed(make(3000)), cfg=FAST)
    r = H.Receiver(PSK, "/u", cfg=FAST, check=lambda m: "OK")
    hello = s.step(0, [])
    r.step(0, hello)
    for t in (0.5, 1.0, 1.1):
        r.step(t, hello)  # replayed HELLO proves nothing
    assert r.state == H.HELLO_ACKED
    ack = r.step(5, [])
    sync = s.step(5, [m for m in ack if P.peek(m).ptype == P.PType.HELLO_ACK])
    assert P.peek(sync[0]).ptype == P.PType.SYNC
    r.step(5.2, sync)
    assert r.state in (H.AUTHED, H.VERIFY)  # tiny manifest: one tile completes SYNC


def test_hello_timeout_and_reshow():
    blob, man = make(10)
    s = H.Sender(PSK, man, blob, cfg=LinkConfig(timeout=1.0, hello_s=5.0))
    shown = [t for t in range(0, 8) if s.step(float(t), [])]
    assert shown == [0, 1, 2, 3, 4, 5] or shown[:5] == [0, 1, 2, 3, 4]
    assert s.abort_reason == "HELLO_TIMEOUT"
    r = H.Receiver(PSK, "/u", cfg=LinkConfig(hello_s=3.0))
    for t in range(6):
        r.step(float(t), [])
    assert r.abort_reason == "HELLO_TIMEOUT"


def test_sync_timeout():
    blob, s, r, ch, clock, rec = setup(cfg=LinkConfig(timeout=1.0, sync_s=5.0))
    # R answers HELLO then goes silent
    s.step(0, [])
    r.step(0, [s._hello_raw])
    s.step(0.2, [r._hello_ack_raw])
    assert s.state == H.AUTHED
    for t in range(1, 10):
        s.step(0.2 + t, [])
    assert s.abort_reason == "SYNC_TIMEOUT"


def test_verify_timeout():
    blob, man = make(2000)
    clock = ManualClock()
    ch = LoopChannel(clock=clock)
    cfg = LinkConfig(timeout=1.0, verify_s=5.0)
    s = H.Sender(PSK, man, blob, cfg=cfg)
    r = H.Receiver(PSK, "/u", cfg=cfg, check=lambda m: "OK")
    run_pair(s, r, ch, clock, max_time=60)
    assert s.abort_reason.endswith("VERIFY_TIMEOUT") and r.abort_reason.endswith("VERIFY_TIMEOUT")


def test_retries_exhausted_in_data():
    blob, s, r, ch, clock, rec = setup(cfg=LinkConfig(timeout=1.0, retries=3))
    while s.state != H.DATA and clock.now < 60:
        pump_sender(s, ch.s_end, clock.now)
        pump_receiver(r, ch.r_end, clock.now)
        clock.advance(0.05)
    assert s.state == H.DATA
    while not s.finished and clock.now < 200:  # receiver vanishes
        pump_sender(s, ch.s_end, clock.now)
        clock.advance(0.05)
    assert s.abort_reason == "RETRIES_EXHAUSTED"


def test_receiver_idle_timeout_and_missing_done_ack():
    blob, s, r, ch, clock, rec = setup(cfg=LinkConfig(timeout=1.0, retries=3, idle_s=5.0))
    while s.state != H.DATA and clock.now < 60:
        pump_sender(s, ch.s_end, clock.now)
        pump_receiver(r, ch.r_end, clock.now)
        clock.advance(0.05)
    while not r.finished and clock.now < 200:  # sender vanishes
        pump_receiver(r, ch.r_end, clock.now)
        clock.advance(0.05)
    assert r.abort_reason == "IDLE_TIMEOUT"
    # DONE_ACK never arrives: warning, data committed
    blob, s, r, ch, clock, rec = setup(cfg=LinkConfig(timeout=1.0, retries=3))
    while r.state != H.DONE and clock.now < 100:
        pump_sender(s, ch.s_end, clock.now)
        pump_receiver(r, ch.r_end, clock.now)
        clock.advance(0.05)
    while not r.finished and clock.now < 200:
        pump_receiver(r, ch.r_end, clock.now)
        clock.advance(0.05)
    assert r.state == H.CLOSED and r.warning == "NO_DONE_ACK" and rec.calls == [blob]


def test_new_session_after_abort():
    blob, s, r, ch, clock, rec = setup(r_kw={"check": lambda m: "EXISTS"})
    run_pair(s, r, ch, clock, max_time=30)
    assert s.finished
    blob2, s2, r2, ch2, clock2, rec2 = setup(blob, make()[1])
    run_pair(s2, r2, ch2, clock2)
    assert r2.state == H.CLOSED and s2.keys.session_id != s.keys.session_id


def test_sha_mismatch_reported():
    blob, man = make(3000)
    man["archive_sha256"] = hashlib.sha256(b"nope").hexdigest()
    blob, s, r, ch, clock, rec = setup(blob, man)
    run_pair(s, r, ch, clock)
    assert r.result["status"] == "SHA_MISMATCH" and not rec.calls
    assert s.result["sha256_ok"] is False


def test_real_archive_end_to_end(tmp_path):
    from lightferry import archive
    src = tmp_path / "proj"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_bytes(b"hello " * 3000)
    (src / "sub" / "b.bin").write_bytes(random.Random(3).randbytes(7000))
    blob, man = archive.pack(src)
    dest = tmp_path / "out"
    clock = ManualClock()
    ch = LoopChannel(clock=clock, fwd=Impairment(drop=0.2, seed=4), back=Impairment(drop=0.2, seed=5))
    s = H.Sender(PSK, man, blob, cfg=LinkConfig(timeout=1.0, retries=30), auto_confirm=True)
    r = H.Receiver(PSK, dest, cfg=LinkConfig(timeout=1.0, retries=30), auto_confirm=True)
    run_pair(s, r, ch, clock, max_time=2000)
    assert r.state == H.CLOSED and r.result["ok"], (r.result, r.abort_reason)
    assert (dest / "proj" / "a.txt").read_bytes() == b"hello " * 3000
    # second send into the same dest collides
    s2 = H.Sender(PSK, man, blob, cfg=FAST, auto_confirm=True)
    r2 = H.Receiver(PSK, dest, cfg=FAST, auto_confirm=True)
    ch2 = LoopChannel(clock=(c2 := ManualClock()))
    run_pair(s2, r2, ch2, c2, max_time=60)
    assert s2.abort_reason == "EXISTS"


def test_default_manifest_check(tmp_path, monkeypatch):
    blob, man = make(100)
    assert H.default_manifest_check(man, tmp_path) == "OK"
    (tmp_path / "x.bin").write_bytes(b"1")
    assert H.default_manifest_check(man, tmp_path) == "EXISTS"
    assert H.default_manifest_check(man, tmp_path, overwrite=True) == "OK"
    (tmp_path / "x.bin").unlink()
    (tmp_path / "X.BIN").write_bytes(b"1")
    assert H.default_manifest_check(man, tmp_path) == "CASE_COLLISION"
    (tmp_path / "X.BIN").unlink()
    f1 = {"path": "a", "size": 1, "sha256": "0" * 64}
    m2 = dict(man, kind="dir", files=[f1, dict(f1, path="A")])
    assert H.default_manifest_check(m2, tmp_path) == "BAD_MANIFEST"  # colliding own entries
    assert H.default_manifest_check(dict(man, kind="dir", files=[f1]), tmp_path) == "OK"
    import shutil
    monkeypatch.setattr(shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(10, 10, 10))
    assert H.default_manifest_check(man, tmp_path) == "NO_SPACE"
    monkeypatch.undo()
    assert H.default_manifest_check({"name": "x"}, tmp_path) == "BAD_MANIFEST"
    assert H.default_manifest_check(dict(man, compression="rar"), tmp_path) == "BAD_MANIFEST"
    f = tmp_path / "file"
    f.write_bytes(b"")
    assert H.default_manifest_check(man, f / "sub") == "DEST_NOT_WRITABLE"


def test_sync_check_uses_the_extract_size_limit(tmp_path):
    """SYNC must refuse what unpack() would refuse after the whole transfer (archive.DEFAULT_MAX_BYTES)."""
    from lightferry import archive
    blob, man = make(100)
    big = archive.DEFAULT_MAX_BYTES + 1
    huge = dict(man, raw_size=big, files=[dict(man["files"][0], size=big)])
    assert H.default_manifest_check(huge, tmp_path) == "BAD_MANIFEST"
    assert H.default_manifest_check(dict(man, archive_size=big), tmp_path) == "BAD_MANIFEST"
    assert H.default_manifest_check(man, tmp_path, max_bytes=99) == "BAD_MANIFEST"
    assert H.default_manifest_check(man, tmp_path, max_bytes=100) == "OK"


def _authed_pair():
    """S and R (camera back-channel) right after HELLO / HELLO_ACK, plus a sealer with R's session key."""
    blob, s, r, ch, clock, rec = setup()
    hello = s.step(0.0, [])
    s.step(0.1, r.step(0.0, hello))
    assert s.state == H.AUTHED
    return s, P.Sealer(s.keys.k_r2s, s.keys.session_id)


@pytest.mark.parametrize("body", [b"[1,2]", b'"str"', b"7", b"null", b"{}"])
def test_malformed_sync_ack_aborts_cleanly(body):
    s, seal = _authed_pair()
    s.step(0.2, [seal.seal(P.PType.SYNC_ACK, body)])
    assert s.state == H.ABORTED and s.abort_reason == "BAD_SYNC_ACK"


@pytest.mark.parametrize("body", [b"[1,2]", b'"x"', b"null"])
def test_malformed_abort_and_done_bodies(body):
    s, seal = _authed_pair()
    s.step(0.2, [seal.seal(P.PType.ABORT, body)])
    assert s.state == H.ABORTED and s.abort_reason == "PEER:?"
    s, seal = _authed_pair()
    s._on_done(P.open_packet(s.keys.k_r2s, seal.seal(P.PType.DONE, body), P.ReplayWindow()), 0.3)
    assert s.state == H.CLOSED and s.result == {"status": "BAD_DONE"}


def test_negotiate():
    a, b = H.Caps(grids=(32, 64), tiles=(1, 2, 3)), H.Caps(grids=(64, 96), tiles=(1, 2))
    assert H.negotiate(Profile(64, 6, 3), a, b) == Profile(64, 6, 2)
    assert H.negotiate(Profile(64, 6, 3), H.Caps(grids=(32,)), H.Caps(grids=(96,))) is None


def test_hello_fits_bootstrap_code():
    blob, man = make(10)
    s = H.Sender(PSK, man, blob)
    assert len(s.step(0, [])[0]) <= 491


def test_loop_image_mode():
    cv2 = pytest.importorskip("cv2")  # noqa: F841
    try:
        from lightferry import vision  # noqa: F401
    except ImportError:
        pytest.skip("vision.py not available")
    blob, man = make(2500)
    blob, s, r, ch, clock, rec = setup(blob, man, image_mode=True, proposal=Profile(64, 4, 2))
    run_pair(s, r, ch, clock, max_time=120)
    assert r.state == H.CLOSED and rec.calls == [blob]


def test_dir_channel(tmp_path):
    pytest.importorskip("cv2")
    from lightferry import codec
    try:
        from lightferry import vision  # noqa: F401
    except ImportError:
        pytest.skip("vision.py not available")
    s_end, r_end = DirChannel(tmp_path, "s"), DirChannel(tmp_path, "r")
    pk = [bytes([i]) * 100 for i in range(4)]
    s_end.sink.show(pk, Profile(64, 6, 2))
    got = r_end.source.read(1.0, [Profile(64, 6, 2)])
    assert sorted(got) == sorted(pk)
    assert r_end.source.read(0.0, [Profile(64, 6, 2)]) is None
    r_end.back.send(b"ack1")
    r_end.back.send(b"ack2")
    assert s_end.back.recv(1.0) == b"ack1" and s_end.back.recv(1.0) == b"ack2"
    assert s_end.back.recv(0.0) is None


def test_impairment_seeded_and_hooks():
    pk = [bytes([i]) * 60 for i in range(50)]
    a = Impairment(drop=0.3, corrupt=0.3, seed=7).frame(pk)
    b = Impairment(drop=0.3, corrupt=0.3, seed=7).frame(pk)
    assert a == b and 0 < len(a) < 50 and a != pk
    assert Impairment(frame_drop=1.0).frame(pk) is None
    clock = ManualClock()
    ch = LoopChannel(clock=clock, back=Impairment(delay=2.0))
    ch.r_end.back.send(b"x")
    assert ch.s_end.back.recv(0) is None
    clock.advance(2.0)
    assert ch.s_end.back.recv(0) == b"x"
