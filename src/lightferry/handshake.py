"""HANDOFF state machines (PLAN section 6): transport-agnostic ``Sender`` and ``Receiver`` engines.

Both are tick driven and never block or touch a transport::

    out = engine.step(now, inbound_packets)      # now: any monotonic float, inbound: list[bytes]

* ``Sender.step`` returns the packets of ONE frame to (re)display (``[]`` = keep showing the current
  frame); render it with ``sender.profile`` (bootstrap profile until the data phase starts).
* ``Receiver.step`` returns packets to send on the back channel (each its own message).
* Receivers should decode frames with each of ``receiver.decode_profiles``.
* Engines are single use. After ``finished`` (CLOSED or ABORTED) create fresh engines for a new
  session; they draw fresh nonces, so packets of any old session fail the MAC (new-session rule).
  A finished Sender must keep being stepped until the peer is done so it can re-answer a repeated DONE.
  Keep the last ABORT/DONE_ACK frame shown for ``linger_s``.
* VERIFY: ``engine.verify_code`` is set; answer via ``confirm(code) -> bool | None`` (called once;
  None = pending), ``engine.operator_answer(ok)``, or ``auto_confirm=True``. The CLI asks the operator on
  real hardware and auto-confirms only for test channels (``dir:``/``loop``/``--yes``); the phone server asks
  on its pages.
* Observability: ``state``, ``history``, ``abort_reason``, ``warning`` ("KEY MISMATCH?" while MAC
  failures persist), ``result``, ``progress``, ``stats``, ``events``.

Abort reasons: HELLO_TIMEOUT SYNC_TIMEOUT VERIFY_TIMEOUT DONE_TIMEOUT IDLE_TIMEOUT RETRIES_EXHAUSTED
KEY_MISMATCH ROLE_COLLISION MODE_MISMATCH MANIFEST_MISMATCH OPERATOR_REJECT NO_COMMON_PROFILE BAD_PROFILE
BAD_HELLO BAD_SYNC_ACK CMD_UNSUPPORTED, the SYNC_ACK statuses (DEST_NOT_WRITABLE NO_SPACE EXISTS CASE_COLLISION
PATH_TOO_LONG BAD_MANIFEST) and ``PEER:<reason>`` when the peer aborted.
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import os
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import protocol, tiler
from .link import BOOTSTRAP, LinkConfig, Profile, TileRx, TileTx, decode_ack, encode_ack
from .protocol import (OBJ_ARCHIVE, OBJ_CTRL, OBJ_MANIFEST, PType, ROLE_R, ROLE_S, FLAG_LAST, Packet,
                       ReplayWindow)

PROTO_VERSION = 1
MAX_MANIFEST_TILES = 16  # a few tiles: a manifest never needs more
MAX_CANDIDATES = 4  # R: concurrent HELLO_ACKED candidate sessions
KEY_MISMATCH_MIN_FAILS = 3
DONE_WAIT_MIN_S = 180.0  # S waits at least this long for DONE (R may be extracting)

IDLE, HELLO, HELLO_ACKED, AUTHED, SYNCED, VERIFY, READY, START, DATA, DONE, CLOSED, ABORTED = (
    "IDLE", "HELLO", "HELLO_ACKED", "AUTHED", "SYNCED", "VERIFY", "READY", "START", "DATA", "DONE",
    "CLOSED", "ABORTED")

KEY_WARNING = "KEY MISMATCH?"
STATUS_OK = "OK"

# ---- audio back-channel: R answers with 2-byte note tokens instead of packets
AUDIO_NONCE_R = bytes(16)  # R sends no nonce over audio: keys come from the PSK and S's fresh nonce
# ...so R itself must refuse a nonce_S it accepted before (a recorded S screen replayed later): the used nonces
# are kept in <dest>/SEEN_FILE (mode 600, the newest SEEN_MAX)
SEEN_FILE = ".lightferry-seen"
SEEN_MAX = 4096
NOTE_GRACE_S = 2.0  # S: tokens for the previous cycle's codes stay valid this long after a re-sign
OK, SYNC_OK, DONE_OK, DONE_FAIL = b"ok", b"sync", b"done", b"fail"
# R speaks three times per transfer: HELLO seen, manifest complete, archive done. The manifest and the data
# (START first) cycle on screen meanwhile; a done token may answer a resealed START as well as a DATA tile.
_NOTE_KINDS = {PType.HELLO: (OK,), PType.SYNC: (SYNC_OK,), PType.START: (DONE_OK, DONE_FAIL),
               PType.DATA: (DONE_OK, DONE_FAIL)}


def note_token(keys: protocol.SessionKeys, kind: bytes, ptype: int, counter: int, extra: bytes = b"") -> bytes:
    """R's whole reply over audio: "I got your packet (ptype, counter)" plus what it led to (``kind``), keyed with
    the session's R->S key so it can't be forged or replayed into another session. 16 bits = 4 notes."""
    msg = b"lf-note" + kind + bytes([ptype]) + counter.to_bytes(4, "big") + extra
    return hmac.new(keys.k_r2s, msg, hashlib.sha256).digest()[:2]


class HandoffError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Caps:
    """What a side supports (sent in HELLO). ``compress`` lists archive compressions it can unpack."""
    grids: tuple = (32, 64, 96, 128, 177)
    module_px: tuple = (2, 3, 4, 6, 8)
    tiles: tuple = (1, 2, 3, 4)
    compress: tuple = ("none", "zlib", "lzma")  # archive.py needs lzma, so every side unpacks all three
    max_res: tuple | None = None

    def to_json(self) -> dict:
        return {"grids": list(self.grids), "module_px": list(self.module_px), "tiles": list(self.tiles),
                "compress": list(self.compress), "max_res": list(self.max_res) if self.max_res else None}

    @classmethod
    def from_json(cls, d) -> "Caps":
        try:
            return cls(tuple(int(x) for x in d["grids"]), tuple(int(x) for x in d["module_px"]),
                       tuple(int(x) for x in d["tiles"]), tuple(str(x) for x in d["compress"]),
                       tuple(d["max_res"]) if d.get("max_res") else None)
        except (KeyError, TypeError, ValueError) as e:
            raise protocol.ProtocolError("bad caps") from e


def negotiate(proposal: Profile, a: Caps, b: Caps) -> Profile | None:
    """Best common profile: the proposal where both support it, else the closest common value."""
    def pick(want, x, y):
        common = sorted(set(x) & set(y))
        return min(common, key=lambda v: (abs(v - want), -v)) if common else None
    g = pick(proposal.grid, a.grids, b.grids)
    m = pick(proposal.module_px, a.module_px, b.module_px)
    t = pick(proposal.tiles_n, a.tiles, b.tiles)
    if None in (g, m, t):
        return None
    return Profile(g, m, t)


def manifest_bytes(manifest: dict) -> bytes:
    return protocol.pack_json(manifest)


# ---------------------------------------------------------------- receiver-side manifest checks

def default_manifest_check(manifest: dict, dest: Path, overwrite: bool = False,
                           compress: tuple = ("none", "zlib", "lzma"), max_bytes: int | None = None) -> str:
    """Return a SYNC_ACK status: OK | BAD_MANIFEST | DEST_NOT_WRITABLE | NO_SPACE | EXISTS |
    CASE_COLLISION | PATH_TOO_LONG (PLAN section 6 step 3). ``max_bytes`` must match what the extract step
    allows (default ``archive.DEFAULT_MAX_BYTES``), so a manifest accepted here is never refused after the data.
    Collisions among the manifest's own entries are already BAD_MANIFEST (``archive.parse_manifest``)."""
    from . import archive
    try:
        archive.parse_manifest(manifest, archive.DEFAULT_MAX_BYTES if max_bytes is None else max_bytes)
    except archive.ArchiveError:
        return "BAD_MANIFEST"
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return "BAD_MANIFEST"
    if manifest["compression"] not in compress:
        return "BAD_MANIFEST"
    name, files = manifest["name"], manifest["files"]
    a_size, raw = manifest["archive_size"], manifest["raw_size"]
    dest = Path(dest)
    try:
        dest.mkdir(parents=True, exist_ok=True)
        probe = dest / f".lightferry-probe-{os.getpid()}"
        probe.write_bytes(b"x")
        probe.unlink()
    except OSError:
        return "DEST_NOT_WRITABLE"
    if os.name == "nt" and files and len(str(dest)) + 1 + len(name) + max(len(f["path"]) for f in files) >= 260:
        return "PATH_TOO_LONG"
    key = unicodedata.normalize("NFC", name).casefold()
    for p in dest.iterdir():
        if p.name.startswith(".lightferry-"):
            continue
        if p.name == name:
            if not overwrite:
                return "EXISTS"
        elif unicodedata.normalize("NFC", p.name).casefold() == key:
            return "CASE_COLLISION"
    if shutil.disk_usage(dest).free < raw + a_size:
        return "NO_SPACE"
    return STATUS_OK


