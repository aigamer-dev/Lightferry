# Getting started

## Install

Python 3.11.4 or newer is required.

```bash
pip install "lightferry @ git+https://github.com/aigamer-dev/Lightferry"
pip install "lightferry[mobile,audio] @ git+https://github.com/aigamer-dev/Lightferry"   # with extras
```

| Extra | Adds | Needed for |
|---|---|---|
| `mobile` | aiohttp, segno, cryptography | `mobile-serve` |
| `audio` | sounddevice | acoustic back-channel |
| `dev` | pytest, build | development |

From a checkout: `pip install -e ".[mobile,audio]"`.

For machines with no internet, download wheels on a connected machine (`pip download -d wheelhouse ...`) matching the target OS, architecture and Python, then `pip install --no-index --find-links wheelhouse lightferry`.

## First transfer

No key and no flag are needed.

1. Sanity check without hardware:

    ```bash
    lightferry selftest
    ```

2. On the receiver (needs a webcam):

    ```bash
    lightferry --receive --path ./incoming
    ```

3. On the sender, then point the receiver's camera at the sender's screen:

    ```bash
    lightferry --send --path ./my-folder
    ```

Both windows accept `f` (toggle fullscreen) and `q` (quit). Progress prints as `DATA n/total tiles`. In the default mode a one-line note on stderr says the data is not encrypted (anyone who can see the screen can read or inject), and the 6-digit VERIFY code is logged and confirmed automatically. Add `--verify` to be asked `does the other machine show NNNNNN? [y/N]` on both consoles; no answer ends the session with `VERIFY_TIMEOUT`. With `--encrypt` the prompt is always on. `--yes` is a test hook for `--camera dir:` only.

If the sender has no camera, use the audio back-channel; the sender then reports success as "unconfirmed" and the receiver's console has the authoritative result:

```bash
lightferry --send --path ./my-folder --backchannel audio --no-camera --fullscreen
lightferry --receive --path ./incoming --backchannel audio
```

## Optional: encrypt

To keep onlookers from reading or spoofing the transfer, create a key once, carry it to the other machine by hand (USB, paper, anything not on the network), and add `--encrypt --key-file` on both sides:

```bash
lightferry keygen --out lightferry.key
lightferry --receive --path ./incoming --encrypt --key-file lightferry.key
lightferry --send --path ./my-folder --encrypt --key-file lightferry.key
```

See [Security](security.md) for what each mode protects.

## Tips for a reliable scan

- Aim the receiver's camera so the code fills most of the view, and keep the camera in focus.
- Run the sender fullscreen at full screen brightness.
- `LIGHTFERRY_DEBUG=1` prints camera diagnostics and saves undecodable frames.
- On Windows the camera may need a second try or a moment to lock on.

If tiles keep failing, try fewer or larger codes: `--tiles 1`, `--module-px 8`, or a smaller `--grid 32`. `lightferry bench` suggests a profile for your camera resolution.
