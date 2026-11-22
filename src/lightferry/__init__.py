"""Lightferry: visual sneakernet between air-gapped machines."""
import os

# Every numpy call here is small (a 0.25 s audio block, one frame): a BLAS thread pool only adds idle worker
# threads that spin between calls, which pinned >100% CPU on the audio listener and froze the display window.
# Must run before numpy is imported; a value the user set in the environment wins.
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")
