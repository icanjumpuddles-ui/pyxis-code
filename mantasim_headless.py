# Copyright (c) 2026 Benjamin Pullin. All rights reserved.
# Headless Simulator Environment for Reinforcement Learning
import numpy as np
import os, math, time, random, threading, requests
from datetime import datetime

# --- System Parameters & Config ---
SCALE = 22           
SHIP_DEPTH = 30.0  
MAX_WAVE_HEIGHT = 4.0    
MAX_SLED_DEPTH = 100.0

# --- Tactical Config ---
MAX_KNOTS = 15.0  
KNOT_TO_MS = 0.514444
BOAT_START = (-38.2, 144.9) # Port Phillip Bay Vic
CREW_START = (-38.2, 144.9)

# --- 100 Tactical Scenarios ---
SCENARIOS = [
    {"name": "Port Phillip Bay Vic (Home)", "lat": -38.2, "lon": 144.9, "threat": "LOW"},
    {"name": "Sydney Harbour (High Traffic)", "lat": -33.85, "lon": 151.27, "threat": "LOW"},
    {"name": "Arctic Ocean (North Pole)", "lat": 89.9, "lon": 0.0, "threat": "LOW"},
    {"name": "Bering Strait (Chokepoint)", "lat": 65.9, "lon": -169.0, "threat": "HIGH"},
    {"name": "Strait of Hormuz (Chokepoint)", "lat": 26.56, "lon": 56.25, "threat": "EXTREME"},
    {"name": "Bab el-Mandeb (Red Sea)", "lat": 12.58, "lon": 43.33, "threat": "EXTREME"},
    {"name": "Suez Canal (Mediterranean)", "lat": 31.25, "lon": 32.33, "threat": "HIGH"},
    {"name": "Strait of Malacca (Piracy Zone)", "lat": 3.0, "lon": 100.8, "threat": "HIGH"},
    {"name": "South China Sea (Spratly Islands)", "lat": 10.0, "lon": 114.0, "threat": "EXTREME"},
    {"name": "Taiwan Strait (Tension Zone)", "lat": 24.0, "lon": 119.0, "threat": "EXTREME"},
    {"name": "Gulf of Aden (Piracy Zone)", "lat": 12.0, "lon": 48.0, "threat": "HIGH"},
    {"name": "English Channel (High Traffic)", "lat": 50.1, "lon": -1.5, "threat": "MEDIUM"},
    {"name": "Panama Canal (Caribbean)", "lat": 9.2, "lon": -79.9, "threat": "LOW"},
    {"name": "Cape of Good Hope", "lat": -34.35, "lon": 18.47, "threat": "MEDIUM"},
    {"name": "Drake Passage (Cape Horn)", "lat": -58.0, "lon": -65.0, "threat": "LOW"},
    {"name": "Southern Ocean (Roaring Forties)", "lat": -45.0, "lon": 90.0, "threat": "LOW"},
    {"name": "Mid-Atlantic Ridge", "lat": 0.0, "lon": -30.0, "threat": "LOW"},
    {"name": "Mariana Trench", "lat": 11.35, "lon": 142.2, "threat": "LOW"},
    {"name": "Barents Sea (Russian Submarines)", "lat": 73.0, "lon": 40.0, "threat": "HIGH"},
    {"name": "Baltic Sea (Chokepoint)", "lat": 57.0, "lon": 20.0, "threat": "MEDIUM"},
    {"name": "Black Sea (Crimean Coast)", "lat": 44.0, "lon": 35.0, "threat": "EXTREME"},
    {"name": "Sea of Japan", "lat": 40.0, "lon": 135.0, "threat": "HIGH"},
    {"name": "Gibraltar Strait", "lat": 35.9, "lon": -5.3, "threat": "MEDIUM"},
    {"name": "Bosphorus Strait", "lat": 41.0, "lon": 29.0, "threat": "HIGH"},
    {"name": "Dardanelles", "lat": 40.2, "lon": 26.4, "threat": "HIGH"},
    {"name": "Kattegat (Denmark)", "lat": 56.9, "lon": 11.2, "threat": "LOW"},
    {"name": "Skagerrak", "lat": 57.8, "lon": 9.0, "threat": "LOW"},
    {"name": "Oresund", "lat": 55.7, "lon": 12.7, "threat": "LOW"},
    {"name": "Florida Strait", "lat": 23.8, "lon": -80.4, "threat": "LOW"},
    {"name": "Yucatan Channel", "lat": 21.8, "lon": -85.9, "threat": "LOW"},
    {"name": "Windward Passage", "lat": 19.9, "lon": -73.8, "threat": "LOW"},
    {"name": "Mona Passage", "lat": 18.5, "lon": -67.8, "threat": "LOW"},
    {"name": "Magellan Strait", "lat": -53.2, "lon": -70.9, "threat": "LOW"},
    {"name": "Cook Strait (NZ)", "lat": -41.2, "lon": 174.5, "threat": "LOW"},
    {"name": "Torres Strait (Aus/PNG)", "lat": -9.8, "lon": 142.5, "threat": "LOW"},
    {"name": "Makassar Strait", "lat": -1.0, "lon": 118.5, "threat": "MEDIUM"},
    {"name": "Lombok Strait", "lat": -8.7, "lon": 115.7, "threat": "MEDIUM"},
    {"name": "Sunda Strait", "lat": -5.9, "lon": 105.7, "threat": "MEDIUM"},
    {"name": "Tsugaru Strait", "lat": 41.5, "lon": 140.5, "threat": "MEDIUM"},
    {"name": "Soya Strait", "lat": 45.6, "lon": 142.0, "threat": "HIGH"},
    {"name": "Tartary Strait", "lat": 52.0, "lon": 141.5, "threat": "HIGH"},
    {"name": "Palk Strait", "lat": 9.9, "lon": 79.5, "threat": "LOW"},
    {"name": "Mozambique Channel", "lat": -15.0, "lon": 41.0, "threat": "MEDIUM"},
    {"name": "Davis Strait", "lat": 65.0, "lon": -57.0, "threat": "LOW"},
    {"name": "Denmark Strait", "lat": 67.0, "lon": -24.0, "threat": "LOW"},
    {"name": "Hudson Strait", "lat": 62.5, "lon": -70.0, "threat": "LOW"},
    {"name": "Lancaster Sound", "lat": 74.2, "lon": -84.0, "threat": "LOW"},
    {"name": "Amundsen Gulf", "lat": 70.8, "lon": -122.0, "threat": "LOW"},
    {"name": "McClure Strait", "lat": 73.8, "lon": -118.0, "threat": "LOW"},
    {"name": "Vilnius Gap (Baltic)", "lat": 55.0, "lon": 21.0, "threat": "HIGH"},
    {"name": "Kola Peninsula (Russian Fleet)", "lat": 69.0, "lon": 33.0, "threat": "EXTREME"},
    {"name": "Sea of Okhotsk", "lat": 54.0, "lon": 149.0, "threat": "HIGH"},
    {"name": "Kuril Islands", "lat": 46.5, "lon": 151.0, "threat": "HIGH"},
    {"name": "Yellow Sea", "lat": 35.0, "lon": 123.0, "threat": "HIGH"},
    {"name": "East China Sea", "lat": 29.0, "lon": 125.0, "threat": "HIGH"},
    {"name": "Philippine Sea", "lat": 18.0, "lon": 133.0, "threat": "MEDIUM"},
    {"name": "Celebes Sea", "lat": 3.0, "lon": 122.0, "threat": "MEDIUM"},
    {"name": "Java Sea", "lat": -5.0, "lon": 110.0, "threat": "LOW"},
    {"name": "Banda Sea", "lat": -6.0, "lon": 127.0, "threat": "LOW"},
    {"name": "Arafura Sea", "lat": -9.0, "lon": 135.0, "threat": "LOW"},
    {"name": "Coral Sea", "lat": -17.0, "lon": 150.0, "threat": "LOW"},
    {"name": "Tasman Sea", "lat": -40.0, "lon": 160.0, "threat": "LOW"},
    {"name": "Gulf of Mexico", "lat": 25.0, "lon": -90.0, "threat": "LOW"},
    {"name": "Caribbean Sea", "lat": 15.0, "lon": -75.0, "threat": "LOW"},
    {"name": "Mediterranean Sea", "lat": 35.0, "lon": 18.0, "threat": "MEDIUM"},
    {"name": "Tyrrhenian Sea", "lat": 39.0, "lon": 12.0, "threat": "LOW"},
    {"name": "Adriatic Sea", "lat": 43.0, "lon": 15.0, "threat": "LOW"},
    {"name": "Ionian Sea", "lat": 38.0, "lon": 19.0, "threat": "LOW"},
    {"name": "Aegean Sea", "lat": 39.0, "lon": 25.0, "threat": "MEDIUM"},
    {"name": "Celtic Sea", "lat": 50.0, "lon": -8.0, "threat": "LOW"},
    {"name": "Irish Sea", "lat": 53.5, "lon": -5.0, "threat": "LOW"},
    {"name": "North Sea (Oil Rigs)", "lat": 56.0, "lon": 3.0, "threat": "MEDIUM"},
    {"name": "Norwegian Sea", "lat": 68.0, "lon": 5.0, "threat": "MEDIUM"},
    {"name": "Greenland Sea", "lat": 73.0, "lon": -8.0, "threat": "LOW"},
    {"name": "Baffin Bay", "lat": 73.0, "lon": -65.0, "threat": "LOW"},
    {"name": "Beaufort Sea", "lat": 72.0, "lon": -137.0, "threat": "LOW"},
    {"name": "Chukchi Sea", "lat": 69.0, "lon": -169.0, "threat": "MEDIUM"},
    {"name": "East Siberian Sea", "lat": 72.0, "lon": 163.0, "threat": "LOW"},
    {"name": "Laptev Sea", "lat": 76.0, "lon": 125.0, "threat": "LOW"},
    {"name": "Kara Sea", "lat": 74.0, "lon": 70.0, "threat": "LOW"},
    {"name": "White Sea", "lat": 65.5, "lon": 38.0, "threat": "LOW"},
    {"name": "Gulf of Bothnia", "lat": 63.0, "lon": 20.0, "threat": "LOW"},
    {"name": "Gulf of Finland", "lat": 60.0, "lon": 26.0, "threat": "MEDIUM"},
    {"name": "Gulf of Riga", "lat": 57.5, "lon": 23.5, "threat": "LOW"},
    {"name": "Bay of Biscay", "lat": 45.0, "lon": -5.0, "threat": "LOW"},
    {"name": "Gulf of Cadiz", "lat": 36.5, "lon": -7.5, "threat": "LOW"},
    {"name": "Alboran Sea", "lat": 36.0, "lon": -3.0, "threat": "LOW"},
    {"name": "Ligurian Sea", "lat": 43.5, "lon": 9.0, "threat": "LOW"},
    {"name": "Gulf of Lion", "lat": 42.5, "lon": 4.0, "threat": "LOW"},
    {"name": "Balearic Sea", "lat": 40.0, "lon": 2.0, "threat": "LOW"},
    {"name": "Levantine Sea", "lat": 34.0, "lon": 33.0, "threat": "HIGH"},
    {"name": "Gulf of Sidra", "lat": 32.0, "lon": 18.0, "threat": "MEDIUM"},
    {"name": "Gulf of Gabes", "lat": 34.0, "lon": 11.0, "threat": "LOW"},
    {"name": "Red Sea (Houthi Threat)", "lat": 20.0, "lon": 38.0, "threat": "EXTREME"},
    {"name": "Gulf of Aqaba", "lat": 28.5, "lon": 34.7, "threat": "MEDIUM"},
    {"name": "Gulf of Suez", "lat": 28.5, "lon": 33.0, "threat": "MEDIUM"},
    {"name": "Persian Gulf", "lat": 27.0, "lon": 52.0, "threat": "HIGH"},
    {"name": "Gulf of Oman", "lat": 24.0, "lon": 58.0, "threat": "HIGH"},
    {"name": "Arabian Sea", "lat": 15.0, "lon": 65.0, "threat": "MEDIUM"},
    {"name": "Laccadive Sea", "lat": 8.0, "lon": 75.0, "threat": "LOW"},
    {"name": "Bay of Bengal", "lat": 15.0, "lon": 88.0, "threat": "LOW"},
    {"name": "Andaman Sea", "lat": 10.0, "lon": 96.0, "threat": "LOW"}
]

