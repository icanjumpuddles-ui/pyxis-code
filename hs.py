import numpy as np
import math, requests, threading, random, uuid
from datetime import datetime
import os
import sqlite3
import time

# --- System Parameters & Config ---
GRID_DIM = 120       
SCALE = 22           
SHIP_DEPTH = 30.0  
MAX_WAVE_HEIGHT = 4.0    
MAX_SLED_DEPTH = 100.0

# --- Tactical Config ---
MAX_KNOTS = 15.0  
KNOT_TO_MS = 0.514444
BOAT_START = (-38.277, 144.731) # Popes Eye, Port Phillip Bay (open water)
CREW_START = (-38.277, 144.731)

# --- Proxy Endpoints ---
STATUS_URL = "https://127.0.0.1:443/status_api" # Should run on the same server

# --- Shared State ---
watch_uuv_state = "RECALL" 
crew_loc_lat = 0.0
crew_loc_lon = 0.0
last_scenario_id = None
osm_cache = []
osm_cache_lock = threading.Lock()

def get_math_depth(x, z):
    dist = math.hypot(x, z)
    return -SHIP_DEPTH - (dist * 0.05)
crew_loc_lon = 0.0
last_audio_ts = 0
last_scenario_id = None

def get_math_depth(world_x, world_z):
    # Convert local x,z to approximate Lat/Lon based on starting Pyxis point
    # BRIDGE: 1 deg Lat = ~111,320m. 1 deg Lon = ~92,000m (at 26.3N)
    lat_offset = (world_x) / 111320.0
    lon_offset = (world_z) / 92000.0
    
    current_lat = BOAT_START[0] + lat_offset
    current_lon = BOAT_START[1] + lon_offset
    
    depth = -85.0 # Base channel depth
        
    # Introduce some rocky coastal shelves and islands
    # Procedural Noise for natural look
    wx = np.array(world_x, dtype=np.float32)
    wz = np.array(world_z, dtype=np.float32)
    depth += np.sin(wx*0.0021)*np.cos(wz*0.0016)*15.0
    depth += np.sin(wx*0.011 + wz*0.008)*8.0
    
    return depth

# --- Physics Classes (Stripped of OpenGL) ---
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
        
        target_y = ocean.get_exact_height(nx, nz, t, sea_state)
        dw_dx, dw_dz = ocean.get_wave_derivatives(nx, nz, t, sea_state)
        target_pitch = float(np.arctan(dw_dx * np.sin(self.yaw) + dw_dz * np.cos(self.yaw)))
        target_roll = float(np.arctan(-(dw_dx * np.cos(self.yaw) - dw_dz * np.sin(self.yaw))))
        
        ny = py + (target_y - py) * 25.0 * dt
        self.pos = np.array((nx, ny, nz))
        self.pitch += (target_pitch - self.pitch) * 25.0 * dt
        self.roll += (target_roll - self.roll) * 25.0 * dt

class OceanHeadless:
    def __init__(self, dim, scale, base_y):
        self.dim, self.scale, self.base_y = dim, scale, base_y

    def update(self, ship_x, ship_z, t, sea_state):
        pass # No vertices to update for headless

    def get_exact_height(self, x, z, t, sea_state):
        if sea_state <= 0.01: return float(self.base_y)
        return float(self.base_y + (np.sin(x*0.02 + t*1.5)*1.5 + np.cos(z*0.025 + t*1.2) + np.sin((x+z)*0.08 + t*3.0)*0.5) * sea_state)
        
    def get_wave_derivatives(self, x, z, t, sea_state):
        if sea_state <= 0.01: return 0.0, 0.0
        dx = (np.cos(x*0.02 + t*1.5)*0.03 + np.cos((x+z)*0.08 + t*3.0)*0.04) * sea_state
        dz = (-np.sin(z*0.025 + t*1.2)*0.025 + np.cos((x+z)*0.08 + t*3.0)*0.04) * sea_state
        return float(dx), float(dz)

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

