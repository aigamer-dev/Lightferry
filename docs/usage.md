# Usage

```
lightferry {send,receive} --path PATH [options]
lightferry {keygen,selftest,bench,mobile-serve} [...]
```

The older spellings `--send` and `--receive` still work.

## Keys

Provide a key with one of:

- `--key-file FILE`: 32 raw bytes, or hex text (what `keygen` writes is raw bytes)
- `LIGHTFERRY_KEY` environment variable: hex, or `@path` to a key file (`.env.example` uses `LIGHTFERRY_KEY=@lightferry.key`)
- `--key HEX`: works but leaks into the process list; a warning is printed

Keys shorter than 16 bytes are rejected.

## keygen

```bash
lightferry keygen [--out lightferry.key]
```

Writes 32 random bytes with mode 600. Refuses to overwrite an existing file.

## send

```bash
lightferry send --path ./folder-or-file --key-file lightferry.key
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
| `--fullscreen` | start fullscreen |
| `--yes` | test hook: auto-confirm VERIFY; only with `--camera dir:` (an error with `webcam`) |

VERIFY: with a real webcam the console prints the code and asks `[y/N]`; `dir:` and `--yes` confirm automatically. `--camera dir:` works only with the camera back-channel: `--backchannel audio` or `--no-camera` with it is rejected. When the audio back-channel is used, the sender ends with `transfer complete (unconfirmed ...)`, since it only heard a 16-bit token.

The grid, module size and tiles are only proposals; the receiver may negotiate a profile both sides support.

## receive

```bash
lightferry receive --path ./incoming --key-file lightferry.key
```

Needs a camera. The item is written to `<path>/<name>`. Existing items are not replaced unless you pass `--overwrite`. Other flags as for `send` (`--camera`, `--camera-index`, `--backchannel camera|audio`, `--timeout`, `--retries`, `--fullscreen`, `--yes`). Use `--backchannel audio` when the sender has no camera: the receiver then plays note tokens from its speaker, and records accepted sender nonces in `<path>/.lightferry-seen` to refuse replays (it aborts with `DEST_NOT_WRITABLE` if it cannot write that file).

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

Needs the `mobile` extra. Binds this machine's LAN IP unless `--host` is given. `python -m lightferry.mobile ...` is the same as `lightferry mobile-serve ...`. Set `LIGHTFERRY_DEBUG=1` for per-frame debug output (nothing is dumped to disk). See [Phone test client](phone-test-client.md).

## Exit codes

`0` success, `1` transfer aborted, `2` usage or input error, `130` interrupted.

## Planned

`lightferry cmd` and `lightferry serve` (a signed command channel) are not implemented. The wire format reserves CMD/RESULT packet types only.