# --- Sim Time Compression ---
SIM_SPEED_MULT = 1.0       

# --- Autopilot Paths (lists of SCENARIOS indices) ---
AUTOPILOT_PATHS = [
    {
        "name": "GLOBAL THREAT TOUR",
        "waypoints": [0, 7, 4, 5, 6],          
    },
    {
        "name": "PACIFIC WAR GAME",
        "waypoints": [0, 1, 80, 9, 8, 76],     
    },
    {
        "name": "ARCTIC RECON",
        "waypoints": [0, 97, 17, 18, 75, 2],   
    },
]

REMOTE_URL = "https://benfishmanta.duckdns.org/sim_ingress"
STATUS_URL = "https://benfishmanta.duckdns.org/status_api"
GEMINI_URL = "https://benfishmanta.duckdns.org/gemini"
AUTH_TOKEN = os.getenv("PYXIS_AUTH_TOKEN")

# --- Shared State ---
watch_uuv_state = "RECALL" 
crew_loc_lat = 0.0
crew_loc_lon = 0.0
live_ais_contacts = []
last_warp_ts = 0
pending_warp = None
autopilot_active  = False
current_path_idx  = 0    
current_wp_idx    = 0    

live_proxy_data = {
    "wave_h": None, "wave_period": None, "wave_dir": None, "sst_c": None, "curr_v": None, "curr_dir": None,
    "rpm": None, "coolant_c": None, "oil_psi": None, "egt_c": None, "er_temp_c": None,
    "house_v": None, "house_soc": None, "house_amps": None, "start_v": None, "alt_v": None,
    "solar_w": None, "wind_gen_w": None, "inverter_w": None, "gen_status": None,
    "fuel_pct": None, "fresh_water_pct": None, "bilge_status": None, "er_bilge": None,
    "sog_kts": None, "depth_m": None, "aws_kts": None, "awa_deg": None, "pressure_hpa": None,
    "last_update": 0,
}
proxy_lock  = threading.Lock()
proxy_online = False

