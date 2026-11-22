# Usage

```
lightferry {--send,--receive} --path PATH [options]
lightferry {keygen,selftest,bench,mobile-serve} [...]
```

## Default mode (no key)

Nothing else is needed:

```bash
lightferry --receive --path ./incoming
lightferry --send --path ./folder
# sender without a webcam: acoustic back-channel
lightferry --send --path ./folder --backchannel audio --no-camera --fullscreen
lightferry --receive --path ./incoming --backchannel audio
```

The two sides are paired by the on-screen handshake (HELLO/SYNC/START/DONE over the codes, plus the acoustic reply notes), session nonces, CRC, replay window, sha256/size checks and the hardened archive extraction. A one-line stderr note says the data is not encrypted: anyone who can see the screen can read or inject. The 6-digit VERIFY code is logged and confirmed automatically; add `--verify` to be asked y/N. A keyless side cannot talk to an `--encrypt` side (`KEY_MISMATCH`). See [Security](security.md).

## Encryption (optional)

`--encrypt` encrypts and authenticates every session packet body and keeps the y/N VERIFY prompt on. Both machines need the same key, provided with one of:

- `--key-file FILE`: 32 raw bytes, or hex text (what `keygen` writes is raw bytes)
- `LIGHTFERRY_KEY` environment variable: hex, or `@path` to a key file (`.env.example` uses `LIGHTFERRY_KEY=@lightferry.key`)
- `--key HEX`: works but leaks into the process list; a warning is printed
- a shared passphrase (see below)

Keys shorter than 16 bytes are rejected. A key without `--encrypt` is an error, and so is `--encrypt` without a key.

```bash
lightferry keygen --out lightferry.key     # carry the file to the other machine by hand
lightferry --receive --path ./incoming --encrypt --key-file lightferry.key
lightferry --send --path ./folder --encrypt --key-file lightferry.key
```

## keygen

```bash
lightferry keygen [--out lightferry.key]
```

Writes 32 random bytes with mode 600. Refuses to overwrite an existing file.

## send

```bash
lightferry --send --path ./folder-or-file
```

| Flag | Meaning |
|---|---|
| `--path` | file or folder to send |
| `--camera` | `webcam` (default) or `dir:<path>` (frames as PNG files, for two processes on one machine); `loop` is selftest only |
| `--camera-index N` | webcam device index (default 0) |
| `--backchannel` | `auto` (default: audio if `--no-camera`, else camera), `camera`, `audio` |
| `--no-camera` | sender has no camera; use the audio back-channel |
| `--grid N` | proposed code grid (default 64; allowed 32, 64, 96, 128, 177) |
| `--module-px N` | proposed pixels per module (default 6) |
| `--tiles N` or `NxN` | codes per frame side, 1 to 4 (default 3; 1 with audio) |
| `--timeout S` | per-packet timeout seconds (default 1.5) |
| `--retries N` | retry limit (default 8) |
| `--compress [auto\|lzma\|zlib\|none]`, `--no-compress` | archive compression (default auto) |
| `--encrypt` | encrypt and authenticate with a key (see Encryption) |
| `--verify` | default mode: ask y/N for the VERIFY code instead of auto-confirming |
| `--fullscreen` | start fullscreen |
| `--yes` | test hook: auto-confirm VERIFY; only with `--camera dir:` (an error with `webcam`) |

VERIFY: in the default mode the code is logged and auto-confirmed; with `--verify` or `--encrypt` and a real webcam the console asks `[y/N]`. `dir:` and `--yes` confirm automatically. `--camera dir:` works only with the camera back-channel: `--backchannel audio` or `--no-camera` with it is rejected. When the audio back-channel is used, the sender ends with `transfer complete (unconfirmed ...)`, since it only heard a 16-bit token.

The grid, module size and tiles are only proposals; the receiver may negotiate a profile both sides support.

## receive

```bash
lightferry --receive --path ./incoming
```

Needs a camera. The item is written to `<path>/<name>`. Existing items are not replaced unless you pass `--overwrite`. Other flags as for `send` (`--camera`, `--camera-index`, `--backchannel camera|audio`, `--timeout`, `--retries`, `--fullscreen`, `--yes`). Use `--backchannel audio` when the sender has no camera: the receiver then plays note tokens from its speaker, and records accepted sender nonces in `<path>/.lightferry-seen` to refuse replays (it aborts with `DEST_NOT_WRITABLE` if it cannot write that file).

## Tips for a reliable scan

- Aim the receiver's camera so the code fills most of the view; keep the camera in focus.
- Run the sender fullscreen at full screen brightness.
- `LIGHTFERRY_DEBUG=1` prints camera diagnostics and saves undecodable frames.
- On Windows the camera may need a second try or a moment to lock on.

## selftest

```bash
lightferry selftest
```

Runs a complete sender/receiver transfer in-process on a virtual clock with a random key, then compares files byte for byte. Prints `selftest OK` or `FAILED`. Exit code 0 on success.

## bench

```bash
lightferry bench [--quick] [--out out/bench.csv] [--trials N]
```

Simulates a degraded camera capture (blur, perspective, lighting, noise, JPEG) across resolutions and code profiles, writes a CSV and recommends the densest profile with at least 95% tile success. It is a simulation, not a measurement of your hardware.

## mobile-serve

```bash
lightferry mobile-serve --path PATH [--port 8443] [--host LAN_IP] \
    [--backchannel audio|direct] [--remote-sender] [--out DIR] [--key-file FILE]
```

Needs the `mobile` extra. Binds this machine's LAN IP unless `--host` is given. `python -m lightferry.mobile ...` is the same as `lightferry mobile-serve ...`. It is keyless by default; `--key-file` there gives MAC authentication only (a test tool, no encryption). Set `LIGHTFERRY_DEBUG=1` for per-frame debug output (nothing is dumped to disk). See [Phone test client](phone-test-client.md).

## Exit codes

`0` success, `1` transfer aborted, `2` usage or input error, `130` interrupted.

## Planned

`lightferry cmd` and `lightferry serve` (a signed command channel) are not implemented. The wire format reserves CMD/RESULT packet types only.

## Keys from a passphrase (with `--encrypt`)

A passphrase is another way to provide the `--encrypt` key. If you do not want to carry a key file between machines, both sides can derive the same 32-byte key from a
shared passphrase (at least 12 characters) using scrypt with a fixed salt. Type the same phrase on both
machines yourself; never send it through chat or e-mail.

```bash
# Linux/macOS
export LIGHTFERRY_PASSPHRASE='your shared phrase'
lightferry --send --path ./folder --encrypt
```

```powershell
# Windows PowerShell
$env:LIGHTFERRY_PASSPHRASE = 'your shared phrase'
lightferry --receive --path .\incoming --encrypt
```

- `--passphrase PHRASE` (or `--passphrase @FILE`) does the same, but a command-line value is visible in the
  process list, so prefer the environment variable.
- `--key`, `--key-file` and `LIGHTFERRY_KEY` take precedence; `--passphrase` cannot be combined with
  `--key` or `--key-file`.
- A passphrase is weaker than a random key file: anyone who records the codes can try guesses offline. Use a
  long, random phrase.
