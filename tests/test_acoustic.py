"""Harsh acoustic-modem suite. Rates are printed (run with `-s`) and asserted >= 90 %.

SNR convention: signal power = mean square of the whole transmitted waveform (guards
included); noise is white noise confined to 1.0-6.0 kHz (the modem band), so "0 dB" is
a true in-band SNR. Every case uses seeded rngs, ~15 trials, random body/seq/lead-in.
"""
import numpy as np
import pytest

from lightferry import acoustic as ac

KEY = b"shared-secret-key"
FS = 48000
TRIALS = 15
MIN_OK = int(np.ceil(0.9 * TRIALS))  # 14/15


# ---------------------------------------------------------------- channel helpers
def band_noise(n, fs, rng, lo=1000.0, hi=6000.0):
    X = np.fft.rfft(rng.normal(size=n))
    f = np.fft.rfftfreq(n, 1 / fs)
    X[(f < lo) | (f > hi)] = 0
    y = np.fft.irfft(X, n)
    return y / np.sqrt(np.mean(y**2))


def embed(sig, fs, rng, lead=(0.2, 1.5), tail=0.6):
    lead_n = int(rng.uniform(*lead) * fs)
    return np.concatenate([np.zeros(lead_n), sig.astype(np.float64), np.zeros(int(tail * fs))])


def add_noise(x, p_sig, snr_db, fs, rng):
    return x + band_noise(len(x), fs, rng) * np.sqrt(p_sig / 10 ** (snr_db / 10))


def random_ir(fs, rng, t60, drr_db=0.0):
    n = int(t60 * fs * 1.1)
    t = np.arange(n) / fs
    h = rng.normal(size=n) * 10 ** (-3 * t / t60)  # -60 dB at T60
    h[: int(0.002 * fs)] = 0
    h *= np.sqrt(10 ** (-drr_db / 10) / np.sum(h**2))
    h[0] = 1.0
    return h


def fconv(x, h):
    n = 1 << int(np.ceil(np.log2(len(x) + len(h))))
    return np.fft.irfft(np.fft.rfft(x, n) * np.fft.rfft(h, n), n)[: len(x)]


def notch(x, fs, f0, depth_db=25.0, width=20.0):
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / fs)
    X *= 1 - (1 - 10 ** (-depth_db / 20)) * np.exp(-0.5 * ((f - f0) / width) ** 2)
    return np.fft.irfft(X, len(x))


def sustained_tones(n, fs, rng, p_each, k=3):
    t = np.arange(n) / fs
    y = np.zeros(n)
    for f in rng.uniform(1300, 5700, k):
        y += np.sqrt(2 * p_each) * np.sin(2 * np.pi * f * t + rng.uniform(0, 6.28))
    return y


def babble(n, fs, rng, talkers=4):
    """Speech-like: voiced harmonics + noise under formant weights and ~4 Hz syllable envelopes."""
    t = np.arange(n) / fs
    f = np.fft.rfftfreq(n, 1 / fs)
    out = np.zeros(n)
    for _ in range(talkers):
        f0 = rng.uniform(90, 250)
        form = rng.uniform(400, 3500, 3)
        shape = sum(np.exp(-0.5 * ((f - c) / 350) ** 2) for c in form) + 0.05
        voiced = np.zeros(n)
        for h in range(1, int(5500 / f0)):
            w = shape[min(int(h * f0 * n / fs), len(shape) - 1)]
            voiced += w * np.sin(2 * np.pi * h * f0 * (1 + 0.03 * np.sin(2 * np.pi * 0.7 * t + h)) * t + rng.uniform(0, 6))
        hiss = np.fft.irfft(np.fft.rfft(rng.normal(size=n)) * shape, n)
        env = np.abs(np.sin(np.pi * rng.uniform(2, 5) * t + rng.uniform(0, 3))) ** 1.5
        out += env * (voiced * 0.5 + hiss * 3)
    return out / np.sqrt(np.mean(out**2))


