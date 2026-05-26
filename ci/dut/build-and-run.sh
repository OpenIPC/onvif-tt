#!/usr/bin/env bash
# Build + run roleoroleo/onvif_simple_server as a localhost virtual ONVIF
# device for the onvif-tt integration job. No Docker.
#
# Deps needed beyond a stock Debian/Ubuntu CI runner:
#   libmbedtls-dev  (~5 MB)
#   zlib1g-dev      (<1 MB)
#   lighttpd        (~5 MB, with the CGI module)
# gcc / make / git are pre-installed on GitHub-hosted ubuntu-latest runners.
#
# Layout the script produces:
#   /tmp/dut/onvif_simple_server   ← compiled binary
#   /tmp/dut/www/onvif/*_service   ← CGI symlinks to the binary
#   /tmp/dut/www/onvif/*_files/    ← XML template directories from upstream
#   /tmp/dut/lighttpd.conf         ← generated CGI config
#   /tmp/dut/onvif.conf            ← copied from ci/dut/
#
# After the script exits the device is listening on http://127.0.0.1:8080/.

set -euo pipefail

CI_DUT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${ONVIF_DUT_WORK:-/tmp/dut}"
SRC="$WORK/src"   # clone target — NOT named onvif_simple_server because
                  # the built binary has that name and we'd collide.
REPO="${ONVIF_SRC_REPO:-https://github.com/roleoroleo/onvif_simple_server.git}"
REF="${ONVIF_SRC_REF:-master}"

step()  { printf '\033[36m[dut]\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------------------
# 1. apt deps (idempotent — skip if installed)
# ---------------------------------------------------------------------------
need_apt=()
for pkg in libmbedtls-dev libjson-c-dev zlib1g-dev lighttpd; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
        need_apt+=("$pkg")
    fi
done
if [[ ${#need_apt[@]} -gt 0 ]]; then
    step "apt install: ${need_apt[*]}"
    if [[ "$EUID" -eq 0 ]]; then SUDO=""; else SUDO="sudo"; fi
    $SUDO apt-get update -qq
    $SUDO apt-get install -y --no-install-recommends "${need_apt[@]}"
fi

# ---------------------------------------------------------------------------
# 2. clone + build
# ---------------------------------------------------------------------------
mkdir -p "$WORK"
if [[ ! -d "$SRC" ]]; then
    step "git clone $REPO @ $REF"
    git clone --depth 1 --branch "$REF" "$REPO" "$SRC"
fi

step "build onvif_simple_server (HAVE_MBEDTLS=1)"
make -C "$SRC" HAVE_MBEDTLS=1 -j"$(nproc)" >/dev/null

# ---------------------------------------------------------------------------
# 3. assemble the runtime dir under $WORK/www/onvif/
# ---------------------------------------------------------------------------
WWW="$WORK/www/onvif"
mkdir -p "$WWW" "$WORK/bin"
cp "$SRC/onvif_simple_server" "$WORK/bin/"
for s in device_service events_service media_service media2_service \
         ptz_service deviceio_service imaging_service; do
    ln -sfn "$WORK/bin/onvif_simple_server" "$WWW/$s"
done
for d in device_service_files events_service_files generic_files \
         media_service_files media2_service_files ptz_service_files \
         deviceio_service_files; do
    rm -rf "$WWW/$d"
    cp -R "$SRC/$d" "$WWW/$d"
done

cp "$CI_DUT_DIR/onvif.conf" "$WORK/onvif.conf"

# Event input files referenced by onvif.conf must exist (or the binary
# logs warnings when serving GetEventProperties). Touch them.
mkdir -p /tmp/onvif_notify_server
touch /tmp/onvif_notify_server/motion_alarm

# ---------------------------------------------------------------------------
# 4. minimal lighttpd config: CGI for any URL ending in `_service`
# ---------------------------------------------------------------------------
cat > "$WORK/lighttpd.conf" <<EOF
server.document-root = "$WORK/www/"
server.port          = 8080
server.username      = "$(id -un)"
server.groupname     = "$(id -gn)"
server.errorlog      = "$WORK/lighttpd.err"
server.pid-file      = "$WORK/lighttpd.pid"

server.modules = ( "mod_cgi", "mod_setenv" )

# Any file whose name ends in '_service' is a CGI executable. Upstream
# uses the same trick (`cgi.assign = ( "_service" => "" )`).
cgi.assign = ( "_service" => "" )

# Forward the OnvifSimpleServer config file path to the CGI binary
# via env. onvif_simple_server checks for this env var when invoked
# via CGI (no argv hand-off available).
setenv.add-environment = (
    "ONVIF_SIMPLE_SERVER_CONF" => "$WORK/onvif.conf",
)
EOF

# Also make the config reachable at the binary's compiled-in default
# path (`/etc/onvif_simple_server.conf`), in case the env-var approach
# isn't honoured by this build of onvif_simple_server. Idempotent.
if [[ "$EUID" -eq 0 ]]; then SUDO=""; else SUDO="sudo"; fi
$SUDO ln -sfn "$WORK/onvif.conf" /etc/onvif_simple_server.conf

# ---------------------------------------------------------------------------
# 5. start wsd_simple_server (WS-Discovery) + lighttpd (HTTP+CGI)
# ---------------------------------------------------------------------------
IFACE="${ONVIF_DUT_IFACE:-$(ip route get 1 2>/dev/null | awk '{print $5; exit}')}"
IFACE="${IFACE:-lo}"

# Discover our advertised xaddr — wsd_simple_server announces this URL.
HOST_IP="$(ip -4 addr show "$IFACE" 2>/dev/null \
           | awk '/inet / {print $2}' | cut -d/ -f1 | head -1)"
HOST_IP="${HOST_IP:-127.0.0.1}"
XADDR="http://${HOST_IP}:8080/onvif/device_service"

step "starting wsd_simple_server on $IFACE (xaddr=$XADDR)"
"$SRC/wsd_simple_server" -i "$IFACE" -x "$XADDR" -p "$WORK/wsd.pid" \
    >"$WORK/wsd.log" 2>&1 &

step "starting lighttpd on :8080 (docroot=$WORK/www)"
lighttpd -D -f "$WORK/lighttpd.conf" &
LIGHTTPD_PID=$!
echo "$LIGHTTPD_PID" > "$WORK/lighttpd.parent.pid"

# Wait for the HTTP listener to come up.
for i in $(seq 1 20); do
    if ss -tln 2>/dev/null | grep -q ':8080 '; then
        step "lighttpd listening; DUT is up at http://127.0.0.1:8080/"
        exit 0
    fi
    sleep 0.5
done
step "ERROR: lighttpd did not start listening on :8080 within 10s"
tail -50 "$WORK/lighttpd.err" 2>/dev/null || true
exit 1