def get_math_depth(world_x, world_z):
    # Convert local x,z to approximate Lat/Lon
    lat_offset = (world_x) / 111320.0
    lon_offset = (world_z) / 92000.0
    current_lat = BOAT_START[0] + lat_offset
    current_lon = BOAT_START[1] + lon_offset
    
    wx = np.array(world_x, dtype=np.float32)
    wz = np.array(world_z, dtype=np.float32)
    
    depth = -60.0 
    depth += np.sin(wx * 0.0004) * np.cos(wz * 0.0005) * 85.0
    depth += np.sin(wx * 0.0018) * np.cos(wz * 0.0015) * 25.0
    depth += np.sin(wx * 0.01) * np.cos(wz * 0.008) * 6.0
    depth = np.clip(depth, -180.0, 100.0)
    return depth

class OceanPhysics:
    def __init__(self, base_y):
        self.base_y = base_y
    def get_exact_height(self, x, z, t, sea_state):
        if sea_state <= 0.01: return float(self.base_y)
        return float(self.base_y + (np.sin(x*0.02 + t*1.5)*1.5 + np.cos(z*0.025 + t*1.2) + np.sin((x+z)*0.08 + t*3.0)*0.5) * sea_state)
        
    def get_wave_derivatives(self, x, z, t, sea_state):
        if sea_state <= 0.01: return 0.0, 0.0
        dx = (np.cos(x*0.02 + t*1.5)*0.03 + np.cos((x+z)*0.08 + t*3.0)*0.04) * sea_state
        dz = (-np.sin(z*0.025 + t*1.2)*0.025 + np.cos((x+z)*0.08 + t*3.0)*0.04) * sea_state
        return float(dx), float(dz)