def default_extract(blob: bytes, dest: Path, manifest: dict, overwrite: bool = False):
    """Thin adapter over archive.unpack(blob, dest, manifest, overwrite) -> list of paths."""
    from . import archive
    return archive.unpack(blob, dest, manifest, overwrite=overwrite)


# ---------------------------------------------------------------- shared engine base

class _Engine:
    ROLE = ""
    PEER = ""

    def __init__(self, psk: bytes, cfg: LinkConfig | None, caps: Caps | None, mode: str,
                 confirm: Callable[[str], bool | None] | None, auto_confirm: bool,
                 rng: Callable[[int], bytes], backchannel: str, encrypt: bool = False):
        if mode not in ("file", "cmd"):
            raise ValueError("mode must be 'file' or 'cmd'")
        self.psk = psk
        self.encrypt = bool(encrypt)  # --encrypt: session packet bodies are encrypted with the key
        self.cfg = cfg or LinkConfig()
        self.caps = caps or Caps()
        self.mode = mode
        self.backchannel = backchannel
        self.confirm = confirm
        self.auto_confirm = auto_confirm
        self.rng = rng
        self.state = IDLE
        self.history: list[str] = [IDLE]
        self.abort_reason: str | None = None
        self.warning: str | None = None
        self.verify_code: str | None = None
        self.result: dict | None = None
        self.stats = {"valid": 0, "mac_fail": 0, "replay": 0, "duplicate": 0, "noise": 0}
        self.events: list[tuple[float, str, str]] = []
        self.nonce = rng(16)
        self.keys: protocol.SessionKeys | None = None
        self.profile: Profile = BOOTSTRAP
        self.data_profile: Profile = BOOTSTRAP
        self.linger_s = self.cfg.linger_s
        self._hello_key_own = protocol.hello_key(psk, self.ROLE)
        self._hello_key_peer = protocol.hello_key(psk, self.PEER)
        self._hello_sealer = protocol.Sealer(self._hello_key_own)
        self._sess_sealer: protocol.Sealer | None = None
        self._win = ReplayWindow()
        self._mac_fail_since: float | None = None
        self._mac_fail_last = 0.0
        self._mac_fail_n = 0
        self._phase_start = 0.0
        self._answer: bool | None = None
        self._asked = False
        self.last_valid = 0.0
        self._seen_tags: set[bytes] = set()
        self._fresh: tuple[int, int] | None = None  # (ptype, counter) of a never-seen packet in this step

    # ---- public helpers
    @property
    def finished(self) -> bool:
        return self.state in (CLOSED, ABORTED)

    def operator_answer(self, ok: bool) -> None:
        """Operator's y/n for the VERIFY code (alternative to ``confirm``)."""
        self._answer = bool(ok)

    # ---- internals
    def _set(self, state: str, now: float | None = None) -> None:
        self.state = state
        self.history.append(state)
        if now is not None:
            self._phase_start = now

    def _event(self, now: float, kind: str, detail: str = "") -> None:
        if len(self.events) < 2000:
            self.events.append((now, kind, detail))

    def _emit(self, packets: list[bytes]) -> None:  # overridden
        raise NotImplementedError

    def _sess_seal(self, ptype, body=b"", **kw) -> bytes:
        return self._sess_sealer.seal(ptype, body, **kw)

    def _hello_seal(self, ptype, body=b"") -> bytes:
        return self._hello_sealer.seal(ptype, body)

    def _sess_candidates(self):
        """[(rx_key, window, SessionKeys)] session keys an inbound packet may be sealed with."""
        if self.keys is None:
            return []
        return [(self.keys.rx_key(self.ROLE), self._win, self.keys)]

    def _candidates(self, ptype: int):
        """Candidate (name, key, window, keys) for an inbound packet type. Names: hello_peer, hello_own,
        hello_abort, sess."""
        c = []
        if ptype == PType.HELLO:
            if self.keys is None:
                c = [("hello_peer", self._hello_key_peer, None, None),
                     ("hello_own", self._hello_key_own, None, None)]
            elif self.ROLE == ROLE_R and self.state in (HELLO_ACKED, AUTHED):
                c = [("hello_peer", self._hello_key_peer, None, None)]
        elif ptype == PType.HELLO_ACK:
            if self.ROLE == ROLE_S and self.keys is None:
                c = [("hello_peer", self._hello_key_peer, None, None)]
        else:
            if ptype == PType.ABORT and self.keys is None:
                c.append(("hello_abort", self._hello_key_peer, ReplayWindow(8), None))
            for key, win, kk in self._sess_candidates():
                c.append(("sess", key, win, kk))
        return c

    def _echoes_me(self, pkt: Packet) -> bool:
        try:
            d = pkt.json()
        except protocol.ProtocolError:
            return False
        return isinstance(d, dict) and d.get("nonce") == self.nonce.hex()

    def _on_raw(self, raw: bytes, now: float) -> None:
        h = protocol.peek(raw)
        if h is None:
            self.stats["noise"] += 1
            return
        cands = self._candidates(h.ptype)
        if not cands:
            return
        for name, key, win, kk in cands:
            try:
                pkt = protocol.open_packet(key, raw, win, kk.enc_rx(self.ROLE) if (name == "sess" and kk is not None) else None)
            except protocol.ReplayError:
                self.stats["replay"] += 1
                self._event(now, "replay", f"ptype={h.ptype} counter={h.counter}")
                self._valid(now, new=False)
                return
            except protocol.AuthError:
                continue
            if name == "sess" and pkt.session_id != kk.session_id:
                continue
            self._remember(raw, pkt)
            if name == "hello_abort" and not self._echoes_me(pkt):
                return  # hello-keyed ABORT must echo our own nonce
            self._valid(now, new=not pkt.duplicate)
            if pkt.duplicate:
                self.stats["duplicate"] += 1
            if name == "sess":
                self._on_session_packet(now, kk)
            if not self.finished or (self.ROLE == ROLE_S and self.state == CLOSED):
                self._handle(pkt, name, now)
            return
        self._mac_failed(now)

    def _remember(self, raw: bytes, pkt: Packet) -> None:
        """Audio: a code still on screen is seen again and again; only a never-seen packet counts as asking."""
        tag = raw[-protocol.TAG_LEN:]
        if tag not in self._seen_tags:
            if len(self._seen_tags) >= 4096:
                self._seen_tags.clear()
            self._seen_tags.add(tag)
            self._fresh = (pkt.ptype, pkt.counter)

    def _valid(self, now: float, new: bool = True) -> None:
        self.stats["valid"] += 1
        if new:
            self.last_valid = now
        self._mac_fail_since = None
        self._mac_fail_n = 0
        if self.warning == KEY_WARNING:
            self.warning = None

    def _mac_failed(self, now: float) -> None:
        self.stats["mac_fail"] += 1
        if self._mac_fail_since is not None and now - self._mac_fail_last > 2 * self.cfg.timeout:
            self._mac_fail_since, self._mac_fail_n = None, 0  # quiet gap: the old streak is over
        if self._mac_fail_since is None:
            self._mac_fail_since = now
            self._mac_fail_n = 0
            self.warning = KEY_WARNING
            self._event(now, "mac_fail", "CRC-good frame failed MAC: wrong key?")
        self._mac_fail_n += 1
        self._mac_fail_last = now

    def _check_key_mismatch(self, now: float) -> None:
        if self._mac_fail_since is None or self.finished:
            return
        if now - self._mac_fail_last > 2 * self.cfg.timeout:  # failures stopped: stray frame, not a mismatch
            self._mac_fail_since, self._mac_fail_n = None, 0
            if self.warning == KEY_WARNING:
                self.warning = None
            return
        if now - self._mac_fail_since >= self.cfg.key_mismatch_s and self._mac_fail_n >= KEY_MISMATCH_MIN_FAILS:
            self._abort(now, "KEY_MISMATCH")

    def _on_session_packet(self, now: float, keys=None) -> None:
        pass

    def _handle(self, pkt: Packet, name: str, now: float) -> None:
        raise NotImplementedError

    def _abort(self, now: float, reason: str, notify: bool = True, peer: bool = False) -> None:
        if self.state in (ABORTED, CLOSED):
            return
        self.abort_reason = ("PEER:" + reason) if peer else reason
        self._event(now, "abort", self.abort_reason)
        if notify and not peer:
            self._emit(self._abort_packets(reason))
        self._set(ABORTED, now)

    def _abort_packets(self, reason: str) -> list[bytes]:
        body = protocol.pack_json({"reason": reason})
        return [self._sess_seal(PType.ABORT, body) if self._sess_sealer is not None
                else self._hello_seal(PType.ABORT, body)]

    def _operator(self) -> bool | None:
        if self._answer is None:
            if self.auto_confirm:
                self._answer = True
            elif self.confirm is not None and not self._asked:
                self._asked = True
                r = self.confirm(self.verify_code)
                if r is not None:
                    self._answer = bool(r)
        return self._answer

    def _hello_body(self, extra: dict) -> bytes:
        body = protocol.pack_json({"role": self.ROLE, "v": PROTO_VERSION, "nonce": self.nonce.hex(),
                                   "mode": self.mode, "backchannel": self.backchannel,
                                   "caps": self.caps.to_json(), **extra})
        if len(body) > BOOTSTRAP.body_capacity:
            raise ValueError("HELLO does not fit one bootstrap code")
        return body

    def _on_peer_abort(self, pkt: Packet, now: float) -> None:
        try:
            d = pkt.json()
            reason = str(d.get("reason", "?")) if isinstance(d, dict) else "?"
        except protocol.ProtocolError:
            reason = "?"
        self._abort(now, reason, notify=False, peer=True)


