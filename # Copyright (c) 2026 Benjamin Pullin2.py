# Copyright (c) 2026 Benjamin Pullin. All rights reserved.
import pygame
import numpy as np
from OpenGL.GL import *
from OpenGL.GLU import *
import math, time, random, threading, requests
from datetime import datetime
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- System Parameters & Config ---
WIDTH, HEIGHT = 1600, 900
GRID_DIM = 120       
SCALE = 22           
SHIP_DEPTH = 0.0  
MAX_WAVE_HEIGHT = 4.0    
MAX_SLED_DEPTH = 100.0

# --- Tactical Config ---
MAX_KNOTS = 15.0  
KNOT_TO_MS = 0.514444
BOAT_START = (-38.2, 144.9) # Port Phillip Bay Vic
CREW_START = (-38.2, 144.9)

# --- Proxy Telemetry Configurations ---
LIVE_MODE = False
if LIVE_MODE:
    REMOTE_INGRESS = "https://benfishmanta.duckdns.org/sim_ingress"
    REMOTE_TELEM = "https://benfishmanta.duckdns.org/telemetry"
else:
    REMOTE_INGRESS = "http://localhost:5000/sim_ingress"
    REMOTE_TELEM = "http://localhost:5000/telemetry"

watch_uuv_state = "RECALL"
crew_loc_lat = 0.0
crew_loc_lon = 0.0
live_ais_contacts = []

# --- 100 Tactical Scenarios ---
SCENARIOS = [
    {"name": "Port Phillip Bay Vic (Home)", "lat": -38.2, "lon": 144.9, "threat": "LOW"},
    {"name": "Refuge Cove (Wilsons Prom)", "lat": -39.039, "lon": 146.458, "threat": "LOW"},
    {"name": "Sydney Harbour (High Traffic)", "lat": -33.85, "lon": 151.27, "threat": "LOW"},
    {"name": "Strait of Hormuz (Chokepoint)", "lat": 26.56, "lon": 56.25, "threat": "EXTREME"},
    {"name": "Bab el-Mandeb (Red Sea)", "lat": 12.58, "lon": 43.33, "threat": "EXTREME"},
    {"name": "Taiwan Strait (Tension Zone)", "lat": 24.0, "lon": 119.0, "threat": "EXTREME"},
    {"name": "English Channel (High Traffic)", "lat": 50.1, "lon": -1.5, "threat": "MEDIUM"},
    {"name": "Cape of Good Hope", "lat": -34.35, "lon": 18.47, "threat": "MEDIUM"},
    {"name": "Mariana Trench", "lat": 11.35, "lon": 142.2, "threat": "LOW"}
]

# --- Proxy Endpoints ---
REMOTE_URL = "https://benfishmanta.duckdns.org/sim_ingress"
STATUS_URL = "https://benfishmanta.duckdns.org/status_api"
GEMINI_URL = "https://benfishmanta.duckdns.org/gemini"
AUDIO_URL = "https://benfishmanta.duckdns.org/audio"
AUTH_TOKEN = "BENFISH_ACTUAL_77X"
FIRMS_MAP_KEY = ""

# --- Shared State ---
last_audio_ts = 0
osint_thermal_anomalies = []
last_warp_ts = 0
pending_warp = None
terrain_cache = None
cache_lat_min = 0.0
cache_lat_max = 0.0
cache_lon_min = 0.0
cache_lon_max = 0.0

# --- Color Palette (RGB 0-255) ---
SONAR_CYAN = (0, 180, 255)
SONAR_DIM = (0, 40, 60)
SURFACE_BLUE = (0, 100, 150)
CABLE_ORANGE = (255, 140, 0)
SLED_WHITE = (220, 230, 255)
VECTOR_RED = (255, 50, 50)
TARGET_GREEN = (50, 255, 100)
UUV_MAGENTA = (255, 50, 255)

def gl_color(c):
    cr, cg, cb = c
    return (cr/255.0, cg/255.0, cb/255.0)

def euler_matrix(yaw, pitch, roll):
    cy, sy = float(np.cos(yaw)), float(np.sin(yaw))
    cp, sp = float(np.cos(pitch)), float(np.sin(pitch))
    cr, sr = float(np.cos(roll)), float(np.sin(roll))
    Rx = np.array(((1.0, 0.0, 0.0), (0.0, cp, -sp), (0.0, sp, cp)))
    Ry = np.array(((cy, 0.0, sy), (0.0, 1.0, 0.0), (-sy, 0.0, cy)))
    Rz = np.array(((cr, -sr, 0.0), (sr, cr, 0.0), (0.0, 0.0, 1.0)))
    mat_3x3 = Ry @ Rx @ Rz
    mat_4x4 = np.eye(4, dtype=np.float32)
    mat_4x4[:3, :3] = mat_3x3
    return mat_4x4

