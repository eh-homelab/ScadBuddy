# A fake Bambu printer on the LAN for the hot-path stack (#3818 tier A, Task 12):
# MQTT over TLS on 8883 (its own mosquitto), implicit FTPS on 990, and a control port on
# 8081 that lists what it was sent. printer.py's docstring has the details and variables.
#
#   docker build -t scadbuddy-fake-printer:dev e2e-stack/fake-printer
#   docker run --rm -e FAKE_PRINTER_SERIAL=01P00FAKE000001 -e FAKE_PRINTER_ACCESS_CODE=12345678 \
#     -p 8883:8883 -p 990:990 -p 8081:8081 -p 50000-50009:50000-50009 scadbuddy-fake-printer:dev
#
# Reach it from a container on the same network, as the hot-path stack does. Through
# published ports on Docker Desktop (WSL2) the FTPS data connection never reached the
# client (measured 2026-10-18: STOR answered "125 Data connection already open" and the
# client's own connection timed out), so a client on the host needs Linux's Docker.
#
# The broker is Debian's mosquitto 2 inside this image, not the eclipse-mosquitto image:
# printer.py starts and stops it with its own certificate and password file, so the
# printer is one container, as a real one is one address.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

# mosquitto is whatever 2.x trixie carries at build time (2.0.21 on 2026-10-18): it is
# not pinned, as DL3008 is ignored repo-wide (.hadolint.yaml). The password hash
# printer.py writes ($7$, PBKDF2-SHA512) is the same across 2.x.
RUN apt-get update \
    && apt-get install -y --no-install-recommends mosquitto \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

RUN useradd --system --uid 10001 --home-dir /app printer \
    && mkdir -p /sdcard /tmp/fake-printer \
    && chown printer /sdcard /tmp/fake-printer

COPY printer.py report.json /app/

USER 10001
WORKDIR /app
EXPOSE 8883 990 8081 50000-50009
HEALTHCHECK --interval=2s --timeout=2s --retries=30 \
    CMD ["python", "/app/printer.py", "--health"]
ENTRYPOINT ["python", "/app/printer.py"]
