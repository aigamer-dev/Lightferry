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
