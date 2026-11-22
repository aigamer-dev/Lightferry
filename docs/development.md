# Development

## Setup

```bash
git clone https://github.com/aigamer-dev/Lightferry && cd Lightferry
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,mobile,audio]"
pytest
```

Tests live in `tests/` and cover the acoustic modem, archive, codec, link, mobile server, protocol, tiler and vision.

## Layout

```
src/lightferry/
  cli.py          command line
  codec.py        one matrix code
  vision.py       frame layout, camera detection
  tiler.py        bytes <-> tiles
  archive.py      tar, compression, safe extraction
  protocol.py     packets, HMAC, keys, replay window
  link.py         ARQ, profiles
  handshake.py    Sender / Receiver state machines
  channels.py     loop and dir channels, frame rendering
  io_platform.py  webcam, screen, audio adapters
  acoustic.py     MFSK modem and note tokens
  bench.py        degradation benchmark
  mobile.py       phone test server (+ mobile_static/)
  viewer.py       sender-side viewer for the phone test
tests/
docs/
```

The engines are transport-agnostic (`step(now, inbound) -> outbound`), so they can be tested on a virtual clock without hardware.

## Docs site

```bash
pip install -r docs-requirements.txt
mkdocs serve
mkdocs build --strict
```

## Design notes

The original design document is [PLAN.md](https://github.com/aigamer-dev/Lightferry/blob/main/docs/PLAN.md) in the repository. It is a working document and may describe planned items (such as the `cmd`/`serve` command channel) that are not built yet.
