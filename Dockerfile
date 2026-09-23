# syntax=docker/dockerfile:1

# ── NEPSE Analytics Platform ────────────────────────────────────────────────
# Django 6 + DRF app. Two native dependencies drive the extra build steps:
#   • TA-Lib   -> needs the TA-Lib C library present before the wheel compiles
#   • mysqlclient -> needs MySQL client dev headers + a C toolchain
# Python 3.13 to match the project's local interpreter.
FROM python:3.13-slim-bookworm

# Faster, quieter, unbuffered Python; no .pyc clutter in the image.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# TA-Lib C library version (installed from the project's prebuilt .deb release).
ARG TALIB_VERSION=0.6.4

# ── System dependencies ─────────────────────────────────────────────────────
# build-essential + pkg-config + libmysqlclient headers are needed to compile
# mysqlclient and the TA-Lib Python wrapper. We install the TA-Lib C library
# from the official prebuilt .deb so we don't have to compile it from source.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        default-libmysqlclient-dev \
        curl \
        ca-certificates \
    && curl -fsSL -o /tmp/ta-lib.deb \
        "https://github.com/TA-Lib/ta-lib/releases/download/v${TALIB_VERSION}/ta-lib_${TALIB_VERSION}_amd64.deb" \
    && apt-get install -y --no-install-recommends /tmp/ta-lib.deb \
    && rm -f /tmp/ta-lib.deb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python dependencies ─────────────────────────────────────────────────────
# Copy only requirements first so this layer caches across code-only changes.
# gunicorn is added here (not in requirements.txt) as the production WSGI server.
COPY requirements.txt ./
RUN pip install -r requirements.txt \
    && pip install gunicorn==23.0.0

# ── Application code ────────────────────────────────────────────────────────
COPY . .

# Normalise line endings and make the entrypoint executable (repo is authored on
# Windows, so the script may carry CRLF that would break the shebang).
RUN sed -i 's/\r$//' /app/docker/entrypoint.sh \
    && chmod +x /app/docker/entrypoint.sh

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/staticfiles \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# entrypoint waits for the DB, runs migrations + collectstatic, then exec's CMD.
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["gunicorn", "nepse_project.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "1", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