# ---------------------------------------------------------------- Sender

class Sender(_Engine):
    """S side. States: IDLE HELLO AUTHED(+SYNC tiles) VERIFY START DATA DONE CLOSED | ABORTED.

    ``manifest``/``blob`` come from ``archive.pack(path) -> (blob, manifest)``. ``proposal`` is S's
    data-phase profile proposal (grid/module_px/tiles). Result after CLOSED: ``result`` = R's DONE body.
    """
    ROLE, PEER = ROLE_S, ROLE_R

    def __init__(self, psk: bytes, manifest: dict | None = None, blob: bytes = b"", *, mode: str = "file",
                 cfg: LinkConfig | None = None, caps: Caps | None = None, proposal: Profile | None = None,
                 backchannel: str = "camera", confirm=None, auto_confirm: bool = False,
                 rng: Callable[[int], bytes] = os.urandom, encrypt: bool = False):
        super().__init__(psk, cfg, caps, mode, confirm, auto_confirm, rng, backchannel, encrypt)
        if mode == "file" and manifest is None:
            raise ValueError("file mode needs a manifest")
        self.manifest = manifest
        self.blob = blob
        self.manifest_raw = manifest_bytes(manifest) if manifest is not None else b""
        self.manifest_sha = hashlib.sha256(self.manifest_raw).digest()
        self.proposal = proposal or Profile(64, 6, 3)
        if backchannel == "audio":  # one tile per frame: each note token answers exactly one shown packet
            self.proposal = Profile(self.proposal.grid, self.proposal.module_px, 1)
        self._frame: list[bytes] = []
        self._hello_raw: bytes | None = None
        self._shown_at = 0.0
        self._nonce_r = b""
        self._tx_m: TileTx | None = None
        self._tx_d: TileTx | None = None
        self._ready = False
        self._op_ok = False
        self._start_raw: bytes | None = None
        self._tries = 0
        self._done_ack_raw: bytes | None = None
        self._showing: list[bytes] = []  # the frame on screen now
        self._showing_at = 0.0
        # audio: (shown_at, [(note token, (kind, ptype, obj_id, seq))]) per recently shown packet
        self._shown_log: collections.deque = collections.deque(maxlen=64)
        self._cycle_at = 0.0  # audio: when the codes on screen were last (re)signed
        self._car: list[bytes] = []  # audio: the codes cycling on screen (manifest, or START + data)
        self._car_tx: TileTx | None = None
        self._car_lead: list[bytes] = []
        self._car_i, self._car_at, self._car_sealed_at = 0, 0.0, 0.0
        self._heard_frame: list[bytes] = []
        self.expected: dict[bytes, tuple] = {}
        self._note_keys = (protocol.derive_keys(psk, self.nonce, AUDIO_NONCE_R, self.encrypt) if backchannel == "audio"
                           else None)

    def _emit(self, packets: list[bytes]) -> None:
        self._frame = list(packets)

    @property
    def progress(self) -> tuple[int, int]:
        t = self._tx_d
        return (t.base, t.total) if t else (0, 0)

    def step(self, now: float, inbound: list[bytes] = ()) -> list[bytes]:
        self._frame, self._heard_frame = self._heard_frame, []  # a frame ``hear`` decided on (DONE_ACK)
        if self.state == IDLE:
            self._set(HELLO, now)
            self._hello_raw = self._hello_seal(
                PType.HELLO, self._hello_body({"proposal": self.proposal.to_json()}))
            self._frame, self._shown_at, self._cycle_at = [self._hello_raw], now, now
        for raw in inbound:
            self._on_raw(raw, now)
        if self.state not in (ABORTED, CLOSED):
            self._check_key_mismatch(now)
            self._tick(now)
        frame, self._frame = self._frame, []
        if self.backchannel == "audio" and not self.finished:
            # R answers only packets it has not seen, so a byte-identical re-show asks nothing. When nothing
            # was heard for a timeout, re-sign the frame on screen: the code changes and R answers it once.
            if frame == self._showing:
                frame = []
            if (not frame and self._showing and self.state not in (AUTHED, DATA)
                    and now - max(self.last_valid, self._showing_at) >= self.cfg.timeout):
                frame = [self._reseal(p) for p in self._showing]
                self._cycle_at = now
        if frame:
            self._showing, self._showing_at = frame, now
            if self._note_keys is not None:
                for p in frame:
                    self._log_shown(p, now)
        if self._note_keys is not None:
            self._refresh_expected(now)
        return frame

    # ---- audio: cycle an object's codes on screen until R's one note says it has them all
    def _car_start(self, tx: TileTx, lead: list[bytes], now: float) -> None:
        self._car_tx, self._car_lead, self._car = tx, lead, []
        self._car_seal(now)

    def _car_seal(self, now: float) -> None:
        """(Re)sign every code: fresh counters, so R (which ignores codes it has seen) answers again."""
        tx = self._car_tx
        tx._sealed.clear()
        tx._counters.clear()
        self._car_lead = [self._reseal(p) if self._car else p for p in self._car_lead]
        self._car = self._car_lead + tx._packets(list(range(tx.total)))
        self._car_i, self._car_at, self._car_sealed_at = 0, -1e18, now
        self._cycle_at = now

    def _car_tick(self, now: float) -> None:
        if now - self._car_at < self.cfg.dwell_s:
            return
        if self._car_i >= len(self._car):
            self._car_i = 0
            if now - max(self.last_valid, self._car_sealed_at) >= self.cfg.timeout:
                self._car_seal(now)  # a full cycle and nothing heard for a timeout: ask again
        i = self._car_i
        self.profile = BOOTSTRAP if i < len(self._car_lead) or self.state == AUTHED else self.data_profile
        self._frame, self._car_i, self._car_at = [self._car[i]], i + 1, now

    def _log_shown(self, raw: bytes, now: float) -> None:
        h = protocol.peek(raw)
        toks = []
        for kind in _NOTE_KINDS.get(h.ptype, ()):
            extra = self.manifest_sha if kind == SYNC_OK else b""
            toks.append((note_token(self._note_keys, kind, h.ptype, h.counter, extra), (kind, h.ptype, h.obj_id, h.seq)))
        self._shown_log.append((now, toks))

    def _refresh_expected(self, now: float) -> None:
        """Valid tokens: answers that fit the current state, for codes of the latest cycle (plus a short grace
        for the cycle before a re-sign) shown within the last timeout. A 16-bit token is guessable, so the
        fewer are valid at once, the lower the odds that noise or a forger hits one (PLAN section 11)."""
        kinds = {HELLO: (OK,), AUTHED: (SYNC_OK,), DATA: (DONE_OK, DONE_FAIL)}.get(self.state, ())
        t0 = max(self._cycle_at - NOTE_GRACE_S, now - self.cfg.timeout)
        exp = {}
        if kinds and not self.finished:
            for at, toks in self._shown_log:
                if at >= t0:
                    exp.update((t, e) for t, e in toks if e[0] in kinds)
        self.expected = exp

    def hear(self, token: bytes, now: float) -> bool:
        """Audio: a note token heard from R. Returns False (and does nothing) unless it answers a packet this
        engine showed recently. ``expected`` holds the valid tokens, for the listener's noise filter.
        A heard token is all S learns, so an audio-mode ``result`` carries ``"confirmed": False``."""
        e = self.expected.get(token)
        if e is None or self.finished:
            return False
        kind, pt, obj, seq = e
        self._valid(now)
        st = self.state
        if kind == OK and st == HELLO:
            self._authed(AUDIO_NONCE_R, self.proposal, now)
            self._car_start(self._tx_m, [], now)
        elif kind == SYNC_OK and st == AUTHED:  # manifest complete, same sha, and R is ready (auto-confirmed)
            self._tx_m.on_ack(self._tx_m.total, 0, 0)
            self._ready = True
            self._sync_ok(now)
        elif kind in (DONE_OK, DONE_FAIL) and st == DATA:
            self._tx_d.on_ack(self._tx_d.total, 0, 0)
            self.result = {"status": STATUS_OK if kind == DONE_OK else "FAILED", "sha256_ok": kind == DONE_OK,
                           "confirmed": False}  # a 16-bit token, not a sealed DONE: S cannot be sure
            self._done_ack_raw = self._sess_seal(PType.DONE_ACK, b"")
            self._heard_frame = [self._done_ack_raw]
            if kind == DONE_OK:
                self._set(CLOSED, now)
            else:
                self._abort(now, "TRANSFER_FAILED", notify=False, peer=True)
        self._refresh_expected(now)
        return True

    def _reseal(self, raw: bytes) -> bytes:
        """The same packet (type, fields, body) under the next counter of its own sealer."""
        h = protocol.peek(raw)
        sealer = self._hello_sealer if h.session_id == protocol.NO_SESSION else self._sess_sealer
        body = protocol.plain_body(raw, sealer.enc_key)
        return sealer.seal(h.ptype, body, flags=h.flags & ~protocol.FLAG_ENC, obj_id=h.obj_id,
                           seq=h.seq, total=h.total)

    # ---- timers
    def _tick(self, now: float) -> None:
        cfg, st = self.cfg, self.state
        if st == HELLO:
            if now - self._phase_start > cfg.hello_s:
                return self._abort(now, "HELLO_TIMEOUT")
            if now - self._shown_at >= cfg.timeout:
                self._frame, self._shown_at = [self._hello_raw], now
        elif st == AUTHED:
            if now - self._phase_start > cfg.sync_s:
                return self._abort(now, "SYNC_TIMEOUT")
            if self._car:
                return self._car_tick(now)
            tx = self._tx_m
            f = tx.poll(now)
            if f:
                self._frame = f
            elif tx.failed:
                tx.rearm()  # keep waiting for SYNC_ACK until SYNC_TIMEOUT
        elif st == VERIFY:
            if now - self._phase_start > cfg.verify_s:
                return self._abort(now, "VERIFY_TIMEOUT")
            ans = self._operator()
            if ans is False:
                return self._abort(now, "OPERATOR_REJECT")
            self._op_ok = ans is True
            if self._op_ok and self._ready:
                self._start_raw = self._sess_seal(PType.START, b"", obj_id=OBJ_CTRL, seq=0, total=1,
                                                  flags=FLAG_LAST)
                if self._note_keys is not None:  # audio: START + data cycle, R speaks once when done
                    self._data(now)
                    self._car_start(self._tx_d, [self._start_raw], now)
                    return self._car_tick(now)
                self._set(START, now)
                self._frame, self._shown_at, self._tries = [self._start_raw], now, 0
        elif st == START:
            if now - self._shown_at >= cfg.timeout:
                self._tries += 1
                if self._tries > cfg.retries:
                    return self._abort(now, "RETRIES_EXHAUSTED")
                self._frame, self._shown_at = [self._start_raw], now
        elif st == DATA and self._car:
            if now - self._phase_start > cfg.idle_s + 3 * len(self._car) * cfg.dwell_s:
                return self._abort(now, "DONE_TIMEOUT")
            self._car_tick(now)
        elif st == DATA:
            self._poll(self._tx_d, now)
            if self._tx_d.done:
                self._set(DONE, now)
        elif st == DONE:
            if now - self._phase_start > max(cfg.idle_s, DONE_WAIT_MIN_S):
                self._abort(now, "DONE_TIMEOUT")

    def _poll(self, tx: TileTx, now: float) -> None:
        f = tx.poll(now)
        if f:
            self._frame = f
        elif tx.failed:
            self._abort(now, "RETRIES_EXHAUSTED")

    def _make_tx(self, obj_id: int, ptype: int, data: bytes, prof: Profile) -> TileTx:
        tiles = tiler.split(data, prof.body_capacity, obj_id)

        def seal(t: tiler.Tile) -> bytes:
            return self._sess_seal(ptype, t.data, obj_id=t.obj_id, seq=t.seq, total=t.total,
                                   flags=FLAG_LAST if t.seq == t.total - 1 else 0)
        return TileTx(tiles, seal, prof.per_frame, self.cfg.timeout, self.cfg.retries)

    # ---- inbound
    def _handle(self, pkt: Packet, name: str, now: float) -> None:
        pt = pkt.ptype
        if pt == PType.ABORT:
            return self._on_peer_abort(pkt, now)
        if self.state == CLOSED:
            if pt == PType.DONE and self._done_ack_raw:
                self._frame = [self._done_ack_raw]  # repeated DONE: DONE_ACK was lost
            return
        if pt == PType.HELLO:
            if self.state != HELLO:
                return
            try:
                d = pkt.json()
                other = d.get("nonce") if isinstance(d, dict) else None
            except protocol.ProtocolError:
                d, other = None, None
            if other == self.nonce.hex():
                return  # our own HELLO reflected back
            if name == "hello_own":
                return self._abort(now, "ROLE_COLLISION")
            if not isinstance(d, dict):
                return
            peer_mode = d.get("mode")
            if peer_mode != self.mode:
                self._abort(now, "MODE_MISMATCH")
        elif pt == PType.HELLO_ACK:
            if self.state == HELLO and self.keys is None:
                self._on_hello_ack(pkt, now)
        elif pt == PType.ACK:
            self._on_ack(pkt, now)
        elif pt == PType.SYNC_ACK:
            if self.state == AUTHED:
                self._on_sync_ack(pkt, now)
        elif pt == PType.READY:
            if self.state in (AUTHED, VERIFY):
                self._ready = True
        elif pt == PType.DONE:
            if self.state in (DATA, DONE):
                self._on_done(pkt, now)

    def _on_hello_ack(self, pkt: Packet, now: float) -> None:
        try:
            d = pkt.json()
            if int(d["v"]) != PROTO_VERSION or bytes.fromhex(d["nonce_s"]) != self.nonce:
                return  # not an answer to our HELLO (stale session): ignore
            nonce_r = bytes.fromhex(d["nonce_r"])
            prof = Profile.from_json(d["profile"])
        except (KeyError, TypeError, ValueError, protocol.ProtocolError):
            return self._abort(now, "BAD_HELLO")
        if (len(nonce_r) != 16 or prof.grid not in self.caps.grids or prof.tiles_n not in self.caps.tiles
                or prof.module_px not in self.caps.module_px):
            return self._abort(now, "BAD_PROFILE")
        if self.mode != "file":
            return self._abort(now, "CMD_UNSUPPORTED")
        self._authed(nonce_r, prof, now)

    def _authed(self, nonce_r: bytes, prof: Profile, now: float) -> None:
        self._nonce_r = nonce_r
        self.keys = protocol.derive_keys(self.psk, self.nonce, nonce_r, self.encrypt)
        self._sess_sealer = protocol.Sealer(self.keys.k_s2r, self.keys.session_id, enc_key=self.keys.enc_tx(self.ROLE))
        self._win = ReplayWindow()
        self.data_profile = prof
        self._set(AUTHED, now)
        self._tx_m = self._make_tx(OBJ_MANIFEST, PType.SYNC, self.manifest_raw, BOOTSTRAP)

    def _on_ack(self, pkt: Packet, now: float) -> None:
        try:
            obj, base, top, bitmap = decode_ack(pkt.body)
        except protocol.ProtocolError:
            return
        if obj == OBJ_MANIFEST and self._tx_m is not None and self.state == AUTHED:
            self._tx_m.on_ack(base, top, bitmap)
        elif obj == OBJ_CTRL and self.state == START:
            self._data(now)
        elif obj == OBJ_ARCHIVE and self._tx_d is not None and self.state in (DATA, DONE):
            self._tx_d.on_ack(base, top, bitmap)

    def _on_sync_ack(self, pkt: Packet, now: float) -> None:
        try:
            d = pkt.json()
            if not isinstance(d, dict):
                raise protocol.ProtocolError("SYNC_ACK body is not an object")
            echo, status = str(d["sha"]), str(d["status"])
        except (KeyError, protocol.ProtocolError):
            return self._abort(now, "BAD_SYNC_ACK")  # authenticated but malformed: R is broken, stop cleanly
        mine = self.manifest_sha.hex()
        if len(echo) < 16 or not mine.startswith(echo):
            return self._abort(now, "MANIFEST_MISMATCH")
        if status != STATUS_OK:
            return self._abort(now, status, notify=False)
        self._sync_ok(now)

    def _sync_ok(self, now: float) -> None:
        self._set(SYNCED, now)
        self.verify_code = protocol.verify_code(self.keys, self.mode, self.manifest_sha, self.nonce,
                                                self._nonce_r)
        self._set(VERIFY, now)

    def _data(self, now: float) -> None:
        self.profile = self.data_profile
        self._tx_d = self._make_tx(OBJ_ARCHIVE, PType.DATA, self.blob, self.data_profile)
        self._set(DATA, now)

    def _on_done(self, pkt: Packet, now: float) -> None:
        try:
            self.result = pkt.json()
        except protocol.ProtocolError:
            self.result = None
        if not isinstance(self.result, dict):
            self.result = {"status": "BAD_DONE"}
        self._done_ack_raw = self._sess_seal(PType.DONE_ACK, b"")
        self._frame = [self._done_ack_raw]
        self._set(CLOSED, now)


