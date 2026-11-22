# Phone test client

A development aid: a phone acts as the receiver's camera, speaker and buttons while all protocol logic (decoding, keys, unpack) runs on the host. The phone never holds a key. It is a test tool, not part of the real path.

## Install and run

```bash
pip install -e ".[mobile,audio]"
lightferry mobile-serve --path ./my-folder
```

The command prints a Phone URL, a screen URL (`https://LAN_IP:PORT/screen?...`), a QR code and the SHA-256 of a freshly generated self-signed certificate. Both URLs carry a random token.

1. Open the Phone URL on the phone. Accept the certificate warning (compare the fingerprint if you want).
2. Press Start, grant camera and audio access.
3. Open the screen URL on the host and point the phone at it.
4. Confirm the VERIFY code with the on-screen buttons: on the screen page (sender) and on the phone (receiver).

## Options

| Flag | Meaning |
|---|---|
| `--path` | folder or file this machine sends; with `--remote-sender`, the destination directory |
| `--port` | default 8443 |
| `--host` | bind address, default this machine's LAN IP |
| `--backchannel` | `audio` (default): replies are note tokens played by the phone; `direct`: replies go straight to the sender, a test shortcut |
| `--remote-sender` | the sender is another machine running `lightferry send --no-camera --backchannel audio`; the phone scans that machine's screen |
| `--out DIR` | where received files go (default `out/received`) |
| `--key-file`, `--key` | optional: MAC authentication only (test tool, no encryption); keyless by default |

## Notes

- Use a trusted network. The server binds the LAN IP only, not `0.0.0.0`; `--host` overrides it. One WebSocket client at a time: a second gets HTTP 409.
- A new self-signed cert is made per run in a private temp dir and removed once loaded.
- `python -m lightferry.mobile ...` is the same as `lightferry mobile-serve ...`.
- `LIGHTFERRY_DEBUG=1` prints per-frame debug lines and camera diagnostics; nothing is dumped to disk otherwise.
- Browsers require HTTPS for camera access, hence the self-signed certificate.
- Needs the `mobile` extra (aiohttp, segno, cryptography).