class Mothership:
    def __init__(self, start_x, start_z):
        self.pos = np.array((start_x, float(SHIP_DEPTH), start_z), dtype=float)
        self.yaw, self.pitch, self.roll = 0.0, 0.0, 0.0

    def update(self, dt, ocean, t, sea_state, thrust_dir, speed_knots):
        speed_ms = speed_knots * KNOT_TO_MS
        if np.linalg.norm(thrust_dir) > 0.01:
            target_yaw = float(np.arctan2(thrust_dir[0], thrust_dir[2]))
            dyaw = (target_yaw - self.yaw + np.pi) % (2 * np.pi) - np.pi
            self.yaw += dyaw * 2.0 * dt
            
        vx, vz = float(np.sin(self.yaw) * speed_ms), float(np.cos(self.yaw) * speed_ms)
        px, py, pz = self.pos
        nx, nz = px + vx * dt, pz + vz * dt
        
        # Grounding Check
        ground_height = get_math_depth(nx, nz)
        if ground_height > -1.0:
            nx, nz = px, pz 
            target_y = float(ground_height) 
            target_pitch, target_roll = 0.0, 0.0 
        else:
            target_y = ocean.get_exact_height(nx, nz, t, sea_state)
            dw_dx, dw_dz = ocean.get_wave_derivatives(nx, nz, t, sea_state)
            target_pitch = float(np.arctan(dw_dx * np.sin(self.yaw) + dw_dz * np.cos(self.yaw)))
            target_roll = float(np.arctan(-(dw_dx * np.cos(self.yaw) - dw_dz * np.sin(self.yaw))))
            
        ny = py + (target_y - py) * 25.0 * dt
        self.pos = np.array((nx, ny, nz))
        self.pitch += (target_pitch - self.pitch) * 25.0 * dt
        self.roll += (target_roll - self.roll) * 25.0 * dt

class PIDController:
    def __init__(self, kp, ki, kd):
        self.kp, self.ki, self.kd = kp, ki, kd
        self.prev_error, self.integral = 0.0, 0.0
    def compute(self, setpoint, measured_value, dt):
        if dt <= 0: return 0.0
        error = setpoint - measured_value
        self.integral += error * dt
        derivative = (error - self.prev_error) / dt
        self.prev_error = error
        return (self.kp * error) + (self.ki * self.integral) + (self.kd * derivative)

class MantaCoreSled:
    def __init__(self, start_pos):
        self.pos = np.array(start_pos, dtype=float)
        self.vel = np.zeros(3, dtype=float)
        self.target_depth = 20.0  
        self.pid = PIDController(2.5, 0.08, 1.8)
        self.auto_pilot_active = True
        self.cable_length = 120.0
        self.yaw, self.pitch, self.wing_pitch = 0.0, 0.0, 0.0

    def update(self, dt, ship_pos, base_current):
        sx, sy, sz = self.pos
        target_y = float(SHIP_DEPTH - self.target_depth)
        pid_pitch = float(np.clip(self.pid.compute(target_y, sy, dt), -40.0, 40.0))
        self.wing_pitch += (math.radians(pid_pitch) - self.wing_pitch) * 8.0 * dt
        cx, cy, cz = base_current
        if sy < -55.0: cx -= 1.5; cz += 0.5
        cur = np.array((cx, cy, cz), dtype=float)
        vec_to_ship = ship_pos - self.pos
        dist = float(np.linalg.norm(vec_to_ship))
        tether_dir = vec_to_ship / max(dist, 0.01)
        slack_threshold = self.cable_length * 0.8
        tether_force = tether_dir * (dist - slack_threshold) * 80.0 if dist > slack_threshold else np.zeros(3)
        speed = float(np.linalg.norm(self.vel))
        vx, vy, vz = float(self.vel[0]), float(self.vel[1]), float(self.vel[2])
        drag = np.array((-0.05 * vx * speed, -1.50 * vy * speed, -0.05 * vz * speed), dtype=float)
        lift = np.array((0.0, pid_pitch * max(speed, 1.0) * 1.5, 0.0), dtype=float)
        dv = ((drag + cur + lift + tether_force) / 85.0) * float(dt)
        self.vel = np.add(self.vel, dv, casting="unsafe")
        self.pos = np.add(self.pos, self.vel * float(dt), casting="unsafe")
        if np.any(np.isnan(self.pos)):
            self.pos = np.array(ship_pos)
            self.vel = np.zeros(3)
        if dist > self.cable_length: 
            over_dist = float(dist - self.cable_length)
            self.pos = np.add(self.pos, tether_dir * over_dist * 0.5, casting="unsafe")
            v_dot_t = float(np.dot(self.vel, -tether_dir))
            if v_dot_t > 0: self.vel = np.add(self.vel, tether_dir * v_dot_t, casting="unsafe")
        nsx, nsy, nsz = self.pos
        nvx, nvy, nvz = self.vel
        speed_xz = float(math.hypot(nvx, nvz))
        if speed_xz > 0.1:
            target_yaw = float(math.atan2(nvx, nvz))
            dyaw = (target_yaw - self.yaw + math.pi) % (2 * math.pi) - math.pi
            self.yaw += dyaw * 4.0 * dt
            self.pitch = float(math.atan2(nvy, speed_xz))
        gy_new = get_math_depth(nsx, nsz)
        if nsy < gy_new + 1.5: 
            self.pos = np.array((nsx, gy_new + 1.5, nsz))
            self.vel = np.array((nvx, 0.0, nvz))