def fetch_terrain_grid(center_lat, center_lon):
    global terrain_cache, cache_lat_min, cache_lat_max, cache_lon_min, cache_lon_max
    points = 10
    size_km = 8.0 
    
    lat_deg_per_km = 1.0 / 111.32
    lon_deg_per_km = 1.0 / (111.32 * math.cos(math.radians(center_lat)))
    
    half_size = size_km / 2.0
    cache_lat_min = center_lat - (half_size * lat_deg_per_km)
    cache_lat_max = center_lat + (half_size * lat_deg_per_km)
    cache_lon_min = center_lon - (half_size * lon_deg_per_km)
    cache_lon_max = center_lon + (half_size * lon_deg_per_km)
    
    lats = np.linspace(cache_lat_min, cache_lat_max, points)
    lons = np.linspace(cache_lon_min, cache_lon_max, points)
    
    req_lats, req_lons = [], []
    for lat in lats:
        for lon in lons:
            req_lats.append(round(lat, 4))
            req_lons.append(round(lon, 4))
            
    lat_str = ",".join(map(str, req_lats))
    lon_str = ",".join(map(str, req_lons))
    
    url = f"https://api.open-meteo.com/v1/elevation?latitude={lat_str}&longitude={lon_str}"
    try:
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            elevations = r.json().get("elevation", [])
            if len(elevations) == points * points:
                terrain_cache = np.array(elevations, dtype=np.float32).reshape((points, points))
                print("DEBUG: Fetched real-world terrain from Open-Meteo!")
    except Exception as e:
        print(f"Terrain Fetch Error: {e}")

def get_math_depth(world_x, world_z):
    wx = np.array(world_x, dtype=np.float32)
    wz = np.array(world_z, dtype=np.float32)
    
    depth = -60.0 
    depth += np.sin(wx * 0.0004) * np.cos(wz * 0.0005) * 85.0
    depth += np.sin(wx * 0.0018) * np.cos(wz * 0.0015) * 25.0
    depth += np.sin(wx * 0.01) * np.cos(wz * 0.008) * 6.0
    depth = np.clip(depth, -180.0, 100.0)
    
    global terrain_cache
    if terrain_cache is not None:
        c_lat = BOAT_START[0] + (wx / 111320.0)
        c_lon = BOAT_START[1] + (wz / (111320.0 * math.cos(math.radians(BOAT_START[0]))))
        lat_t = (c_lat - cache_lat_min) / max(1e-6, cache_lat_max - cache_lat_min)
        lon_t = (c_lon - cache_lon_min) / max(1e-6, cache_lon_max - cache_lon_min)
        lat_t = np.clip(lat_t, 0.0, 0.999)
        lon_t = np.clip(lon_t, 0.0, 0.999)
        lat_idx = np.clip(np.array(lat_t * 10, dtype=int), 0, 9)
        lon_idx = np.clip(np.array(lon_t * 10, dtype=int), 0, 9)
        real_elev = terrain_cache[lat_idx, lon_idx]
        depth = np.where(real_elev > 0.5, real_elev, depth)
        
    return depth if isinstance(world_x, (np.ndarray, list)) else float(depth)

_cached_font = None
def draw_text_3d(text, x, y, z, color, surface_w=WIDTH, surface_h=HEIGHT, base_scale=1.0):
    global _cached_font
    if _cached_font is None:
        _cached_font = pygame.font.SysFont('Consolas', 22, bold=True)
    text_surface = _cached_font.render(text, True, color)
    tw, th = text_surface.get_size()
    pad = 8
    alpha_surf = pygame.Surface((tw + pad*2, th + pad*2), pygame.SRCALPHA)
    alpha_surf.fill((0, 30, 50, 190))
    pygame.draw.rect(alpha_surf, color, alpha_surf.get_rect(), 2)
    alpha_surf.blit(text_surface, (pad, pad))
    modelview = glGetDoublev(GL_MODELVIEW_MATRIX)
    projection = glGetDoublev(GL_PROJECTION_MATRIX)
    viewport = glGetIntegerv(GL_VIEWPORT)
    try:
        win_x, win_y, win_z = gluProject(x, y, z, modelview, projection, viewport)
    except:
        return
    on_screen = True
    if win_z > 1.0 or win_z < 0.0: on_screen = False
    if win_x < 0 or win_x > surface_w: on_screen = False
    if win_y < 0 or win_y > surface_h: on_screen = False
    
    if on_screen:
        text_data = pygame.image.tostring(alpha_surf, "RGBA", True)
        width, height = alpha_surf.get_size()
        tex = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, tex)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, width, height, 0, GL_RGBA, GL_UNSIGNED_BYTE, text_data)
        glEnable(GL_TEXTURE_2D); glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glBindTexture(GL_TEXTURE_2D, tex)
        glPushMatrix(); glTranslatef(x, y, z)
        m = glGetFloatv(GL_MODELVIEW_MATRIX)
        m[0][0] = 1.0; m[0][1] = 0.0; m[0][2] = 0.0
        m[1][0] = 0.0; m[1][1] = 1.0; m[1][2] = 0.0
        m[2][0] = 0.0; m[2][1] = 0.0; m[2][2] = 1.0
        glLoadMatrixf(m)
        scale = (0.35 + (win_z * 0.3)) * base_scale
        ws, hs = width * scale, height * scale
        glColor4f(1.0, 1.0, 1.0, 1.0)
        glBegin(GL_QUADS)
        glTexCoord2f(0, 0); glVertex3f(-ws/2, -hs/2, 0)
        glTexCoord2f(1, 0); glVertex3f(ws/2, -hs/2, 0)
        glTexCoord2f(1, 1); glVertex3f(ws/2, hs/2, 0)
        glTexCoord2f(0, 1); glVertex3f(-ws/2, hs/2, 0)
        glEnd()
        glPopMatrix(); glDisable(GL_BLEND); glDisable(GL_TEXTURE_2D); glDeleteTextures([tex])

