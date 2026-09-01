#!/bin/sh
set -eu

export LANG="${LANG:-C.UTF-8}"
export LC_ALL="${LC_ALL:-C.UTF-8}"

desktop_state_dir=/var/lib/mcp-virtual-computer
desktop_state_file=$desktop_state_dir/desktop-enabled
network_state_file=$desktop_state_dir/network-enabled

if [ "$(id -u)" = "0" ]; then
  install -d -o computer -g computer "$desktop_state_dir"
  install -d -m 0700 -o computer -g computer /run/user/1000
  # Older image layers let `install -d .../.local/share` create the `.local`
  # parent as root. Repair the user install prefix before Xfce starts so tools
  # such as uv can always install into ~/.local/bin on persistent computers.
  install -d -m 0755 -o computer -g computer \
    /home/computer/.local \
    /home/computer/.local/bin \
    /home/computer/.local/share
  if [ ! -f "$desktop_state_file" ]; then
    case "${DESKTOP_ENVIRONMENT:-true}" in
      true|false) printf '%s\n' "$DESKTOP_ENVIRONMENT" > "$desktop_state_file" ;;
      *) echo "DESKTOP_ENVIRONMENT must be true or false" >&2; exit 2 ;;
    esac
    chown computer:computer "$desktop_state_file"
  fi
  if [ ! -f "$network_state_file" ]; then
    case "${NETWORK_ACCESS:-true}" in
      true|false) printf '%s\n' "${NETWORK_ACCESS:-true}" > "$network_state_file" ;;
      *) echo "NETWORK_ACCESS must be true or false" >&2; exit 2 ;;
    esac
    chown computer:computer "$network_state_file"
  fi
  if [ "$(cat "$network_state_file")" = "false" ]; then
    iptables -N MCP_NO_NETWORK_IN 2>/dev/null || true
    iptables -F MCP_NO_NETWORK_IN
    iptables -A MCP_NO_NETWORK_IN -i lo -j ACCEPT
    iptables -A MCP_NO_NETWORK_IN -p tcp --dport 6080 -j ACCEPT
    iptables -A MCP_NO_NETWORK_IN -p tcp -j REJECT --reject-with tcp-reset
    iptables -A MCP_NO_NETWORK_IN -j REJECT
    iptables -N MCP_NO_NETWORK_OUT 2>/dev/null || true
    iptables -F MCP_NO_NETWORK_OUT
    iptables -A MCP_NO_NETWORK_OUT -o lo -j ACCEPT
    # Preserve the host-only VNC/audio transport without allowing unrelated
    # established internet transfers to survive unplugging the LAN cable.
    iptables -A MCP_NO_NETWORK_OUT -p tcp --sport 6080 -j ACCEPT
    iptables -A MCP_NO_NETWORK_OUT -p tcp -j REJECT --reject-with tcp-reset
    iptables -A MCP_NO_NETWORK_OUT -j REJECT
    iptables -C INPUT -j MCP_NO_NETWORK_IN 2>/dev/null || iptables -I INPUT 1 -j MCP_NO_NETWORK_IN
    iptables -C OUTPUT -j MCP_NO_NETWORK_OUT 2>/dev/null || iptables -I OUTPUT 1 -j MCP_NO_NETWORK_OUT
    touch /run/mcp-network-disabled
  else
    rm -f /run/mcp-network-disabled
  fi
  exec runuser -u computer -- env \
    HOME=/home/computer \
    USER=computer \
    LOGNAME=computer \
    DISPLAY="${DISPLAY:-:99}" \
    LANG="$LANG" \
    LC_ALL="$LC_ALL" \
    NO_AT_BRIDGE=0 \
    GTK_MODULES=atk-bridge \
    XDG_CONFIG_HOME=/home/computer/.config \
    XDG_CACHE_HOME=/home/computer/.cache \
    XDG_DATA_HOME=/home/computer/.local/share \
    XDG_RUNTIME_DIR=/run/user/1000 \
    PULSE_SERVER=unix:/run/user/1000/pulse/native \
    NETWORK_ACCESS="$(cat "$network_state_file")" \
    DESKTOP_ENVIRONMENT="${DESKTOP_ENVIRONMENT:-true}" \
    /usr/local/bin/start-desktop
fi

export DISPLAY="${DISPLAY:-:99}"
export NO_AT_BRIDGE=0
export GTK_MODULES="${GTK_MODULES:-atk-bridge}"

pulse_pid=

start_audio() {
  if [ -n "$pulse_pid" ] && kill -0 "$pulse_pid" 2>/dev/null; then
    return
  fi
  rm -rf "$XDG_RUNTIME_DIR/pulse"
  pulseaudio \
    --daemonize=no \
    --exit-idle-time=-1 \
    --disallow-exit \
    --log-target=stderr \
    --log-level=warning \
    --load="module-null-sink sink_name=mcp_output rate=48000 channels=2 sink_properties=device.description=MCP_Virtual_Computer" &
  pulse_pid=$!
  count=0
  while ! pactl info >/dev/null 2>&1; do
    count=$((count + 1))
    if ! kill -0 "$pulse_pid" 2>/dev/null || [ "$count" -gt 100 ]; then
      echo "PulseAudio did not become ready" >&2
      return 1
    fi
    sleep 0.1
  done
  pactl set-default-sink mcp_output
  pactl set-default-source mcp_output.monitor
}

