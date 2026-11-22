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

1. Create a key once and copy it to the other machine by hand (USB, paper, anything not on the network):

    ```bash
    lightferry keygen --out lightferry.key
    ```

2. Sanity check without hardware:

    ```bash
    lightferry selftest
    ```

3. On the receiver (needs a webcam):

    ```bash
    lightferry receive --path ./incoming --key-file lightferry.key
    ```

4. On the sender, then point the receiver's camera at the sender's screen:

    ```bash
    lightferry send --path ./my-folder --key-file lightferry.key
    ```

Both windows accept `f` (toggle fullscreen) and `q` (quit). Progress prints as `DATA n/total tiles`. At VERIFY both consoles show a 6-digit code and ask `does the other machine show NNNNNN? [y/N]`. Compare the codes by eye and answer `y`; no answer ends the session with `VERIFY_TIMEOUT`. With `--camera dir:` (two processes on one machine) or `--yes` the code is confirmed automatically; `--yes` with a real webcam is an error.

Instead of passing a key every time, set `LIGHTFERRY_KEY=@lightferry.key` (see `.env.example`). If the sender has no camera, use `--no-camera` on the sender and `--backchannel audio` on the receiver; the sender then reports success as "unconfirmed" and the receiver's console has the authoritative result.

If tiles keep failing, try fewer or larger codes: `--tiles 1`, `--module-px 8`, or a smaller `--grid 32`. `lightferry bench` suggests a profile for your camera resolution.
