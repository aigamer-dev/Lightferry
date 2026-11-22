# Lightferry

Move files between air-gapped machines with a screen and a webcam.

The sender shows dense matrix codes; the receiver scans them. The default mode needs no key: an on-screen handshake pairs the two sides. Add `--encrypt` with a shared key for encrypted, authenticated transfers. An acoustic back-channel lets a camera-less sender hear the receiver's acknowledgements.

!!! warning "Early alpha"
    Version `0.0.1a3`. The core logic is tested; real webcam, screen and audio paths are lightly tested.

- [Getting started](getting-started.md): install and first transfer
- [Usage](usage.md): every command and flag
- [How it works](how-it-works.md): codec, tiler, link, handshake, back-channel
- [Security](security.md): default mode vs `--encrypt`, threat model
- [Phone test client](phone-test-client.md): use a phone as the receiver while developing

Source: <https://github.com/aigamer-dev/Lightferry>
