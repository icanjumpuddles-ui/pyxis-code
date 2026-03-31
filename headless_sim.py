import time
import math
import random
import requests
import json
import warnings
import os
import urllib3
# Suppress localhost SSL warnings
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

# ======================================================================
# PYXIS HEADLESS VESSEL SIMULATOR
# ======================================================================
# Simulates physical vessel sensors (Engine RPM, Fuel, Wind, Sonar Mesh)
# and continuously POSTs them to the Pyxis Proxy `/telemetry` endpoint.
# Useful for testing Watch UI elements offline without the full Pygame.
# ======================================================================

PROXY_URL = "https://localhost:443/telemetry"

def generate_sonar_grid(tick):
    """Generates a 10x10 sonar depth mesh with a rolling wave effect."""
    grid = []
    for r in range(10):
        row = []
        for c in range(10):
            # Base depth 40m, wave variance +/- 10m based on time and position
            depth = 40.0 + (math.sin(tick * 0.1 + r) * 5.0) + (math.cos(tick * 0.1 + c) * 5.0)
            row.append(round(depth, 1))
        grid.append(row)
    return grid

def run_simulation():
    print(f"Starting Headless Pyxis Simulator... Press Ctrl+C to Stop.")
    print(f"Targeting: {PROXY_URL}")
    
    tick = 0
    base_lat = 25.1527 # Approx UAE/Dubai coast
    base_lon = 55.3896
    
    # Vessel States
    rpm = 0
    fuel = 100.0
    bat_v = 13.5
    wind = 15.0
    heading = 0.0
    
    # Set initial base states
    override_path = "/home/icanjumpuddles/manta-comms/sim_override.json"
    if not os.path.exists(override_path):
        override_path = os.path.join(os.path.dirname(__file__), "sim_override.json")

    while True:
        tick += 1
        
        # Load dashboard overrides if the file exists
        ov = {}
        if os.path.exists(override_path):
            try:
                with open(override_path, "r") as f: ov = json.load(f)
            except: pass
            
        rpm = ov.get("rpm", 1200)
        fuel = ov.get("fuel_pct", 95)
        wind = ov.get("wind_speed", 15)
        base_lat = ov.get("base_lat", -38.0)
        base_lon = ov.get("base_lon", 145.24599)
        bilge_status = ov.get("bilge_status", "OK")
        gen_status = ov.get("gen_status", "ON")
        auto_active = ov.get("autopilot_active", True)
        
        # Fluctuate heading
        heading = (heading + random.uniform(-1.0, 1.0)) % 360.0
        
        payload = {
            "source": "headless_sim",
            "BOAT_LAT": base_lat,
            "BOAT_LON": base_lon,
            "heading": round(heading, 1),
            "SOG": 12.5,
            
            # --- NAV & MOTION (N2K) ---
            "sog_kts": ov.get("sog_kts", 12.5), 
            "cog_deg": ov.get("cog_deg", round(heading, 1)), 
            "hdg_mag_deg": ov.get("hdg_mag_deg", round(heading, 1)), 
            "rot_deg_min": ov.get("rot_deg_min", 0.5),
            "pitch_deg": ov.get("pitch_deg", 1.2), 
            "roll_deg": ov.get("roll_deg", 5.5), 
            "heave_m": ov.get("heave_m", 0.8),
            
            # --- WIND & WEATHER (N2K) ---
            "aws_kts": ov.get("aws_kts", wind), 
            "awa_deg": ov.get("awa_deg", 45.0),
            "tws_kts": ov.get("tws_kts", wind * 0.8), 
            "twd_deg": ov.get("twd_deg", 180.0),
            "out_temp_c": ov.get("out_temp_c", 22.5), 
            "pressure_hpa": ov.get("pressure_hpa", 1015.2),
            "pressure_trend": ov.get("pressure_trend", "STEADY"), 
            "humidity_pct": ov.get("humidity_pct", 65.0),
            
            # --- WATER & DEPTH (N2K) ---
            "depth_m": ov.get("depth_m", 45.5), 
            "stw_kts": ov.get("stw_kts", 12.0),
            "sea_temp_c": ov.get("sea_temp_c", 19.5), 
            "sonar_range_m": ov.get("sonar_range_m", 150.0),
            
            # --- AUTOPILOT (N2K) ---
            "rudder_deg": ov.get("rudder_deg", -2.5), 
            "ap_state": ov.get("ap_state", "AUTO" if auto_active else "STANDBY"),
            "xte_m": ov.get("xte_m", 12.5),
            
            # --- ENGINE (N2K) ---
            "rpm": ov.get("rpm", rpm), 
            "oil_press_psi": ov.get("oil_press_psi", 45.0),
            "coolant_temp_c": ov.get("coolant_temp_c", 85.0), 
            "alt_v": ov.get("alt_v", 14.1),
            "egt_c": ov.get("egt_c", 350.0), 
            "engine_hr": ov.get("engine_hr", 1245.5),
            
            # --- POWER GEN (Victron SignalK) ---
            "solar_w": ov.get("solar_w", 450.0), 
            "wind_gen_w": ov.get("wind_gen_w", 120.0),
            "hydro_gen_w": ov.get("hydro_gen_w", 0.0), 
            "shore_power": ov.get("shore_power", False),
            
            # --- ENERGY STORAGE (Victron) ---
            "house_v": ov.get("house_v", bat_v), 
            "house_amps": ov.get("house_amps", -15.5),
            "house_soc": ov.get("house_soc", 95.0), 
            "start_v": ov.get("start_v", 12.8),
            "inverter_w": ov.get("inverter_w", 250.0),
            
            # --- FLUIDS (Arduino) ---
            "fuel_stbd_pct": ov.get("fuel_stbd_pct", fuel), 
            "fuel_port_pct": ov.get("fuel_port_pct", fuel),
            "fresh_water_pct": ov.get("fresh_water_pct", 70.0), 
            "watermaker_lph": ov.get("watermaker_lph", 0.0),
            "watermaker_ppm": ov.get("watermaker_ppm", 250), 
            "blackwater_pct": ov.get("blackwater_pct", 15.0),
            "greywater_pct": ov.get("greywater_pct", 20.0),
            
            # --- PLUMBING (Arduino) ---
            "bilge_keel_primary": ov.get("bilge_keel_primary", bilge_status),
            "bilge_high_water": ov.get("bilge_high_water", bilge_status),
            "bilge_er_alarm": ov.get("bilge_er_alarm", "OK"),
            "bilge_pump_hr": ov.get("bilge_pump_hr", 12.5),
            "fw_pump_psi": ov.get("fw_pump_psi", 40.0),
            
            # --- INTERNAL ENV (Arduino) ---
            "cabin_temp_c": ov.get("cabin_temp_c", 24.5), 
            "saloon_temp_c": ov.get("saloon_temp_c", 25.0),
            "er_temp_c": ov.get("er_temp_c", 40.0), 
            "fridge_temp_c": ov.get("fridge_temp_c", 4.0),
            "freezer_temp_c": ov.get("freezer_temp_c", -18.0), 
            "co_smoke_alarm": ov.get("co_smoke_alarm", "OK"),
            
            # --- SECURITY (Arduino) ---
            "anchor_rode_m": ov.get("anchor_rode_m", 0.0), 
            "hatch_status": ov.get("hatch_status", "SECURED"),
            "dish_status": ov.get("dish_status", "ONLINE_NOMINAL"),


            # Original Backwards Compatibility
            "fuel_pct": int(fuel),
            "bat_v": round(bat_v, 1),
            "bilge_status": bilge_status,
            "gen_status": gen_status,
            "autopilot_active": auto_active,
            "wind_speed": round(wind, 1),
            "sonar_grid": generate_sonar_grid(tick),
            "sled_depth": 35.5,
            "last_sim_update": time.time()
        }
        
        # --- RPV / USV DRONE SWARM SIMULATION ---
        in_drones = ov.get("drones", [])
        sim_drones = []
        for d in in_drones:
            off_nm = d.get("offset_nm", 0)
            rel_b = d.get("relative_bearing", 0)
            true_b = (heading + rel_b) % 360.0
            
            # Simple Haversine reverse for testing:
            d_lat = base_lat + (off_nm / 60.0) * math.cos(math.radians(true_b))
            d_lon = base_lon + ((off_nm / 60.0) / math.cos(math.radians(base_lat))) * math.sin(math.radians(true_b))
            
            sim_drones.append({
                "id": d.get("id", "DRONE"),
                "type": d.get("type", "USV"),
                "lat": round(d_lat, 6),
                "lon": round(d_lon, 6),
                "bat_v": d.get("bat_v", 24.5),
                "depth_m": d.get("depth_m", 0.0),
                "speed_kts": d.get("sog_kts", 0.0),
                "heading": true_b,
                "status": "ONLINE_ACTIVE"
            })
            
        payload["active_drones"] = sim_drones
        
        try:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            r = requests.post(PROXY_URL, json=payload, timeout=2.0, verify=False)
            print(f"[Tick {tick}] Dashboard Synced. HTTP {r.status_code}. RPM: {rpm}, Bilge: {bilge_status}")
        except Exception as e:
            print(f"[Tick {tick}] Proxy Offline: {e}")
            
        time.sleep(1.0) # 1Hz Tick

if __name__ == "__main__":
    run_simulation()