class OnboardSystems:
    def __init__(self):
        self.engine_temp, self.battery_v, self.fuel_pct = 85.0, 24.2, 78.5
        self.rpms, self.status = 0, "NOMINAL"
    def update(self, dt, speed_knots, sea_state, ew_active, sys_state):
        if not sys_state.get("engine", True): self.rpms += (0 - self.rpms) * 0.05
        else:
            target_rpms = max(600, abs(speed_knots) * 120 + 600) if speed_knots != 0 else 600
            self.rpms += (target_rpms - self.rpms) * 0.1
        heat_gen = (self.rpms / 2000.0) * (1.0 + sea_state * 0.2)
        cooling = (abs(speed_knots) / 25.0) + 0.15 + (1.0 - (self.rpms / 3000.0)) * 0.5
        if not sys_state.get("engine", True): heat_gen = 0.0
        self.engine_temp += (heat_gen - cooling) * dt
        draw = 0.0005
        if ew_active: draw += 0.005
        if sys_state.get("sonar", True): draw += 0.002
        if self.rpms > 800: self.battery_v += 0.005 * dt
        self.battery_v -= draw * dt
        self.fuel_pct -= (self.rpms / 150000.0) * dt
        self.engine_temp = max(60.0, min(self.engine_temp, 115.0))
        self.battery_v = max(20.0, min(self.battery_v, 28.5))
        self.status = "OVERHEAT" if self.engine_temp > 105.0 else "NOMINAL"

class MantaCoreTerrainGL:
    def __init__(self):
        self.vert_count = GRID_DIM * GRID_DIM
        self.vertices = np.zeros((self.vert_count, 3), dtype=np.float32)
        self.colors = np.full((self.vert_count, 3), gl_color(SONAR_DIM), dtype=np.float32)
        self.discovered = np.zeros((GRID_DIM, GRID_DIM), dtype=bool)
        self.last_gx, self.last_gz = 0, 0
        idx_list = []
        for y in range(GRID_DIM - 1):
            for x in range(GRID_DIM - 1):
                i = y * GRID_DIM + x
                idx_list.extend((i, i + 1, i, i + GRID_DIM))
        self.indices = np.array(idx_list, dtype=np.uint32)
        self.refresh_mesh()

    def refresh_mesh(self):
        x_range = (np.arange(GRID_DIM) - float(GRID_DIM/2.0) + float(self.last_gx)) * SCALE
        z_range = (np.arange(GRID_DIM) - float(GRID_DIM/2.0) + float(self.last_gz)) * SCALE
        xx, zz = np.meshgrid(x_range, z_range)
        flat_x, flat_z = xx.flatten(), zz.flatten()
        depths = get_math_depth(flat_x, flat_z)
        colors = np.zeros((len(depths), 3), dtype=np.float32)
        colors[depths > 0.0] = gl_color((34, 139, 34)) 
        colors[(depths <= 0.0) & (depths > -20.0)] = gl_color((194, 178, 128)) 
        deep_mask = depths <= -20.0
        c_cyan_arr = np.array(gl_color(SONAR_CYAN), dtype=np.float32)
        c_dim_arr = np.array(gl_color(SONAR_DIM), dtype=np.float32)
        colors[deep_mask] = np.where(self.discovered.flatten()[deep_mask][:, None], c_cyan_arr, c_dim_arr)
        self.vertices = np.column_stack((flat_x, depths, flat_z)).astype(np.float32)
        self.colors = colors

    def update_treadmill(self, ship_pos):
        center_gx = int(np.floor(ship_pos[0] / SCALE))
        center_gz = int(np.floor(ship_pos[2] / SCALE))
        if center_gx != self.last_gx or center_gz != self.last_gz:
            dx, dz = center_gx - self.last_gx, center_gz - self.last_gz
            self.last_gx, self.last_gz = center_gx, center_gz
            self.discovered = np.roll(self.discovered, shift=(-dz, -dx), axis=(0, 1))
            if dx > 0: self.discovered[:, -dx:] = False
            elif dx < 0: self.discovered[:, :-dx] = False
            if dz > 0: self.discovered[-dz:, :] = False
            elif dz < 0: self.discovered[:-dz, :] = False
            self.refresh_mesh()

    def update_side_scan(self, sled_pos, sled_vel, sonar_active):
        if not sonar_active: return
        self.discovered[int(GRID_DIM/2)-2:int(GRID_DIM/2)+2, int(GRID_DIM/2)-2:int(GRID_DIM/2)+2] = True
        self.refresh_mesh()

    def render(self):
        glEnableClientState(GL_VERTEX_ARRAY); glEnableClientState(GL_COLOR_ARRAY)
        glVertexPointer(3, GL_FLOAT, 0, self.vertices); glColorPointer(3, GL_FLOAT, 0, self.colors)
        glDrawElements(GL_LINES, len(self.indices), GL_UNSIGNED_INT, self.indices)
        glDisableClientState(GL_COLOR_ARRAY); glDisableClientState(GL_VERTEX_ARRAY)

