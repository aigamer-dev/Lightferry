# Changelog

## 0.0.1 alpha

First public alpha.

- Dense matrix code with asymmetric finders, tiler, replay window, stop-and-wait link with tile bitmaps.
- Handshake over the screen codes (HELLO/SYNC/START/DONE) with session nonces and a 6-digit VERIFY code; deterministic archive with hardened extraction.
- Keyless by default: `--send` / `--receive` need no key and no flag. VERIFY is logged and confirmed automatically (add `--verify` to be asked y/N).
- Optional `--encrypt` with a key (`--key-file`, `--key`, `LIGHTFERRY_KEY`, or a passphrase): payload encryption and HMAC-SHA256 authentication of every session packet, standard library only.
- Camera and audio (sound) back-channels; phone web-app test client.
- Receiver camera preview and capture diagnostics (`LIGHTFERRY_DEBUG=1`); lighting-tolerant decoding for soft, unevenly lit webcam frames.
- `send`, `receive`, `keygen`, `selftest`, `bench`, `mobile-serve` commands.
- Not built yet: signed command channel (`cmd`, `serve`).
