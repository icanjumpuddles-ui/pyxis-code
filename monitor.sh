#!/bin/bash

# Manta Proxy Monitor Wrapper
# This script ensures the python monitor is running.

MONITOR_PATH="/home/icanjumpuddles/manta-comms/server_monitor.py"
LOG_PATH="/home/icanjumpuddles/manta-comms/health_check.log"

# Check if the monitor is already running
if ps aux | grep -v grep | grep "python3 $MONITOR_PATH" > /dev/null
then
    echo "[$(date)] Monitor is already running."
else
    echo "[$(date)] Starting Server Monitor..." >> $LOG_PATH
    nohup python3 $MONITOR_PATH >> $LOG_PATH 2>&1 &
fi