def run_trials(make_rx, fs=FS, seed=0, **dec_kw):
    """make_rx(rng, tx, fs) -> received array. Returns (ok, trials)."""
    rng = np.random.default_rng(seed)
    ok = 0
    for i in range(TRIALS):
        body = bytes(rng.integers(0, 256, int(rng.integers(1, 9)), dtype=np.uint8))
        seq = int(rng.integers(0, 65536))
        tx = ac.encode_message(body, KEY, fs, context=b"sess", seq=seq)
        rx = make_rx(rng, tx, fs)
        got = ac.decode_audio(rx, KEY, fs, context=b"sess", **dec_kw)
        ok += (seq, body) in got
    return ok


def report(name, ok):
    print(f"RATE {name}: {ok}/{TRIALS} = {100 * ok / TRIALS:.0f}%")


# ---------------------------------------------------------------- basics / API
def test_clean_roundtrip_and_tuple_result():
    out = ac.decode_audio(ac.encode_message(b"\x01\xff\x03tile-ack", KEY, FS, seq=7), KEY, FS)
    assert out == [(7, b"\x01\xff\x03tile-ack")]
    assert out[0].seq == 7 and out[0].body == b"\x01\xff\x03tile-ack"


@pytest.mark.parametrize("body", [b"", b"x" * ac.MAX_BODY])
def test_body_length_extremes(body):
    assert ac.decode_audio(ac.encode_message(body, KEY, FS), KEY, FS) == [(0, body)]


def test_fs_required_and_validated():
    with pytest.raises(TypeError):
        ac.encode_message(b"a", KEY)  # no default fs
    with pytest.raises(TypeError):
        ac.decode_audio(np.zeros(100), KEY)
    with pytest.raises(ValueError):
        ac.encode_message(b"a", KEY, 12000)
    with pytest.raises(ValueError):
        ac.decode_audio(np.zeros(100), KEY, 8000)
    assert ac.decode_audio(ac.encode_message(b"ok", KEY, 16000), KEY, 16000) == [(0, b"ok")]


def test_tag_length_configurable():
    for tb in (4, 6, 10):
        sig = ac.encode_message(b"hi", KEY, FS, tag_bytes=tb)
        assert ac.decode_audio(sig, KEY, FS, tag_bytes=tb) == [(0, b"hi")]
    assert ac.decode_audio(ac.encode_message(b"hi", KEY, FS, tag_bytes=4), KEY, FS, tag_bytes=6) == []


def test_wrong_key_ignored():
    sig = ac.encode_message(b"ack", KEY, FS)
    assert ac.decode_audio(sig, b"other-key", FS) == []


def test_replay_and_session_binding():
    sig_a = ac.encode_message(b"ack", KEY, FS, context=b"session-A", direction=1, seq=5)
    assert ac.decode_audio(sig_a, KEY, FS, context=b"session-A") == [(5, b"ack")]
    assert ac.decode_audio(sig_a, KEY, FS, context=b"session-B") == []   # replay into session B
    assert ac.decode_audio(sig_a, KEY, FS, context=b"session-A", direction=2) == []  # reflected
    assert ac.decode_audio(sig_a, KEY, FS, context=b"session-A", seq=6) == []        # stale seq
    assert ac.decode_audio(sig_a, KEY, FS, context=b"session-A", seq=5) == [(5, b"ack")]


def test_replay_trials():
    rng = np.random.default_rng(11)
    for _ in range(TRIALS):
        tx = ac.encode_message(bytes(rng.integers(0, 256, 4, dtype=np.uint8)), KEY, FS, context=b"A", seq=3)
        rx = add_noise(embed(tx, FS, rng), np.mean(tx**2), 10, FS, rng)
        assert ac.decode_audio(rx, KEY, FS, context=b"B") == []


