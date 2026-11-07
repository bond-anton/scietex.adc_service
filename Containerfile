# Stage 1: Build Python dependencies
# python:3.14 matches the interpreter the published wheels are built for and the
# one glide_shared's slim step below keeps.
FROM python:3.14-slim-trixie AS builder

# Version of the published scietex.adc_service wheel to install. build_image.sh
# passes the version read from src/scietex/adc_service/version.py, so the image
# and the PyPI artifact always match.
ARG VERSION

# Backend extra to install alongside the core service: "spi" for spidev boards,
# "gpiozero" for the plain-GPIO backend. Both ship manylinux wheels; the HAL is
# pure Python, so no compiler is needed. If a future spidev release ever fails to
# resolve a wheel, re-adding modbus's gcc/python3-dev apt block is the fallback.
ARG EXTRAS="spi"

WORKDIR /app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install the released package from PyPI. The image is a distribution channel of
# the same tagged source that was published, so it does not build from the local
# checkout.
# Then slim the venv: pip/setuptools/wheel are build-time only, bytecode caches
# are regenerated on first import, and valkey-glide ships one _fast_response
# extension per supported interpreter (cp39..cp314, pypy) while only the running
# interpreter's is ever loaded.
RUN pip install --no-cache-dir -U pip && \
    pip install --no-cache-dir "scietex.adc_service[${EXTRAS}]==${VERSION}" && \
    pip uninstall -y pip setuptools wheel && \
    find /opt/venv -name '*.pyc' -delete && \
    find /opt/venv -name '__pycache__' -type d -prune -exec rm -rf {} + && \
    find /opt/venv/lib/python3.14/site-packages/glide_shared -name '*.so' \
        ! -name '*cpython-314*' ! -name 'libglide_ffi.so' -delete

# Stage 2: Runtime image
FROM python:3.14-slim-trixie

WORKDIR /app

# Copy virtual env from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Run as non-root user. No dialout-style group is needed: /dev/spidevX.Y must be
# passed with --device at run time; without it the worker still starts and
# reports the device unavailable.
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app && chmod -R 755 /app

# Configuration lives in a mounted volume so it survives container replacement.
# SCIETEX_CONFIG_DIR is honored by the framework's prepare_conf_dir; the service
# namespaces its files under <config-dir>/adc/.
# Neither this service nor the framework listens on a socket: the worker only
# dials Valkey. There is no EXPOSE, no published port and no NET_BIND_SERVICE.
ENV SCIETEX_CONFIG_DIR=/config \
    SCIETEX_SERVICE_NAME=AdcService \
    SCIETEX_LOGGING_LEVEL=INFO
RUN mkdir -p /config && chown appuser:appuser /config
VOLUME ["/config"]

USER appuser

# Run the app. There is no exposed socket: the worker dials Valkey outbound and
# never listens, so the image declares no EXPOSE and needs no NET_BIND_SERVICE.
# The console script is the only foreground process, so SIGTERM from the
# container runtime reaches it directly and shuts down gracefully.
CMD ["start-adc-service"]
