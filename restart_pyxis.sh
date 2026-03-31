#!/bin/bash
echo "Restarting Proxy as Root Service..."
sudo systemctl restart manta-proxy.service

echo "Detaching old Simulator..."
pkill -f "headless_sim.py"

echo "Spawning Headless Simulator Tracking..."
nohup python3 "headless_sim.py" > sim.log 2>&1 &

echo ""
echo "================ LIVE C3 TELEMETRY TRACKING ACTIVE ================"
echo "(Press Ctrl+C at any time to hide these logs. Pyxis will continue running securely in the background!)"
echo ""
journalctl -u manta-proxy.service -f &
tail -f sim.log
