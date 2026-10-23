#!/usr/bin/env bash
# Install the Linux runtime for this project: Python venv, systemd services
# (dashboard on 8004 + trading bot, both start at boot and restart on failure)
# and the firewall rule. Idempotent — safe to re-run.
#
# Dual-platform note: the Windows side (scripts/manage.ps1, Task Scheduler) is
# untouched and still works; this script is the Linux counterpart.
# Run:  bash scripts/install_linux.sh
#
# SINGLETON WARNING: the bot is one-process-per-universe. Never run this host's
# bot unit while the Windows loop (scripts/manage.ps1 start-bot) is active —
# two loops double-trade and double-email. The installer enables + starts the
# dashboard but leaves the bot unit DISABLED until you fail over:
#   1. Windows: pwsh scripts\manage.ps1 stop-bot
#   2. Linux:   sudo systemctl enable --now alpaca-swing-bot
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSTEMD_DIR="$ROOT/systemd"
DASHBOARD_UNIT="alpaca-swing-dashboard.service"
BOT_UNIT="alpaca-swing-bot.service"

log() { echo "install_linux: $*"; }
die() { echo "install_linux: ERROR: $*" >&2; exit 1; }

# --- 1. Python 3.11+ (system python3 here is 3.9) ---
PYBIN="$(command -v python3.12 || command -v python3.11 || true)"
[ -n "$PYBIN" ] || die "need python3.11+ (tried python3.12, python3.11)"
log "python: $PYBIN ($($PYBIN --version 2>&1))"

# --- 2. Project venv (full stack: scipy/sklearn take a few minutes) ---
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  log "creating .venv ..."
  "$PYBIN" -m venv "$ROOT/.venv" || die "venv creation failed"
fi
VENV_PY="$ROOT/.venv/bin/python"
if ! "$VENV_PY" -c "import fastapi, uvicorn" 2>/dev/null; then
  log "installing requirements.txt into .venv (slow first run) ..."
  "$VENV_PY" -m pip install --quiet -r "$ROOT/requirements.txt" || die "pip install failed"
fi
log "venv ok: $VENV_PY ($("$VENV_PY" --version 2>&1))"

mkdir -p "$ROOT/logs" "$ROOT/run"

# --- 3. systemd services ---
for unit in "$DASHBOARD_UNIT" "$BOT_UNIT"; do
  [ -f "$SYSTEMD_DIR/$unit" ] || die "missing $SYSTEMD_DIR/$unit"
done
command -v systemctl >/dev/null || die "systemctl not found"
sudo -n true 2>/dev/null || die "need passwordless sudo for systemctl/firewall-cmd"
for unit in "$DASHBOARD_UNIT" "$BOT_UNIT"; do
  # Units exec via bash (SELinux) with a relative .venv path, so only the
  # WorkingDirectory placeholder needs substituting.
  sed -e "s#__REPO_DIR__#$ROOT#g" \
      "$SYSTEMD_DIR/$unit" | sudo tee "/etc/systemd/system/$unit" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now "$DASHBOARD_UNIT" || die "dashboard enable failed"
# Bot stays DISABLED while the Windows loop owns trading (singleton rule above).
if sudo systemctl is-enabled --quiet "$BOT_UNIT" 2>/dev/null; then
  log "bot unit already enabled — leaving as-is (make sure Windows loop is stopped)."
else
  log "bot unit installed but NOT enabled (Windows loop still owns trading)."
  log "failover: pwsh scripts\\manage.ps1 stop-bot  # on Windows, then"
  log "          sudo systemctl enable --now $BOT_UNIT  # here"
fi
log "services installed."

# --- 4. Firewall: port 8004 on Tailscale only ---
# Tailscale traffic arrives on tailscale0 (zone `internal`), NOT the default
# zone. Deliberately NOT opened on `public`: the dashboard token gate is the
# only auth, so LAN exposure stays off unless you open it yourself.
if sudo firewall-cmd --state >/dev/null 2>&1; then
  TAIL_ZONE="$(sudo firewall-cmd --get-zone-of-interface=tailscale0 2>/dev/null || true)"
  [ -n "$TAIL_ZONE" ] || TAIL_ZONE="internal"
  if ! sudo firewall-cmd --zone="$TAIL_ZONE" --list-ports 2>/dev/null | grep -qw "8004/tcp"; then
    sudo firewall-cmd --zone="$TAIL_ZONE" --permanent --add-port=8004/tcp >/dev/null \
      && log "firewall: opened 8004/tcp in zone '$TAIL_ZONE'." \
      || log "firewall: could not open 8004/tcp in zone '$TAIL_ZONE' — check manually."
  else
    log "firewall: 8004/tcp already open in zone '$TAIL_ZONE'."
  fi
  if sudo firewall-cmd --zone=public --list-ports 2>/dev/null | grep -qw "8004/tcp"; then
    log "firewall: NOTE 8004/tcp is also open in zone 'public' (LAN can reach it)."
  fi
  sudo firewall-cmd --reload >/dev/null
else
  log "firewall: firewalld not running — nothing to open."
fi

# --- 5. Report ---
sleep 3
if "$VENV_PY" -c "import socket; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1',8004))" 2>/dev/null; then
  log "dashboard: listening on 8004."
else
  log "dashboard: NOT listening yet — check: sudo journalctl -u $DASHBOARD_UNIT"
fi
if ! grep -q "^DASHBOARD_TOKEN=..*" "$ROOT/.env" 2>/dev/null; then
  log "WARNING: DASHBOARD_TOKEN is not set in .env — the dashboard is open to"
  log "every device on the tailnet. Set it and use ?token=<token> in the URL."
fi
TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
[ -n "$TS_IP" ] && log "dashboard over Tailscale: http://$TS_IP:8004"
log "done."
