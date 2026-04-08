#!/bin/bash
# test_local.sh
# Script to run the Pyxis system locally for testing

echo "Starting Pyxis locally..."

# Set environment variables for local testing
export PYXIS_LOCAL="1"
export PROXY_URL="http://localhost:5000/telemetry"
export PYXIS_PROXY_URL="http://localhost:5000"
export CHECK_URL="http://localhost:5000/status_api"

# Start the proxy server in the background
echo "Starting Proxy Server (proxy_v4.1.0_RADAR.py)..."
python3 proxy_v4.1.0_RADAR.py > proxy_local.log 2>&1 &
PROXY_PID=$!

# Wait a few seconds for the proxy to initialize
echo "Waiting for Proxy Server to initialize..."
sleep 3

# Start the headless simulator in the background
echo "Starting Headless Simulator (headless_sim.py)..."
python3 headless_sim.py > headless_local.log 2>&1 &
SIM_PID=$!

# Start the watch simulator in the foreground
echo "Starting Watch Simulator (watch_simulator.py)..."
python3 watch_simulator.py

# Cleanup when the watch simulator is closed
echo "Cleaning up background processes..."
kill $PROXY_PID 2>/dev/null
kill $SIM_PID 2>/dev/null

echo "Local testing finished."