class Mothership:
    def __init__(self, start_x, start_z):
        self.pos = np.array((start_x, float(SHIP_DEPTH), start_z), dtype=float)
        self.vel = np.zeros(3, dtype=float)
        self.yaw, self.pitch, self.roll = 0.0, 0.0, 0.0
        self.ang_vel = np.zeros(3, dtype=float)
        
        self.mass = 20000.0         
        self.I_roll = 150000.0      
        self.I_pitch = 400000.0     
        self.I_yaw = 400000.0       
        self.mast_height = 9.0      
        self.GM = 1.8               
        self.sail_area = 85.0       
        self.rho_air, self.rho_water = 1.225, 1025.0

    def _calc_wave_forces(self, ocean, t, sea_state):
        px, py, pz = self.pos
        bow_x, bow_z = px + np.sin(self.yaw) * 6.0, pz + np.cos(self.yaw) * 6.0
        stern_x, stern_z = px - np.sin(self.yaw) * 6.0, pz - np.cos(self.yaw) * 6.0
        bow_y = ocean.get_exact_height(bow_x, bow_z, t, sea_state)
        stern_y = ocean.get_exact_height(stern_x, stern_z, t, sea_state)
        mid_y = ocean.get_exact_height(px, pz, t, sea_state)

        draft = mid_y - py
        F_heave = draft * 300000.0 - self.vel[1] * 80000.0
        wave_slope = float(np.arctan2(bow_y - stern_y, 12.0))
        M_pitch = (wave_slope - self.pitch) * 4000000.0 - self.ang_vel[1] * 1500000.0
        return F_heave, M_pitch

    def _calc_wind_forces(self):
        true_wind_speed = 15.0 * KNOT_TO_MS
        app_wind_vx, app_wind_vz = 0.0 - self.vel[0], true_wind_speed - self.vel[2]
        app_wind_speed = float(np.linalg.norm([app_wind_vx, app_wind_vz]))
        AWA_rel = (float(np.arctan2(app_wind_vx, app_wind_vz)) - self.yaw + np.pi) % (2 * np.pi) - np.pi
        
        dynamic_pressure = 0.5 * self.rho_air * app_wind_speed**2
        if abs(AWA_rel) > np.radians(35):
            C_lift, C_drag = 1.2 * np.sin(2.0 * AWA_rel), 0.15 + (1.0 - np.cos(AWA_rel))
        else: C_lift, C_drag = 0.0, 0.4
            
        Lift, Drag = dynamic_pressure * self.sail_area * C_lift, dynamic_pressure * self.sail_area * C_drag
        F_surge_wind = Lift * np.sin(abs(AWA_rel)) - Drag * np.cos(AWA_rel)
        F_sway_wind = Lift * np.cos(AWA_rel) * np.sign(AWA_rel) + Drag * np.sin(AWA_rel)
        return F_surge_wind, F_sway_wind

    def update(self, dt, ocean, t, sea_state, thrust_dir, engine_speed_knots):
        F_heave, M_pitch = self._calc_wave_forces(ocean, t, sea_state)

        current_surge = self.vel[0] * np.sin(self.yaw) + self.vel[2] * np.cos(self.yaw)
        F_surge = (engine_speed_knots * KNOT_TO_MS - current_surge) * 8000.0

        F_surge_wind, F_sway_wind = self._calc_wind_forces()

        F_surge += F_surge_wind
        F_sway = F_sway_wind
        
        boat_sway = self.vel[0] * np.cos(self.yaw) - self.vel[2] * np.sin(self.yaw)
        F_sway -= boat_sway * abs(boat_sway) * 20000.0 
        F_surge -= current_surge * abs(current_surge) * 1200.0 
        
        M_roll = F_sway * self.mast_height - (self.mass * 9.81 * self.GM * np.sin(self.roll)) - self.ang_vel[2] * 900000.0

        M_yaw = 0.0
        if np.linalg.norm(thrust_dir) > 0.01:
            M_yaw += ((float(np.arctan2(thrust_dir[0], thrust_dir[2])) - self.yaw + np.pi) % (2 * np.pi) - np.pi) * 150000.0
        M_yaw -= self.ang_vel[0] * 500000.0 
        
        F_global_x = F_surge * np.sin(self.yaw) + F_sway * np.cos(self.yaw)
        F_global_z = F_surge * np.cos(self.yaw) - F_sway * np.sin(self.yaw)
        
        self.vel += np.array([F_global_x / self.mass, (F_heave / self.mass) - 9.81, F_global_z / self.mass]) * dt
        self.pos += self.vel * dt
        self.ang_vel += np.array([M_yaw / self.I_yaw, M_pitch / self.I_pitch, M_roll / self.I_roll]) * dt
        self.yaw += self.ang_vel[0] * dt; self.pitch += self.ang_vel[1] * dt; self.roll += self.ang_vel[2] * dt
        
        ground_height = get_math_depth(self.pos[0], self.pos[2])
        if ground_height > -1.0:
            self.pos[1] = ground_height; self.vel *= 0.0; self.ang_vel *= 0.0; self.pitch *= 0.95; self.roll *= 0.95

    def get_matrix(self): 
        return euler_matrix(self.yaw, self.pitch, self.roll)

