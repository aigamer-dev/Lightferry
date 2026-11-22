# Lightferry

Move files between air-gapped machines with a screen and a webcam.

The sender shows dense matrix codes; the receiver scans them. Every packet is HMAC-signed with a pre-shared key, and a 6-digit VERIFY code lets you confirm the session. An acoustic back-channel lets a camera-less sender hear the receiver's acknowledgements.

!!! warning "Early alpha"
    Version `0.0.1a1`. The core logic is tested; real webcam, screen and audio paths are lightly tested.

- [Getting started](getting-started.md): install and first transfer
- [Usage](usage.md): every command and flag
- [How it works](how-it-works.md): codec, tiler, link, handshake, back-channel
- [Security](security.md): threat model and key handling
- [Phone test client](phone-test-client.md): use a phone as the receiver while developing

Source: <https://github.com/aigamer-dev/Lightferry>
