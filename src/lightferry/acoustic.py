"""Acoustic back-channel modem (receiver speaker -> sender microphone).

Used when the sender has no camera. Messages are tiny (acks, verify yes/no).
  * 32-tone MFSK grid (1.2-5.85 kHz, 150 Hz spacing) split into two disjoint 16-tone
    sets (even / odd grid index). Symbols alternate between the sets, so the room
    reverb tail of symbol n (same tone, other set) never lands on the tone bins read
    for symbol n+1. 4 bits per symbol, 100 ms slots = 80 ms burst with 5 ms
    raised-cosine ramps, centred in a ~20 ms guard.
  * Linear-chirp preamble (0.8-5.95 kHz) found with a band-passed, normalised matched
    filter. Its received spectrum gives a per-tone channel-gain profile.
  * Wire frame = [seq:2][len:1][body][tag]. tag = HMAC-SHA256(key, label || context ||
    direction || seq || len || body) truncated to `tag_bytes` (default 6). Context
    (session id) and direction are NOT transmitted: both ends supply them, so a frame
    from another session/direction/key fails the tag (replay and cross-talk rejected).
    `seq` IS transmitted (the receiver needs it to recompute the tag) and returned;
    pass `seq=` to decode_audio to demand an exact value.
  * The frame is sent `repeat` times (own preamble each) with a different
    nibble->tone permutation per copy. The receiver scores each copy per symbol
    (equalised on/off log-likelihood) and sums the scores across copies before the
    decision, so a fade/notch/interferer that kills a tone in one copy hits a
    different nibble in the other.
  * decode_audio is a one-shot scan; AudioListener is the streaming wrapper (scans
    every >= 0.5 s of new audio, correlating only the new samples and caching the result).
  * `tag_bytes` must be >= 4 (a shorter tag is forgeable by brute force).
  * NOT handled here (protocol layer's job): replay protection across seq values
    (keep a highest-seen-seq window per session + direction and reject stale/duplicate
    seq) and seq wrap / context rotation (seq is 16-bit, so rotate the session context
    or key before 65536 messages in one session+direction; a wrapped seq would
    otherwise verify again).
Pure numpy: audio I/O (sounddevice) lives elsewhere. `fs` is always an explicit
argument (>= 16 kHz so the 6 kHz band stays inside Nyquist).
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from functools import lru_cache
from typing import NamedTuple

import numpy as np

FS_MIN = 16000
SLOT_S = 0.10          # symbol slot
BURST_S = 0.08         # tone burst inside the slot (rest = guard, split evenly)
RAMP_S = 0.005         # raised-cosine on/off ramps
N_TONES = 32           # grid; set A = even index, set B = odd index
TONES_PER_SET = 16
F0 = 1200.0
DF = 150.0
CHIRP_S = 0.25
CHIRP_F0, CHIRP_F1 = 800.0, 5950.0
BAND = (700.0, 6000.0)  # receiver pre-correlation band-pass
GAP_S = 0.05
TAG_BYTES = 6
MAX_BODY = 48
SEQ_BYTES = 2
HDR_SYMS = 2 * (SEQ_BYTES + 1)
_LABEL = b"lightferry-audio-v2"


class Message(NamedTuple):
    seq: int
    body: bytes


def tag(body: bytes, key: bytes, context: bytes = b"", direction: int = 1, seq: int = 0,
        tag_bytes: int = TAG_BYTES) -> bytes:
    _check_params(direction, seq, tag_bytes)
    mac = hmac.new(key, _LABEL, hashlib.sha256)
    mac.update(len(context).to_bytes(2, "big") + context)  # length-prefixed: unambiguous
    mac.update(bytes([direction]) + seq.to_bytes(SEQ_BYTES, "big") + bytes([len(body)]) + body)
    return mac.digest()[:tag_bytes]


def _check_params(direction: int, seq: int, tag_bytes: int) -> None:
    if not 0 <= direction <= 255:
        raise ValueError("direction must fit in one byte")
    if not 0 <= seq < 1 << (8 * SEQ_BYTES):
        raise ValueError("seq out of range")
    if not 4 <= tag_bytes <= 32:
        raise ValueError("tag_bytes must be 4..32")


def _perm(copy: int) -> np.ndarray:
    """Nibble -> tone-index permutation for the given copy (bijective; copy 0 = identity)."""
    a = (1, 5, 9, 13)[copy % 4]
    return (a * np.arange(16) + 3 * copy) % 16


_PERMS = [_perm(c) for c in range(16)]
_INVS = [np.argsort(p) for p in _PERMS]


@dataclass(frozen=True, eq=False)
class _Geo:
    fs: int
    slot: int
    n: int
    lead: int
    gap: int
    m: int
    chirp: np.ndarray
    S: np.ndarray
    C: np.ndarray
    band_lo: np.ndarray
    band_hi: np.ndarray
    pc_band: np.ndarray
    mask_cache: dict


@lru_cache(maxsize=8)
def _geo(fs: int, slot_s: float = SLOT_S, burst_s: float = BURST_S, chirp_s: float = CHIRP_S,
         gap_s: float = GAP_S) -> _Geo:
    if int(fs) != fs or fs < FS_MIN:
        raise ValueError(f"fs must be an integer >= {FS_MIN} Hz (got {fs})")
    fs = int(fs)
    slot, n, ramp = round(slot_s * fs), round(burst_s * fs), round(RAMP_S * fs)
    env = np.ones(n)
    r = 0.5 * (1 - np.cos(np.pi * (np.arange(ramp) + 0.5) / ramp))
    env[:ramp], env[n - ramp:] = r, r[::-1]
    t = np.arange(n) / fs
    ph = 2 * np.pi * (F0 + DF * np.arange(N_TONES))[:, None] * t
    m = round(chirp_s * fs)
    tc = np.arange(m) / fs
    k = (CHIRP_F1 - CHIRP_F0) / chirp_s
    chirp = np.sin(2 * np.pi * (CHIRP_F0 * tc + 0.5 * k * tc * tc))
    tk = round(0.1 * m)  # Tukey-ish ramps keep the chirp spectrum flat
    w = np.ones(m)
    rr = 0.5 * (1 - np.cos(np.pi * (np.arange(tk) + 0.5) / tk))
    w[:tk], w[m - tk:] = rr, rr[::-1]
    chirp = chirp * w
    fb = np.fft.rfftfreq(m, 1 / fs)
    pc = np.abs(np.fft.rfft(chirp)) ** 2
    cs = np.concatenate([[0.0], np.cumsum(pc)])
    freqs = F0 + DF * np.arange(N_TONES)
    lo = np.searchsorted(fb, freqs - 50.0)
    hi = np.searchsorted(fb, freqs + 50.0)
    return _Geo(fs, slot, n, (slot - n) // 2, round(gap_s * fs), m, chirp,
                np.sin(ph) * env, np.cos(ph) * env, lo, hi, cs[hi] - cs[lo], {})


def _frame_syms(body_len: int, tag_bytes: int) -> int:
    return 2 * (SEQ_BYTES + 1 + body_len + tag_bytes)


def _copy_len(g: _Geo, nsym: int) -> int:
    return g.m + g.gap + nsym * g.slot + g.gap


def max_transmission_s(fs: int, max_body: int = MAX_BODY, tag_bytes: int = TAG_BYTES, repeat: int = 2) -> float:
    g = _geo(fs)
    return repeat * _copy_len(g, _frame_syms(max_body, tag_bytes)) / g.fs


def _nibbles(data: bytes) -> list[int]:
    out = []
    for b in data:
        out += [b >> 4, b & 15]
    return out


def encode_message(body: bytes, key: bytes, fs: int, context: bytes = b"", direction: int = 1,
                   seq: int = 0, repeat: int = 2, amplitude: float = 0.6,
                   tag_bytes: int = TAG_BYTES) -> np.ndarray:
    if len(body) > MAX_BODY:
        raise ValueError("body too long for acoustic channel")
    if not 1 <= repeat <= 16:
        raise ValueError("repeat must be 1..16")
    g = _geo(fs)
    frame = (seq.to_bytes(SEQ_BYTES, "big") + bytes([len(body)]) + body
             + tag(body, key, context, direction, seq, tag_bytes))
    nib = _nibbles(frame)
    clen = _copy_len(g, len(nib))
    copies = []
    for j in range(repeat):
        sig = np.zeros(clen)
        sig[: g.m] = g.chirp
        base = g.m + g.gap + g.lead
        perm = _PERMS[j]
        for i, v in enumerate(nib):
            s0 = base + i * g.slot
            sig[s0 : s0 + g.n] = g.S[2 * perm[v] + (i & 1)]
        copies.append(sig)
    out = np.concatenate(copies + [np.zeros(round(0.1 * fs))])
    return (amplitude * out / np.max(np.abs(out))).astype(np.float32)


# ---------------------------------------------------------------------------
# receiver
# ---------------------------------------------------------------------------

def _band_mask(nfft: int, fs: int) -> np.ndarray:
    f = np.fft.rfftfreq(nfft, 1 / fs)
    lo, hi = BAND
    tp = 100.0
    mask = np.ones_like(f)
    a = f < lo
    mask[a] = 0.5 * (1 + np.cos(np.pi * np.clip((lo - f[a]) / tp, 0, 1)))
    b = f > hi
    mask[b] = 0.5 * (1 + np.cos(np.pi * np.clip((f[b] - hi) / tp, 0, 1)))
    return mask


def _cached(g: _Geo, key, make):
    v = g.mask_cache.get(key)
    if v is None:
        if len(g.mask_cache) > 64:
            g.mask_cache.clear()
        v = g.mask_cache[key] = make()
    return v


def _norm_corr(x: np.ndarray, g: _Geo, band_pass: bool) -> np.ndarray:
    m = g.m
    n = 1 << int(np.ceil(np.log2(len(x) + m)))
    X = np.fft.rfft(x, n)
    if band_pass:
        X = X * _cached(g, ("mask", n), lambda: _band_mask(n, g.fs))
        x = np.fft.irfft(X, n)[: len(x)]
    H = _cached(g, ("chirp", n), lambda: np.conj(np.fft.rfft(g.chirp, n)))
    corr = np.fft.irfft(X * H, n)[: len(x) - m + 1]
    csum = np.concatenate([[0.0], np.cumsum(x * x)])
    energy = np.sqrt(np.maximum(csum[m:] - csum[:-m], 1e-12))
    return corr / (energy * np.linalg.norm(g.chirp))


def _peaks(norm: np.ndarray, thr: float, min_dist: int, cap: int) -> list[int]:
    """Local maxima >= thr, strongest first, at least min_dist apart."""
    idx = np.flatnonzero(norm >= thr)
    if idx.size == 0:
        return []
    vals = norm[idx].copy()
    out: list[int] = []
    while len(out) < cap:
        k = int(np.argmax(vals))
        if vals[k] < 0:
            break
        p = int(idx[k])
        out.append(p)
        vals[np.abs(idx - p) < min_dist] = -1.0
    return out


def _energies(x: np.ndarray, body_start: int, nsym: int, g: _Geo) -> np.ndarray:
    """(nsym, 16) tone-bin energies; row i holds only the tone set used by symbol i."""
    starts = body_start + g.lead + np.arange(nsym) * g.slot
    segs = x[starts[:, None] + np.arange(g.n)]
    e = (segs @ g.S.T) ** 2 + (segs @ g.C.T) ** 2  # (nsym, 32)
    return e.reshape(nsym, TONES_PER_SET, 2)[np.arange(nsym), :, np.arange(nsym) & 1]


def _profile(x: np.ndarray, pos: int, g: _Geo) -> np.ndarray:
    """Relative per-tone gain (median 1, tone-grid order) from the received preamble."""
    seg = x[pos : pos + g.m]
    if len(seg) < g.m:
        seg = np.pad(seg, (0, g.m - len(seg)))
    cs = np.concatenate([[0.0], np.cumsum(np.abs(np.fft.rfft(seg)) ** 2)])
    r = (cs[g.band_hi] - cs[g.band_lo]) / g.pc_band
    r = r / (np.median(r) + 1e-30)
    return np.clip(r, 0.03, 5.0)


def _scores(E: np.ndarray, rel: np.ndarray) -> np.ndarray:
    """Equalised on/off log-likelihood per (symbol, tone index).

    Tone energy ~ Exp(N) when off, Exp(N + a_k) when on, with a_k the expected on-level
    scaled by the preamble gain profile. With flat gains this is monotone in energy
    (plain argmax); a faded tone gets less trust instead of its noise being amplified.
    """
    noise = float(np.median(E)) / 0.693
    top = float(E.max(axis=1).mean())
    a_hat = max(top - noise, 0.0)
    noise = max(noise, 1e-4 * a_hat, 1e-30)
    par = np.arange(len(E)) & 1
    a = a_hat * rel.reshape(TONES_PER_SET, 2).T[par]
    return E * a / (noise * (noise + a)) - np.log1p(a / noise)


def _parse(nib: np.ndarray, length: int, key, context, direction, seq, tag_bytes) -> Message | None:
    raw = bytes((int(nib[i]) << 4) | int(nib[i + 1]) for i in range(0, len(nib), 2))
    rseq, ln = int.from_bytes(raw[:SEQ_BYTES], "big"), raw[SEQ_BYTES]
    if ln != length or (seq is not None and rseq != seq):
        return None
    body, t = raw[SEQ_BYTES + 1 : SEQ_BYTES + 1 + ln], raw[SEQ_BYTES + 1 + ln :]
    return Message(rseq, body) if hmac.compare_digest(t, tag(body, key, context, direction, rseq, tag_bytes)) else None


def _length_candidates(scr: np.ndarray, max_body: int, k: int = 3) -> list[int]:
    """Top-k plausible length bytes from the two length-nibble score rows (joint ranking)."""
    hi, lo = scr[-2], scr[-1]
    top_h, top_l = np.argsort(-hi)[:3], np.argsort(-lo)[:3]
    combos = sorted(((hi[a] + lo[b], int(a), int(b)) for a in top_h for b in top_l), reverse=True)
    return [(a << 4) | b for _, a, b in combos if ((a << 4) | b) <= max_body][:k]


def _try(x, norm, p, g, key, context, direction, seq, tag_bytes, repeat, thr, max_body):
    """Try candidate preamble at p under every copy-index hypothesis.

    The length byte is not a single hard argmax: the top few (score-ranked) length
    candidates are each tried; the tag check decides which one is real.
    """
    base_off = g.m + g.gap
    for c in range(repeat):
        bs = p + base_off
        if bs + HDR_SYMS * g.slot > len(x):
            continue
        scr = _scores(_energies(x, bs, HDR_SYMS, g), _profile(x, p, g))[:, _PERMS[c]]
        for length in _length_candidates(scr, max_body):
            res = _try_len(x, norm, p, c, length, g, key, context, direction, seq, tag_bytes, repeat, thr)
            if res is not None:
                return res
    return None


def _try_len(x, norm, p, c, length, g, key, context, direction, seq, tag_bytes, repeat, thr):
    base_off = g.m + g.gap
    search = round(0.01 * g.fs)
    nsym = _frame_syms(length, tag_bytes)
    clen = _copy_len(g, nsym)
    need = base_off + (nsym - 1) * g.slot + g.lead + g.n

    def complete(s):
        return s >= 0 and s + need <= len(x)

    if not complete(p):
        return None
    copies = [(c, p)]
    for j in range(repeat):
        if j == c:
            continue
        s = p + (j - c) * clen
        lo, hi = max(0, s - search), min(len(norm), s + search + 1)
        if hi <= lo:
            continue
        q = lo + int(np.argmax(norm[lo:hi]))
        if norm[q] >= 0.5 * thr and complete(q):
            copies.append((j, q))
    sc = {}
    for j, q in copies:
        E = _energies(x, q + base_off, nsym, g)
        sc[j] = _scores(E, _profile(x, q, g))[:, _PERMS[j]]  # column v = score of nibble v
    for subset in ([j for j, _ in copies], [c]):
        tot = sum(sc[j] for j in subset)
        msg = _parse(tot.argmax(axis=1), length, key, context, direction, seq, tag_bytes)
        if msg is not None:
            start = p - c * clen
            return msg, start, start + repeat * clen
        if len(copies) == 1:
            break
    return None


def _scan(x, key, fs, context, direction, seq, tag_bytes, repeat, threshold, max_body, band_pass,
          max_candidates=256, norm=None):
    """-> [(Message, span_start, span_end)] sorted by position (spans in sample indices)."""
    g = _geo(fs)
    if len(x) < g.m:
        return []
    if norm is None:
        norm = _norm_corr(x, g, band_pass)
    found, spans = [], []
    for p in _peaks(norm, threshold, g.m // 4, max_candidates):
        if any(s <= p < e for s, e in spans):
            continue  # suppress only inside a transmission already decoded
        res = _try(x, norm, p, g, key, context, direction, seq, tag_bytes, repeat, threshold, max_body)
        if res is not None:
            found.append(res)
            spans.append((res[1], res[2]))
    found.sort(key=lambda r: r[1])
    return found


def _to_mono(x: np.ndarray, g: _Geo, band_pass: bool = True) -> tuple[np.ndarray, np.ndarray | None]:
    """-> (mono signal, its correlation or None). Picks the channel with the strongest
    preamble correlation (not the mean) and returns that correlation so it is not redone."""
    x = np.asarray(x, dtype=np.float64)
    if x.ndim == 1:
        return x, None
    if x.ndim != 2:
        raise ValueError("samples must be 1-D or (n, channels)")
    if x.shape[1] == 1 or len(x) < g.m:
        return x[:, 0], None
    best, bx, bn = -1.0, None, None
    for i in range(x.shape[1]):
        xi = np.ascontiguousarray(x[:, i])
        nrm = _norm_corr(xi, g, band_pass)
        pk = float(np.max(nrm))
        if pk > best:
            best, bx, bn = pk, xi, nrm
    return bx, bn


def decode_audio(samples: np.ndarray, key: bytes, fs: int, context: bytes = b"", direction: int = 1,
                 seq: int | None = None, tag_bytes: int = TAG_BYTES, repeat: int = 2,
                 threshold: float = 0.2, max_body: int = MAX_BODY, band_pass: bool = True) -> list[Message]:
    """Return verified messages [(seq, body), ...] found in a recording (duplicates collapsed).

    `context`/`direction`/`tag_bytes`/`repeat` must match the sender. `seq=None` accepts any
    sequence number (returned); an int accepts only that value.
    """
    _check_params(direction, 0, tag_bytes)
    g = _geo(fs)
    x, norm = _to_mono(samples, g, band_pass)
    out: list[Message] = []
    for msg, _, _ in _scan(x, key, fs, context, direction, seq, tag_bytes, repeat, threshold,
                           max_body, band_pass, norm=norm):
        if msg not in out:
            out.append(msg)
    return out


class AudioListener:
    """Streaming decoder: feed(samples) -> newly decoded messages.

    Real-time design: feed() only buffers until >= 0.5 s of new audio has accumulated,
    then scans. A scan correlates just the new samples (plus chirp-length/filter-warm-up
    overlap; the rest of the correlation is cached per channel), picks peaks over the
    retained window (one max-transmission length + 1 s, so a message split across chunks
    completes), and tries each peak that is not inside an already-decoded span or a
    finally-failed one. Multichannel input keeps one correlation per channel and, per
    peak, uses the channel with the strongest correlation (re-evaluated every scan, never
    locked). Results are deduped by absolute position + seq/body.
    """

    SCAN_S = 0.5

    def __init__(self, key: bytes, fs: int, context: bytes = b"", direction: int = 1,
                 seq: int | None = None, tag_bytes: int = TAG_BYTES, repeat: int = 2,
                 threshold: float = 0.2, max_body: int = MAX_BODY, band_pass: bool = True):
        _check_params(direction, 0, tag_bytes)
        self._g = _geo(fs)
        self.fs = int(fs)
        self._args = (key, context, direction, seq, tag_bytes, repeat, threshold, max_body)
        self._band_pass = band_pass
        self._thr = threshold
        self._maxlen = round(max_transmission_s(fs, max_body, tag_bytes, repeat) * fs)
        self._keep = self._maxlen + self.fs
        self._scan_n = round(self.SCAN_S * self.fs)
        self._pad = round(0.25 * self.fs)  # band-pass edge warm-up / unsettled tail
        self._bufs: list[np.ndarray] | None = None  # per channel
        self._norms: list[np.ndarray] = []           # per channel, valid cached correlation
        self._pend: list[np.ndarray] = []
        self._npend = 0
        self._base = 0  # absolute index of buf[0]
        self._seen: list[tuple[int, int, Message]] = []
        self._failed: list[int] = []  # absolute peak positions that can never decode

    @property
    def position(self) -> int:
        """Absolute number of samples fed so far."""
        return self._base + (len(self._bufs[0]) if self._bufs else 0) + self._npend

    def feed(self, samples: np.ndarray) -> list[Message]:
        x = np.array(samples, dtype=np.float64)  # copy: callers reuse their buffers
        if x.ndim == 1:
            x = x[:, None]
        elif x.ndim != 2:
            raise ValueError("samples must be 1-D or (n, channels)")
        if self._bufs is None:
            self._bufs = [np.zeros(0) for _ in range(x.shape[1])]
            self._norms = [np.zeros(0) for _ in range(x.shape[1])]
        elif x.shape[1] < len(self._bufs):
            raise ValueError("channel count changed between feeds")
        self._pend.append(x[:, : len(self._bufs)])
        self._npend += len(x)
        if self._npend < self._scan_n:
            return []
        return self._scan_now()

    def _update_norms(self) -> list[np.ndarray]:
        g, m, pad = self._g, self._g.m, self._pad
        fulls = []
        for i, buf in enumerate(self._bufs):
            old = self._norms[i]
            a = len(old)
            n_full = len(buf) - m + 1
            if n_full <= a:
                fulls.append(old)
                continue
            s0 = max(0, a - pad)
            fresh = _norm_corr(buf[s0:], g, self._band_pass)[a - s0:]
            full = np.concatenate([old, fresh])
            fulls.append(full)
            self._norms[i] = full[: max(a, len(full) - pad)]  # tail edge unsettled: recompute
        return fulls

    def _scan_now(self) -> list[Message]:
        new = np.concatenate(self._pend)
        self._pend, self._npend = [], 0
        self._bufs = [np.concatenate([b, new[:, i]]) for i, b in enumerate(self._bufs)]
        g, L = self._g, len(self._bufs[0])
        found: list[Message] = []
        if L >= g.m:
            key, context, direction, seq, tag_bytes, repeat, thr, max_body = self._args
            fulls = self._update_norms()
            if len(fulls) == 1:
                norm, chs = fulls[0], None
            else:
                stack = np.vstack(fulls)
                norm, chs = stack.max(axis=0), stack.argmax(axis=0)
            for p in _peaks(norm, thr, g.m // 4, 256):
                ap = self._base + p
                if any(b0 <= ap < b1 for b0, b1, _ in self._seen):
                    continue
                if any(abs(ap - f) < g.m // 4 for f in self._failed):
                    continue
                ch = 0 if chs is None else int(chs[p])
                res = _try(self._bufs[ch], fulls[ch], p, g, key, context, direction, seq,
                           tag_bytes, repeat, thr, max_body)
                if res is None:
                    if L - p >= self._maxlen:
                        self._failed.append(ap)  # whole transmission was in view: final
                    continue
                msg, s, e = res
                a0, a1 = self._base + s, self._base + e
                if any(m == msg and a0 < b1 and b0 < a1 for b0, b1, m in self._seen):
                    continue
                self._seen.append((a0, a1, msg))
                found.append(msg)
        if L > self._keep:
            drop = L - self._keep
            self._bufs = [b[drop:] for b in self._bufs]
            self._norms = [n[drop:] for n in self._norms]
            self._base += drop
        self._seen = [r for r in self._seen if r[1] > self._base]
        self._failed = [f for f in self._failed if f > self._base]
        return found


# ---------------------------------------------------------------------------
# notes: a few raw bytes, no seq/len/tag (the caller's ``accept`` decides what is real)
# ---------------------------------------------------------------------------

# Notes use a faster geometry than framed messages: 0.1 s chirp, 50 ms notes (40 ms tone), 20 ms gaps.
NOTE_GEO = dict(slot_s=0.05, burst_s=0.04, chirp_s=0.10, gap_s=0.02)


def _note_geo(fs: int) -> _Geo:
    return _geo(fs, **NOTE_GEO)


def encode_notes(data: bytes, fs: int, repeat: int = 2, amplitude: float = 0.6) -> np.ndarray:
    """``data`` as 2 notes per byte after the chirp, ``repeat`` copies (each its own tone permutation).
    A 2-byte token is 0.34 s per copy (~0.7 s at repeat 2). Nothing else is sent: the receiver must know
    what it expects."""
    g = _note_geo(fs)
    nib = _nibbles(data)
    clen = _copy_len(g, len(nib))
    copies = []
    for j in range(repeat):
        sig = np.zeros(clen)
        sig[: g.m] = g.chirp
        base = g.m + g.gap + g.lead
        for i, v in enumerate(nib):
            s0 = base + i * g.slot
            sig[s0 : s0 + g.n] = g.S[2 * _PERMS[j][v] + (i & 1)]
        copies.append(sig)
    out = np.concatenate(copies + [np.zeros(round(0.03 * fs))])
    return (amplitude * out / np.max(np.abs(out))).astype(np.float32)


class NoteListener:
    """Streaming decoder for ``encode_notes``: feed(samples) -> accepted payloads (each once).

    Keeps the last ~2 s, rescans them every 0.25 s of new audio, and tries every chirp peak under each
    copy hypothesis; a candidate counts only if ``accept(payload)`` is true (e.g. an expected keyed token), which
    is what rejects noise. Payloads already returned are not returned again until they leave the window.
    """

    SCAN_S = 0.25

    def __init__(self, fs: int, nbytes: int = 2, repeat: int = 2, accept=lambda b: True, threshold: float = 0.2):
        self._g = _note_geo(fs)
        self.fs, self.nbytes, self.repeat, self.accept, self._thr = int(fs), nbytes, repeat, accept, threshold
        self._nsym = 2 * nbytes
        self._clen = _copy_len(self._g, self._nsym)
        self._keep = repeat * self._clen + self.fs
        self._buf = np.zeros(0)
        self._new = 0
        self._base = 0  # absolute index of _buf[0]
        self._seen: dict[int, bytes] = {}  # absolute start -> payload already returned

    def feed(self, samples: np.ndarray) -> list[bytes]:
        x = np.asarray(samples, dtype=np.float64)
        if x.ndim == 2:
            x = x[:, 0]
        self._buf = np.concatenate([self._buf, x])
        self._new += len(x)
        if self._new < self.SCAN_S * self.fs:
            return []
        self._new = 0
        cut = max(0, len(self._buf) - self._keep)
        self._buf, self._base = self._buf[cut:], self._base + cut
        self._seen = {k: v for k, v in self._seen.items() if k >= self._base - self.repeat * self._clen}
        return self._scan()

    def _scan(self) -> list[bytes]:
        g, x, out = self._g, self._buf, []
        if len(x) < g.m:
            return out
        norm = _norm_corr(x, g, True)
        need = g.m + g.gap + (self._nsym - 1) * g.slot + g.lead + g.n
        for p in _peaks(norm, self._thr, g.m // 4, 64):
            starts = [self._base + p - c * self._clen for c in range(self.repeat)]
            if any(abs(st - s) < g.m for st in starts for s in self._seen):
                continue  # a copy of a transmission already returned
            for c in range(self.repeat):  # which copy this chirp starts
                start = starts[c]
                tot, ok = 0, False
                for j in range(self.repeat):
                    q = p + (j - c) * self._clen
                    if q < 0 or q + need > len(x):
                        continue
                    E = _energies(x, q + g.m + g.gap, self._nsym, g)
                    tot = tot + _scores(E, _profile(x, q, g))[:, _PERMS[j]]
                    ok = ok or j == c
                if not ok:
                    continue
                nib = np.asarray(tot).argmax(axis=1)
                data = bytes((int(nib[i]) << 4) | int(nib[i + 1]) for i in range(0, len(nib), 2))
                if self.accept(data):
                    self._seen[start] = data
                    out.append(data)
                    break
        return out
