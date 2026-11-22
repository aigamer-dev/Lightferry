# Lightferry

**Move files between air-gapped machines with nothing but a screen and a camera.**

Lightferry is a visual sneakernet. The sender shows a stream of dense black-and-white matrix codes on its screen; the receiver reads them with a webcam. The two machines are paired by an on-screen handshake, so the default mode needs no key and no setup. Add `--encrypt` with a shared key when the screen can be seen by others. No network, no USB stick, no radio.

> Status: early alpha (`0.0.1a3`). The codec, protocol, archive and handshake logic are covered by tests; the real webcam, screen and audio paths have had only light testing. See [Status and limitations](#status-and-limitations).

Documentation: <https://lightferry.aigamer.dev>

## How it works

```
 SENDER                                            RECEIVER
 file/folder                                       destination folder
   |  tar + compress (deterministic)                  ^  verify sha256, safe extract
   v                                                  |
 tiles (chunks) -----packets----------->  dense    camera --> detect, warp, decode
   |            shown as 1..4x4 codes   matrix    frame        (CRC per code)
   v                                    codes        |
 screen  ==================== light ===================> webcam
   ^                                                  |
   |  acks / verify: second screen+camera, OR        v
 camera or microphone  <==== back-channel ====  screen or speaker
```

1. The sender packs the path into a deterministic archive and splits it into tiles.
2. Each tile becomes a packet (CRC-checked; encrypted and HMAC-SHA256 authenticated with `--encrypt`), drawn as a dense binary matrix code (not QR), several codes per frame.
3. The receiver's webcam finds the frame, corrects perspective, decodes each code and checks its CRC.
4. Stop-and-wait ARQ: the receiver acknowledges over a back-channel (camera-to-screen, or acoustic notes from its speaker to the sender's microphone), and the sender resends what is missing.
5. A HELLO/SYNC/START/DONE handshake with fresh session nonces pairs the two sides and shows a 6-digit VERIFY code derived from the session and the manifest hash. By default it is logged and confirmed automatically; with `--encrypt` (or `--verify`) each operator confirms it with y/N on the console.
6. The receiver checks size and SHA-256, then extracts into a private temp dir and moves the result into place.

## Features

- Dense custom matrix code with asymmetric finders (exact orientation, no rotation search), grids 32 to 177, up to 4x4 codes per frame.
- Default mode needs no key: handshake, session nonces, CRC, 4096-wide replay window, size and sha256 checks.
- Optional `--encrypt`: every packet body encrypted and HMAC-SHA256 authenticated with per-session keys (standard library only, no extra dependency).
- Files and folders, deterministic tar, `auto`/`lzma`/`zlib`/`none` compression.
- Hardened extraction (path jail, size and sha256 checks, bounded decompression, Windows/macOS/Linux-safe names).
- Two back-channels: camera (both machines have a camera) or audio (sender has no camera).
- Offline `selftest`, a degradation `bench`, and a phone web-app test client (`mobile-serve`).
- Planned, not built: a signed command channel (`cmd` / `serve`). Packet types exist as stubs only.

## Install

Requires Python 3.11.4 or newer.

```bash
pip install "lightferry @ git+https://github.com/aigamer-dev/Lightferry"
# optional extras: phone test client and acoustic back-channel
pip install "lightferry[mobile,audio] @ git+https://github.com/aigamer-dev/Lightferry"
```

From a checkout:

```bash
git clone https://github.com/aigamer-dev/Lightferry && cd Lightferry
python -m venv .venv && . .venv/bin/activate
pip install -e ".[mobile,audio]"
```

## Quick start

```bash
# 1. Check the whole pipeline in-process, no hardware needed
lightferry selftest

# 2. Receiver (needs a webcam): save into ./incoming
lightferry --receive --path ./incoming

# 3. Sender: point the receiver's camera at this screen
lightferry --send --path ./my-folder
```

If the sender has no camera, use the acoustic back-channel (the receiver plays notes the sender's microphone listens for):

```bash
lightferry --send --path ./my-folder --backchannel audio --no-camera --fullscreen
lightferry --receive --path ./incoming --backchannel audio
```

The sender can only report success as "unconfirmed" in audio mode: it heard a 16-bit token, so check the receiver's console for the real result.

A one-line note on stderr says the default mode is not encrypted: anyone who can see the screen can read or inject. The 6-digit VERIFY code is logged and confirmed automatically; add `--verify` (or `--encrypt`) to be asked `does the other machine show NNNNNN? [y/N]`. No answer ends the session with `VERIFY_TIMEOUT`.

### Encrypted transfer (optional)

```bash
lightferry keygen --out lightferry.key      # once; carry the file to the other machine by hand
lightferry --receive --path ./incoming --encrypt --key-file lightferry.key
lightferry --send --path ./my-folder --encrypt --key-file lightferry.key
```

A key without `--encrypt`, or `--encrypt` without a key, is an error. A keyless side against an `--encrypt` side fails with `KEY_MISMATCH`. Instead of a key file you can use `LIGHTFERRY_KEY`, `--key`, or a shared passphrase (`LIGHTFERRY_PASSPHRASE`, 12+ characters).

### Tips for a reliable scan

- Aim the receiver's camera so the code fills most of the view; keep the camera in focus.
- Run the sender fullscreen (`--fullscreen`, or `f`) at full screen brightness.
- `LIGHTFERRY_DEBUG=1` prints camera diagnostics (resolution, code sharpness and contrast) and saves undecodable frames.
- On Windows the camera may need a second try or a moment to lock on.

### Phone test

Use a phone as the receiver's camera, speaker and buttons while developing:

```bash
lightferry mobile-serve --path ./my-folder
```

The server binds this machine's LAN IP only (use `--host` to pick another address) and accepts one WebSocket client at a time (a second gets HTTP 409). Open the printed Phone URL on the phone (accept the self-signed certificate warning), press Start, and point it at the screen page; confirm the VERIFY code with the on-screen buttons on both the screen page and the phone. This is a test tool, not the real path. It is keyless by default; `--key-file` there adds MAC authentication only. `python -m lightferry.mobile ...` is the same as `lightferry mobile-serve ...`.

> **Want encryption without a key file?** With `--encrypt`, both machines can derive the key from a shared passphrase: set `LIGHTFERRY_PASSPHRASE` (12+ characters). See the usage guide.

## CLI reference

| Command | Purpose |
|---|---|
| `lightferry --send --path P` | show a file or folder as codes |
| `lightferry --receive --path DIR` | scan codes and write the result under `DIR` |
| `lightferry keygen [--out FILE]` | write a fresh 32-byte key, refuses to overwrite |
| `lightferry selftest` | full transfer in-process on a virtual clock |
| `lightferry bench [--quick] [--out CSV] [--trials N]` | simulate camera degradation, recommend a profile |
| `lightferry mobile-serve --path P [--port N] [--host H] [--backchannel audio\|direct] [--remote-sender] [--out DIR]` | phone test client |

Common flags for `--send`/`--receive`: `--encrypt` with `--key-file`, `--key` (visible in process list; avoid), env `LIGHTFERRY_KEY` or `LIGHTFERRY_PASSPHRASE` (keys are only used with `--encrypt`), `--verify`, `--camera webcam|dir:<path>`, `--camera-index`, `--backchannel auto|camera|audio`, `--no-camera`, `--grid`, `--module-px`, `--tiles`, `--timeout`, `--retries`, `--compress`, `--no-compress`, `--overwrite`, `--fullscreen`, `--yes` (test hook, `dir:` only: with a real webcam it is an error, and real hardware always asks y/N at VERIFY). `--camera dir:` works only with the camera back-channel, so combining it with `--backchannel audio` or `--no-camera` is rejected. With `--encrypt`, set `LIGHTFERRY_KEY=@lightferry.key` (see `.env.example`) instead of passing a key each time. Full details in [Usage](https://lightferry.aigamer.dev/usage/).

## Security model

- Default mode: pairing by handshake, CRC, replay window and sha256/size checks, but no secrecy and no authentication. Anyone who can see the screen can read or inject.
- `--encrypt`: bodies are encrypted and authenticated with a shared key, so an observer cannot read or spoof the transfer. The key never leaves the machines; move it by hand and keep it out of version control.
- Fresh nonces per session; replayed packets are rejected (with `--encrypt`, old sessions also fail the MAC).
- Audio mode: the receiver records each accepted sender nonce in `<dest>/.lightferry-seen` and ignores a replayed one; if it cannot write that file it aborts with `DEST_NOT_WRITABLE`. Audio note tokens are rate limited (more than 20 bad tokens in a minute pauses acceptance).
- The phone test server uses a fresh private TLS cert per run and binds the LAN IP only.
- Received archives are validated before and during extraction and cannot write outside the destination.

See [Security](https://lightferry.aigamer.dev/security/).

## Status and limitations

- Early alpha `0.0.1a3`; formats and flags may change.
- Real webcam, on-screen display and audio paths are lightly tested; expect tuning (`--grid`, `--module-px`, `--tiles`).
- No error correction in the code itself: reliability comes from ack/retry, so throughput depends on camera quality.
- Everything is held in RAM; large transfers need several times the data size in memory.
- Audio-mode success on the sender is "unconfirmed" (16-bit tokens).
- The command channel (`cmd`/`serve`) is planned, not implemented.

## Development

```bash
pip install -e ".[dev,mobile,audio]"
pytest
```

Set `LIGHTFERRY_DEBUG=1` for camera diagnostics, undecodable-frame dumps and phone-server frame debug output.

Design notes live in [docs/PLAN.md](docs/PLAN.md). See [Development](https://lightferry.aigamer.dev/development/).