def test_multichannel_uses_strongest_not_mean():
    rng = np.random.default_rng(5)
    tx = ac.encode_message(b"mc", KEY, FS)
    good = embed(tx, FS, rng)
    # opposite-polarity junk channel would cancel the mean; strongest-channel pick still works
    two = np.stack([-good, good + 0.0 * good], axis=1)
    assert ac.decode_audio(two, KEY, FS) == [(0, b"mc")]
    noise = rng.normal(0, 0.3, len(good))
    assert ac.decode_audio(np.stack([noise, good], axis=1), KEY, FS) == [(0, b"mc")]


# ---------------------------------------------------------------- harsh channels
@pytest.mark.parametrize("snr", [0, -6])
def test_inband_noise(snr):
    def mk(rng, tx, fs):
        return add_noise(embed(tx, fs, rng), np.mean(tx**2), snr, fs, rng)

    ok = run_trials(mk, seed=100 + snr)
    report(f"in-band noise {snr} dB", ok)
    assert ok >= MIN_OK


@pytest.mark.parametrize("t60", [0.3, 0.6])
def test_reverb(t60):
    def mk(rng, tx, fs):
        x = embed(tx, fs, rng)
        x = fconv(x, random_ir(fs, rng, t60, 0.0))
        return add_noise(x, np.mean(x**2), 20, fs, rng)

    ok = run_trials(mk, seed=int(t60 * 1000))
    report(f"reverb T60={t60}s DRR 0 dB (+20 dB noise)", ok)
    assert ok >= MIN_OK


def test_notch_one_tone():
    def mk(rng, tx, fs):
        x = embed(tx, fs, rng)
        f0 = ac.F0 + ac.DF * int(rng.integers(0, ac.N_TONES))
        x = notch(x, fs, f0, 25.0)
        return add_noise(x, np.mean(x**2), 10, fs, rng)

    ok = run_trials(mk, seed=42)
    report("25 dB notch on one tone (+10 dB noise)", ok)
    assert ok >= MIN_OK


def test_sustained_inband_tones():
    def mk(rng, tx, fs):
        x = embed(tx, fs, rng)
        p = np.mean(tx**2)
        x = x + sustained_tones(len(x), fs, rng, p_each=p * 0.5)  # 3 tones, -3 dB each (~+1.8 dB total)
        return add_noise(x, p, 10, fs, rng)

    ok = run_trials(mk, seed=77)
    report("3 sustained in-band tones, SIR ~ -2 dB", ok)
    assert ok >= MIN_OK


def test_babble():
    def mk(rng, tx, fs):
        x = embed(tx, fs, rng)
        p = np.mean(tx**2)
        return x + babble(len(x), fs, rng) * np.sqrt(p)  # SIR 0 dB over the whole recording

    ok = run_trials(mk, seed=88)
    report("speech-like babble, SIR 0 dB", ok)
    assert ok >= MIN_OK


def test_44k1():
    def mk(rng, tx, fs):
        x = embed(tx, fs, rng)
        x = fconv(x, random_ir(fs, rng, 0.3, 3.0))
        return add_noise(x, np.mean(x**2), 3, fs, rng)

    ok = run_trials(mk, fs=44100, seed=441)
    report("fs=44100, T60 0.3 s DRR 3 dB, 3 dB noise", ok)
    assert ok >= MIN_OK


def test_clock_drift():
    def mk(rng, tx, fs):
        x = embed(tx, fs, rng)
        n = int(len(x) * (1 + 200e-6))
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
        return add_noise(x, np.mean(tx**2), 3, fs, rng)

    ok = run_trials(mk, seed=9)
    report("200 ppm clock drift, 3 dB noise", ok)
    assert ok >= MIN_OK


