FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/aigamer-dev/Lightferry" \
      org.opencontainers.image.title="lightferry" \
      org.opencontainers.image.licenses="NOASSERTION"

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

# Headless OpenCV replaces the pinned opencv-python (no GUI libs in the image).
# Dependencies are installed first, then the package itself without deps so pip
# does not pull opencv-python. The audio extra is skipped: containers have no
# sound device, so PortAudio is not needed.
RUN pip install "numpy>=1.26" "opencv-python-headless>=4.8" \
        "aiohttp>=3.9" "segno>=1.6" "cryptography>=42" \
    && pip install --no-deps . \
    && rm -rf /build

RUN useradd --create-home --uid 1000 lightferry
USER lightferry
WORKDIR /home/lightferry

ENTRYPOINT ["lightferry"]
CMD ["--help"]