class StrikeEntity:
    def __init__(self, start_pos, target_pos, type):
        self.pos = np.array(start_pos, dtype=float)
        self.target = np.array(target_pos, dtype=float)
        self.active = True
        self.type = type
        self.speed = 1200.0 if type == "MISSILE" else 150.0 
        self.name = "HYPERSONIC_STRIKE" if type == "MISSILE" else "SUICIDE_DRONE"
        self.id = f"SIM_{random.randint(1000, 9999)}"
        self.splashing = False
        self.impact_timer = 0.0
        
    def update(self, dt):
        if not self.active: return
        if self.splashing:
            self.impact_timer += dt
            if self.impact_timer > 2.0: self.active = False
            return
        dir_vec = self.target - self.pos
        dist = float(np.linalg.norm(dir_vec))
        if dist < 50.0:
            self.splashing = True
        else:
            self.pos += (dir_vec / dist) * self.speed * dt

class PatrolUUV:
    def __init__(self, start_pos):
        self.pos = np.array(start_pos, dtype=float)
        self.target = self.pos.copy()
        self.speed = 12.0 
        self.patrol_angle = 0.0 
        self.patrol_radius = 250.0

    def get_sweep_target(self, ref_pos):
        self.patrol_angle += math.pi / 4.0 
        rx, ry, rz = ref_pos
        tx = rx + math.cos(self.patrol_angle) * self.patrol_radius
        tz = rz + math.sin(self.patrol_angle) * self.patrol_radius
        target_ground = get_math_depth(tx, tz)
        ty = min(target_ground + 15.0, -10.0)
        return np.array((tx, ty, tz))

    def update(self, dt, sled_pos):
        global watch_uuv_state
        if watch_uuv_state == "RECALL":
            self.target = sled_pos
            tx, ty, tz = sled_pos
        dir_vec = self.target - self.pos
        dist = float(np.linalg.norm(dir_vec))
        if dist < 20.0 and watch_uuv_state == "DEPLOY": 
            self.target = self.get_sweep_target(sled_pos)
        elif dist < 5.0 and watch_uuv_state == "RECALL":
            self.pos = np.array(sled_pos) 
            return 
        velocity = (dir_vec / max(dist, 0.01)) * self.speed
        self.pos += velocity * dt
        look_ahead_x = self.pos[0] + (velocity[0] * 2.5)
        look_ahead_z = self.pos[2] + (velocity[2] * 2.5)
        ground_ahead = get_math_depth(look_ahead_x, look_ahead_z)
        current_ground = get_math_depth(self.pos[0], self.pos[2])
        safe_y = max(current_ground + 8.0, ground_ahead + 8.0)
        if self.pos[1] < safe_y:
            self.pos[1] += (safe_y - self.pos[1]) * 2.0 * dt

class OnboardSystems:
    def __init__(self):
        self.engine_temp = 85.0
        self.battery_v   = 13.5
        self.house_soc   = 94.0
        self.house_amps  = -15.5
        self.start_v     = 12.8
        self.alt_v       = 14.1
        self.solar_w     = 0.0
        self.wind_gen_w  = 0.0
        self.inverter_w  = 0.0
        self.gen_status  = "OFF"
        self.fuel_pct    = 88.0
        self.fresh_water_pct = 70.0
        self.bilge_status = "OK"
        self.er_temp_c   = 40.0
        self.oil_psi     = 45.0
        self.egt_c       = 0.0
        self.rpms        = 0
        self.status      = "NOMINAL"
        self._live       = False   

    def update(self, dt, speed_knots, sea_state, ew_active, sys_state):
        with proxy_lock:
            _d = dict(live_proxy_data)
        _live_rpm  = _d.get("rpm")
        _live_cool = _d.get("coolant_c")
        _live_age  = _d.get("last_update") or 0
        self._live = (time.time() - _live_age) < 60  

        if self._live and _live_rpm is not None:
            self.rpms          = float(_live_rpm)
            self.engine_temp   = float(_live_cool or self.engine_temp)
            self.oil_psi       = float(_d.get("oil_psi")     or self.oil_psi)
            self.egt_c         = float(_d.get("egt_c")       or self.egt_c)
            self.er_temp_c     = float(_d.get("er_temp_c")   or self.er_temp_c)
            self.battery_v     = float(_d.get("house_v")     or self.battery_v)
            self.house_soc     = float(_d.get("house_soc")   or self.house_soc)
            self.house_amps    = float(_d.get("house_amps")  or self.house_amps)
            self.start_v       = float(_d.get("start_v")     or self.start_v)
            self.alt_v         = float(_d.get("alt_v")       or self.alt_v)
            self.solar_w       = float(_d.get("solar_w")     or 0.0)
            self.wind_gen_w    = float(_d.get("wind_gen_w")  or 0.0)
            self.inverter_w    = float(_d.get("inverter_w")  or 0.0)
            self.gen_status    = str(_d.get("gen_status")    or "OFF")
            self.fuel_pct      = float(_d.get("fuel_pct")    or self.fuel_pct)
            self.fresh_water_pct = float(_d.get("fresh_water_pct") or self.fresh_water_pct)
            self.bilge_status  = str(_d.get("bilge_status")  or "OK")
        else:
            if not sys_state.get("engine", True):
                self.rpms += (0 - self.rpms) * 0.05
            else:
                target_rpms = max(600, abs(speed_knots) * 120 + 600) if speed_knots != 0 else 600
                self.rpms += (target_rpms - self.rpms) * 0.1
            heat_gen = (self.rpms / 2000.0) * (1.0 + sea_state * 0.2)
            cooling  = (abs(speed_knots) / 25.0) + 0.15 + (1.0 - (self.rpms / 3000.0)) * 0.5
            if not sys_state.get("engine", True): heat_gen = 0.0
            self.engine_temp += (heat_gen - cooling) * dt
            draw = 0.0005
            if ew_active: draw += 0.005
            if sys_state.get("sonar", True): draw += 0.002
            if sys_state.get("comms", True): draw += 0.001
            if self.rpms > 800:
                self.battery_v += 0.005 * dt
            self.battery_v -= draw * dt
            self.fuel_pct  -= (self.rpms / 150000.0) * dt

        self.engine_temp = max(60.0, min(self.engine_temp, 200.0))
        self.battery_v   = max(10.0, min(self.battery_v,   30.0))
        self.fuel_pct    = max(0.0,  min(self.fuel_pct,   100.0))

        if self.engine_temp > 105.0:
            self.status = "OVERHEAT"
        elif self.bilge_status not in ("OK", None, ""):
            self.status = "BILGE ALARM"
        elif self.battery_v < 12.0:
            self.status = "LOW VOLTAGE"
        elif self.fuel_pct < 10.0:
            self.status = "LOW FUEL"
        else:
            self.status = "NOMINAL"