class PatrolUUV:
    def __init__(self, start_pos):
        self.pos = np.array(start_pos, dtype=float)
        self.target = self.get_new_target(start_pos)
        self.speed = 10.0 
    def get_new_target(self, ref_pos):
        rx, ry, rz = ref_pos
        return np.array((rx + np.random.uniform(-300, 300), ry + np.random.uniform(-30, 20), rz + np.random.uniform(-300, 300)))
    def update(self, dt, sled_pos):
        tx, ty, tz = self.target
        px, py, pz = self.pos
        
        global watch_uuv_state
        if watch_uuv_state == "RECALL":
            self.target = sled_pos
            tx, ty, tz = sled_pos
            
        dir_vec = np.array((tx-px, ty-py, tz-pz))
        dist = float(np.linalg.norm(dir_vec))
        
        if dist < 15.0 and watch_uuv_state == "DEPLOY": 
            self.target = self.get_new_target(sled_pos)
        elif dist < 5.0 and watch_uuv_state == "RECALL":
            self.pos = np.array(sled_pos)
        else: 
            self.pos += (dir_vec / max(dist, 0.01)) * self.speed * dt
            
        ux, uy, uz = self.pos
        gy = get_math_depth(ux, uz)
        if uy < gy + 10.0: self.pos = np.array((ux, gy + 10.0, uz))

# --- Headless Telemetry Exporter ---
def post_telemetry(payload):
    global watch_uuv_state, crew_loc_lat, crew_loc_lon
    try:
        r = requests.post("https://127.0.0.1:443/telemetry", json=payload, timeout=0.5, verify=False)
        if r.status_code == 200:
            resp = r.json()
            if "uuv_state" in resp: watch_uuv_state = resp["uuv_state"]
            if "crew_loc" in resp:
                crew_loc_lat = resp["crew_loc"][0]
                crew_loc_lon = resp["crew_loc"][1]
    except:
        pass