class OceanSurfaceGL:
    def __init__(self, dim, scale, base_y):
        self.dim, self.scale, self.base_y = dim, scale, base_y
        self.vert_count = dim * dim
        self.vertices = np.zeros((self.vert_count, 3), dtype=np.float32)
        self.colors = np.full((self.vert_count, 3), gl_color(SURFACE_BLUE), dtype=np.float32)
        idx_list = []
        for y in range(dim - 1):
            for x in range(dim - 1):
                i = y * dim + x
                idx_list.extend((i, i + 1, i, i + dim))
        self.indices = np.array(idx_list, dtype=np.uint32)

    def update(self, ship_x, ship_z, t, sea_state):
        snap_x = float(int(ship_x/self.scale)*self.scale)
        snap_z = float(int(ship_z/self.scale)*self.scale)
        x_range = np.linspace(-self.dim/2.0, self.dim/2.0-1.0, self.dim) * self.scale + snap_x
        z_range = np.linspace(-self.dim/2.0, self.dim/2.0-1.0, self.dim) * self.scale + snap_z
        xx, zz = np.meshgrid(x_range, z_range)
        xx_f, zz_f = xx.flatten(), zz.flatten()
        yy_f = np.full_like(xx_f, self.base_y)
        if sea_state > 0.01:
            waves = (np.sin(xx_f*0.02 + t*1.5)*1.5 + np.cos(zz_f*0.025 + t*1.2) + np.sin((xx_f+zz_f)*0.08 + t*3.0)*0.5) * sea_state
            dist = np.sqrt((xx_f - ship_x)**2 + (zz_f - ship_z)**2)
            att = np.clip(1.0 - (dist - 1500.0) / 500.0, 0.0, 1.0)
            yy_f += waves * att
        self.vertices = np.column_stack((xx_f, yy_f, zz_f)).astype(np.float32)

    def get_exact_height(self, x, z, t, sea_state):
        if sea_state <= 0.01: return float(self.base_y)
        return float(self.base_y + (np.sin(x*0.02 + t*1.5)*1.5 + np.cos(z*0.025 + t*1.2) + np.sin((x+z)*0.08 + t*3.0)*0.5) * sea_state)

    def render(self):
        glEnableClientState(GL_VERTEX_ARRAY); glVertexPointer(3, GL_FLOAT, 0, self.vertices)
        glEnableClientState(GL_COLOR_ARRAY); glColorPointer(3, GL_FLOAT, 0, self.colors)
        glDrawElements(GL_LINES, len(self.indices), GL_UNSIGNED_INT, self.indices)
        glDisableClientState(GL_COLOR_ARRAY); glDisableClientState(GL_VERTEX_ARRAY)