# ---------------------------------------------------------------- streaming + soak
def test_listener_streaming_dedupe():
    rng = np.random.default_rng(3)
    tx1 = ac.encode_message(b"first", KEY, FS, context=b"s", seq=1)
    tx2 = ac.encode_message(b"second", KEY, FS, context=b"s", seq=2)
    stream = np.concatenate([np.zeros(FS), tx1, np.zeros(2 * FS), tx2, np.zeros(2 * FS)])
    stream = add_noise(stream, np.mean(tx1**2), 10, FS, rng)
    lst = ac.AudioListener(KEY, FS, context=b"s", max_body=16)
    got = []
    for i in range(0, len(stream), 4800):  # 100 ms chunks, messages straddle chunk edges
        got += lst.feed(stream[i : i + 4800])
    assert got == [(1, b"first"), (2, b"second")]
    assert lst.position == len(stream)


def test_listener_replay_ignored_but_resend_counts():
    tx = ac.encode_message(b"again", KEY, FS, context=b"s", seq=9)
    stream = np.concatenate([np.zeros(FS), tx, np.zeros(FS), tx, np.zeros(FS)])
    lst = ac.AudioListener(KEY, FS, context=b"s", max_body=16)
    got = []
    for i in range(0, len(stream), 9600):
        got += lst.feed(stream[i : i + 9600])
    assert got == [(9, b"again"), (9, b"again")]  # two distinct transmissions, each once
    other = ac.AudioListener(KEY, FS, context=b"other", max_body=16)
    assert other.feed(stream) == []


def test_false_accept_soak():
    """~2 min of babble + music + chirp-like sweeps (candidate bait), no message inside."""
    fs = 16000
    rng = np.random.default_rng(2026)
    dur = 120
    n = dur * fs
    parts = [babble(30 * fs, fs, rng) for _ in range(dur // 30)]
    x = 0.3 * np.concatenate(parts)
    t = np.arange(n) / fs
    for _ in range(60):  # melodic notes
        f, s = rng.uniform(300, 5500), int(rng.uniform(0, dur - 1) * fs)
        L = int(rng.uniform(0.1, 0.6) * fs)
        x[s : s + L] += 0.15 * np.sin(2 * np.pi * f * t[:L])
    for _ in range(40):  # sweeps in the preamble band
        s, L = int(rng.uniform(0, dur - 1) * fs), int(0.25 * fs)
        f0, f1 = rng.uniform(600, 3000), rng.uniform(3000, 6000)
        tt = t[:L]
        x[s : s + L] += 0.3 * np.sin(2 * np.pi * (f0 * tt + 0.5 * (f1 - f0) / 0.25 * tt**2))
    assert ac.decode_audio(x, KEY, fs) == []
    lst = ac.AudioListener(KEY, fs, max_body=8)
    got = []
    for i in range(0, len(x), fs):
        got += lst.feed(x[i : i + fs])
    assert got == []


def test_pure_noise_never_decodes():
    rng = np.random.default_rng(7)
    for _ in range(5):
        assert ac.decode_audio(rng.normal(0, 0.3, 5 * FS), KEY, FS) == []


def test_notes_round_trip_and_reject_noise():
    fs = 48000
    rng = np.random.default_rng(3)
    toks = [b"\xa7\x3c", b"\x01\xfe"]
    sig = np.concatenate([np.zeros(fs)] + [np.concatenate([ac.encode_notes(t, fs), np.zeros(fs // 2)])
                                           for t in toks] + [np.zeros(2 * fs)])
    assert len(ac.encode_notes(toks[0], fs)) / fs < 0.8
    x = (sig + rng.standard_normal(len(sig)) * 0.2).astype(np.float32)
    lst = ac.NoteListener(fs, accept=lambda b: b in toks)
    got = []
    for i in range(0, len(x), fs // 4):
        got += lst.feed(x[i:i + fs // 4])
    assert got == toks
    lst = ac.NoteListener(fs, accept=lambda b: b in toks)
    noise = (rng.standard_normal(fs * 20) * 0.3).astype(np.float32)
    assert sum((lst.feed(noise[i:i + fs // 4]) for i in range(0, len(noise), fs // 4)), []) == []
