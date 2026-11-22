"""Lightferry wire format (PLAN section 5): packet header, HMAC sealing, keys, replay window.

A packet is the payload of exactly one code::

    ">BBQIHII" header (24 B): ptype, flags, session_id, counter, obj_id, seq, total
    body (n B)
    tag (32 B) = HMAC-SHA256(key, b"LF1" || header || body)

Public API
----------
* ``PType`` packet types, ``Header``, ``Packet``
* ``hello_key(psk, role)``, ``derive_keys(psk, nonce_s, nonce_r) -> SessionKeys``
* ``seal(key, hdr, body) -> bytes``, ``open_packet(key, raw, window=None) -> Packet``
* ``Sealer`` (assigns the per-direction counter once, at first seal)
* ``ReplayWindow`` (4096-wide sliding bitmap), ``peek(raw) -> Header | None``
* ``verify_code(keys, mode, manifest_sha256, nonce_s, nonce_r) -> str``
* ``AuthError``, ``ReplayError``, ``ProtocolError``
"""
from __future__ import annotations

import hashlib
import hmac
import json
import struct
from dataclasses import dataclass
from enum import IntEnum

HEADER_FMT = ">BBQIHII"
HEADER_LEN = struct.calcsize(HEADER_FMT)  # 24
TAG_LEN = 32
OVERHEAD = HEADER_LEN + TAG_LEN  # 56
LABEL = b"LF1"
WINDOW = 4096
FLAG_LAST = 0x01
FLAG_ENC = 0x02  # body is encrypted (--encrypt): HMAC-SHA256 keystream, then the MAC covers the ciphertext
NO_SESSION = bytes(8)
MIN_PSK_LEN = 16

ROLE_S = "s"
ROLE_R = "r"

# obj_id conventions
OBJ_MANIFEST = 0
OBJ_ARCHIVE = 1
OBJ_CTRL = 0xFFFF  # START and its ACK


class PType(IntEnum):
    HELLO = 1
    HELLO_ACK = 2
    SYNC = 3
    SYNC_ACK = 4
    READY = 5
    START = 6
    DATA = 7
    ACK = 8
    CMD = 9  # stub (command channel)
    RESULT = 10  # stub (command channel)
    DONE = 11
    ABORT = 12
    DONE_ACK = 13
    VERIFY = 14  # reserved: VERIFY is a human screen compare, never sent on the wire


VALID_PTYPES = frozenset(int(p) for p in PType)


class ProtocolError(Exception):
    """Malformed packet or message."""


class AuthError(ProtocolError):
    """MAC did not verify (wrong key, direction, session or tampering)."""


class ReplayError(ProtocolError):
    """MAC is valid but the counter is below the replay window."""


@dataclass(frozen=True)
class Header:
    ptype: int
    flags: int = 0
    session_id: bytes = NO_SESSION
    counter: int = 0
    obj_id: int = 0
    seq: int = 0
    total: int = 0

    def pack(self) -> bytes:
        if len(self.session_id) != 8:
            raise ProtocolError("session_id must be 8 bytes")
        return struct.pack(HEADER_FMT, self.ptype, self.flags, int.from_bytes(self.session_id, "big"),
                           self.counter, self.obj_id, self.seq, self.total)

    @classmethod
    def unpack(cls, raw: bytes) -> "Header":
        if len(raw) < HEADER_LEN:
            raise ProtocolError("short header")
        pt, fl, sid, ctr, obj, seq, tot = struct.unpack(HEADER_FMT, raw[:HEADER_LEN])
        return cls(pt, fl, sid.to_bytes(8, "big"), ctr, obj, seq, tot)


@dataclass(frozen=True)
class Packet:
    """An authenticated packet. ``duplicate`` is True when the exact counter was already seen."""
    ptype: int
    flags: int
    session_id: bytes
    counter: int
    obj_id: int
    seq: int
    total: int
    body: bytes
    duplicate: bool = False

    @property
    def last(self) -> bool:
        return bool(self.flags & FLAG_LAST)

    def json(self):
        return json.loads(self.body.decode("utf-8"))


