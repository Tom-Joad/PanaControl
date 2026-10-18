#!/usr/bin/env bash
# Smoke test of the image, run as in docker-compose.yml. No camera needed:
# the address is from 192.0.2.0/24 and never answers. Checks the settings,
# the switch to PUID:PGID, TZ, the banner, the web UI and the ordered
# shutdown.
# Usage: tests/smoke.sh <image>
set -euo pipefail

IMAGE=${1:?usage: smoke.sh <image>}
NAME=panacontrol-smoke-$$
CAMERA=192.0.2.10
# The caller's IDs, as on CI; abc must not be root.
PUID=$(id -u); PGID=$(id -g)
[[ $PUID != 0 ]] || { PUID=1000; PGID=1000; }
HARDENING=(--security-opt no-new-privileges)

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; docker logs "$NAME" >&2 2>&1 || true; exit 1; }

# Read the whole log first: with pipefail, "docker logs | grep -q" fails at
# random when grep stops early and docker logs gets SIGPIPE.
in_log() { local out; out=$(docker logs "$NAME" 2>&1); grep -qE -- "$1" <<<"$out"; }

wait_for_exit() {
    for _ in $(seq 1 30); do
        [[ $(docker inspect -f '{{.State.Status}}' "$NAME") == running ]] || return 0
        sleep 1
    done
    return 1
}

# HTTP request from inside the container; prints the status code.
request() { # path
    docker exec -i "$NAME" python3 - "$1" <<'PY'
import os, sys, urllib.error, urllib.request
url = f"http://127.0.0.1:{os.environ['LUMIX_WEB_PORT']}{sys.argv[1]}"
try:
    print(urllib.request.urlopen(url, timeout=5).status)
except urllib.error.HTTPError as exc:
    print(exc.code)
PY
}

# POST from inside the container; prints the status code.
post() { # path body [header:value ...]
    docker exec -i "$NAME" python3 - "$@" <<'PY'
import os, sys, urllib.error, urllib.request
url = f"http://127.0.0.1:{os.environ['LUMIX_WEB_PORT']}{sys.argv[1]}"
req = urllib.request.Request(url, data=sys.argv[2].encode(), method="POST")
for header in sys.argv[3:]:
    req.add_header(*header.split(":", 1))
try:
    print(urllib.request.urlopen(req, timeout=5).status)
except urllib.error.HTTPError as exc:
    print(exc.code)
PY
}

refused() { # message, docker run args...
    local message=$1; shift
    docker run -d --name "$NAME" "${HARDENING[@]}" "$@" "$IMAGE" >/dev/null
    wait_for_exit || fail "kept running ($message)"
    in_log "ERROR: $message" || fail "no ERROR line: $message"
    docker rm -f "$NAME" >/dev/null
}

echo "== without LUMIX_CAMERA_IP the container stops"
refused 'LUMIX_CAMERA_IP is required' -e PUID="$PUID" -e PGID="$PGID"

echo "== PUID 0 is refused"
refused 'refusing to run as root' -e PUID=0 -e LUMIX_CAMERA_IP="$CAMERA"

echo "== PUID must be numeric"
refused 'PUID and PGID must be numeric' -e PUID=abc -e LUMIX_CAMERA_IP="$CAMERA"

echo "== start"
docker run -d --name "$NAME" "${HARDENING[@]}" -e PUID="$PUID" -e PGID="$PGID" \
    -e TZ=Europe/Berlin -e LUMIX_CAMERA_IP="$CAMERA" "$IMAGE" >/dev/null
for _ in $(seq 1 30); do
    [[ $(docker inspect -f '{{.State.Health.Status}}' "$NAME") == healthy ]] && break
    sleep 2
done
[[ $(docker inspect -f '{{.State.Health.Status}}' "$NAME") == healthy ]] || fail "not healthy"
in_log 'BASED ON IMAGES FROM LINUXSERVER\.IO' || fail "the start banner is not ours"
if in_log 'Based on images from linuxserver\.io'; then fail "the base image's banner is still shown"; fi
in_log '^\{"ts":"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]{12}(Z|[+-][0-9:]{5})","level":"INFO","event":"starting","version":"[0-9]+\.[0-9]+\.[0-9]+"' \
    || fail "no JSON start line with time stamp and version"
# Every line the app writes is one JSON object (the banner and s6 lines
# before it are not the app's).
APP_LINES=$(docker logs "$NAME" 2>&1 | grep '^{' || true)
[[ $(head -n1 <<<"$APP_LINES") == *'"event":"starting"'* ]] || fail "the first line of the app is not 'starting'"
docker exec -i "$NAME" python3 -c 'import json, sys; [json.loads(l) for l in sys.stdin]' <<<"$APP_LINES" \
    || fail "a log line of the app is not JSON"

PS_OUT=$(docker exec "$NAME" ps -eo user,args)
USERS=$(awk '/python3 -m lumixremote.app/ && !/awk/ {print $1}' <<<"$PS_OUT")
[[ -n $USERS ]] || fail "app not running"
[[ $USERS == abc ]] || fail "app runs as '$USERS', not abc"
[[ $(docker exec "$NAME" id -u abc) == "$PUID" ]] || fail "abc does not have PUID"
if grep -q crond <<<"$PS_OUT"; then fail "cron is running, the app needs none"; fi

PIDS=$(docker exec "$NAME" pgrep -f "python3 -m lumixremote.app")
PID=${PIDS%%$'\n'*}
# Read as abc: root may not read another user's environ without ptrace.
APP_HOME=$(docker exec --user abc "$NAME" sh -c "tr '\\0' '\\n' < /proc/$PID/environ" | sed -n 's/^HOME=//p')
[[ $APP_HOME == /config ]] || fail "the app's HOME is '$APP_HOME', not /config"

[[ $(docker exec "$NAME" date +%Z) =~ ^CES?T$ ]] || fail "TZ is ignored"

echo "== web UI"
[[ $(request /) == 200 ]] || fail "/ does not answer 200"
[[ $(request /static/css/tokens.css) == 200 ]] || fail "the stylesheet is not served"
[[ $(request /static/css/fonts/Anton-Regular-latin.woff2) == 200 ]] || fail "the fonts are not served"
[[ $(request /api/status) == 200 ]] || fail "/api/status does not answer 200"

echo "== commands from other web sites are refused"
[[ $(post /api/rec/start '' 'Sec-Fetch-Site:cross-site') == 403 ]] || fail "cross-site POST not refused"
[[ $(post /api/rec/start '' 'Origin:https://evil.example') == 403 ]] || fail "foreign Origin not refused"
# Without a camera there is no session: 409, but not refused.
[[ $(post /api/af '' 'Sec-Fetch-Site:same-origin') == 409 ]] || fail "the UI's own POST refused"
[[ $(post /api/iso 'not json' 'Sec-Fetch-Site:same-origin') == 400 ]] || fail "a broken ISO body is not a 400"
[[ $(post /api/iso '[1]' 'Sec-Fetch-Site:same-origin') == 400 ]] || fail "an ISO list body is not a 400"

echo "== ordered shutdown"
# SIGTERM must reach the Python process: only then its lifespan handler runs
# and sends stopstream. Without it, s6 kills the app after
# S6_SERVICES_GRACETIME and the line below is missing.
docker stop -t 30 "$NAME" >/dev/null
CODE=$(docker inspect -f '{{.State.ExitCode}}' "$NAME")
[[ $CODE == 0 ]] || fail "exit code $CODE instead of 0"
in_log '"event":"shutting_down"' || fail "the app did not shut down in order"

echo "OK"