# --- MAIN HEADLESS LOOP ---
def run_headless_sim():
    print("Initialize Manta Core Headless Simulator...")
    
    rib = Mothership(0.0, 0.0)
    ocean = OceanHeadless(GRID_DIM, SCALE, 0.0)
    sled = MantaCoreSled(rib.pos)
    uuv = PatrolUUV(sled.pos)
    
    last_time = time.time()
    last_telemetry_time = last_time
    
    # AI Autopilot Variables
    ai_target_heading = 0.0
    ai_speed_knots = 5.0
    ai_wave_h = 1.0
    ai_change_timer = 0.0
    
    dynamic_contacts = []
    
    print("Manta Core Headless running. Generating scenarios...")
    while True:
        current_time = time.time()
        dt = current_time - last_time
        last_time = current_time
        sim_time = current_time
        
        # --- SCENARIO INJECTOR POLLING ---
        global BOAT_START, last_scenario_id
        if current_time % 2.0 < 0.1: # Rough 2-second polling interval
            try:
                r = requests.get("https://127.0.0.1:443/scenario", timeout=0.5, verify=False)
                if r.status_code == 200:
                    s_data = r.json()
                    new_id = s_data.get("id")
                    if new_id and new_id != last_scenario_id:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] ---> EXECUTING SCENARIO INJECTION: {new_id}")
                        last_scenario_id = new_id
                        
                        # Apply Warp
                        BOAT_START = (float(s_data.get("lat", BOAT_START[0])), float(s_data.get("lon", BOAT_START[1])))
                        ai_wave_h = float(s_data.get("sea_state", 1.0))
                        
                        # Reset Physics Origin to prevent warping causing NaN velocities
                        rib.pos = np.array((0.0, float(SHIP_DEPTH), 0.0), dtype=float)
                        sled.pos = np.array((0.0, float(SHIP_DEPTH - 20.0), 0.0), dtype=float)
                        sled.vel = np.zeros(3)
                        uuv.pos = np.array((0.0, float(SHIP_DEPTH - 20.0), 0.0), dtype=float)
                        dynamic_contacts.clear()
                        
                        # Inject Threats
                        threats = s_data.get("threats", [])
                        for i, threat in enumerate(threats):
                            bearing = random.uniform(0, 360)
                            distance = random.uniform(2000, 5000) if threat == "MERCHANT" else random.uniform(800, 1500)
                            m_x = math.sin(math.radians(bearing)) * distance
                            m_z = math.cos(math.radians(bearing)) * distance
                            t_speed = random.uniform(10, 25) if threat == "MERCHANT" else random.uniform(30, 60)
                            
                            dynamic_contacts.append({
                                "id": f"INJ_{threat}_{i}",
                                "type": threat,
                                "x": m_x,
                                "z": m_z,
                                "heading": (bearing + 180) % 360, # Head roughly towards Pyxis
                                "speed": t_speed
                            })
                            print(f"-> Injected {threat} at {distance:.0f}m")
            except Exception as e:
                pass


        # --- AI SCENARIO GENERATOR ---
        ai_change_timer -= dt
        if ai_change_timer <= 0:
            ai_change_timer = random.uniform(30.0, 120.0) # Change state every 30-120 seconds
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Scenario AI triggering new state...")
            
            # Randomize heading gently
            ai_target_heading += random.uniform(-45.0, 45.0)
            
            # Randomize speed
            ai_speed_knots = random.uniform(2.0, 14.0)
            
            # Randomize wave height
            ai_wave_h = random.uniform(0.5, 2.5)
            
            # Maybe spawn a merchant ship
            if random.random() < 0.3:
                bearing = random.uniform(0, 360)
                distance = random.uniform(2000, 8000)
                m_x = math.sin(math.radians(bearing)) * distance + rib.pos[0]
                m_z = math.cos(math.radians(bearing)) * distance + rib.pos[2]
                dynamic_contacts.append({
                    "id": str(uuid.uuid4())[:8],
                    "type": "MERCHANT",
                    "x": m_x,
                    "z": m_z,
                    "heading": random.uniform(0, 360),
                    "speed": random.uniform(10, 25)
                })
                print(f"-> Spawned Merchant Vessel at {distance:.0f}m bearing {bearing:.0f}")

            # Despawn old contacts
            if len(dynamic_contacts) > 5:
                dynamic_contacts.pop(0)

        # Smooth AI heading vector for physics
        rad_heading = math.radians(ai_target_heading)
        ai_thrust = np.array((math.sin(rad_heading), 0.0, math.cos(rad_heading)))
        
        # Update AI Contacts
        for c in dynamic_contacts:
            c_rad = math.radians(c["heading"])
            c_ms = c["speed"] * KNOT_TO_MS
            c["x"] += math.sin(c_rad) * c_ms * dt
            c["z"] += math.cos(c_rad) * c_ms * dt

        # --- CORE PHYSICS ---
        rib.update(dt, ocean, sim_time, ai_wave_h, ai_thrust, ai_speed_knots)
        ocean.update(rib.pos[0], rib.pos[2], sim_time, ai_wave_h)
        sled.update(dt, rib.pos, (0.0, 0.0, 0.0))
        uuv.update(dt, sled.pos)

        # --- EXPORT TELEMETRY ---
        if current_time - last_telemetry_time > 0.5: # 2Hz telemetry limit for headless
            last_telemetry_time = current_time
            
            rx, _, rz = rib.pos
            # In Pygame world coords, Z maps to Latitude (North/South) and X maps to Longitude (East/West)
            b_lat = BOAT_START[0] + (rz / 111320.0)
            b_lon = BOAT_START[1] + (rx / (111320.0 * math.cos(math.radians(BOAT_START[0]))))
            
            # Compile Dynamic Radar Contacts for payload
            radar_export = []
            for c in dynamic_contacts:
                c_lat = BOAT_START[0] + (c["z"] / 111320.0)
                c_lon = BOAT_START[1] + (c["x"] / (111320.0 * math.cos(math.radians(BOAT_START[0]))))
                radar_export.append({
                    "id": c["id"],
                    "type": c["type"],
                    "lat": round(c_lat, 6),
                    "lon": round(c_lon, 6),
                    "speed": round(c["speed"], 1),
                    "heading": round(c["heading"], 1)
                })
            
            # Capture Sonar Depth Grid
            sonar_grid = np.zeros((10, 10))
            grid_center_x, grid_center_z = sled.pos[0], sled.pos[2]
            for row in range(10):
                for col in range(10):
                    sample_x = grid_center_x + (col - 5) * 8
                    sample_z = grid_center_z + (row - 5) * 8
                    depth_val = np.clip(get_math_depth(sample_x, sample_z), -100.0, 50.0)
                    sonar_grid[row][col] = depth_val

            # Payload Construction
            payload = {
                "BOAT_LAT": round(b_lat, 5), "BOAT_LON": round(b_lon, 5),
                "CREW_LAT": round(crew_loc_lat, 5) if crew_loc_lat != 0.0 else round(CREW_START[0], 5), 
                "CREW_LON": round(crew_loc_lon, 5) if crew_loc_lon != 0.0 else round(CREW_START[1], 5), 
                "speed_kn": round(ai_speed_knots, 1),
                "sea_state": round(ai_wave_h, 1),
                "sled_depth": round(float(SHIP_DEPTH - sled.pos[1]), 1),
                "sonar_grid": sonar_grid.astype(int).tolist(),
                "radar_contacts": radar_export
            }
            threading.Thread(target=post_telemetry, args=(payload,), daemon=True).start()

        # Throttle Headless Loop (~20fps logic lock)
        time.sleep(0.05)

if __name__ == "__main__":
    run_headless_sim()
