# How it works

## Layers

| Layer | Module | Job |
|---|---|---|
| Archive | `archive.py` | deterministic tar, compression, safe unpack |
| Tiler | `tiler.py` | split bytes into tiles, reassemble, sha256 check |
| Protocol | `protocol.py` | packet header, HMAC seal, keys, replay window |
| Link | `link.py` | stop-and-wait ARQ, profiles, ACK format |
| Handshake | `handshake.py` | Sender and Receiver state machines |
| Codec | `codec.py`, `vision.py` | matrix code, frame layout, camera detection |
| Channels | `channels.py`, `io_platform.py`, `acoustic.py` | webcam, screen, audio, test channels |

## Codec

One code is an N x N grid of black or white modules, N in 32, 64, 96, 128, 177. Each corner has a 4x4 finder, each with a different white-mark pattern, so every rotation and mirror of a code is distinguishable and the matrix can be restored exactly. A 1-module white ring separates finders from data. Data cells carry an 8-byte header (magic, version, payload length, CRC32) plus the payload, XOR-whitened with a fixed public keystream. There is no error correction; a code with a bad CRC is simply dropped and retransmitted.

Payload capacity is `(N*N - 100) // 8 - 8` bytes, for example 491 bytes at grid 64. A packet adds 56 bytes of header and tag.

A displayed frame is a white card with a thick black border containing 1x1 to 4x4 codes, each with its own quiet zone. The camera side finds the border, corrects perspective, then decodes each code on its own, so tile order does not matter.

## Tiler

The archive is cut into chunks sized to one code's body capacity. Each tile carries object id, sequence number and total. The reassembler dedupes by (object, seq) and the final blob is checked against its SHA-256.

## Link

Packets are `header(24) | body | HMAC tag(32)`. The sender shows a frame of pending tiles and keeps it up until acknowledged or a timeout fires. An ACK carries the lowest missing sequence plus a 16-bit bitmap of the following tiles, so a frame of several codes is acknowledged in one reply. Retries are bounded (`--retries`).

## Handshake

States: HELLO, SYNC, VERIFY, READY, START, DATA, DONE, CLOSED (or ABORTED).

1. Both sides prove knowledge of the pre-shared key in HELLO/HELLO_ACK, each with a fresh nonce.
2. Session keys are derived with HMAC from the key and both nonces; the session id is a hash of the nonces. A packet from another session fails its MAC.
3. SYNC sends the manifest (names, sizes, sha256, compression). The receiver checks that the destination is writable, has space and that nothing collides.
4. Both sides display the same 6-digit VERIFY code derived from the session keys and manifest hash. It is confirmed by a person: the CLI asks y/N on the console with a real webcam, and the phone server shows buttons on the screen page (sender) and the phone (receiver). Only test channels (`--camera dir:`, `--yes`, in-process `selftest`) confirm automatically. No answer gives `VERIFY_TIMEOUT`, `n` gives `OPERATOR_REJECT`.
5. A profile (grid, module size, tiles) is negotiated, then the sender sends START and the DATA tiles.
6. The receiver verifies, extracts and sends DONE; the sender answers DONE_ACK and both linger so the last frame stays visible.

Abort reasons include timeouts, `KEY_MISMATCH` (persistent MAC failures show a "KEY MISMATCH?" warning), `NO_COMMON_PROFILE`, `EXISTS` and `NO_SPACE`.

## Back-channel

- **Camera**: the receiver shows its replies as codes on its own screen and the sender reads them with its webcam.
- **Audio**: the receiver never sends packets. Each reply is a 2-byte keyed note token ("I got your packet X") played as 4 notes after a chirp, twice (about 1.6 s), and heard by the sender's microphone. The tokens are HMAC-derived from the session key and bound to the packet they answer, so they cannot be replayed into another session. Because 16 bits are guessable, the sender keeps only a few tokens valid at once and reports an audio-mode success as "unconfirmed". The receiver sends no nonce in audio mode, so it records each accepted sender nonce in `<dest>/.lightferry-seen` (mode 600, newest 4096) and ignores a HELLO that reuses one (a recorded sender screen replayed later); if it cannot write the file it aborts with `DEST_NOT_WRITABLE`. The sender's listener also has a rate limit: more than 20 bad tokens in a minute pauses acceptance until it clears. `--camera dir:` carries packets only, so it cannot be combined with the audio back-channel. The underlying modem (`acoustic.py`) is a 32-tone MFSK scheme between 1.2 and 5.85 kHz with a chirp preamble and repeated copies for robustness.

With audio the sender defaults to one large code per frame, since phones or distant webcams do not resolve a 3x3 layout well.