def draw_pyxis_monohull():
    secs = [
        (7.6,  2.2,  0.0,   0.0, 0.0,  -0.5, 0.0),   
        (5.0,  2.0,  1.4,   0.0, 1.1,  -0.7, 0.4),   
        (2.0,  1.85, 2.1,   0.0, 1.9,  -0.8, 0.7),   
        (-1.0, 1.8,  2.25,  0.0, 2.0,  -0.8, 0.7),   
        (-4.0, 1.8,  2.0,   0.0, 1.7,  -0.6, 0.5),   
        (-7.6, 1.9,  1.5,   0.1, 1.2,  -0.1, 0.0)    
    ]
    glColor3f(0.08, 0.12, 0.20)
    glBegin(GL_QUADS)
    for i in range(len(secs)-1):
        z1, dy1, dx1, wy1, wx1, _, _ = secs[i]; z2, dy2, dx2, wy2, wx2, _, _ = secs[i+1]
        glVertex3f(dx1, dy1, z1); glVertex3f(dx2, dy2, z2); glVertex3f(wx2, wy2, z2); glVertex3f(wx1, wy1, z1)
        glVertex3f(-dx1, dy1, z1); glVertex3f(-dx1, wy1, z1); glVertex3f(-dx2, wy2, z2); glVertex3f(-dx2, dy2, z2)
    glEnd()
    glColor3f(0.9, 0.9, 0.9)
    glBegin(GL_QUADS)
    for i in range(len(secs)-1):
        z1, _, _, wy1, wx1, _, _ = secs[i]; z2, _, _, wy2, wx2, _, _ = secs[i+1]
        glVertex3f(wx1, wy1, z1); glVertex3f(wx2, wy2, z2); glVertex3f(wx2, wy2-0.1, z2); glVertex3f(wx1, wy1-0.1, z1)
        glVertex3f(-wx1, wy1, z1); glVertex3f(-wx1, wy1-0.1, z1); glVertex3f(-wx2, wy2-0.1, z2); glVertex3f(-wx2, wy2, z2)
    glEnd()
    glColor3f(0.5, 0.1, 0.1)
    glBegin(GL_QUADS)
    for i in range(len(secs)-1):
        z1, _, _, wy1, wx1, by1, bx1 = secs[i]; z2, _, _, wy2, wx2, by2, bx2 = secs[i+1]
        glVertex3f(wx1, wy1-0.1, z1); glVertex3f(wx2, wy2-0.1, z2); glVertex3f(bx2, by2, z2); glVertex3f(bx1, by1, z1)
        glVertex3f(-wx1, wy1-0.1, z1); glVertex3f(-bx1, by1, z1); glVertex3f(-bx2, by2, z2); glVertex3f(-wx2, wy2-0.1, z2)
        glVertex3f(bx1, by1, z1); glVertex3f(bx2, by2, z2); glVertex3f(0.0, by2, z2); glVertex3f(0.0, by1, z1)
        glVertex3f(-bx1, by1, z1); glVertex3f(0.0, by1, z1); glVertex3f(0.0, by2, z2); glVertex3f(-bx2, by2, z2)
    glEnd()
    glColor3f(0.08, 0.12, 0.20)
    glBegin(GL_POLYGON)
    z_t, dy_t, dx_t, wy_t, wx_t, by_t, bx_t = secs[-1]
    glVertex3f(dx_t, dy_t, z_t); glVertex3f(-dx_t, dy_t, z_t); glVertex3f(-wx_t, wy_t, z_t); glVertex3f(wx_t, wy_t, z_t)
    glEnd()
    glColor3f(0.5, 0.1, 0.1)
    glBegin(GL_TRIANGLES)
    glVertex3f(wx_t, wy_t, z_t); glVertex3f(-wx_t, wy_t, z_t); glVertex3f(0.0, by_t, z_t)
    glEnd()
    glColor3f(0.65, 0.65, 0.6)
    glBegin(GL_POLYGON)
    for s in secs: glVertex3f(s[2], s[1], s[0])
    for s in reversed(secs): glVertex3f(-s[2], s[1], s[0])
    glEnd()
    glColor3f(0.85, 0.85, 0.9)
    glBegin(GL_QUADS)
    glVertex3f( 1.2, 1.8,  1.0); glVertex3f(-1.2, 1.8,  1.0); glVertex3f(-1.0, 2.6,  0.0); glVertex3f( 1.0, 2.6,  0.0)
    glVertex3f( 1.0, 2.6,  0.0); glVertex3f(-1.0, 2.6,  0.0); glVertex3f(-1.0, 2.6, -3.5); glVertex3f( 1.0, 2.6, -3.5)
    glVertex3f( 1.2, 1.8,  1.0); glVertex3f( 1.0, 2.6,  0.0); glVertex3f( 1.0, 2.6, -3.5); glVertex3f( 1.2, 1.8, -3.5)
    glVertex3f(-1.2, 1.8,  1.0); glVertex3f(-1.2, 1.8, -3.5); glVertex3f(-1.0, 2.6, -3.5); glVertex3f(-1.0, 2.6,  0.0)
    glVertex3f( 1.2, 1.8, -3.5); glVertex3f( 1.0, 2.6, -3.5); glVertex3f(-1.0, 2.6, -3.5); glVertex3f(-1.2, 1.8, -3.5)
    glEnd()
    glColor3f(0.1, 0.1, 0.15)
    glBegin(GL_QUADS)
    glVertex3f( 0.9, 2.0,  0.75); glVertex3f(-0.9, 2.0,  0.75); glVertex3f(-0.8, 2.4,  0.1); glVertex3f( 0.8, 2.4,  0.1)
    glVertex3f( 1.1, 2.0,  0.7); glVertex3f( 0.95, 2.4,  0.0); glVertex3f( 0.95, 2.4, -2.5); glVertex3f( 1.1, 2.0, -2.5)
    glVertex3f(-1.1, 2.0,  0.7); glVertex3f(-1.1, 2.0, -2.5); glVertex3f(-0.95, 2.4, -2.5); glVertex3f(-0.95, 2.4,  0.0)
    glEnd()
    glColor3f(0.15, 0.15, 0.15)
    glBegin(GL_QUADS)
    glVertex3f( 0.1, -0.8,  1.0); glVertex3f(-0.1, -0.8,  1.0); glVertex3f(-0.1, -2.5,  0.0); glVertex3f( 0.1, -2.5,  0.0)
    glVertex3f( 0.1, -0.8, -1.0); glVertex3f( 0.1, -2.5, -0.5); glVertex3f(-0.1, -2.5, -0.5); glVertex3f(-0.1, -0.8, -1.0)
    glVertex3f( 0.1, -0.8,  1.0); glVertex3f( 0.1, -2.5,  0.0); glVertex3f( 0.1, -2.5, -0.5); glVertex3f( 0.1, -0.8, -1.0)
    glVertex3f(-0.1, -0.8,  1.0); glVertex3f(-0.1, -0.8, -1.0); glVertex3f(-0.1, -2.5, -0.5); glVertex3f(-0.1, -2.5,  0.0)
    glVertex3f( 0.3, -2.3,  0.5); glVertex3f(-0.3, -2.3,  0.5); glVertex3f(-0.3, -2.7,  0.2); glVertex3f( 0.3, -2.7,  0.2)
    glVertex3f( 0.3, -2.3, -1.5); glVertex3f( 0.3, -2.7, -1.2); glVertex3f(-0.3, -2.7, -1.2); glVertex3f(-0.3, -2.3, -1.5)
    glVertex3f( 0.3, -2.3,  0.5); glVertex3f( 0.3, -2.7,  0.2); glVertex3f( 0.3, -2.7, -1.2); glVertex3f( 0.3, -2.3, -1.5)
    glVertex3f(-0.3, -2.3,  0.5); glVertex3f(-0.3, -2.3, -1.5); glVertex3f(-0.3, -2.7, -1.2); glVertex3f(-0.3, -2.7,  0.2)
    glEnd()
    glBegin(GL_QUADS)
    glVertex3f( 0.05, -0.2, -6.0); glVertex3f(-0.05, -0.2, -6.0); glVertex3f(-0.05, -1.8, -6.2); glVertex3f( 0.05, -1.8, -6.2)
    glVertex3f( 0.05, -0.2, -7.0); glVertex3f( 0.05, -1.8, -6.8); glVertex3f(-0.05, -1.8, -6.8); glVertex3f(-0.05, -0.2, -7.0)
    glVertex3f( 0.05, -0.2, -6.0); glVertex3f( 0.05, -1.8, -6.2); glVertex3f( 0.05, -1.8, -6.8); glVertex3f( 0.05, -0.2, -7.0)
    glVertex3f(-0.05, -0.2, -6.0); glVertex3f(-0.05, -0.2, -7.0); glVertex3f(-0.05, -1.8, -6.8); glVertex3f(-0.05, -1.8, -6.2)
    glEnd()
    mast_h = 19.0
    glColor3f(0.8, 0.8, 0.8)
    glLineWidth(4.0); glBegin(GL_LINES); glVertex3f(0.0, 1.8, 2.0); glVertex3f(0.0, mast_h, 1.5); glEnd()
    glLineWidth(3.0); glBegin(GL_LINES); glVertex3f(0.0, 3.0, 1.9); glVertex3f(0.0, 3.2, -5.0); glEnd()
    glBegin(GL_LINE_STRIP); glVertex3f( 1.4, 1.9, -7.0); glVertex3f( 1.2, 4.0, -7.2); glVertex3f(-1.2, 4.0, -7.2); glVertex3f(-1.4, 1.9, -7.0); glEnd()
    glColor4f(0.95, 0.95, 0.95, 0.85); glEnable(GL_BLEND); glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
    glBegin(GL_TRIANGLES)
    glVertex3f(0.0, mast_h - 0.5, 1.45); glVertex3f(0.0, 3.2, -4.8); glVertex3f(0.0, 3.0, 1.8)
    glVertex3f(0.0, mast_h - 1.0, 1.55); glVertex3f(0.0, 2.2, 7.3); glVertex3f(-1.4, 2.0, 2.0)
    glVertex3f(0.0, mast_h - 6.0, 1.65); glVertex3f(0.0, 2.2, 4.0); glVertex3f(-0.8, 2.0, 1.0)
    glEnd(); glDisable(GL_BLEND)

