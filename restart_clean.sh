#!/bin/bash
# restart_clean.sh — Pyxis Clean Restart v1.4
# Kills all project processes, wipes cached state, starts fresh.
# Run from: /home/icanjumpuddles/manta-comms/
# v1.4: removed set -e; added disown; cmems_worker kill; -u flag on all python3

BASE="/home/icanjumpuddles/manta-comms"
HOME_DIR="/home/icanjumpuddles"
LOG="$BASE/restart_clean.log"

echo "============================================"
echo " PYXIS CLEAN RESTART - $(date)"
echo "============================================"
exec > >(tee -a "$LOG") 2>&1

# ── 1. KILL ALL PROJECT PROCESSES ───────────────────────────────────────────
echo "[1] Killing all project processes..."

# Stop proxy service (systemd)
sudo systemctl stop manta-proxy 2>/dev/null && echo "  ✓ manta-proxy service stopped" || echo "  ~ manta-proxy not a systemd service"

# Kill any direct python processes related to the project
for pattern in "marine_map_gen" "proxy_v4" "cmems_worker" "manta_sim_headless" "server_monitor" "headless_sim" "hs.py" "combined_mantasim"; do
    if pkill -9 -f "$pattern" 2>/dev/null; then
        echo "  ✓ Killed: $pattern"
    fi
done

sleep 2

# Verify nothing is left
REMAINING=$(ps aux | grep python3 | grep -v grep | grep -E "marine|proxy|headless|monitor" || true)
if [ -n "$REMAINING" ]; then
    echo "  ! WARNING: Still running:"
    echo "$REMAINING"
else
    echo "  ✓ All project processes stopped"
fi

# ── 2. WIPE VOLATILE CACHE ──────────────────────────────────────────────────
echo ""
echo "[2] Clearing volatile cache files..."

# Marine map images
for f in "$BASE"/meteo_map*.jpg; do
    [ -f "$f" ] && rm -f "$f" && echo "  ✓ Deleted: $(basename $f)"
done

# Meteo cache (cause of Melbourne lock — will be regenerated)
if [ -f "$BASE/meteo_cache.json" ]; then
    rm -f "$BASE/meteo_cache.json"
    echo "  ✓ Deleted: meteo_cache.json"
fi

# Marine map log + worker log — rotate instead of truncate (preserves crash evidence)
mv "$BASE/marine_map.log" "$BASE/marine_map.log.bak" 2>/dev/null || true
mv "$BASE/worker.log"     "$BASE/worker.log.bak"     2>/dev/null || true
echo "  ✓ Rotated: marine_map.log → .bak, worker.log → .bak"

echo "  ✓ Volatile cache cleared"

# ── 3. SHOW CURRENT TELEMETRY ───────────────────────────────────────────────
echo ""
echo "[3] Current sim_telemetry.json (boat position):"
if [ -f "$BASE/sim_telemetry.json" ]; then
    python3 -c "
import json
d = json.load(open('$BASE/sim_telemetry.json'))
print(f\"  BOAT_LAT: {d.get('BOAT_LAT', 'MISSING')}\")
print(f\"  BOAT_LON: {d.get('BOAT_LON', 'MISSING')}\")
print(f\"  lat:      {d.get('lat', 'MISSING')} (crew)\")
print(f\"  lon:      {d.get('lon', 'MISSING')} (crew)\")
"
else
    echo "  ! sim_telemetry.json NOT FOUND — sim not running"
fi

# ── 4. START THE HEADLESS SIM (if not via systemd) ──────────────────────────
echo ""
echo "[4] Starting headless sim..."
SIM_SCRIPT="$HOME_DIR/manta_sim_headless.py"
if [ -f "$SIM_SCRIPT" ]; then
    nohup python3 -u "$SIM_SCRIPT" >> "$BASE/headless_sim.log" 2>&1 &
    SIM_PID=$!
    disown $SIM_PID
    echo "  ✓ Sim started PID: $SIM_PID"
    sleep 3
    # Show what position the sim wrote
    echo "  Sim wrote to sim_telemetry.json:"
    python3 -c "
import json, os
p = '$BASE/sim_telemetry.json'
if os.path.exists(p):
    d = json.load(open(p))
    print(f\"    BOAT_LAT={d.get('BOAT_LAT','?')}  BOAT_LON={d.get('BOAT_LON','?')}\")
    print(f\"    lat={d.get('lat','?')}  lon={d.get('lon','?')} (crew)\")
else:
    print('    NOT YET WRITTEN')
" 2>/dev/null || true
else
    echo "  ~ No sim script found at $SIM_SCRIPT — skipping"
fi

# ── 5. START PROXY ──────────────────────────────────────────────────────────
echo ""
echo "[5] Starting proxy..."
PROXY="$HOME_DIR/proxy_v4.1.0_RADAR.py"
if [ ! -f "$PROXY" ]; then
    PROXY="$BASE/proxy_v4.1.0_RADAR.py"
fi

if sudo systemctl start manta-proxy 2>/dev/null; then
    echo "  ✓ manta-proxy service started"
else
    nohup python3 -u "$PROXY" >> "$BASE/proxy.log" 2>&1 &
    PROXY_PID=$!
    disown $PROXY_PID
    echo "  ✓ Proxy started directly PID: $PROXY_PID"
fi

sleep 2

# ── 6. START CMEMS WORKER ────────────────────────────────────────────────────
echo ""
echo "[6] Starting CMEMS Worker..."
WORKER_PID=$(pgrep -f "cmems_worker.py" || true)
if [ -n "$WORKER_PID" ]; then
    echo "  ~ CMEMS worker already running PID: $WORKER_PID"
else
    # Use systemd-run to escape the SSH cgroup so the process survives session end
    systemd-run --user --unit=cmems-worker --remain-after-exit \
        python3 -u "$BASE/cmems_worker.py" >> "$BASE/cmems_worker.log" 2>&1
    WORKER_PID=$(pgrep -f cmems_worker | head -1)
    echo "  ✓ CMEMS worker started via systemd-run PID: $WORKER_PID"
fi

# ── 7. START MARINE MAP GENERATOR v2 ─────────────────────────────────────────
echo ""
echo "[7] Starting marine_map_gen.py v2..."
nohup python3 -u "$BASE/marine_map_gen.py" >> "$BASE/marine_map.log" 2>&1 &
MAP_PID=$!
disown $MAP_PID
echo "  ✓ marine_map_gen started PID: $MAP_PID"

sleep 4
echo ""
echo "[7] First map gen output:"
tail -15 "$BASE/marine_map.log"

# ── 7. FINAL STATUS ─────────────────────────────────────────────────────────
echo ""
echo "============================================"
echo " RUNNING PROCESSES:"
ps aux | grep python3 | grep -v grep | awk '{print "  PID:"$2, $11, $12}' | head -10
echo ""
echo " MAP FILES:"
ls -lah "$BASE"/meteo_map*.jpg 2>/dev/null || echo "  (none yet — generating...)"
echo "============================================"
echo " Done. Watch marine_map.log for progress:"
echo " tail -f $BASE/marine_map.log"
echo "============================================"
