import os
import time
import requests
import psutil
import subprocess
import logging
from datetime import datetime

# Configuration
SERVICE_NAME = "manta-proxy.service"
CHECK_URL = os.environ.get("CHECK_URL", "https://localhost:443/status_api")
MAX_RAM_PERCENT = 85.0
CHECK_INTERVAL = 30 # Seconds
BOOT_GRACE_PERIOD = 90 # Seconds to allow Kokoro/Ruby to wake up
LOG_FILE = "/home/icanjumpuddles/manta-comms/health_check.log"

# Setup Logging
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def log(msg):
    print(f"[{datetime.now()}] {msg}")
    logging.info(msg)

def restart_service():
    log(f"CRITICAL: Restarting {SERVICE_NAME}...")
    try:
        subprocess.run(["sudo", "systemctl", "restart", SERVICE_NAME], check=True)
        log("Restart successful.")
    except subprocess.CalledProcessError as e:
        log(f"ERROR: Failed to restart service: {e}")

def check_health():
    # 1. Check if service is actually running via systemd
    try:
        status = subprocess.check_output(["systemctl", "is-active", SERVICE_NAME]).decode().strip()
        if status != "active":
            log(f"WARNING: Service status is '{status}'. Triggering restart.")
            return False
    except subprocess.CalledProcessError:
        log("WARNING: Could not determine service status. Triggering restart.")
        return False

    # 2. Check API Endpoint
    try:
        # Use verify=False because localhost might not match the SSL cert domain
        res = requests.get(CHECK_URL, timeout=15, verify=False)
        if res.status_code != 200:
            log(f"WARNING: Health check URL returned {res.status_code}. Possible deadlock.")
            return False
    except Exception as e:
        log(f"WARNING: Could not reach status API: {e}")
        return False

    # 3. Check Memory Usage (Kokoro Deadlock Prevention)
    ram_usage = psutil.virtual_memory().percent
    if ram_usage > MAX_RAM_PERCENT:
        log(f"CRITICAL: System RAM at {ram_usage}%. Exceeds threshold of {MAX_RAM_PERCENT}%. Stopping potential deadlock.")
        return False

    return True

def get_service_uptime_seconds():
    """Returns how many seconds the service has been running, using multiple fallback methods."""
    try:
        # Method 1: Systemd Monotonic (High Precision)
        cmd = ["systemctl", "show", SERVICE_NAME, "--property=ActiveEnterTimestampMonotonic", "--value"]
        output = subprocess.check_output(cmd).decode().strip()
        log(f"DEBUG: systemctl ActiveEnterTimestampMonotonic='{output}'")
        
        if output and output != "0":
            with open("/proc/uptime", "r") as f:
                sys_uptime = float(f.readline().split()[0])
            s_uptime = sys_uptime - (int(output) / 1000000.0)
            log(f"DEBUG: Calculated systemd uptime: {s_uptime}s (Sys:{sys_uptime}s)")
            return max(0, s_uptime)
            
        # Method 2: Process Creation Time (Fallback if systemd status is lagging/denied)
        for proc in psutil.process_iter(['pid', 'cmdline', 'create_time']):
            cmdline = " ".join(proc.info.get('cmdline') or [])
            if 'proxy.py' in cmdline and 'python' in cmdline.lower():
                p_uptime = time.time() - proc.info['create_time']
                log(f"DEBUG: Found proxy.py process (PID:{proc.info['pid']}). Proc Uptime: {p_uptime}s")
                return p_uptime
                
    except Exception as e:
        log(f"DEBUG: Uptime error: {e}")
    return 0

if __name__ == "__main__":
    log("--- CRON HEALTH CHECK START ---")
    
    # 1. Check Uptime - High grace period for Kokoro loading
    uptime = get_service_uptime_seconds()
    log(f"FINAL DETECTED UPTIME: {int(uptime)}s")
    
    if 0 < uptime < 300: # 5 minutes grace
        log(f"Service state: INITIALIZING. Skipping check to prevent assassination.")
        exit(0)
    elif uptime == 0:
        log("DEBUG: Uptime is 0. This usually means the service is stopped or uptime check failed. Proceeding with caution.")

    # 2. Run Health Check
    log(f"Starting API check on {CHECK_URL}...")
    if not check_health():
        log("HEALTH CHECK FAILED. Triggering systemctl restart.")
        restart_service()
    else:
        log("HEALTH CHECK PASSED. System nominal.")
    log("--- CRON HEALTH CHECK END ---")
