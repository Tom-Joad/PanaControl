# linuxserver.io's Alpine base: s6-overlay, PUID/PGID/UMASK/TZ, the abc user
# and docker mods, as in every linuxserver.io container.
# Pinned by digest (a multi-arch index); Dependabot proposes new digests.
FROM ghcr.io/linuxserver/baseimage-alpine:3.24@sha256:b661ece23fbc86a96a0c014c911c36b923f3c767a73255a5131eaae9f103167c

# image.source is what makes the GHCR package inherit the repository's
# visibility instead of staying private on its own.
LABEL org.opencontainers.image.source="https://github.com/Tom-Joad/PanaControl" \
      org.opencontainers.image.title="PanaControl" \
      org.opencontainers.image.description="Remote control and live view for LUMIX cameras over cam.cgi" \
      org.opencontainers.image.licenses="MIT"

# S6_BEHAVIOUR_IF_STAGE2_FAILS: a failing init step (no camera address,
# PUID 0) stops the container instead of leaving it half up.
# S6_SERVICES_GRACETIME: how long s6 waits for the app after SIGTERM. The
# default of 3 s is not always enough for stopstream, and without stopstream
# the camera keeps its remote session occupied until it is power-cycled.
# Together with S6_KILL_GRACETIME it stays below `docker stop -t 30`.
ENV S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_SERVICES_GRACETIME=20000 \
    S6_KILL_GRACETIME=5000 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    LUMIX_WEB_HOST=0.0.0.0 \
    LUMIX_WEB_PORT=8080 \
    LUMIX_STREAM_PORT=49199

# /lsiopy is linuxserver.io's place for a Python venv and already on PATH.
# pip is only needed to build the image and is removed afterwards: the
# libraries it bundles would show up in every vulnerability scan.
# apk upgrade picks up security fixes newer than the pinned base image.
# The app needs no cron, so the base image's cron service is dropped.
COPY requirements.txt /app/
RUN apk upgrade --no-cache \
 && apk add --no-cache python3 \
 && python3 -m venv /lsiopy \
 && /lsiopy/bin/pip install --no-cache-dir --requirement /app/requirements.txt \
 && /lsiopy/bin/pip uninstall --yes pip \
 && rm -f /etc/s6-overlay/s6-rc.d/user/contents.d/svc-cron

COPY lumixremote/ /app/lumixremote/
# s6 services (init-panacontrol-config, svc-panacontrol) and the start banner.
COPY root/ /

# COPY keeps the source files' modes. Files at 0600 in the working copy would
# be unreadable for abc, and the app would stop at import.
RUN chmod -R a+rX /app \
 && chmod +x /etc/s6-overlay/s6-rc.d/init-panacontrol-config/run \
      /etc/s6-overlay/s6-rc.d/svc-panacontrol/run \
 && printf 'PanaControl version: %s\n' \
      "$(python3 -c 'import lumixremote; print(lumixremote.__version__)')" > /build_version

EXPOSE 8080/tcp
EXPOSE 49199/udp

# No VOLUME: the app writes no files.

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('LUMIX_WEB_PORT','8080')+'/api/status', timeout=4).status==200 else 1)"

# The entrypoint stays the base image's /init (s6-overlay), which must run as
# PID 1: don't add `--init` or `--user` to `docker run`.