def render_ui_to_opengl(surface, tex_id, screen_w, screen_h):
    texture_data = pygame.image.tobytes(surface, "RGBA", False) 
    glDisable(GL_DEPTH_TEST); glDisable(GL_LIGHTING); glEnable(GL_TEXTURE_2D)
    glEnable(GL_BLEND); glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
    glBindTexture(GL_TEXTURE_2D, tex_id)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, screen_w, screen_h, 0, GL_RGBA, GL_UNSIGNED_BYTE, texture_data)
    glMatrixMode(GL_PROJECTION); glPushMatrix(); glLoadIdentity()
    glOrtho(0, screen_w, screen_h, 0, -1, 1); glMatrixMode(GL_MODELVIEW); glPushMatrix(); glLoadIdentity()
    glColor4f(1.0, 1.0, 1.0, 1.0)
    glBegin(GL_QUADS)
    glTexCoord2f(0.0, 0.0); glVertex3f(0.0, 0.0, 0.0)
    glTexCoord2f(1.0, 0.0); glVertex3f(screen_w, 0.0, 0.0)
    glTexCoord2f(1.0, 1.0); glVertex3f(screen_w, screen_h, 0.0)
    glTexCoord2f(0.0, 1.0); glVertex3f(0.0, screen_h, 0.0)
    glEnd()
    glPopMatrix(); glMatrixMode(GL_PROJECTION); glPopMatrix(); glMatrixMode(GL_MODELVIEW)
    glDisable(GL_BLEND); glDisable(GL_TEXTURE_2D); glEnable(GL_DEPTH_TEST)