def pack_json(obj) -> bytes:
    """Canonical compact JSON bytes (sorted keys)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def unpack_json(body: bytes):
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ProtocolError("bad json body") from e


# ---------------------------------------------------------------- keys

def _hmac(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()


def hello_key(psk: bytes, role: str) -> bytes:
    """K_hello_<role> = HMAC(PSK, b"lf-hello-" || role); role is "s" or "r"."""
    if role not in (ROLE_S, ROLE_R):
        raise ValueError("role must be 's' or 'r'")
    if len(psk) < MIN_PSK_LEN:
        raise ValueError("psk too short")
    return _hmac(psk, b"lf-hello-" + role.encode())


@dataclass(frozen=True)
class SessionKeys:
    session_id: bytes
    k_sess: bytes
    k_s2r: bytes
    k_r2s: bytes
    k_audio: bytes
    k_enc_s2r: bytes = b""
    k_enc_r2s: bytes = b""
    encrypt: bool = False

    def enc_tx(self, role: str):
        """Body-encryption key for packets this role sends (None when --encrypt is off)."""
        if not self.encrypt:
            return None
        return self.k_enc_s2r if role == ROLE_S else self.k_enc_r2s

    def enc_rx(self, role: str):
        if not self.encrypt:
            return None
        return self.k_enc_r2s if role == ROLE_S else self.k_enc_s2r

    def tx_key(self, role: str) -> bytes:
        return self.k_s2r if role == ROLE_S else self.k_r2s

    def rx_key(self, role: str) -> bytes:
        return self.k_r2s if role == ROLE_S else self.k_s2r


def derive_keys(psk: bytes, nonce_s: bytes, nonce_r: bytes, encrypt: bool = False) -> SessionKeys:
    """Session keys from the PSK and both fresh nonces; session_id = SHA-256(nonce_S || nonce_R)[:8]."""
    if len(psk) < MIN_PSK_LEN:
        raise ValueError("psk too short")
    k_sess = _hmac(psk, b"lf-session" + nonce_s + nonce_r)
    k_s2r = _hmac(k_sess, b"lf-s2r")
    k_r2s = _hmac(k_sess, b"lf-r2s")
    return SessionKeys(hashlib.sha256(nonce_s + nonce_r).digest()[:8], k_sess, k_s2r, k_r2s,
                       _hmac(k_sess, b"lf-audio-tag"), _hmac(k_sess, b"lf-enc-s2r"), _hmac(k_sess, b"lf-enc-r2s"),
                       bool(encrypt))


def verify_code(keys: SessionKeys, mode: str, manifest_sha256: bytes | None, nonce_s: bytes,
                nonce_r: bytes) -> str:
    """6-digit operator comparison code. File mode binds the manifest hash, cmd mode only the nonces."""
    k_verify = _hmac(keys.k_sess, b"lf-verify")
    if mode == "cmd":
        mac = _hmac(k_verify, b"verify-cmd" + nonce_s + nonce_r)
    else:
        if manifest_sha256 is None:
            raise ValueError("file mode needs manifest_sha256")
        mac = _hmac(k_verify, b"verify" + manifest_sha256)
    return "%06d" % (int.from_bytes(mac[:4], "big") % 10**6)


# ---------------------------------------------------------------- seal / open

def _crypt(enc_key: bytes, head: bytes, body: bytes) -> bytes:
    """XOR ``body`` with an HMAC-SHA256 keystream keyed per session and direction; ``head`` (the packed header,
    unique per packet because it carries the session id and counter) is the nonce. Symmetric."""
    if not body:
        return b""
    out = bytearray()
    i = 0
    while len(out) < len(body):
        out += _hmac(enc_key, b"LF1-enc" + head + i.to_bytes(4, "big"))
        i += 1
    n = len(body)
    return (int.from_bytes(body, "big") ^ int.from_bytes(bytes(out[:n]), "big")).to_bytes(n, "big")


def plain_body(raw: bytes, enc_key: bytes | None) -> bytes:
    """Body of a sealed packet without verifying it (used to re-seal our own cached packets)."""
    head = raw[:-TAG_LEN]
    body = head[HEADER_LEN:]
    if enc_key is not None and (head[1] & FLAG_ENC):
        body = _crypt(enc_key, head[:HEADER_LEN], body)
    return body


def seal(key: bytes, hdr: Header, body: bytes, enc_key: bytes | None = None) -> bytes:
    """Serialise and authenticate one packet. The counter in ``hdr`` is used as given. With ``enc_key`` the body is
    encrypted first (encrypt-then-MAC)."""
    if hdr.ptype not in VALID_PTYPES:
        raise ProtocolError("bad ptype")
    if enc_key is not None:
        hdr = Header(hdr.ptype, hdr.flags | FLAG_ENC, hdr.session_id, hdr.counter, hdr.obj_id, hdr.seq, hdr.total)
        head0 = hdr.pack()
        head = head0 + _crypt(enc_key, head0, bytes(body))
    else:
        head = hdr.pack() + bytes(body)
    return head + _hmac(key, LABEL + head)


def peek(raw: bytes) -> Header | None:
    """Unauthenticated header parse (used only to pick a candidate key). None if not packet-shaped."""
    if len(raw) < OVERHEAD:
        return None
    h = Header.unpack(raw)
    return h if h.ptype in VALID_PTYPES else None


class ReplayWindow:
    """Sliding bitmap of the last ``size`` counters (PLAN section 5).

    above highest -> NEW (slide); inside and unseen -> NEW (mark); inside and seen -> DUP;
    below the window -> REPLAY. ``check`` never mutates; ``commit`` records an accepted counter.
    """
    NEW, DUP, REPLAY = "new", "dup", "replay"

    def __init__(self, size: int = WINDOW):
        self.size = size
        self.highest: int | None = None
        self._bits = 0  # bit i set <=> counter (highest - i) seen
        self._mask = (1 << size) - 1

    def check(self, counter: int) -> str:
        if self.highest is None or counter > self.highest:
            return self.NEW
        off = self.highest - counter
        if off >= self.size:
            return self.REPLAY
        return self.DUP if (self._bits >> off) & 1 else self.NEW

    def commit(self, counter: int) -> None:
        if self.highest is None:
            self.highest, self._bits = counter, 1
        elif counter > self.highest:
            diff = counter - self.highest
            self._bits = 1 if diff >= self.size else ((self._bits << diff) | 1) & self._mask
            self.highest = counter
        else:
            off = self.highest - counter
            if off < self.size:
                self._bits |= 1 << off


def open_packet(key: bytes, raw: bytes, window: ReplayWindow | None = None,
                enc_key: bytes | None = None) -> Packet:
    """Verify the MAC (constant time) before parsing, then apply the replay window.

    Raises ``AuthError`` (bad MAC / malformed), ``ReplayError`` (below window). A packet whose counter
    was already accepted returns with ``duplicate=True`` and is not re-marked. ``window=None`` skips
    replay handling (HELLO/HELLO_ACK are nonce-bound instead).
    """
    if len(raw) < OVERHEAD:
        raise AuthError("short packet")
    head, tag = raw[:-TAG_LEN], raw[-TAG_LEN:]
    if not hmac.compare_digest(_hmac(key, LABEL + head), tag):
        raise AuthError("bad mac")
    h = Header.unpack(head)
    if h.ptype not in VALID_PTYPES:
        raise AuthError("bad ptype")
    body = head[HEADER_LEN:]
    if enc_key is not None:
        if not h.flags & FLAG_ENC:
            raise AuthError("expected an encrypted packet")
        body = _crypt(enc_key, head[:HEADER_LEN], body)
    elif h.flags & FLAG_ENC:
        raise AuthError("encrypted packet but this side has no key")
    dup = False
    if window is not None:
        verdict = window.check(h.counter)
        if verdict == ReplayWindow.REPLAY:
            raise ReplayError("counter below window")
        if verdict == ReplayWindow.DUP:
            dup = True
        else:
            window.commit(h.counter)
    return Packet(h.ptype, h.flags, h.session_id, h.counter, h.obj_id, h.seq, h.total,
                  body, dup)


class Sealer:
    """Per-direction sealer: assigns the next counter once per *new* packet. Cache and re-show the
    returned bytes for retransmissions (same counter and tag); never call ``seal`` again for them."""

    def __init__(self, key: bytes, session_id: bytes = NO_SESSION, start: int = 0, enc_key: bytes | None = None):
        self.key = key
        self.enc_key = enc_key
        self.session_id = session_id
        self.counter = start

    def seal(self, ptype: int, body: bytes = b"", *, flags: int = 0, obj_id: int = 0, seq: int = 0,
             total: int = 0) -> bytes:
        hdr = Header(int(ptype), flags, self.session_id, self.counter, obj_id, seq, total)
        self.counter += 1
        if self.counter > 0xFFFFFFFF:
            raise ProtocolError("counter exhausted")
        return seal(self.key, hdr, body, self.enc_key)