def compute_autopilot(b_lat, b_lon, path_idx, wp_idx):
    path    = AUTOPILOT_PATHS[path_idx]
    wp_scen = SCENARIOS[path["waypoints"][wp_idx]]
    t_lat, t_lon = wp_scen["lat"], wp_scen["lon"]
    dlat = t_lat - b_lat
    dlon = (t_lon - b_lon) * math.cos(math.radians(b_lat))
    dist_nm = math.sqrt(dlat**2 + dlon**2) * 60.0
    bearing = math.atan2(dlat, dlon)
    return bearing, dist_nm

def poll_proxy_live_data():
    global proxy_online, watch_uuv_state, crew_loc_lat, crew_loc_lon, pending_warp, last_warp_ts, live_ais_contacts
    while True:
        try:
            r = requests.get(STATUS_URL, headers={"X-Garmin-Auth": AUTH_TOKEN}, timeout=5.0, verify=False)
            if r.status_code == 200:
                d = r.json()
                with proxy_lock:
                    w = d.get("vessel_wave") or {}
                    live_proxy_data["wave_h"]      = d.get("wave_height_m")  or w.get("wave_h")
                    live_proxy_data["wave_period"] = w.get("wave_period")    or w.get("VTM10")
                    live_proxy_data["wave_dir"]    = d.get("wave_dir")       or w.get("wave_dir") or w.get("VMDR")
                    live_proxy_data["sst_c"]       = d.get("sea_temp_c")     or w.get("sst_c")
                    live_proxy_data["curr_v"]      = d.get("current_kn")     or w.get("curr_v")
                    live_proxy_data["curr_dir"]    = w.get("curr_dir")
                    live_proxy_data["rpm"]         = d.get("rpm")
                    live_proxy_data["coolant_c"]   = d.get("coolant_temp_c")
                    live_proxy_data["oil_psi"]     = d.get("oil_press_psi")
                    live_proxy_data["egt_c"]       = d.get("egt_c")
                    live_proxy_data["er_temp_c"]   = d.get("er_temp_c")
                    live_proxy_data["house_v"]     = d.get("house_v")    or d.get("bat_v")
                    live_proxy_data["house_soc"]   = d.get("house_soc")
                    live_proxy_data["house_amps"]  = d.get("house_amps")
                    live_proxy_data["start_v"]     = d.get("start_v")
                    live_proxy_data["alt_v"]       = d.get("alt_v")
                    live_proxy_data["solar_w"]     = d.get("solar_w")
                    live_proxy_data["wind_gen_w"]  = d.get("wind_gen_w")
                    live_proxy_data["inverter_w"]  = d.get("inverter_w")
                    live_proxy_data["gen_status"]  = d.get("gen_status")
                    live_proxy_data["fuel_pct"]        = d.get("fuel_pct")
                    live_proxy_data["fresh_water_pct"] = d.get("fresh_water_pct")
                    live_proxy_data["bilge_status"]    = d.get("bilge_status")
                    live_proxy_data["er_bilge"]        = d.get("bilge_er_alarm")
                    live_proxy_data["sog_kts"]     = d.get("sog_kts")
                    live_proxy_data["depth_m"]     = d.get("depth_m")
                    live_proxy_data["aws_kts"]     = d.get("aws_kts")
                    live_proxy_data["awa_deg"]     = d.get("awa_deg")
                    live_proxy_data["pressure_hpa"]= d.get("pressure_hpa")
                    live_proxy_data["last_update"] = time.time()
                    
                ais_raw = d.get("radar_contacts", [])
                live_ais_contacts = [c for c in ais_raw if "lat" in c and "lon" in c and c.get("lat") is not None and c.get("lon") is not None]

                watch_cmd = d.get("UUV_STATE", "")
                if watch_cmd in ["DEPLOY", "RECALL"]:
                    watch_uuv_state = watch_cmd
                    
                crew_loc_lat = float(d.get("CREW_LAT", 0.0))
                crew_loc_lon = float(d.get("CREW_LON", 0.0))
                
                warp_data = d.get("scenario_warp")
                if warp_data:
                    w_ts = warp_data.get("ts", 0)
                    if last_warp_ts == 0:
                        last_warp_ts = w_ts 
                    elif w_ts > last_warp_ts:
                        last_warp_ts = w_ts
                        pending_warp = (float(warp_data.get("lat", 0)), float(warp_data.get("lon", 0)))
                
                proxy_online = True
            else:
                proxy_online = False
        except Exception:
            proxy_online = False
        time.sleep(2) # Faster polling for AI reaction times!