start_audio

Xvfb "$DISPLAY" -screen 0 1280x800x24 -ac -nolisten tcp &
xvfb_pid=$!

i=0
while ! xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 100 ]; then
    echo "Xvfb did not become ready" >&2
    exit 1
  fi
  sleep 0.1
done

x11vnc_pid=
wsproxy_pid=

start_x11vnc() {
  if [ -n "$x11vnc_pid" ] && kill -0 "$x11vnc_pid" 2>/dev/null; then
    return
  fi
  if [ -n "$x11vnc_pid" ]; then
    wait "$x11vnc_pid" 2>/dev/null || true
  fi
  # XDamage can crash x11vnc while Xfce tears down and recreates its
  # compositor. Polling is slightly less efficient but survives mode flips.
  x11vnc \
    -display "$DISPLAY" \
    -forever \
    -shared \
    -nopw \
    -localhost \
    -rfbport 5900 \
    -noxdamage \
    -quiet &
  x11vnc_pid=$!
}

start_wsproxy() {
  if [ -n "$wsproxy_pid" ] && kill -0 "$wsproxy_pid" 2>/dev/null; then
    return
  fi
  if [ -n "$wsproxy_pid" ]; then
    wait "$wsproxy_pid" 2>/dev/null || true
  fi
  python3 /usr/local/bin/wsproxy &
  wsproxy_pid=$!
}

start_x11vnc
start_wsproxy
desktop_pid=

start_xfce() {
  if [ -n "$desktop_pid" ] && kill -0 "$desktop_pid" 2>/dev/null; then
    return
  fi
  setsid dbus-run-session -- sh -lc '
    export DISPLAY=:99 LANG="${LANG:-C.UTF-8}" LC_ALL="${LC_ALL:-C.UTF-8}" NO_AT_BRIDGE=0 GTK_MODULES=atk-bridge
    python3 -c "from gi.repository import Gio; settings = Gio.Settings.new(\"org.gnome.desktop.interface\"); settings.set_boolean(\"toolkit-accessibility\", True); Gio.Settings.sync()"
    exec startxfce4
  ' &
  desktop_pid=$!
}

xfce_session_pids() {
  ps -eo pid=,pgid=,stat=,comm= | awk -v group="$desktop_pid" '
    $2 == group && $3 !~ /^Z/ && $4 == "xfce4-session" { print $1 }
  '
}

stop_xfce() {
  if [ -z "$desktop_pid" ] || ! kill -0 "$desktop_pid" 2>/dev/null; then
    desktop_pid=
    return
  fi
  # Let xfce4-session ask its children to exit before stopping the remaining
  # session group. This avoids orphaned/zombie desktop processes under Docker.
  session_pids=$(xfce_session_pids)
  if [ -n "$session_pids" ]; then
    kill -TERM $session_pids 2>/dev/null || true
  fi
  count=0
  while kill -0 "$desktop_pid" 2>/dev/null && [ "$count" -lt 50 ]; do
    count=$((count + 1))
    sleep 0.1
  done
  if kill -0 "$desktop_pid" 2>/dev/null; then
    python3 -c 'import os, signal, sys; os.killpg(int(sys.argv[1]), signal.SIGTERM)' "$desktop_pid" 2>/dev/null || true
    sleep 0.5
  fi
  if kill -0 "$desktop_pid" 2>/dev/null; then
    python3 -c 'import os, signal, sys; os.killpg(int(sys.argv[1]), signal.SIGKILL)' "$desktop_pid" 2>/dev/null || true
  fi
  wait "$desktop_pid" 2>/dev/null || true
  desktop_pid=
}

cleanup() {
  trap - TERM INT EXIT
  stop_xfce
  kill "$wsproxy_pid" "$x11vnc_pid" "$xvfb_pid" "$pulse_pid" 2>/dev/null || true
  wait "$wsproxy_pid" "$x11vnc_pid" "$xvfb_pid" "$pulse_pid" 2>/dev/null || true
}
trap cleanup TERM INT EXIT

while kill -0 "$xvfb_pid" 2>/dev/null; do
  if ! kill -0 "$pulse_pid" 2>/dev/null; then
    wait "$pulse_pid" 2>/dev/null || true
    pulse_pid=
    start_audio
  fi
  start_x11vnc
  start_wsproxy
  desired=$(cat "$desktop_state_file" 2>/dev/null || printf '%s' "${DESKTOP_ENVIRONMENT:-true}")
  case "$desired" in
    true) start_xfce ;;
    false) stop_xfce ;;
    *) echo "Ignoring invalid desktop state: $desired" >&2 ;;
  esac
  sleep 0.2
done

exit 1