# ---------------------------------------------------------------- Receiver

class Receiver(_Engine):
    """R side. States: IDLE HELLO HELLO_ACKED AUTHED VERIFY READY DATA DONE CLOSED | ABORTED.

    ``dest`` is the output folder. ``check(manifest) -> status`` and ``extract(blob, dest, manifest,
    overwrite) -> paths`` are injectable (defaults: ``default_manifest_check`` / archive.unpack).
    ``announce`` makes R also show HELLO (camera back-channel); use False for the audio back-channel.
    """
    ROLE, PEER = ROLE_R, ROLE_S

    def __init__(self, psk: bytes, dest: str | os.PathLike | None = None, *, mode: str = "file",
                 cfg: LinkConfig | None = None, caps: Caps | None = None, announce: bool = True,
                 backchannel: str = "camera", check=None, extract=None, overwrite: bool = False,
                 confirm=None, auto_confirm: bool = False, rng: Callable[[int], bytes] = os.urandom,
                 encrypt: bool = False):
        super().__init__(psk, cfg, caps, mode, confirm, auto_confirm, rng, backchannel, encrypt)
        if backchannel == "audio":
            self.nonce = AUDIO_NONCE_R
        self.dest = Path(dest) if dest is not None else None
        self.announce = announce
        self.overwrite = overwrite
        self._check = check or (lambda m: default_manifest_check(m, self.dest, overwrite, self.caps.compress))
        self._extract = extract or default_extract
        self._out: list[bytes] = []
        self._last_sent: dict[str, float] = {}
        self._hello_raw: bytes | None = None
        self._hello_ack_raw: bytes | None = None
        self._nonce_s = b""
        self.manifest: dict | None = None
        self.manifest_sha: bytes | None = None
        self._rx_m = TileRx(OBJ_MANIFEST)
        self._rx_d: TileRx | None = None
        self._batch: dict[int, list] = {}  # obj_id -> [new_any, max_counter]
        self._sync_ack_raw: bytes | None = None
        self._ready_raw: bytes | None = None
        self._last_ack: dict[int, bytes] = {}
        self._done_raw: bytes | None = None
        self._tries = 0
        self._cands: list[dict] = []  # HELLO_ACKED candidate sessions
        self._expect_total: int | None = None
        self._seen_mem: set[str] = set()  # used nonce_S values when there is no dest to persist them in

    def _emit(self, packets: list[bytes]) -> None:
        self._out.extend(packets)

    def _sess_candidates(self):
        if self.state == HELLO_ACKED and self._cands:
            return [(c["keys"].rx_key(ROLE_R), c["win"], c["keys"]) for c in self._cands]
        return super()._sess_candidates()

    def _abort_packets(self, reason: str) -> list[bytes]:
        if self.state in (HELLO, HELLO_ACKED):  # not AUTHED yet: S may hold no session keys
            nonces = [c["nonce_s"] for c in self._cands] or [None]
            out = []
            for ns in nonces:
                b = {"reason": reason}
                if ns is not None:
                    b["nonce"] = ns.hex()
                out.append(self._hello_seal(PType.ABORT, protocol.pack_json(b)))
            return out
        return super()._abort_packets(reason)

    def _adopt(self, c: dict) -> None:
        self._cands = []
        self._select(c)

    def _select(self, c: dict) -> None:
        self.keys, self._sess_sealer, self._win = c["keys"], c["sealer"], c["win"]
        self._hello_ack_raw, self._nonce_s, self.data_profile = c["ack_raw"], c["nonce_s"], c["prof"]

    @property
    def decode_profiles(self) -> list[Profile]:
        """Profiles a frame reader should try, best first (bootstrap stays valid for START resends)."""
        return [self.data_profile, BOOTSTRAP] if self.state in (DATA, DONE, CLOSED) else [BOOTSTRAP]

    @property
    def progress(self) -> tuple[int, int]:
        r = self._rx_d.r if self._rx_d else None
        return (r.received, r.total or 0) if r else (0, 0)

    def step(self, now: float, inbound: list[bytes] = ()) -> list[bytes]:
        self._out = []
        self._batch = {}
        if self.state == IDLE:
            self._set(HELLO, now)
            if self.announce:
                self._hello_raw = self._hello_seal(PType.HELLO, self._hello_body({}))
                self._send_timed("hello", [self._hello_raw], now, force=True)
        for raw in inbound:
            self._on_raw(raw, now)
        if not self.finished:
            self._flush_batch(now)
            self._check_key_mismatch(now)
            self._tick(now)
        out, self._out = self._out, []
        if self.backchannel == "audio":  # one note token per never-seen packet; a code still on screen asks nothing
            tok = self._note() if self._fresh and self.state not in (HELLO, CLOSED, ABORTED) else None
            out = [tok] if tok else []
        self._fresh = None
        return out

    def _note(self) -> bytes | None:
        """R's only words over audio: HELLO seen, manifest complete (and confirmed), archive done."""
        pt, ctr = self._fresh
        if pt == PType.HELLO:
            return note_token(self.keys, OK, pt, ctr)
        if self.state == READY and pt == PType.SYNC:  # manifest complete and R's operator said y
            return note_token(self.keys, SYNC_OK, pt, ctr, self.manifest_sha)
        if self.state == DONE and pt in (PType.DATA, PType.START):
            return note_token(self.keys, DONE_OK if self.result["ok"] else DONE_FAIL, pt, ctr)
        return None

    def _send_timed(self, key: str, packets: list[bytes], now: float, force: bool = False) -> bool:
        if force or now - self._last_sent.get(key, -1e18) >= self.cfg.timeout:
            self._last_sent[key] = now
            self._out.extend(packets)
            return True
        return False

    # ---- timers
    def _tick(self, now: float) -> None:
        cfg, st = self.cfg, self.state
        if st == HELLO:
            if now - self._phase_start > cfg.hello_s:
                return self._abort(now, "HELLO_TIMEOUT")
            if self.announce and self._hello_raw:
                self._send_timed("hello", [self._hello_raw], now)
        elif st in (HELLO_ACKED, AUTHED):
            if now - self._phase_start > cfg.sync_s:
                return self._abort(now, "SYNC_TIMEOUT")
            if st == HELLO_ACKED:
                self._send_timed("hello_ack", [c["ack_raw"] for c in self._cands], now)
            elif self._last_ack.get(OBJ_MANIFEST):
                self._send_timed("ack0", [self._last_ack[OBJ_MANIFEST]], now)
        elif st in (VERIFY, READY):
            if now - self._phase_start > cfg.verify_s:
                return self._abort(now, "VERIFY_TIMEOUT")
            if st == VERIFY:
                self._send_timed("sync_ack", [self._sync_ack_raw], now)
                ans = self._operator()
                if ans is False:
                    return self._abort(now, "OPERATOR_REJECT")
                if ans is True:
                    self._ready_raw = self._sess_seal(PType.READY, b"")
                    self._set(READY)
                    self._send_timed("ready", [self._ready_raw], now, force=True)
            else:  # keep re-answering SYNC_ACK too: S may have missed it (S takes READY in either order)
                self._send_timed("sync_ack", [self._sync_ack_raw], now)
                self._send_timed("ready", [self._ready_raw], now)
        elif st == DATA:
            if now - self.last_valid > cfg.idle_s:
                return self._abort(now, "IDLE_TIMEOUT")
            for obj in (OBJ_CTRL, OBJ_ARCHIVE):
                if obj == OBJ_CTRL and self._rx_d.r.received:
                    continue  # START was answered: S moved on
                if obj in self._last_ack and not (obj == OBJ_ARCHIVE and self._rx_d.complete):
                    self._send_timed(f"ack{obj}", [self._last_ack[obj]], now)
        elif st == DONE:
            if self._send_timed("done", [self._done_raw], now):
                self._tries += 1
                if self._tries > cfg.retries + 1:
                    self.warning = "NO_DONE_ACK"
                    self._set(CLOSED, now)

    # ---- inbound
    def _on_session_packet(self, now: float, keys=None) -> None:
        if self.state == HELLO_ACKED:
            for c in self._cands:
                if c["keys"] is keys:
                    self._adopt(c)
                    break
            self._set(AUTHED, now)

    def _note_tile(self, obj: int, new: bool, counter: int) -> None:
        b = self._batch.setdefault(obj, [False, 0])
        b[0] = b[0] or new
        b[1] = max(b[1], counter)

    def _flush_batch(self, now: float) -> None:
        """One ACK per observed frame when something is new; for duplicates only at the resend cadence."""
        for obj, (new, top) in self._batch.items():
            rx = self._rx_m if obj == OBJ_MANIFEST else self._rx_d
            if rx is None:
                continue
            if obj == OBJ_MANIFEST and self.state not in (AUTHED, VERIFY, READY):
                continue
            rx.note_counter(top)
            if new:
                self._last_ack[obj] = self._sess_seal(PType.ACK, rx.ack_body())
                self._send_timed(f"ack{obj}" if obj else "ack0", [self._last_ack[obj]], now, force=True)
            elif obj in self._last_ack:
                self._send_timed(f"ack{obj}" if obj else "ack0", [self._last_ack[obj]], now)
        if self._rx_d is not None and self.state == DATA and self._rx_d.complete:
            self._finalize(now)
        if self.state == AUTHED and self._rx_m.complete:
            self._manifest_done(now)

    def _handle(self, pkt: Packet, name: str, now: float) -> None:
        pt = pkt.ptype
        if pt == PType.ABORT:
            return self._on_peer_abort(pkt, now)
        if pt == PType.HELLO:
            return self._on_hello(pkt, name, now)
        if self.state in (HELLO, HELLO_ACKED):
            return
        if pt == PType.SYNC:
            if pkt.obj_id == OBJ_MANIFEST and self.state in (AUTHED, VERIFY, READY):
                if not 1 <= pkt.total <= MAX_MANIFEST_TILES:
                    return self._abort(now, "BAD_MANIFEST")
                new = self._rx_m.add(pkt) if self.state == AUTHED else False
                self._note_tile(OBJ_MANIFEST, new, pkt.counter)
        elif pt == PType.START:
            if self.state == READY:
                self._ack_start(pkt, now)
            elif self.state in (DATA, DONE) and OBJ_CTRL in self._last_ack:
                self._send_timed("ack65535", [self._last_ack[OBJ_CTRL]], now)
        elif pt == PType.DATA:
            if (self._rx_d is not None and self.state in (DATA, DONE) and pkt.obj_id == OBJ_ARCHIVE
                    and (self._expect_total is None or pkt.total == self._expect_total)):
                new = self._rx_d.add(pkt) if self.state == DATA else False
                self._note_tile(OBJ_ARCHIVE, new, pkt.counter)
        elif pt == PType.DONE_ACK:
            if self.state == DONE:
                self._set(CLOSED, now)

    def _on_hello(self, pkt: Packet, name: str, now: float) -> None:
        try:
            d = pkt.json()
            nonce_hex = d.get("nonce") if isinstance(d, dict) else None
        except protocol.ProtocolError:
            d, nonce_hex = None, None
        if nonce_hex == self.nonce.hex():
            return  # our own HELLO reflected back
        strict = self.state == HELLO
        if name == "hello_own":
            if strict:
                self._abort(now, "ROLE_COLLISION")
            return
        if self.state not in (HELLO, HELLO_ACKED, AUTHED):
            return
        try:
            nonce_s = bytes.fromhex(d["nonce"])
            ok = d["role"] == ROLE_S and int(d["v"]) == PROTO_VERSION and len(nonce_s) == 16
            mode = d["mode"]
            s_caps = Caps.from_json(d["caps"])
            proposal = Profile.from_json(d["proposal"])
        except (KeyError, TypeError, ValueError, protocol.ProtocolError):
            ok = False
        if not ok:
            return self._abort(now, "BAD_HELLO") if strict else None
        if self.state == AUTHED:
            if nonce_s == self._nonce_s:
                self._send_timed("hello_ack", [self._hello_ack_raw], now)  # duplicate HELLO: re-answer
            return
        if self.state == HELLO_ACKED:
            for c in self._cands:
                if c["nonce_s"] == nonce_s:  # duplicate HELLO: re-answer every candidate
                    self._send_timed("hello_ack", [x["ack_raw"] for x in self._cands], now)
                    return
            if len(self._cands) >= MAX_CANDIDATES or mode != self.mode or self.mode != "file":
                return
        if mode != self.mode:
            return self._abort(now, "MODE_MISMATCH")
        prof = negotiate(proposal, s_caps, self.caps)
        if prof is None:
            return self._abort(now, "NO_COMMON_PROFILE") if strict else None
        if self.mode != "file":
            return self._abort(now, "CMD_UNSUPPORTED")
        if self.backchannel == "audio":  # no HELLO_ACK: S's proposal is the profile, keys need no R nonce
            if prof != proposal:
                return self._abort(now, "BAD_PROFILE") if strict else None
            try:
                fresh = self._claim_nonce(nonce_s)
            except OSError:
                return self._abort(now, "DEST_NOT_WRITABLE")  # can't record it: accepting would allow replays
            if not fresh:  # keys depend on nonce_S alone: a reused one is a recorded session played again
                self.stats["replay"] += 1
                self.warning = "REPLAYED HELLO?"
                self._event(now, "replay", "audio HELLO with a nonce_S accepted before")
                return
            keys = protocol.derive_keys(self.psk, nonce_s, self.nonce, self.encrypt)
            self._adopt({"nonce_s": nonce_s, "keys": keys, "sealer": protocol.Sealer(keys.k_r2s, keys.session_id, enc_key=keys.enc_tx(self.ROLE)),
                         "win": ReplayWindow(), "ack_raw": None, "prof": prof})
            self._set(AUTHED, now)
            return
        keys = protocol.derive_keys(self.psk, nonce_s, self.nonce, self.encrypt)
        ack_raw = self._hello_seal(PType.HELLO_ACK, protocol.pack_json(
            {"v": PROTO_VERSION, "nonce_s": nonce_s.hex(), "nonce_r": self.nonce.hex(), "profile": prof.to_json()}))
        c = {"nonce_s": nonce_s, "keys": keys, "sealer": protocol.Sealer(keys.k_r2s, keys.session_id, enc_key=keys.enc_tx(self.ROLE)),
             "win": ReplayWindow(), "ack_raw": ack_raw, "prof": prof}
        self._cands.append(c)
        self._select(c)
        if self.state == HELLO:
            self._set(HELLO_ACKED, now)
        self._send_timed("hello_ack", [x["ack_raw"] for x in self._cands], now, force=True)

    def _claim_nonce(self, nonce_s: bytes) -> bool:
        """Audio mode: record nonce_S as used. False if it was accepted before (this engine or, through
        ``<dest>/SEEN_FILE``, an earlier receiver on the same dest). Raises OSError if it can't be recorded."""
        h = nonce_s.hex()
        if self.dest is None:  # no folder to persist into (tests with an injected extract)
            if h in self._seen_mem:
                return False
            self._seen_mem.add(h)
            return True
        self.dest.mkdir(parents=True, exist_ok=True)
        path = self.dest / SEEN_FILE
        try:
            seen = path.read_text("ascii").split()
        except FileNotFoundError:
            seen = []
        except UnicodeDecodeError:
            seen = []  # damaged: start over rather than refuse every sender
        if h in seen:
            return False
        tmp = path.with_name(f"{SEEN_FILE}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write("\n".join((seen + [h])[-SEEN_MAX:]) + "\n")
        os.replace(tmp, path)
        return True

    def _manifest_done(self, now: float) -> None:
        raw = self._rx_m.assemble()
        self.manifest_sha = hashlib.sha256(raw).digest()
        try:
            self.manifest = protocol.unpack_json(raw)
            status = self._check(self.manifest) if isinstance(self.manifest, dict) else "BAD_MANIFEST"
        except (protocol.ProtocolError, OSError):
            status = "BAD_MANIFEST"
        self._sync_ack_raw = self._sess_seal(PType.SYNC_ACK, protocol.pack_json(
            {"sha": self.manifest_sha.hex(), "status": status}))
        self._send_timed("sync_ack", [self._sync_ack_raw], now, force=True)
        if status != STATUS_OK:
            return self._abort(now, status)
        self.verify_code = protocol.verify_code(self.keys, self.mode, self.manifest_sha, self._nonce_s,
                                                self.nonce)
        self._set(VERIFY, now)

    def _ack_start(self, pkt: Packet, now: float) -> None:
        self._last_ack[OBJ_CTRL] = self._sess_seal(
            PType.ACK, encode_ack(OBJ_CTRL, 1, pkt.counter, 0))
        self._send_timed("ack65535", [self._last_ack[OBJ_CTRL]], now, force=True)
        sha = self.manifest.get("archive_sha256")
        try:
            size = int(self.manifest["archive_size"])
            self._expect_total = max(1, -(-size // self.data_profile.body_capacity))
        except (KeyError, TypeError, ValueError):
            self._expect_total = None
        self._rx_d = TileRx(OBJ_ARCHIVE, bytes.fromhex(sha) if sha else None)
        self._set(DATA, now)
        self.last_valid = now

    def _finalize(self, now: float) -> None:
        ok, status, paths, error = True, STATUS_OK, [], None
        try:
            blob = self._rx_d.assemble()
        except tiler.TilerError:
            ok, status, blob = False, "SHA_MISMATCH", b""
        if ok:
            try:
                paths = [str(p) for p in self._extract(blob, self.dest, self.manifest, self.overwrite)]
            except Exception as e:  # archive errors must not crash the engine
                ok, status, error = False, "EXTRACT_FAILED", type(e).__name__  # the name stays local
        self.result = {"ok": ok, "status": status, "paths": paths, "error": error}
        self._done_raw = self._sess_seal(PType.DONE, protocol.pack_json(
            {"status": status, "sha256_ok": ok, "files": len(paths)}))
        self._set(DONE, now)
        self._tries = 0
        self._send_timed("done", [self._done_raw], now, force=True)
