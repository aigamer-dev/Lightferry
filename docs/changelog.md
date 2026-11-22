# Changelog

## 0.0.1a1

First alpha.

- Dense matrix code with asymmetric finders, tiler, HMAC-sealed packets, replay window.
- Stop-and-wait link with tile bitmaps, HELLO/SYNC/VERIFY handshake.
- Deterministic archive with safe extraction.
- `send`, `receive`, `keygen`, `selftest`, `bench`, `mobile-serve` commands.
- Camera and audio back-channels; phone web-app test client.
- VERIFY is confirmed by the operator on real hardware; audio-mode senders report "unconfirmed"; audio receivers refuse replayed sender nonces.
- Not built yet: signed command channel (`cmd`, `serve`).

## 0.0.1a2

- Open mode (`--open` / `LIGHTFERRY_OPEN=1`): no key needed for local trials; packets are not authenticated and VERIFY is auto-confirmed unless `--verify`. (In 0.0.1a3 this is the default, and `--open` is only a hidden alias.)
- Passphrase keys (`--passphrase` / `LIGHTFERRY_PASSPHRASE`, scrypt).
- Receiver: live camera preview and capture diagnostics; more tolerant thresholds for uneven lighting.

## 0.0.1a3

- Keyless default: `--send` / `--receive` need no key and no flag. The handshake, session nonces, CRC, replay window and sha256/size checks pair the two sides; VERIFY is logged and auto-confirmed (add `--verify` to be asked y/N). The `--open` mode of 0.0.1a2 is now the default.
- Optional `--encrypt` with a key (`--key-file`, `--key`, `LIGHTFERRY_KEY`, or a passphrase): real payload encryption and HMAC-SHA256 authentication of every session packet, standard library only.
- Camera preview and capture diagnostics (`LIGHTFERRY_DEBUG=1`).
- Lighting-tolerant decoding for soft, unevenly lit webcam frames.
- Passphrase keys (`--passphrase` / `LIGHTFERRY_PASSPHRASE`, scrypt), used with `--encrypt`.