def post_telemetry(payload):
    try: 
        requests.post(REMOTE_URL, json=payload, headers={"X-Garmin-Auth": AUTH_TOKEN}, timeout=2.0, verify=False)
    except Exception as e: pass

def run_headless_sim():
    global BOAT_START, pending_warp, autopilot_active, current_path_idx, current_wp_idx, SIM_SPEED_MULT, proxy_online
    
    # Initialize Core Classes
    ocean = OceanPhysics(SHIP_DEPTH)
    rib = Mothership(0.0, 0.0)
    uuv = PatrolUUV(np.array((50.0, -40.0, 50.0)))
    sled = MantaCoreSled(np.array((0.0, get_math_depth(0.0, -20.0) + 20.0, -20.0)))
    vessel_systems = OnboardSystems()
    
    t = 0.0
    wave_h = 1.5
    ship_speed_knots = 10.0  
    cruise_active = True
    dynamic_contacts = []
    strike_entities = []
    
    ew_active = False
    sys_state = {"engine": True, "sonar": True, "comms": True}
    
    print("[MANTASIM HEADLESS] Physics Engine Initialized.")
    
    try:
        _r = requests.get(STATUS_URL, timeout=4.0, verify=False)
        if _r.status_code == 200:
            _d = _r.json()
            _blat = float(_d.get("BOAT_LAT") or _d.get("lat") or BOAT_START[0])
            _blon = float(_d.get("BOAT_LON") or _d.get("lon") or BOAT_START[1])
            if _blat != 0.0 and _blon != 0.0:
                BOAT_START = (_blat, _blon)
                print(f"[MANTASIM HEADLESS] Proxy Sync: {_blat}, {_blon}")
    except Exception:
        print("[MANTASIM HEADLESS] Proxy Offline, using defaults.")

    threading.Thread(target=poll_proxy_live_data, daemon=True).start()

    SIM_HZ = 10.0 # Runs 10 per second
    base_dt = 1.0 / SIM_HZ
    
    # By default, autopilot is on
    autopilot_active = True
    current_path_idx = 0
    current_wp_idx = 0
    print(f"[MANTASIM HEADLESS] AUTOPILOT ENGAGED. PATH: {AUTOPILOT_PATHS[current_path_idx]['name']}")
    
    last_up = time.time()
    
    while True:
        cycle_start = time.time()
        
        # In headless/RL mode, dt is our base time step. SIM_SPEED_MULT accelerates physics processing.
        dt = base_dt 
        t += dt 
        dt_scaled = dt * SIM_SPEED_MULT  
        
        if pending_warp:
            w_lat, w_lon = pending_warp
            BOAT_START = (w_lat, w_lon)
            rib.pos = np.array((0.0, float(SHIP_DEPTH), 0.0), dtype=float)
            sled.pos = np.array((0.0, get_math_depth(0.0, -20.0) + 20.0, -20.0), dtype=float)
            uuv.pos = np.array((50.0, -40.0, 50.0), dtype=float)
            dynamic_contacts.clear()
            strike_entities.clear()
            print(f"[MANTASIM HEADLESS] REMOTE WARP RECEIVED: {w_lat}, {w_lon}")
            pending_warp = None
            
        rx, ry, rz = rib.pos
        ux, uy, uz = uuv.pos
        slx, sly, slz = sled.pos
        if np.isnan(slx): slx = 0.0; sled.pos = np.zeros(3)
        if np.isnan(sly): sly = 0.0
        if np.isnan(slz): slz = 0.0
        
        b_lat = BOAT_START[0] + (rx/111320.0)
        b_lon = BOAT_START[1] + (rz/92000.0)

        # Autopilot overrides
        if autopilot_active:
            _bearing, _dist_nm = compute_autopilot(b_lat, b_lon, current_path_idx, current_wp_idx)
            _dyaw = (_bearing - rib.yaw + math.pi) % (2 * math.pi) - math.pi
            rib.yaw += _dyaw * min(1.0, 3.0 * dt_scaled)
            ship_speed_knots = 10.0 
            cruise_active = True
            
            if _dist_nm < 1.0: 
                print(f"[MANTASIM HEADLESS] Waypoint reached. Proceeding to next.")
                current_wp_idx += 1
                if current_wp_idx >= len(AUTOPILOT_PATHS[current_path_idx]["waypoints"]):
                    print("[MANTASIM HEADLESS] Path complete.")
                    autopilot_active = False
                    current_wp_idx = 0
            
        eff_speed = ship_speed_knots if cruise_active and sys_state.get("engine", True) else 0.0
        vessel_systems.update(dt, eff_speed, wave_h, ew_active, sys_state)
        
        if vessel_systems.status == "OVERHEAT" and eff_speed > 5.0:
            ship_speed_knots *= 0.95
            eff_speed = ship_speed_knots
            
        thrust = np.zeros(3) 

        # --- Apply CMEMS Physics ---
        with proxy_lock:
            _wh   = live_proxy_data.get("wave_h")
            _cv   = live_proxy_data.get("curr_v")   or 0.0
            _cdir = live_proxy_data.get("curr_dir") or 0.0
        if _wh is not None:
            wave_h = float(np.clip(_wh, 0.0, MAX_WAVE_HEIGHT))
        _cdir_rad  = math.radians(_cdir)
        _cspeed_ms = float(_cv) * 0.514444 
        _cur_vec   = (_cspeed_ms * math.sin(_cdir_rad) * 120.0, 0.0, _cspeed_ms * math.cos(_cdir_rad) * 120.0)

        # Update Physics
        rib.update(dt_scaled, ocean, t, wave_h, thrust, ship_speed_knots)
        sled.update(dt_scaled, rib.pos, _cur_vec)
        uuv.update(dt_scaled, sled.pos)

        for c in dynamic_contacts:
            if c["type"] == "ALARM":
                c_dist = float(np.linalg.norm(np.array((c["x"]-rx, 0, c["z"]-rz))))
                dx, dz = c["x"] - rx, c["z"] - rz
                c["x"] -= (dx / max(c_dist, 0.1)) * (10.0 * KNOT_TO_MS * 2.0)
                c["z"] -= (dz / max(c_dist, 0.1)) * (10.0 * KNOT_TO_MS * 2.0)

        for strike in strike_entities: strike.update(dt)

        # Telemetry Output (Post every 2 real-world seconds, regardless of simulation timescale)
        if time.time() - last_up > 2.0 and sys_state.get("comms", True):
            radar_export = []
            uuv_to_sled = float(np.linalg.norm(sled.pos - uuv.pos))
            if uuv_to_sled < 400.0 and watch_uuv_state == "DEPLOY":
                ux_lat = b_lat + ((ux - rx)/111320.0)
                uz_lon = b_lon + ((uz - rz)/(111320.0 * math.cos(math.radians(b_lat))))
                dx, dz = ux - rx, uz - rz
                bear = (math.degrees(math.atan2(dx, dz)) + 360) % 360
                dist_nm = uuv_to_sled / 1852.0
                radar_export.append({"id": "UUV1", "name": "UUV-PATROL", "type": "DRONE", "lat": ux_lat, "lon": uz_lon, "bearing": bear, "range_nm": dist_nm})

            for strike in strike_entities:
                if strike.active:
                    s_lat = b_lat + ((strike.pos[0] - rx)/111320.0)
                    s_lon = b_lon + ((strike.pos[2] - rz)/(111320.0 * math.cos(math.radians(b_lat))))
                    sx_dx, sz_dz = strike.pos[0] - rx, strike.pos[2] - rz
                    s_bear = (math.degrees(math.atan2(sx_dx, sz_dz)) + 360) % 360
                    s_dist_nm = float(np.linalg.norm(strike.pos - np.array((rx, ry, rz)))) / 1852.0
                    radar_export.append({
                        "id": strike.id, "name": strike.name, "type": "ALARM",
                        "lat": s_lat, "lon": s_lon, "bearing": s_bear, "range_nm": s_dist_nm, "speed": strike.speed * KNOT_TO_MS * 3.6
                    })
            
            spoof_lat = b_lat + 0.05 if ew_active else b_lat
            spoof_lon = b_lon - 0.05 if ew_active else b_lon

            payload = {
                "BOAT_LAT": round(spoof_lat, 5), "BOAT_LON": round(spoof_lon, 5), 
                "CREW_LAT": round(CREW_START[0], 5), "CREW_LON": round(CREW_START[1], 5), 
                "speed_kn": round(eff_speed, 1),
                "sea_state": round(wave_h, 1),
                "sled_depth": round(float(SHIP_DEPTH - sly), 1),
                "radar_contacts": radar_export,
                "metrics": {
                    "rpm_p": int(eff_speed * 120),
                    "rpm_s": int(eff_speed * 120),
                    "fuel_pct": round(vessel_systems.fuel_pct, 1),
                    "water_pct": 95.0,
                    "sea_temp_c": round(live_proxy_data.get("sst_c") or 19.5, 1),
                    "wind_tws": round(wave_h * 10, 1),
                    "wind_aws": round(wave_h * 12 + eff_speed, 1),
                    "ap_engaged": autopilot_active or cruise_active,
                    "ble_count": 3
                }
            }
            threading.Thread(target=post_telemetry, args=(payload,), daemon=True).start()
            last_up = time.time()
            print(f"[MANTASIM HEADLESS] TSYNC >> Lat: {b_lat:.4f} Lon: {b_lon:.4f} Spd: {eff_speed:.1f}kn")

        # Regulate headless loop to match HZ (if Sim is 1x. If it's fast forwarding, we can either sleep less or increase dt. We chose to sleep normally, and increase dt_scaled for physics to maintain CPU composure)
        elapsed = time.time() - cycle_start
        if elapsed < base_dt:
            time.sleep(base_dt - elapsed)

if __name__ == "__main__": 
    run_headless_sim()