def run_unified_sim():
    global BOAT_START, WIDTH, HEIGHT
    pygame.init()
    WIDTH, HEIGHT = 1600, 900
    pygame.display.set_mode((WIDTH, HEIGHT), pygame.OPENGL | pygame.DOUBLEBUF | pygame.RESIZABLE)
    ui_surface = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
    ui_tex_id = glGenTextures(1)
    clock = pygame.time.Clock()
    font = pygame.font.SysFont("monospace", 16)
    title_font = pygame.font.SysFont("monospace", 18, bold=True)
    
    terrain = MantaCoreTerrainGL()
    ocean = OceanSurfaceGL(480, SCALE*0.833, SHIP_DEPTH)
    rib = Mothership(0.0, 0.0)
    vessel_systems = OnboardSystems()
    
    yaw, pitch, dist, wave_h, t = 45.0, 20.0, 600.0, 1.5, 0.0
    ship_speed_knots = 10.0  
    cruise_active = True
    sys_state = {"engine": True, "sonar": True, "comms": True}
    
    while True:
        dt = clock.tick(60)/1000.0
        if dt > 0.1: dt = 0.1
        t += dt
        
        rx, ry, rz = rib.pos
        
        glClearColor(0.008, 0.02, 0.04, 1.0) 
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        glEnable(GL_DEPTH_TEST)
        
        keys = pygame.key.get_pressed()
        if keys[pygame.K_LEFT]: rib.yaw -= (2.5 * dt)
        if keys[pygame.K_RIGHT]: rib.yaw += (2.5 * dt)
        
        if sys_state.get("engine", True):
            if keys[pygame.K_UP]: ship_speed_knots = min(ship_speed_knots + (10.0 * dt), 45.0)
            if keys[pygame.K_DOWN]: ship_speed_knots = max(ship_speed_knots - (15.0 * dt), -10.0)
        else: ship_speed_knots *= 0.98 

        eff_speed = ship_speed_knots if cruise_active and sys_state.get("engine", True) else 0.0
        vessel_systems.update(dt, eff_speed, wave_h, False, sys_state)

        for e in pygame.event.get():
            if e.type == pygame.QUIT: return
            if e.type == pygame.MOUSEWHEEL: dist = float(np.clip(dist - e.y*50, 100, 5000))
            if e.type == pygame.KEYDOWN:
                if e.key == pygame.K_SPACE: cruise_active = not cruise_active

        engine_thrust_knots = ship_speed_knots if (sys_state.get("engine", True) and cruise_active) else 0.0
        rib.update(dt, ocean, t, wave_h, np.zeros(3), engine_thrust_knots)
        terrain.update_treadmill((rx, ry, rz))
        ocean.update(rx, rz, t, wave_h)

        glViewport(0, 0, WIDTH, HEIGHT)
        glMatrixMode(GL_PROJECTION); glLoadIdentity()
        gluPerspective(45, (WIDTH/HEIGHT), 0.1, 60000.0)
        glMatrixMode(GL_MODELVIEW); glLoadIdentity()
        
        cam_x = np.cos(np.radians(pitch))*np.sin(np.radians(yaw))*dist
        cam_y = np.sin(np.radians(pitch))*dist
        cam_z = np.cos(np.radians(pitch))*np.cos(np.radians(yaw))*dist
        gluLookAt(rx+cam_x, SHIP_DEPTH+cam_y, rz+cam_z, rx, SHIP_DEPTH, rz, 0.0, 1.0, 0.0)
        
        terrain.render(); ocean.render()

        glPushMatrix(); glTranslatef(rx, ry, rz)
        glMultMatrixf(rib.get_matrix().T.flatten())
        draw_pyxis_monohull()
        glPopMatrix()

        ui_surface.fill((0,0,0,0))
        pygame.draw.rect(ui_surface, (2,10,20,200), (0,0,WIDTH,80))
        ui_surface.blit(title_font.render("50ft VANDSTADT SAILING VESSEL (UNIFIED)", True, (255,100,100)), (30,15))
        ui_surface.blit(font.render(f"SPD: {eff_speed:.1f} kn | RPM: {vessel_systems.rpms:.0f} | BATT: {vessel_systems.battery_v:.1f}V", True, SONAR_CYAN), (30,45))

        render_ui_to_opengl(ui_surface, ui_tex_id, WIDTH, HEIGHT)
        pygame.display.flip()

if __name__ == "__main__": 
    run_unified_sim()