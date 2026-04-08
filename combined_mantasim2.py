# Copyright (c) 2026 Benjamin Pullin. All rights reserved.
import pygame
import numpy as np
from OpenGL.GL import *
from OpenGL.GLU import *
import math, time, random, threading, requests
from datetime import datetime

# --- System Parameters & Config ---
WIDTH, HEIGHT = 1400, 900
GRID_DIM = 120       
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
SIM_SPEED_MULT = 1.0       # Simulated-seconds per real-second (PageUp/Down)
MIN_SPEED_MULT = 1.0
MAX_SPEED_MULT = 10000.0

# --- Autopilot Paths (lists of SCENARIOS indices) ---
AUTOPILOT_PATHS = [
    {
        "name": "GLOBAL THREAT TOUR",
        "color": (255, 80, 80),
        "waypoints": [0, 7, 4, 5, 6],          # Phil Bay -> Malacca -> Hormuz -> Bab el-Mandeb -> Suez
    },
    {
        "name": "PACIFIC WAR GAME",
        "color": (80, 160, 255),
        "waypoints": [0, 1, 80, 9, 8, 76],     # Phil Bay -> Sydney -> Phil Sea -> Taiwan -> S.China -> Kuril
    },
    {
        "name": "ARCTIC RECON",
        "color": (180, 230, 255),
        "waypoints": [0, 97, 17, 18, 75, 2],   # Phil Bay -> Norwegian -> Barents -> Kola -> Arctic
    },
    {
        "name": "PIRACY RUN",
        "color": (255, 210, 0),
        "waypoints": [0, 7, 10, 5, 117],       # Phil Bay -> Malacca -> Gulf of Aden -> Bab el-Mandeb -> Red Sea
    },
    {
        "name": "COASTAL PATROL",
        "color": (80, 220, 120),
        "waypoints": [0, 1, 33, 85, 14],       # Phil Bay -> Sydney -> Cook Strait -> Tasman -> Cape of Good Hope
    },
]


REMOTE_URL = "https://benfishmanta.duckdns.org/sim_ingress"
STATUS_URL = "https://benfishmanta.duckdns.org/status_api"
GEMINI_URL = "https://benfishmanta.duckdns.org/gemini"
AUDIO_URL = "https://benfishmanta.duckdns.org/audio"
AUTH_TOKEN = "BENFISH_ACTUAL_77X"

# --- Shared State ---
last_audio_ts = 0
watch_uuv_state = "RECALL" 
crew_loc_lat = 0.0
crew_loc_lon = 0.0
live_ais_contacts = []
last_warp_ts = 0
pending_warp = None

# --- Live Proxy Feed State (polled every 10s from /status_api) ---
live_proxy_data = {
    # Environmental / CMEMS
    "wave_h":      None,   # Sig wave height m
    "wave_period": None,   # Mean period s
    "wave_dir":    None,   # Mean wave direction deg
    "sst_c":       None,   # Sea surface temp C
    "curr_v":      None,   # Current speed knots
    "curr_dir":    None,   # Current direction deg
    # Propulsion
    "rpm":         None,   # Engine RPM
    "coolant_c":   None,   # Coolant temp C
    "oil_psi":     None,   # Oil pressure PSI
    "egt_c":       None,   # Exhaust gas temp C
    "er_temp_c":   None,   # Engine room temp C
    # Power grid
    "house_v":     None,   # House battery volts
    "house_soc":   None,   # House battery SOC %
    "house_amps":  None,   # House battery amps
    "start_v":     None,   # Start battery volts
    "alt_v":       None,   # Alternator volts
    "solar_w":     None,   # Solar watts
    "wind_gen_w":  None,   # Wind gen watts
    "inverter_w":  None,   # Inverter draw watts
    "gen_status":  None,   # Generator ON/OFF
    # Fluids
    "fuel_pct":    None,   # Fuel %
    "fresh_water_pct": None, # Fresh water %
    "bilge_status": None,  # Bilge OK/ALARM
    "er_bilge":    None,   # Engine room bilge
    # Navigation
    "sog_kts":     None,   # Speed over ground
    "depth_m":     None,   # Depth under keel
    "aws_kts":     None,   # Apparent wind kts
    "awa_deg":     None,   # Apparent wind angle
    "pressure_hpa": None,  # Barometric hPa
    "last_update": 0,
}
proxy_lock  = threading.Lock()
proxy_online = False

# --- Autopilot State ---
autopilot_active  = False
current_path_idx  = 0    # Which AUTOPILOT_PATHS entry is selected
current_wp_idx    = 0    # Which waypoint within the active path


SONAR_CYAN = (0, 180, 255)
SONAR_DIM = (0, 40, 60)
SURFACE_BLUE = (0, 100, 150)
CABLE_ORANGE = (255, 140, 0)
SLED_WHITE = (220, 230, 255)
VECTOR_RED = (255, 50, 50)
TARGET_GREEN = (50, 255, 100)
UUV_MAGENTA = (255, 50, 255)

def draw_detailed_sled_model(wing_pitch_radians):
    radius = 0.3
    glColor3f(0.8, 0.8, 0.9)
    for z in [1.5, 1.0, 0.0, -1.0, -1.5]:
        glBegin(GL_LINE_LOOP)
        for i in range(8):
            theta = i * (math.pi / 4.0)
            glVertex3f(radius * math.cos(theta), radius * math.sin(theta), z)
        glEnd()
    
    glBegin(GL_LINES)
    for i in range(8):
        theta = i * (math.pi / 4.0)
        cx, cy = radius * math.cos(theta), radius * math.sin(theta)
        glVertex3f(cx, cy, 1.5); glVertex3f(cx, cy, -1.5)
    glEnd()

    glBegin(GL_LINES)
    for i in range(8):
        theta = i * (math.pi / 4.0)
        glVertex3f(radius * math.cos(theta), radius * math.sin(theta), 1.5)
        glVertex3f(0.0, 0.0, 2.0) 
    glEnd()
    
    glColor3f(1.0, 0.5, 0.0)
    glBegin(GL_LINE_LOOP)
    glVertex3f(0.0, 0.05, 2.0); glVertex3f(0.05, 0.0, 2.0)
    glVertex3f(0.0, -0.05, 2.0); glVertex3f(-0.05, 0.0, 2.0)
    glEnd()

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

def get_math_depth(world_x, world_z):
    # Convert local x,z to approximate Lat/Lon
    lat_offset = (world_x) / 111320.0
    lon_offset = (world_z) / 92000.0
    current_lat = BOAT_START[0] + lat_offset
    current_lon = BOAT_START[1] + lon_offset
    
    wx = np.array(world_x, dtype=np.float32)
    wz = np.array(world_z, dtype=np.float32)
    
    # Base depth starts in a safe shipping channel
    depth = -60.0 
    
    # 1. Continents/Islands (Low frequency, high amplitude)
    depth += np.sin(wx * 0.0004) * np.cos(wz * 0.0005) * 85.0
    
    # 2. Coastal Shelves & Shoals (Medium frequency)
    depth += np.sin(wx * 0.0018) * np.cos(wz * 0.0015) * 25.0
    
    # 3. Rocky Seabed Detail (High frequency)
    depth += np.sin(wx * 0.01) * np.cos(wz * 0.008) * 6.0
    
    # Flatten the extreme deep ocean to prevent infinite abysses
    depth = np.clip(depth, -180.0, 100.0)
    
    return depth

_cached_font = None
def draw_text_3d(text, x, y, z, color, surface_w=WIDTH, surface_h=HEIGHT):
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
        glEnable(GL_TEXTURE_2D)
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glBindTexture(GL_TEXTURE_2D, tex)
        glPushMatrix()
        glTranslatef(x, y, z)
        m = glGetFloatv(GL_MODELVIEW_MATRIX)
        m[0][0] = 1.0; m[0][1] = 0.0; m[0][2] = 0.0
        m[1][0] = 0.0; m[1][1] = 1.0; m[1][2] = 0.0
        m[2][0] = 0.0; m[2][1] = 0.0; m[2][2] = 1.0
        glLoadMatrixf(m)
        scale = 0.35 + (win_z * 0.3)
        ws, hs = width * scale, height * scale
        glColor4f(1.0, 1.0, 1.0, 1.0)
        glBegin(GL_QUADS)
        glTexCoord2f(0, 0); glVertex3f(-ws/2, -hs/2, 0)
        glTexCoord2f(1, 0); glVertex3f(ws/2, -hs/2, 0)
        glTexCoord2f(1, 1); glVertex3f(ws/2, hs/2, 0)
        glTexCoord2f(0, 1); glVertex3f(-ws/2, hs/2, 0)
        glEnd()
        glPopMatrix()
        glDisable(GL_BLEND)
        glDisable(GL_TEXTURE_2D)
        glDeleteTextures([tex])
    else:
        disp = pygame.display.get_surface()
        if disp:
            cw, ch = disp.get_size()
            py_y = surface_h - win_y
            margin = 35
            draw_x = max(margin, min(win_x - (tw/2), cw - tw - margin))
            draw_y = max(margin, min(py_y - (th/2), ch - th - margin))
            disp.blit(alpha_surf, (draw_x, draw_y))

# --- Engineering / Onboard Systems ---
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
        self._live       = False   # True when using proxy data

    def update(self, dt, speed_knots, sea_state, ew_active, sys_state):
        # --- Try to pull live values from proxy ---
        with proxy_lock:
            _d = dict(live_proxy_data)
        _live_rpm  = _d.get("rpm")
        _live_cool = _d.get("coolant_c")
        _live_age  = _d.get("last_update") or 0
        self._live = (time.time() - _live_age) < 60  # data is fresh if < 60s old

        if self._live and _live_rpm is not None:
            # --- Mirror live proxy values ---
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
            # --- Fallback: self-simulate ---
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

        # Safety clamps (apply regardless of source)
        self.engine_temp = max(60.0, min(self.engine_temp, 200.0))
        self.battery_v   = max(10.0, min(self.battery_v,   30.0))
        self.fuel_pct    = max(0.0,  min(self.fuel_pct,   100.0))

        # Status derivation
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

# --- Simulation Classes ---
class MantaCoreTerrainGL:
    def __init__(self):
        self.vert_count = GRID_DIM * GRID_DIM
        self.vertices = np.zeros((self.vert_count, 3), dtype=np.float32)
        self.colors = np.full((self.vert_count, 3), gl_color(SONAR_DIM), dtype=np.float32)
        self.discovered = np.zeros((GRID_DIM, GRID_DIM), dtype=bool)
        self.last_gx = 0
        self.last_gz = 0
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
        land_mask = depths > 0.0
        colors[land_mask] = gl_color((34, 139, 34)) 
        shallow_mask = (depths <= 0.0) & (depths > -20.0)
        colors[shallow_mask] = gl_color((194, 178, 128)) 
        deep_mask = depths <= -20.0
        disc_expanded = self.discovered.flatten()
        c_cyan_arr = np.array(gl_color(SONAR_CYAN), dtype=np.float32)
        c_dim_arr = np.array(gl_color(SONAR_DIM), dtype=np.float32)
        colors[deep_mask] = np.where(disc_expanded[deep_mask][:, None], c_cyan_arr, c_dim_arr)
        self.vertices = np.column_stack((flat_x, depths, flat_z)).astype(np.float32)
        self.colors = colors

    def update_treadmill(self, ship_pos):
        sx, sy, sz = ship_pos
        center_gx = int(np.floor(sx / SCALE))
        center_gz = int(np.floor(sz / SCALE))
        if center_gx != self.last_gx or center_gz != self.last_gz:
            dx = center_gx - self.last_gx
            dz = center_gz - self.last_gz
            self.last_gx = center_gx
            self.last_gz = center_gz
            self.discovered = np.roll(self.discovered, shift=(-dz, -dx), axis=(0, 1))
            if dx > 0: self.discovered[:, -dx:] = False
            elif dx < 0: self.discovered[:, :-dx] = False
            if dz > 0: self.discovered[-dz:, :] = False
            elif dz < 0: self.discovered[:-dz, :] = False
            self.refresh_mesh()

    def update_side_scan(self, sled_pos, sled_vel, sonar_active):
        if not sonar_active: return
        speed = float(np.linalg.norm(sled_vel))
        if speed < 0.1: return
        sv_x, sv_y, sv_z = sled_vel
        hx, hy, hz = sv_x / speed, sv_y / speed, sv_z / speed
        sx, sy, sz = sled_pos
        perp_x, perp_z = -hz, hx
        dists = np.arange(-16, 17)
        scan_x = sx + perp_x * dists * SCALE * 0.5
        scan_z = sz + perp_z * dists * SCALE * 0.5
        local_gx = np.floor(scan_x / SCALE).astype(int) - self.last_gx + int(GRID_DIM / 2)
        local_gz = np.floor(scan_z / SCALE).astype(int) - self.last_gz + int(GRID_DIM / 2)
        valid = (local_gx >= 0) & (local_gx < GRID_DIM) & (local_gz >= 0) & (local_gz < GRID_DIM)
        vgx, vgz = local_gx[valid], local_gz[valid]
        self.discovered[vgz, vgx] = True
        indices = vgz * GRID_DIM + vgx
        self.colors[indices] = gl_color(SONAR_CYAN)

    def render(self):
        glEnableClientState(GL_VERTEX_ARRAY)
        glEnableClientState(GL_COLOR_ARRAY)
        glVertexPointer(3, GL_FLOAT, 0, self.vertices)
        glColorPointer(3, GL_FLOAT, 0, self.colors)
        glDrawElements(GL_LINES, len(self.indices), GL_UNSIGNED_INT, self.indices)
        glDisableClientState(GL_COLOR_ARRAY)
        glDisableClientState(GL_VERTEX_ARRAY)

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
        if ground_height > -1.0: # If water is less than 1m deep, we are aground!
            nx, nz = px, pz # Stop forward movement
            target_y = float(ground_height) # Rest the hull on the dirt
            target_pitch, target_roll = 0.0, 0.0 # Stop waving
        else:
            # Normal oceanic wave physics
            target_y = ocean.get_exact_height(nx, nz, t, sea_state)
            dw_dx, dw_dz = ocean.get_wave_derivatives(nx, nz, t, sea_state)
            target_pitch = float(np.arctan(dw_dx * np.sin(self.yaw) + dw_dz * np.cos(self.yaw)))
            target_roll = float(np.arctan(-(dw_dx * np.cos(self.yaw) - dw_dz * np.sin(self.yaw))))
            
        ny = py + (target_y - py) * 25.0 * dt
        self.pos = np.array((nx, ny, nz))
        self.pitch += (target_pitch - self.pitch) * 25.0 * dt
        self.roll += (target_roll - self.roll) * 25.0 * dt
        
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
        
    def get_wave_derivatives(self, x, z, t, sea_state):
        if sea_state <= 0.01: return 0.0, 0.0
        dx = (np.cos(x*0.02 + t*1.5)*0.03 + np.cos((x+z)*0.08 + t*3.0)*0.04) * sea_state
        dz = (-np.sin(z*0.025 + t*1.2)*0.025 + np.cos((x+z)*0.08 + t*3.0)*0.04) * sea_state
        return float(dx), float(dz)

    def render(self):
        glEnableClientState(GL_VERTEX_ARRAY)
        glVertexPointer(3, GL_FLOAT, 0, self.vertices)
        glEnableClientState(GL_COLOR_ARRAY)
        glColorPointer(3, GL_FLOAT, 0, self.colors)
        glDrawElements(GL_LINES, len(self.indices), GL_UNSIGNED_INT, self.indices)
        glDisableClientState(GL_COLOR_ARRAY)
        glDisableClientState(GL_VERTEX_ARRAY)

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
        self.color = (1.0, 0.2, 0.2) if type == "MISSILE" else (1.0, 0.6, 0.0)
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

    def draw(self, gl_time):
        if not self.active: return
        px, py, pz = self.pos
        glPushMatrix(); glTranslatef(px, py, pz)
        if self.splashing:
            scale = self.impact_timer * 30.0
            glColor3f(1.0 - (self.impact_timer*0.5), 1.0, 1.0) 
            glLineWidth(3.0)
            for h in range(3):
                glBegin(GL_LINE_LOOP)
                for i in range(16):
                    theta = i * (math.pi / 8.0)
                    h_offset = (h * 5.0) + (self.impact_timer * 10.0)
                    glVertex3f(scale * math.cos(theta), h_offset, scale * math.sin(theta))
                glEnd()
            glLineWidth(1.0)
        else:
            glColor3f(*self.color)
            if self.type == "MISSILE":
                glBegin(GL_LINES)
                glVertex3f(0, 0, 0)
                glVertex3f((self.pos[0] - self.target[0]) * 0.1, 
                           (self.pos[1] - self.target[1]) * 0.1 + 10.0, 
                           (self.pos[2] - self.target[2]) * 0.1)
                glEnd()
            else:
                glBegin(GL_LINE_LOOP)
                glVertex3f(-5, 5, 0); glVertex3f(0, 5, -5)
                glVertex3f(5, 5, 0); glVertex3f(0, 5, 5)
                glEnd()
        glPopMatrix()

class PatrolUUV:
    def __init__(self, start_pos):
        self.pos = np.array(start_pos, dtype=float)
        self.target = self.pos.copy()
        self.speed = 12.0 
        self.patrol_angle = 0.0 # Used for sweep pattern
        self.patrol_radius = 250.0

    def get_sweep_target(self, ref_pos):
        # Lawnmower / Sweep logic relative to the sled
        self.patrol_angle += math.pi / 4.0 # Rotate 45 degrees
        rx, ry, rz = ref_pos
        tx = rx + math.cos(self.patrol_angle) * self.patrol_radius
        tz = rz + math.sin(self.patrol_angle) * self.patrol_radius
        
        # Predictive depth: Aim for 15m above the sea floor at the target
        target_ground = get_math_depth(tx, tz)
        ty = min(target_ground + 15.0, -10.0) # Stay submerged
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
            self.pos = np.array(sled_pos) # Docked
            return # Skip movement if docked
            
        # Move towards target
        velocity = (dir_vec / max(dist, 0.01)) * self.speed
        self.pos += velocity * dt
        
        # --- NEW: Predictive Terrain Following ---
        # Look 30 meters ahead of the UUV
        look_ahead_x = self.pos[0] + (velocity[0] * 2.5)
        look_ahead_z = self.pos[2] + (velocity[2] * 2.5)
        ground_ahead = get_math_depth(look_ahead_x, look_ahead_z)
        current_ground = get_math_depth(self.pos[0], self.pos[2])
        
        # Ensure we clear the immediate ground and the upcoming ground
        safe_y = max(current_ground + 8.0, ground_ahead + 8.0)
        
        # Smoothly pitch up to avoid terrain
        if self.pos[1] < safe_y:
            self.pos[1] += (safe_y - self.pos[1]) * 2.0 * dt
            
def draw_pyxis_monohull():
    length = 15.0
    beam = 5.0
    glColor3f(0.2, 0.2, 0.25) 
    glBegin(GL_TRIANGLES)
    glVertex3f(0.0, 2.0, length); glVertex3f(0.0, -1.0, length * 0.7); glVertex3f(-beam, 2.0, length * 0.3)
    glVertex3f(0.0, 2.0, length); glVertex3f(beam, 2.0, length * 0.3); glVertex3f(0.0, -1.0, length * 0.7)
    glEnd()
    glBegin(GL_QUADS)
    glVertex3f(-beam, 2.0, length * 0.3); glVertex3f(0.0, -1.0, length * 0.7); glVertex3f(0.0, -1.0, -length * 0.9); glVertex3f(-beam * 0.9, 2.0, -length * 1.0)
    glVertex3f(beam, 2.0, length * 0.3); glVertex3f(beam * 0.9, 2.0, -length * 1.0); glVertex3f(0.0, -1.0, -length * 0.9); glVertex3f(0.0, -1.0, length * 0.7)
    glVertex3f(-beam * 0.9, 2.0, -length * 1.0); glVertex3f(0.0, -1.0, -length * 0.9); glVertex3f(0.0, -1.0, -length * 0.9); glVertex3f(beam * 0.9, 2.0, -length * 1.0)
    glEnd()
    glBegin(GL_POLYGON)
    glColor3f(0.15, 0.15, 0.2)
    glVertex3f(0.0, 2.0, length); glVertex3f(-beam, 2.0, length * 0.3); glVertex3f(-beam * 0.9, 2.0, -length * 1.0); glVertex3f(beam * 0.9, 2.0, -length * 1.0); glVertex3f(beam, 2.0, length * 0.3)
    glEnd()
    glColor3f(0.1, 0.1, 0.15)
    glBegin(GL_QUADS)
    glVertex3f(-beam*0.6, 2.0, length * 0.1); glVertex3f(beam*0.6, 2.0, length * 0.1); glVertex3f(beam*0.5, 4.5, -length * 0.1); glVertex3f(-beam*0.5, 4.5, -length * 0.1)
    glVertex3f(-beam*0.5, 4.5, -length * 0.1); glVertex3f(beam*0.5, 4.5, -length * 0.1); glVertex3f(beam*0.5, 4.5, -length * 0.6); glVertex3f(-beam*0.5, 4.5, -length * 0.6)
    glVertex3f(-beam*0.5, 4.5, -length * 0.6); glVertex3f(beam*0.5, 4.5, -length * 0.6); glVertex3f(beam*0.6, 2.0, -length * 0.6); glVertex3f(-beam*0.6, 2.0, -length * 0.6)
    glVertex3f(-beam*0.6, 2.0, length * 0.1); glVertex3f(-beam*0.5, 4.5, -length * 0.1); glVertex3f(-beam*0.5, 4.5, -length * 0.6); glVertex3f(-beam*0.6, 2.0, -length * 0.6)
    glVertex3f(beam*0.6, 2.0, length * 0.1); glVertex3f(beam*0.6, 2.0, -length * 0.6); glVertex3f(beam*0.5, 4.5, -length * 0.6); glVertex3f(beam*0.5, 4.5, -length * 0.1)
    glEnd()
    glColor3f(0.05, 0.05, 0.05)
    glLineWidth(3.0)
    glBegin(GL_LINES)
    glVertex3f(0.0, 4.5, -length * 0.3); glVertex3f(0.0, 8.0, -length * 0.3)
    glVertex3f(-beam*0.4, 7.0, -length * 0.3); glVertex3f(beam*0.4, 7.0, -length * 0.3)
    glEnd()
    glLineWidth(1.0)

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

def draw_schematic(surface, sw, sh, sys_state):
    bg_w, bg_h = 600, 450
    bx, by = int(sw/2) - int(bg_w/2), int(sh/2) - int(bg_h/2)
    font = pygame.font.SysFont("monospace", 14)
    title_font = pygame.font.SysFont("monospace", 18, bold=True)
    pygame.draw.rect(surface, (0, 15, 30, 240), (bx, by, bg_w, bg_h))
    pygame.draw.rect(surface, SONAR_CYAN, (bx, by, bg_w, bg_h), 2)
    surface.blit(title_font.render("PYXIS SYSTEMS SCHEMATIC", True, TARGET_GREEN), (bx + 20, by + 20))
    hull_pts = [(bx+100, by+100), (bx+500, by+100), (bx+550, by+200), (bx+500, by+300), (bx+100, by+300), (bx+50, by+200)]
    pygame.draw.polygon(surface, SONAR_DIM, hull_pts, 0)
    pygame.draw.polygon(surface, SONAR_CYAN, hull_pts, 2)
    systems = [
        {"id": "engine", "label": "MAIN DRIVE / POWER", "rect": pygame.Rect(bx+120, by+150, 120, 100)},
        {"id": "sonar", "label": "CHIRP/SIDESCAN", "rect": pygame.Rect(bx+280, by+150, 100, 100)},
        {"id": "comms", "label": "UHF/SATCOM", "rect": pygame.Rect(bx+420, by+130, 80, 140)}
    ]
    hitboxes = {}
    for sys in systems:
        rect = sys["rect"]
        sid = sys["id"]
        is_on = sys_state.get(sid, True)
        c_fill = (0, 100, 0, 180) if is_on else (100, 0, 0, 180)
        c_line = TARGET_GREEN if is_on else VECTOR_RED
        pygame.draw.rect(surface, c_fill, rect, border_radius=4)
        pygame.draw.rect(surface, c_line, rect, 2, border_radius=4)
        l_x, l_y = rect.x + 5, rect.y + 10
        for word in sys["label"].split():
            surface.blit(font.render(word, True, (255,255,255)), (l_x, l_y))
            l_y += 18
        hitboxes[sid] = rect
        
    return hitboxes

def draw_sys_buttons(surface, sw, sh, sys_state):
    font = pygame.font.SysFont("monospace", 14, bold=True)
    buttons = [
        {"id": "engine", "label": "TOGGLE ENGINE", "rect": pygame.Rect(sw - 160, sh - 140, 140, 30)},
        {"id": "sonar", "label": "TOGGLE SONAR", "rect": pygame.Rect(sw - 160, sh - 100, 140, 30)},
        {"id": "comms", "label": "TOGGLE COMMS", "rect": pygame.Rect(sw - 160, sh - 60, 140, 30)}
    ]
    
    hitboxes = {}
    for btn in buttons:
        rect = btn["rect"]
        sid = btn["id"]
        is_on = sys_state.get(sid, True)
        
        c_fill = (0, 60, 0, 200) if is_on else (60, 0, 0, 200)
        c_line = TARGET_GREEN if is_on else VECTOR_RED
        c_text = (200, 255, 200) if is_on else (255, 100, 100)
        
        pygame.draw.rect(surface, c_fill, rect, border_radius=4)
        pygame.draw.rect(surface, c_line, rect, 2, border_radius=4)
        
        text = font.render(btn["label"], True, c_text)
        txt_rect = text.get_rect(center=rect.center)
        surface.blit(text, txt_rect)
        
        hitboxes[sid] = rect
        
    # Add Schematic Toggle Button
    sch_rect = pygame.Rect(sw - 160, sh - 180, 140, 30)
    pygame.draw.rect(surface, (0, 40, 80, 200), sch_rect, border_radius=4)
    pygame.draw.rect(surface, SONAR_CYAN, sch_rect, 2, border_radius=4)
    sch_text = font.render("LAY SCHEMATIC", True, (200, 230, 255))
    surface.blit(sch_text, sch_text.get_rect(center=sch_rect.center))
    hitboxes["schematic"] = sch_rect
    
    return hitboxes

def get_contact_dist(contact_tuple): return contact_tuple[1]

# --- Async Telemetry Post ---
def post_telemetry(payload):
    global last_audio_ts, watch_uuv_state, crew_loc_lat, crew_loc_lon, last_warp_ts, pending_warp
    try: 
        try:
            p_lat = payload.get("lat", 0)
            p_lon = payload.get("lon", 0)
            local_x = (p_lat - BOAT_START[0]) * 111320.0
            local_z = (p_lon - BOAT_START[1]) * 92000.0
            sonar_grid = []
            grid_size = 10
            step = 5.0 
            for z_offset in range(-grid_size//2, grid_size//2):
                row = []
                for x_offset in range(-grid_size//2, grid_size//2):
                    sx = local_x + (x_offset * step)
                    sz = local_z + (z_offset * step)
                    depth = get_math_depth(sx, sz)
                    row.append(int(depth))
                sonar_grid.append(row)
            payload["sonar_grid"] = sonar_grid
        except Exception as e:
            print(f"Error packing sonar grid: {e}")

        requests.post(REMOTE_URL, json=payload, headers={"X-Garmin-Auth": AUTH_TOKEN}, timeout=2.0)
        
        r = requests.get(STATUS_URL, timeout=2.0)
        if r.status_code == 200:
            status_data = r.json()
            server_ts = status_data.get("status_id", 0)
            
            # All contacts with valid lat/lon (not just MERCHANT)
            global live_ais_contacts
            ais_raw = status_data.get("radar_contacts", [])
            live_ais_contacts = [c for c in ais_raw if "lat" in c and "lon" in c
                                 and c.get("lat") is not None and c.get("lon") is not None]

            # Extract CMEMS wave/current data if proxy includes vessel_wave
            try:
                w = status_data.get("vessel_wave") or {}
                if w:
                    with proxy_lock:
                        live_proxy_data["wave_h"]      = w.get("wave_h")
                        live_proxy_data["wave_period"]  = w.get("wave_period") or w.get("VTM10")
                        live_proxy_data["wave_dir"]     = w.get("wave_dir")    or w.get("VMDR")
                        live_proxy_data["sst_c"]        = w.get("sst_c")
                        live_proxy_data["curr_v"]       = w.get("curr_v")
                        live_proxy_data["curr_dir"]     = w.get("curr_dir")
                        live_proxy_data["last_update"]  = time.time()
            except Exception:
                pass
            
            watch_cmd = status_data.get("UUV_STATE", "")
            if watch_cmd in ["DEPLOY", "RECALL"]:
                watch_uuv_state = watch_cmd
                
            crew_loc_lat = float(status_data.get("CREW_LAT", 0.0))
            crew_loc_lon = float(status_data.get("CREW_LON", 0.0))
            
            warp_data = status_data.get("scenario_warp")
            if warp_data:
                w_ts = warp_data.get("ts", 0)
                if last_warp_ts == 0:
                    last_warp_ts = w_ts # Initialize on boot
                elif w_ts > last_warp_ts:
                    last_warp_ts = w_ts
                    pending_warp = (float(warp_data.get("lat", 0)), float(warp_data.get("lon", 0)))
            
            if server_ts > last_audio_ts and last_audio_ts != 0:
                last_audio_ts = server_ts
                print("DEBUG: Watch triggered a SITREP. Downloading new audio...")
                bust = int(time.time() * 1000)
                audio_r = requests.get(f"{AUDIO_URL}?type=status&t={bust}", timeout=10)
                if audio_r.status_code == 200:
                    temp_filename = "ruby_temp.wav"
                    with open(temp_filename, "wb") as f: f.write(audio_r.content)
                    print("DEBUG: Remote Watch SITREP Downloaded (Muted Locally)")
            elif last_audio_ts == 0:
                last_audio_ts = server_ts 

    except Exception as e: pass

# --- Background Live Data Poller ---
def poll_proxy_live_data():
    """Polls /status_api every 10s for all onboard systems data. Runs as daemon thread."""
    global proxy_online
    while True:
        try:
            r = requests.get(STATUS_URL, headers={"X-Garmin-Auth": AUTH_TOKEN}, timeout=5.0)
            if r.status_code == 200:
                d = r.json()
                with proxy_lock:
                    # --- Environmental / CMEMS ---
                    w = d.get("vessel_wave") or {}
                    live_proxy_data["wave_h"]      = d.get("wave_height_m")  or w.get("wave_h")
                    live_proxy_data["wave_period"] = w.get("wave_period")    or w.get("VTM10")
                    live_proxy_data["wave_dir"]    = d.get("wave_dir")       or w.get("wave_dir") or w.get("VMDR")
                    live_proxy_data["sst_c"]       = d.get("sea_temp_c")     or w.get("sst_c")
                    live_proxy_data["curr_v"]      = d.get("current_kn")     or w.get("curr_v")
                    live_proxy_data["curr_dir"]    = w.get("curr_dir")
                    # --- Propulsion ---
                    live_proxy_data["rpm"]         = d.get("rpm")
                    live_proxy_data["coolant_c"]   = d.get("coolant_temp_c")
                    live_proxy_data["oil_psi"]     = d.get("oil_press_psi")
                    live_proxy_data["egt_c"]       = d.get("egt_c")
                    live_proxy_data["er_temp_c"]   = d.get("er_temp_c")
                    # --- Power grid ---
                    live_proxy_data["house_v"]     = d.get("house_v")    or d.get("bat_v")
                    live_proxy_data["house_soc"]   = d.get("house_soc")
                    live_proxy_data["house_amps"]  = d.get("house_amps")
                    live_proxy_data["start_v"]     = d.get("start_v")
                    live_proxy_data["alt_v"]       = d.get("alt_v")
                    live_proxy_data["solar_w"]     = d.get("solar_w")
                    live_proxy_data["wind_gen_w"]  = d.get("wind_gen_w")
                    live_proxy_data["inverter_w"]  = d.get("inverter_w")
                    live_proxy_data["gen_status"]  = d.get("gen_status")
                    # --- Fluids ---
                    live_proxy_data["fuel_pct"]        = d.get("fuel_pct")
                    live_proxy_data["fresh_water_pct"] = d.get("fresh_water_pct")
                    live_proxy_data["bilge_status"]    = d.get("bilge_status")
                    live_proxy_data["er_bilge"]        = d.get("bilge_er_alarm")
                    # --- Navigation / env ---
                    live_proxy_data["sog_kts"]     = d.get("sog_kts")
                    live_proxy_data["depth_m"]     = d.get("depth_m")
                    live_proxy_data["aws_kts"]     = d.get("aws_kts")
                    live_proxy_data["awa_deg"]     = d.get("awa_deg")
                    live_proxy_data["pressure_hpa"]= d.get("pressure_hpa")
                    live_proxy_data["last_update"] = time.time()
                proxy_online = True
            else:
                proxy_online = False
        except Exception:
            proxy_online = False
        time.sleep(10)

# --- Autopilot Bearing Compute ---
def compute_autopilot(b_lat, b_lon, path_idx, wp_idx):
    """Returns (target_yaw_rad, dist_nm) toward current waypoint."""
    path    = AUTOPILOT_PATHS[path_idx]
    wp_scen = SCENARIOS[path["waypoints"][wp_idx]]
    t_lat, t_lon = wp_scen["lat"], wp_scen["lon"]
    dlat = t_lat - b_lat
    dlon = (t_lon - b_lon) * math.cos(math.radians(b_lat))
    dist_nm = math.sqrt(dlat**2 + dlon**2) * 60.0
    # atan2(north, east) matches rib.yaw convention (0=east, pi/2=north)
    bearing = math.atan2(dlat, dlon)
    return bearing, dist_nm


def run_unified_sim():
    global BOAT_START, WIDTH, HEIGHT
    global pending_warp, autopilot_active, current_path_idx, current_wp_idx, SIM_SPEED_MULT, proxy_online
    pygame.init()
    di = pygame.display.Info()
    WIDTH, HEIGHT = di.current_w, di.current_h
    pygame.display.set_mode((WIDTH, HEIGHT), pygame.OPENGL | pygame.DOUBLEBUF | pygame.FULLSCREEN)
    ui_surface = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
    ui_tex_id = glGenTextures(1)
    clock = pygame.time.Clock()
    font = pygame.font.SysFont("monospace", 16)
    title_font = pygame.font.SysFont("monospace", 18, bold=True)
    
    # Initialize Core Classes
    terrain = MantaCoreTerrainGL()
    ocean = OceanSurfaceGL(160, SCALE*2.5, SHIP_DEPTH)
    rib = Mothership(0.0, 0.0)
    uuv = PatrolUUV(np.array((50.0, -40.0, 50.0)))
    sled = MantaCoreSled(np.array((0.0, get_math_depth(0.0, -20.0) + 20.0, -20.0)))
    vessel_systems = OnboardSystems()
    
    yaw, pitch, dist, wave_h, t = 45.0, 20.0, 600.0, 1.5, 0.0
    ship_speed_knots = 10.0  
    cruise_active = True
    mission_log = []
    dynamic_contacts = []
    strike_entities = []
    contact_acquired = False
    
    dragging = sonar_dragging = slider_dragging = depth_slider_dragging = False
    sonar_yaw = 0.0
    
    param_x, param_y = 260, HEIGHT - 220
    slider_x_start = param_x + 20
    slider_y, depth_slider_y = HEIGHT - 160, HEIGHT - 90
    slider_width, slider_height = 180, 8
    
    slider_rect = pygame.Rect(slider_x_start, slider_y, slider_width, slider_height)
    depth_slider_rect = pygame.Rect(slider_x_start, depth_slider_y, slider_width, slider_height)
    sitrep_btn_rect = pygame.Rect(35, HEIGHT - 45, 200, 30) 
    msl_btn_rect = pygame.Rect(250, HEIGHT - 45, 120, 30)
    drn_btn_rect = pygame.Rect(380, HEIGHT - 45, 120, 30)
    ew_btn_rect = pygame.Rect(510, HEIGHT - 45, 120, 30)

    vp_w, vp_h = 350, 250
    vp_x, vp_y = int(WIDTH/2.0) - int(vp_w/2.0), 50
    screen_w, screen_h = WIDTH, HEIGHT
    show_schematic = False
    ew_active = False
    sys_state = {"engine": True, "sonar": True, "comms": True}
    sys_hitboxes = {}
    last_up = 0
    
    # --- UI State Variables ---
    scenario_dropdown_open = False
    scenario_scroll_offset = 0
    scenario_btn_rect = pygame.Rect(450, 15, 200, 30)
    dropdown_rect = pygame.Rect(450, 45, 300, 400)
    current_scenario_idx = 0

    mission_log.append(f"{datetime.now().strftime('%H:%M:%S')} - LOCAL SYS INIT COMPLETE")
    last_interaction_time = time.time()

    # --- Startup: Sync position from proxy ---
    try:
        _r = requests.get(STATUS_URL, timeout=4.0)
        if _r.status_code == 200:
            _d = _r.json()
            _blat = float(_d.get("BOAT_LAT") or _d.get("lat") or BOAT_START[0])
            _blon = float(_d.get("BOAT_LON") or _d.get("lon") or BOAT_START[1])
            if _blat != 0.0 and _blon != 0.0:
                BOAT_START = (_blat, _blon)
                mission_log.append(f"{datetime.now().strftime('%H:%M:%S')} - PYXIS SYNC {_blat:.4f},{_blon:.4f}")
    except Exception:
        mission_log.append(f"{datetime.now().strftime('%H:%M:%S')} - PROXY OFFLINE, using defaults")

    # Start background live data poller
    threading.Thread(target=poll_proxy_live_data, daemon=True).start()


    while True:
        dt = clock.tick(60)/1000.0
        if dt > 0.1: dt = 0.1
        t += dt                          # Visual / wave animation time (real-speed)
        dt_scaled = dt * SIM_SPEED_MULT  # Physics time (compressed for autopilot)

        if pending_warp:
            w_lat, w_lon = pending_warp
            BOAT_START = (w_lat, w_lon)
            rib.pos = np.array((0.0, float(SHIP_DEPTH), 0.0), dtype=float)
            sled.pos = np.array((0.0, get_math_depth(0.0, -20.0) + 20.0, -20.0), dtype=float)
            uuv.pos = np.array((50.0, -40.0, 50.0), dtype=float)
            dynamic_contacts.clear()
            strike_entities.clear()
            mission_log.append(f"{datetime.now().strftime('%H:%M:%S')} - REMOTE WARP RECEIVED")
            pending_warp = None
            
        rx, ry, rz = rib.pos
        ux, uy, uz = uuv.pos
        slx, sly, slz = sled.pos
        if np.isnan(slx): slx = 0.0; sled.pos = np.zeros(3)
        if np.isnan(sly): sly = 0.0
        if np.isnan(slz): slz = 0.0
        
        b_lat = BOAT_START[0] + (rx/111320)
        b_lon = BOAT_START[1] + (rz/92000)
        
        glClearColor(0.008, 0.02, 0.04, 1.0) 
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        glEnable(GL_DEPTH_TEST)
        
        keys = pygame.key.get_pressed()
        
        if keys[pygame.K_LEFT]: rib.yaw -= (2.5 * dt)
        if keys[pygame.K_RIGHT]: rib.yaw += (2.5 * dt)
        
        if sys_state.get("engine", True):
            if keys[pygame.K_UP]: ship_speed_knots = min(ship_speed_knots + (10.0 * dt), 45.0)
            if keys[pygame.K_DOWN]: ship_speed_knots = max(ship_speed_knots - (15.0 * dt), -10.0)
        else:
            ship_speed_knots *= 0.98 

        # Unified Mechanical Update
        eff_speed = ship_speed_knots if cruise_active and sys_state.get("engine", True) else 0.0
        vessel_systems.update(dt, eff_speed, wave_h, ew_active, sys_state)
        
        # Interdependency: Overheat Throttle
        if vessel_systems.status == "OVERHEAT" and eff_speed > 5.0:
            ship_speed_knots *= 0.95
            eff_speed = ship_speed_knots

        thrust = np.zeros(3) 

        for e in pygame.event.get():
            if e.type in (pygame.KEYDOWN, pygame.MOUSEBUTTONDOWN, pygame.MOUSEMOTION, pygame.MOUSEWHEEL):
                last_interaction_time = time.time()
                if e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE: return
                if e.type == pygame.KEYDOWN and e.key == pygame.K_F11: pygame.display.toggle_fullscreen()
                
            if e.type == pygame.QUIT: return
            if e.type == pygame.MOUSEWHEEL: 
                if scenario_dropdown_open and dropdown_rect.collidepoint(pygame.mouse.get_pos()):
                    scenario_scroll_offset = max(0, min(scenario_scroll_offset - e.y, len(SCENARIOS) - 10))
                else:
                    dist = float(np.clip(dist - e.y*50, 100, 5000))
            if e.type == pygame.MOUSEBUTTONDOWN:
                if e.button == 1: 
                    m_x, m_y = e.pos
                    
                    # Intercept Dropdown Clicks
                    if scenario_btn_rect.collidepoint(m_x, m_y):
                        scenario_dropdown_open = not scenario_dropdown_open
                        continue
                        
                    if scenario_dropdown_open and dropdown_rect.collidepoint(m_x, m_y):
                        # Calculate which item was clicked
                        click_y = m_y - dropdown_rect.y
                        item_idx = scenario_scroll_offset + (click_y // 40)
                        if item_idx < len(SCENARIOS):
                            # Apply Scenario Change
                            current_scenario_idx = item_idx
                            scen = SCENARIOS[item_idx]
                            BOAT_START = (scen["lat"], scen["lon"])
                            
                            # Reset Physics
                            rib.pos = np.array((0.0, float(SHIP_DEPTH), 0.0), dtype=float)
                            sled.pos = np.array((0.0, get_math_depth(0.0, -20.0) + 20.0, -20.0), dtype=float)
                            uuv.pos = np.array((50.0, -40.0, 50.0), dtype=float)
                            dynamic_contacts.clear()
                            strike_entities.clear()
                            mission_log.append(f"{datetime.now().strftime('%H:%M:%S')} - WARP: {scen['name']}")
                            
                            # Inject Threats Based On Threat Level
                            if scen["threat"] in ["HIGH", "EXTREME"]:
                                ang = random.random() * math.pi * 2
                                tx = rib.pos[0] + math.cos(ang) * 1500.0
                                tz = rib.pos[2] + math.sin(ang) * 1500.0
                                dynamic_contacts.append({"id": f"SIM_{random.randint(1000,9999)}", "name": f"UNK_FAST_DRONE_{random.randint(1,9)}", "type": "ALARM", "x": tx, "z": tz})
                            if scen["threat"] == "EXTREME":
                                ang = random.random() * math.pi * 2
                                sx, sy, sz = rib.pos[0] + math.cos(ang) * 30000.0, 5000.0, rib.pos[2] + math.sin(ang) * 30000.0
                                strike_entities.append(StrikeEntity((sx, sy, sz), (rib.pos[0], rib.pos[1], rib.pos[2]), "MISSILE"))

                            scenario_dropdown_open = False
                            continue
                    else:
                        scenario_dropdown_open = False # Close if clicked outside
                    
                    # Intercept Sys Buttons
                    btn_hit = False
                    for sid, rect in sys_buttons.items():
                        if rect.collidepoint(m_x, m_y):
                            if sid == "schematic":
                                show_schematic = not show_schematic
                            else:
                                sys_state[sid] = not sys_state.get(sid, True)
                            btn_hit = True
                            mission_log.append(f"{datetime.now().strftime('%H:%M:%S')} - OVERRIDE SYS {sid.upper()}")
                    if btn_hit: continue
                    
                    if show_schematic:
                        for sid, rect in sys_hitboxes.items():
                            if rect.collidepoint(m_x, m_y):
                                sys_state[sid] = not sys_state[sid]
                                mission_log.append(f"{datetime.now().strftime('%H:%M:%S')} - SYS {sid.upper()} {'ON' if sys_state[sid] else 'OFF'}")
                    elif sitrep_btn_rect.collidepoint(m_x, m_y):
                        mission_log.append(f"{datetime.now().strftime('%H:%M:%S')} - LOCAL SITREP LOGGED")
                    elif msl_btn_rect.collidepoint(m_x, m_y):
                        ang = random.random() * math.pi * 2
                        sx, sy, sz = rx + math.cos(ang) * 30000.0, 5000.0, rz + math.sin(ang) * 30000.0
                        mission_log.append(f"{datetime.now().strftime('%H:%M:%S')} - HYPERSONIC LAUNCH DETECTED")
                        strike_entities.append(StrikeEntity((sx, sy, sz), (rx, ry, rz), "MISSILE"))
                    elif drn_btn_rect.collidepoint(m_x, m_y):
                        ang = random.random() * math.pi * 2
                        sx, sy, sz = rx + math.cos(ang) * 5000.0, 300.0, rz + math.sin(ang) * 5000.0
                        mission_log.append(f"{datetime.now().strftime('%H:%M:%S')} - DRONE SWARM DETECTED")
                        strike_entities.append(StrikeEntity((sx, sy, sz), (rx, ry, rz), "DRONE"))
                    elif ew_btn_rect.collidepoint(m_x, m_y):
                        ew_active = not ew_active
                        mission_log.append(f"{datetime.now().strftime('%H:%M:%S')} - EW SPOOF {'ACTIVE' if ew_active else 'DEACTIVATED'}")
                    elif pygame.Rect(slider_x_start, slider_y - slider_height, slider_width, slider_height*2).collidepoint((m_x, m_y)):
                        slider_dragging = True
                    elif pygame.Rect(slider_x_start, depth_slider_y - slider_height, slider_width, slider_height*2).collidepoint((m_x, m_y)):
                        depth_slider_dragging = True
                    else: dragging = True
                elif e.button == 3: sonar_dragging = True
            if e.type == pygame.MOUSEBUTTONUP:
                if e.button == 1: slider_dragging = depth_slider_dragging = dragging = False
                elif e.button == 3: sonar_dragging = False
            if e.type == pygame.MOUSEMOTION:
                if slider_dragging:
                    m_x = e.pos[0]
                    wave_h = float(np.clip(((m_x - (slider_x_start + 5)) / (slider_width - 10)) * MAX_WAVE_HEIGHT, 0.0, MAX_WAVE_HEIGHT))
                elif depth_slider_dragging:
                    m_x = e.pos[0]
                    sled.target_depth = float(np.clip(((m_x - (slider_x_start + 5)) / (slider_width - 10)) * MAX_SLED_DEPTH, 5.0, MAX_SLED_DEPTH))
                elif dragging:
                    dx, dy = e.rel
                    yaw += dx * 0.3
                    pitch = float(np.clip(pitch + dy * 0.3, -80, 80))
                elif sonar_dragging: sonar_yaw += e.rel[0] * 0.5
            if e.type == pygame.KEYDOWN:
                if e.key == pygame.K_SPACE: cruise_active = not cruise_active
                elif e.key in [pygame.K_EQUALS, pygame.K_PLUS]: sled.target_depth = float(np.clip(sled.target_depth - 2.0, 5.0, MAX_SLED_DEPTH))
                elif e.key == pygame.K_MINUS: sled.target_depth = float(np.clip(sled.target_depth + 2.0, 5.0, MAX_SLED_DEPTH))
                elif e.key == pygame.K_t: 
                    ang = random.random() * math.pi * 2
                    tx = rx + math.cos(ang) * 1500.0
                    tz = rz + math.sin(ang) * 1500.0
                    dynamic_contacts.append({"id": f"SIM_{random.randint(100,999)}", "name": f"MERCHANT_{random.randint(10,99)}", "type": "MERCHANT", "x": tx, "z": tz})
                    mission_log.append(f"{datetime.now().strftime('%H:%M:%S')} - TRAFFIC INJECTED")
                elif e.key == pygame.K_y:
                    ang = random.random() * math.pi * 2
                    tx = rx + math.cos(ang) * 800.0
                    tz = rz + math.sin(ang) * 800.0
                    dynamic_contacts.append({"id": f"SIM_{random.randint(1000,9999)}", "name": f"UNK_FAST_DRONE_{random.randint(1,9)}", "type": "ALARM", "x": tx, "z": tz})
                    mission_log.append(f"{datetime.now().strftime('%H:%M:%S')} - ALARM TARGET INJECTED")
                elif e.key == pygame.K_TAB:
                    show_schematic = not show_schematic
                elif e.key == pygame.K_a:
                    autopilot_active = not autopilot_active
                    if autopilot_active: current_wp_idx = 0
                    mission_log.append(f"{datetime.now().strftime('%H:%M:%S')} - AUTOPILOT {'ENGAGED' if autopilot_active else 'DISENGAGED'}")
                elif e.key == pygame.K_p:
                    current_path_idx = (current_path_idx + 1) % len(AUTOPILOT_PATHS)
                    current_wp_idx   = 0
                    autopilot_active = False
                    mission_log.append(f"{datetime.now().strftime('%H:%M:%S')} - PATH: {AUTOPILOT_PATHS[current_path_idx]['name']}")
                elif e.key == pygame.K_PAGEUP:
                    SIM_SPEED_MULT = min(MAX_SPEED_MULT, SIM_SPEED_MULT * 10)
                    mission_log.append(f"{datetime.now().strftime('%H:%M:%S')} - SIM SPEED {int(SIM_SPEED_MULT)}x")
                elif e.key == pygame.K_PAGEDOWN:
                    SIM_SPEED_MULT = max(MIN_SPEED_MULT, SIM_SPEED_MULT / 10)
                    mission_log.append(f"{datetime.now().strftime('%H:%M:%S')} - SIM SPEED {int(SIM_SPEED_MULT)}x")

        # Async Telemetry Post
        current_time = pygame.time.get_ticks()
        if current_time - last_up > 2000 and sys_state.get("comms", True):
            radar_export = []
            try:
                uuv_to_sled = float(np.linalg.norm(sled.pos - uuv.pos))
                if uuv_to_sled < 400.0 and watch_uuv_state == "DEPLOY":
                    ux_lat = b_lat + ((ux - rx)/111320.0)
                    uz_lon = b_lon + ((uz - rz)/(111320.0 * math.cos(math.radians(b_lat))))
                    dx, dz = ux - rx, uz - rz
                    bear = (math.degrees(math.atan2(dx, dz)) + 360) % 360
                    dist_nm = uuv_to_sled / 1852.0
                    radar_export.append({"id": "UUV1", "name": "UUV-PATROL", "type": "DRONE", "lat": ux_lat, "lon": uz_lon, "bearing": bear, "range_nm": dist_nm})
                
                scan_pts = np.array(((slx+200, slz), (slx-200, slz), (slx, slz+200), (slx, slz-200)))
                for idx, pt in enumerate(scan_pts):
                    scan_x, scan_z = pt
                    scan_y = get_math_depth(scan_x, scan_z)
                    if scan_y > -50.0:
                        c_dist = float(np.linalg.norm(np.array((scan_x-rx, scan_y-ry, scan_z-rz))))
                        if c_dist < 400.0:
                            cx_lat = b_lat + ((scan_x - rx)/111320.0)
                            cz_lon = b_lon + ((scan_z - rz)/(111320.0 * math.cos(math.radians(b_lat))))
                            dx, dz = scan_x - rx, scan_z - rz
                            bear = (math.degrees(math.atan2(dx, dz)) + 360) % 360
                            radar_export.append({"id": f"STRUCT_{idx}", "name": "UNK_STRUCT", "type": "VESSEL", "lat": cx_lat, "lon": cz_lon, "bearing": bear, "range_nm": c_dist/1852.0})

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

                for c in dynamic_contacts:
                    c_dist = float(np.linalg.norm(np.array((c["x"]-rx, 0, c["z"]-rz))))
                    cx_lat = b_lat + ((c["x"] - rx)/111320.0)
                    cz_lon = b_lon + ((c["z"] - rz)/(111320.0 * math.cos(math.radians(b_lat))))
                    dx, dz = c["x"] - rx, c["z"] - rz
                    bear = (math.degrees(math.atan2(dx, dz)) + 360) % 360
                    radar_export.append({"id": c["id"], "name": c["name"], "type": c["type"], "lat": cx_lat, "lon": cz_lon, "bearing": bear, "range_nm": c_dist/1852.0})

            except Exception as e: print(f"Radar Error: {e}")
            
            spoof_lat = b_lat + 0.05 if ew_active else b_lat
            spoof_lon = b_lon - 0.05 if ew_active else b_lon

            payload = {
                "lat": round(spoof_lat, 5), "lon": round(spoof_lon, 5), 
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
            last_up = current_time

        # --- Autopilot Navigation ---
        if autopilot_active:
            _bearing, _dist_nm = compute_autopilot(b_lat, b_lon, current_path_idx, current_wp_idx)
            # Smoothly steer toward waypoint
            _dyaw = (_bearing - rib.yaw + math.pi) % (2 * math.pi) - math.pi
            rib.yaw += _dyaw * min(1.0, 3.0 * dt_scaled)
            ship_speed_knots = 10.0  # Autopilot cruise
            cruise_active = True
            if _dist_nm < 1.0:  # Waypoint reached (<1nm)
                _path = AUTOPILOT_PATHS[current_path_idx]
                _scen = SCENARIOS[_path["waypoints"][current_wp_idx]]
                mission_log.append(f"{datetime.now().strftime('%H:%M:%S')} - WPT: {_scen['name'][:22]}")
                current_wp_idx += 1
                if current_wp_idx >= len(_path["waypoints"]):
                    mission_log.append(f"{datetime.now().strftime('%H:%M:%S')} - PATH COMPLETE")
                    autopilot_active = False
                    current_wp_idx = 0

        # --- Apply CMEMS Physics ---
        with proxy_lock:
            _wh   = live_proxy_data.get("wave_h")
            _cv   = live_proxy_data.get("curr_v")   or 0.0
            _cdir = live_proxy_data.get("curr_dir") or 0.0
        if _wh is not None and not slider_dragging:
            wave_h = float(np.clip(_wh, 0.0, MAX_WAVE_HEIGHT))
        _cdir_rad  = math.radians(_cdir)
        _cspeed_ms = float(_cv) * 0.514444  # kn -> m/s
        _cur_vec   = (_cspeed_ms * math.sin(_cdir_rad) * 120.0,
                      0.0,
                      _cspeed_ms * math.cos(_cdir_rad) * 120.0)

        # Physics Updates (use dt_scaled so time compresses under autopilot)
        rib.update(dt_scaled, ocean, t, wave_h, thrust, ship_speed_knots)
        terrain.update_treadmill((rx, ry, rz))
        ocean.update(rx, rz, t, wave_h)
        sled.update(dt_scaled, rib.pos, _cur_vec)
        uuv.update(dt_scaled, sled.pos)
        terrain.update_side_scan(sled.pos, sled.vel, sys_state.get("sonar", True))

        for c in dynamic_contacts:
            if c["type"] == "ALARM":
                c_dist = float(np.linalg.norm(np.array((c["x"]-rx, 0, c["z"]-rz))))
                dx, dz = c["x"] - rx, c["z"] - rz
                c["x"] -= (dx / max(c_dist, 0.1)) * (10.0 * KNOT_TO_MS * 2.0)
                c["z"] -= (dz / max(c_dist, 0.1)) * (10.0 * KNOT_TO_MS * 2.0)

        # 3D VIEWPORT
        glViewport(0, 0, screen_w, screen_h)
        glMatrixMode(GL_PROJECTION); glLoadIdentity()
        gluPerspective(45, (screen_w/screen_h), 0.1, 10000.0)
        glMatrixMode(GL_MODELVIEW); glLoadIdentity()
        
        cam_x = np.cos(np.radians(pitch))*np.sin(np.radians(yaw))*dist
        cam_y = np.sin(np.radians(pitch))*dist
        cam_z = np.cos(np.radians(pitch))*np.cos(np.radians(yaw))*dist
        mid_x, mid_y, mid_z = (rx + slx)/2.0, (ry + sly)/2.0, (rz + slz)/2.0
        gluLookAt(mid_x+cam_x, mid_y+cam_y, mid_z+cam_z, mid_x, mid_y, mid_z, 0.0, 1.0, 0.0)
        
        terrain.render(); ocean.render()
        
        # Draw Contacts
        for c in dynamic_contacts:
            glPushMatrix()
            if c["type"] == "MERCHANT":
                glTranslatef(c["x"], float(SHIP_DEPTH), c["z"])
                glColor3f(0.2, 1.0, 0.4) 
                glBegin(GL_QUADS)
                w, h, l = 10.0, 8.0, 40.0
                glVertex3f(-w, h, -l); glVertex3f(w, h, -l); glVertex3f(w, h, l); glVertex3f(-w, h, l)
                glVertex3f(-w, -h, -l); glVertex3f(w, -h, -l); glVertex3f(w, -h, l); glVertex3f(-w, -h, l)
                glVertex3f(-w, -h, l); glVertex3f(w, -h, l); glVertex3f(w, h, l); glVertex3f(-w, h, l)
                glVertex3f(-w, -h, -l); glVertex3f(-w, h, -l); glVertex3f(w, h, -l); glVertex3f(w, -h, -l)
                glVertex3f(-w, -h, -l); glVertex3f(-w, -h, l); glVertex3f(-w, h, l); glVertex3f(-w, h, -l)
                glVertex3f(w, -h, -l); glVertex3f(w, h, -l); glVertex3f(w, h, l); glVertex3f(w, -h, l)
                glEnd()
            elif c["type"] == "ALARM":
                glTranslatef(c["x"], float(SHIP_DEPTH) + 35.0, c["z"]) 
                glColor3f(1.0, 0.2, 0.2) 
                glLineWidth(2.0)
                glBegin(GL_LINES)
                glVertex3f(-4.0, 0.0, -4.0); glVertex3f(4.0, 0.0, 4.0)
                glVertex3f(-4.0, 0.0, 4.0); glVertex3f(4.0, 0.0, -4.0)
                glVertex3f(0.0, -2.0, 0.0); glVertex3f(0.0, 2.0, 0.0)
                glEnd()
                glLineWidth(1.0)
            glPopMatrix()
            
            dist_nm = float(np.linalg.norm([c["x"] - rx, c["z"] - rz])) * 0.000539957
            if c["type"] == "MERCHANT":
                draw_text_3d(f'[MRCH] R:{dist_nm:.1f}nm', c["x"], float(SHIP_DEPTH) + 25.0, c["z"], (50, 255, 100))
            elif c["type"] == "ALARM":
                draw_text_3d(f'[DRONE] R:{dist_nm:.1f}nm', c["x"], float(SHIP_DEPTH) + 55.0, c["z"], (255, 50, 50))
                
        # Draw Live AIS Contacts
        for ais in live_ais_contacts:
            a_lat = ais.get("lat")
            a_lon = ais.get("lon")
            if a_lat and a_lon:
                # Project global AIS lat/lon onto local Pygame X/Z coordinate plane tracking BOAT_START
                a_x = (a_lat - b_lat) * 111320.0 + rx
                a_z = (a_lon - b_lon) * 111320.0 * math.cos(math.radians(b_lat)) + rz
                
                glPushMatrix()
                glTranslatef(a_x, float(SHIP_DEPTH), a_z)
                glColor3f(0.0, 0.6, 1.0) # AIS Blue
                glBegin(GL_QUADS)
                w, h, l = 12.0, 10.0, 50.0  # Container ship size
                glVertex3f(-w, h, -l); glVertex3f(w, h, -l); glVertex3f(w, h, l); glVertex3f(-w, h, l)
                glVertex3f(-w, -h, -l); glVertex3f(w, -h, -l); glVertex3f(w, -h, l); glVertex3f(-w, -h, l)
                glVertex3f(-w, -h, l); glVertex3f(w, -h, l); glVertex3f(w, h, l); glVertex3f(-w, h, l)
                glVertex3f(-w, -h, -l); glVertex3f(-w, h, -l); glVertex3f(w, h, -l); glVertex3f(w, -h, -l)
                glVertex3f(-w, -h, -l); glVertex3f(-w, -h, l); glVertex3f(-w, h, l); glVertex3f(-w, h, -l)
                glVertex3f(w, -h, -l); glVertex3f(w, h, -l); glVertex3f(w, h, l); glVertex3f(w, -h, l)
                glEnd()
                glPopMatrix()
                
                ais_name = ais.get("name", "UNK_AIS")
                ais_dist_nm = float(np.linalg.norm([a_x - rx, a_z - rz])) * 0.000539957
                draw_text_3d(f'[AIS] {ais_name} ({ais_dist_nm:.1f}nm)', a_x, float(SHIP_DEPTH) + 30.0, a_z, (100, 200, 255))

        glPushMatrix(); glTranslatef(rx, ry, rz)
        glMultMatrixf(rib.get_matrix().T.flatten())
        draw_pyxis_monohull()
        glPopMatrix()
        
        glPushMatrix(); glTranslatef(slx, sly, slz)
        glRotatef(math.degrees(sled.yaw), 0.0, 1.0, 0.0); glRotatef(-math.degrees(sled.pitch), 1.0, 0.0, 0.0)
        glColor3f(1.0, 1.0, 1.0)
        glLineWidth(2.0)
        glBegin(GL_LINES)
        glVertex3f(0.0, 0.0, 6.0); glVertex3f(0.0, 0.0, -6.0) 
        glVertex3f(0.0, 3.0, -4.5); glVertex3f(0.0, -3.0, -4.5) 
        glVertex3f(5.0, 0.0, -4.5); glVertex3f(-5.0, 0.0, -4.5) 
        glEnd()
        glLineWidth(1.0)
        glPopMatrix()
        
        ux, uy, uz = uuv.pos
        uuv_to_sled = float(np.linalg.norm(uuv.pos - sled.pos))
        if uuv_to_sled < 400.0:
            if not contact_acquired:
                mission_log.append(f"{datetime.now().strftime('%H:%M:%S')} - ACQUIRED UUV-PATROL")
                contact_acquired = True
            glPushMatrix(); glTranslatef(ux, uy, uz)
            glColor3f(1.0, 0.0, 1.0)
            glBegin(GL_LINE_LOOP)
            glVertex3f(0.0, 0.0, 2.0); glVertex3f(-1.0, 0.0, -1.0); glVertex3f(1.0, 0.0, -1.0); glVertex3f(0.0, 1.0, 0.0)
            glEnd()
            glColor3f(1.0, 1.0, 1.0)
            glBegin(GL_LINES)
            glVertex3f(-3.0, 3.0, -3.0); glVertex3f(3.0, 3.0, -3.0)
            glVertex3f(-3.0, -3.0, -3.0); glVertex3f(3.0, -3.0, -3.0)
            glEnd(); glPopMatrix()
        elif contact_acquired:
            mission_log.append(f"{datetime.now().strftime('%H:%M:%S')} - LOST UUV CONTACT")
            contact_acquired = False
            
        for strike in strike_entities: strike.draw(t)
        for strike in strike_entities: strike.update(dt)
            
        if len(mission_log) > 6: mission_log.pop(0)
        
        nose_x = slx + math.sin(sled.yaw) * math.cos(sled.pitch) * 2.0
        nose_y = sly - math.sin(sled.pitch) * 2.0
        nose_z = slz + math.cos(sled.yaw) * math.cos(sled.pitch) * 2.0
        glColor3f(1.0, 0.5, 0.0)
        glBegin(GL_LINES)
        glVertex3f(rx, ry, rz); glVertex3f(nose_x, nose_y, nose_z)
        glEnd()

        # VIEWPORT 2: SLED DIAGNOSTICS
        glViewport(vp_x, vp_y, vp_w, vp_h)
        glClear(GL_DEPTH_BUFFER_BIT) 
        glMatrixMode(GL_PROJECTION); glLoadIdentity(); gluPerspective(45, (vp_w/float(vp_h)), 0.1, 100.0)
        glMatrixMode(GL_MODELVIEW); glLoadIdentity(); gluLookAt(8.0, 5.0, 10.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)
        glPushMatrix()
        glRotatef(float(math.degrees(sled.yaw)), 0.0, 1.0, 0.0); glRotatef(-float(math.degrees(sled.pitch)), 1.0, 0.0, 0.0)
        draw_detailed_sled_model(sled.wing_pitch)
        glPopMatrix()

        glViewport(0, 0, screen_w, screen_h)

        # UI OVERLAY
        ui_surface.fill((0,0,0,0))
        pygame.draw.rect(ui_surface, (2,10,20,200), (0,0,screen_w,80))
        pygame.draw.line(ui_surface, SONAR_CYAN, (0,80), (screen_w,80), 2)
        
        heading_deg = int(np.degrees(rib.yaw)) % 360
        tether_dist = float(np.linalg.norm(rib.pos - sled.pos))
        sled_depth = float(SHIP_DEPTH - sly)
        
        ui_surface.blit(title_font.render("50ft VANDSTADT SAILING VESSEL (UNIFIED)", True, (255,100,100)), (30,15))
        ui_surface.blit(font.render(f"HDG: {heading_deg:03d}° | SPD: {eff_speed:.1f} kn", True, SONAR_CYAN), (30,45))
        ui_surface.blit(title_font.render("MANTA-CORE SLED", True, SLED_WHITE), (780,15))
        ui_surface.blit(font.render(f"DPTH: {sled_depth:.1f}m | ALT: {float(sly - get_math_depth(slx, slz)):.1f}m", True, SONAR_CYAN), (780,45))
        
        # --- Onboard Systems Panel (expanded with live proxy data) ---
        eng_x, eng_y = WIDTH - 290, 93 + 118 + 8  # below the Live Proxy panel
        eng_pw, eng_ph = 280, 290
        _src_col = (80, 255, 80) if vessel_systems._live else (255, 200, 50)
        _src_txt = "LIVE" if vessel_systems._live else "SIM"
        pygame.draw.rect(ui_surface, (0, 12, 28, 210), (eng_x, eng_y, eng_pw, eng_ph))
        pygame.draw.rect(ui_surface, _src_col, (eng_x, eng_y, eng_pw, eng_ph), 2)
        ui_surface.blit(title_font.render(f"ONBOARD SYS  [{_src_txt}]", True, _src_col), (eng_x+8, eng_y+5))
        # Status row
        _st_col = VECTOR_RED if vessel_systems.status != "NOMINAL" else TARGET_GREEN
        ui_surface.blit(font.render(f"STATUS : {vessel_systems.status}", True, _st_col), (eng_x+8, eng_y+25))
        # Propulsion
        _tc = VECTOR_RED if vessel_systems.engine_temp > 100 else SONAR_CYAN
        ui_surface.blit(font.render(f"RPM    : {int(vessel_systems.rpms)}", True, SONAR_CYAN), (eng_x+8, eng_y+43))
        ui_surface.blit(font.render(f"COOLANT: {vessel_systems.engine_temp:.0f}C", True, _tc), (eng_x+8, eng_y+57))
        ui_surface.blit(font.render(f"OIL    : {vessel_systems.oil_psi:.0f} PSI", True, SONAR_CYAN), (eng_x+8, eng_y+71))
        ui_surface.blit(font.render(f"ER TEMP: {vessel_systems.er_temp_c:.0f}C", True, VECTOR_RED if vessel_systems.er_temp_c > 50 else SONAR_CYAN), (eng_x+8, eng_y+85))
        pygame.draw.line(ui_surface, (0,60,90), (eng_x+6, eng_y+99), (eng_x+eng_pw-6, eng_y+99))
        # Power grid
        ui_surface.blit(font.render(f"HOUSE  : {vessel_systems.battery_v:.1f}V  {vessel_systems.house_soc:.0f}% SOC", True,
            (255,200,50) if vessel_systems.battery_v < 12.2 else SONAR_CYAN), (eng_x+8, eng_y+103))
        ui_surface.blit(font.render(f"AMPS   : {vessel_systems.house_amps:+.1f}A  ALT:{vessel_systems.alt_v:.1f}V", True, SONAR_CYAN), (eng_x+8, eng_y+117))
        ui_surface.blit(font.render(f"START  : {vessel_systems.start_v:.1f}V  GEN:{vessel_systems.gen_status}", True, SONAR_CYAN), (eng_x+8, eng_y+131))
        ui_surface.blit(font.render(f"SOLAR  : {vessel_systems.solar_w:.0f}W  WIND:{vessel_systems.wind_gen_w:.0f}W", True, TARGET_GREEN), (eng_x+8, eng_y+145))
        ui_surface.blit(font.render(f"INV    : {vessel_systems.inverter_w:.0f}W", True, SONAR_CYAN), (eng_x+8, eng_y+159))
        pygame.draw.line(ui_surface, (0,60,90), (eng_x+6, eng_y+172), (eng_x+eng_pw-6, eng_y+172))
        # Fluids
        _fc = VECTOR_RED if vessel_systems.fuel_pct < 15 else SONAR_CYAN
        _wc = VECTOR_RED if vessel_systems.fresh_water_pct < 20 else SONAR_CYAN
        _bc = VECTOR_RED if vessel_systems.bilge_status not in ("OK", None, "") else TARGET_GREEN
        ui_surface.blit(font.render(f"FUEL   : {vessel_systems.fuel_pct:.0f}%", True, _fc), (eng_x+8, eng_y+176))
        ui_surface.blit(font.render(f"WATER  : {vessel_systems.fresh_water_pct:.0f}%", True, _wc), (eng_x+8, eng_y+190))
        ui_surface.blit(font.render(f"BILGE  : {vessel_systems.bilge_status or 'OK'}", True, _bc), (eng_x+8, eng_y+204))
        # Env from proxy
        with proxy_lock:
            _depth = live_proxy_data.get("depth_m")
            _aws   = live_proxy_data.get("aws_kts")
            _awa   = live_proxy_data.get("awa_deg")
            _baro  = live_proxy_data.get("pressure_hpa")
        pygame.draw.line(ui_surface, (0,60,90), (eng_x+6, eng_y+216), (eng_x+eng_pw-6, eng_y+216))
        ui_surface.blit(font.render(
            f"DEPTH  : {f'{_depth:.1f}m' if _depth else 'n/a'}", True, SONAR_CYAN), (eng_x+8, eng_y+220))
        ui_surface.blit(font.render(
            f"AWA/AWS: {f'{_awa:.0f}° / {_aws:.1f}kn' if _aws else 'n/a'}", True, SONAR_CYAN), (eng_x+8, eng_y+234))
        ui_surface.blit(font.render(
            f"BARO   : {f'{_baro:.1f} hPa' if _baro else 'n/a'}", True, SONAR_CYAN), (eng_x+8, eng_y+248))
        
        uuv_status_color = UUV_MAGENTA if watch_uuv_state == "DEPLOY" else SONAR_DIM
        # Moved down
        ui_vp_y = HEIGHT - vp_y - vp_h
        pygame.draw.rect(ui_surface, (0, 10, 25, 180), (vp_x, ui_vp_y, vp_w, vp_h))
        pygame.draw.rect(ui_surface, SONAR_CYAN, (vp_x, ui_vp_y, vp_w, vp_h), 2)
        ui_surface.blit(title_font.render("M-CORE LIVE TELEMETRY", True, SONAR_CYAN), (vp_x + 10, ui_vp_y + 10))
        ui_surface.blit(font.render(f"HULL PITCH: {-float(math.degrees(sled.pitch)):.1f}°", True, (200,200,200)), (vp_x + 10, ui_vp_y + 35))
        ui_surface.blit(font.render(f"ELEV PITCH: {math.degrees(sled.wing_pitch):.1f}°", True, TARGET_GREEN), (vp_x + 10, ui_vp_y + 55))

        panel_y = HEIGHT - 220
        pygame.draw.rect(ui_surface, (0, 30, 50, 200), (25, panel_y, 220, 200))
        pygame.draw.rect(ui_surface, SONAR_CYAN, (25, panel_y, 220, 200), 2)
        ui_surface.blit(title_font.render("HELM CONTROLS", True, SONAR_CYAN), (35, panel_y + 10))
        
        up_act, down_act, left_act, right_act = keys[pygame.K_UP], keys[pygame.K_DOWN], keys[pygame.K_LEFT], keys[pygame.K_RIGHT]
        ctrl_text = [f"{'▲' if up_act else '△'} UP", f"{'▼' if down_act else '▽'} DOWN", f"{'◀' if left_act else '◁'} LEFT", f"{'▶' if right_act else '▷'} RIGHT"]
        colors = ((100, 255, 100) if c else SONAR_CYAN for c in (up_act, down_act, left_act, right_act))
        for i, (txt, col) in enumerate(zip(ctrl_text, colors)):
            ui_surface.blit(font.render(txt, True, col), (35, panel_y + 35 + i*16))
        ui_surface.blit(font.render(f"[SPACE] CRUISE: {'ON' if cruise_active else 'OFF'}", True, (100, 255, 100) if cruise_active else SONAR_CYAN), (35, panel_y + 115))

        pygame.draw.rect(ui_surface, (0, 100, 200), sitrep_btn_rect, border_radius=4)
        pygame.draw.rect(ui_surface, SONAR_CYAN, sitrep_btn_rect, 1, border_radius=4)
        ui_surface.blit(title_font.render("LOCAL LOG [REQ]", True, SLED_WHITE), (sitrep_btn_rect.x + 12, sitrep_btn_rect.y + 6))
        
        pygame.draw.rect(ui_surface, (200, 0, 0), msl_btn_rect, border_radius=4)
        pygame.draw.rect(ui_surface, (255, 100, 100), msl_btn_rect, 1, border_radius=4)
        ui_surface.blit(title_font.render("INJ MISSILE", True, SLED_WHITE), (msl_btn_rect.x + 8, msl_btn_rect.y + 6))
        
        pygame.draw.rect(ui_surface, (200, 150, 0), drn_btn_rect, border_radius=4)
        pygame.draw.rect(ui_surface, (255, 200, 50), drn_btn_rect, 1, border_radius=4)
        ui_surface.blit(title_font.render("INJ DRONE", True, SLED_WHITE), (drn_btn_rect.x + 12, drn_btn_rect.y + 6))
        
        ew_color = (200, 0, 0) if ew_active else (50, 50, 50)
        ew_border = (255, 100, 100) if ew_active else (100, 100, 100)
        pygame.draw.rect(ui_surface, ew_color, ew_btn_rect, border_radius=4)
        pygame.draw.rect(ui_surface, ew_border, ew_btn_rect, 1, border_radius=4)
        ui_surface.blit(title_font.render("EW SPOOF", True, SLED_WHITE), (ew_btn_rect.x + 16, ew_btn_rect.y + 6))

        pygame.draw.rect(ui_surface, (0, 30, 50, 200), (param_x, param_y, 220, 170))
        pygame.draw.rect(ui_surface, SONAR_CYAN, (param_x, param_y, 220, 170), 2)
        ui_surface.blit(title_font.render("ENVIRONMENT", True, SONAR_CYAN), (param_x + 10, param_y + 10))

        ui_surface.blit(font.render(f"SEA STATE: {wave_h:.1f}m", True, SONAR_CYAN), (slider_x_start, slider_y - 20))
        pygame.draw.rect(ui_surface, (0, 40, 60, 255), slider_rect)
        pygame.draw.rect(ui_surface, SONAR_CYAN, slider_rect, 2)
        slider_handle_x = slider_x_start + 5 + (wave_h / MAX_WAVE_HEIGHT) * (slider_width - 10)
        pygame.draw.circle(ui_surface, (100, 255, 100), (int(slider_handle_x), slider_y + slider_height // 2), 6)

        ui_surface.blit(font.render(f"TARGET DEPTH: {sled.target_depth:.0f}m", True, SONAR_CYAN), (slider_x_start, depth_slider_y - 20))
        pygame.draw.rect(ui_surface, (0, 40, 60, 255), depth_slider_rect)
        pygame.draw.rect(ui_surface, SONAR_CYAN, depth_slider_rect, 2)
        depth_handle_x = slider_x_start + 5 + (sled.target_depth / MAX_SLED_DEPTH) * (slider_width - 10)
        pygame.draw.circle(ui_surface, (100, 255, 100), (int(depth_handle_x), depth_slider_y + slider_height // 2), 6)

        log_y = HEIGHT - 400
        pygame.draw.rect(ui_surface, (0,30,50,150), (25, log_y, 350, 150))
        pygame.draw.rect(ui_surface, SONAR_CYAN, (25, log_y, 350, 150), 2)
        ui_surface.blit(title_font.render("MISSION LOG", True, SONAR_CYAN), (35, log_y + 10))
        for i, log in enumerate(mission_log):
            ui_surface.blit(font.render(log, True, (200,200,200)), (35, log_y + 35 + i*18))
            
        contact_x, contact_y = WIDTH - 280, 100
        pygame.draw.rect(ui_surface, (0,30,50,180), (contact_x, contact_y, 250, 150))
        pygame.draw.rect(ui_surface, SONAR_CYAN, (contact_x, contact_y, 250, 150), 2)
        ui_surface.blit(title_font.render("ACTIVE CONTACTS", True, SONAR_CYAN), (contact_x + 10, contact_y + 10))
        
        active_contacts = []
        if sys_state.get("sonar", True):
            if uuv_to_sled < 400.0 and watch_uuv_state == "DEPLOY": 
                active_contacts.append(("ID: UUV-PATROL", uuv_to_sled, UUV_MAGENTA))
            scan_pts = np.array(((slx+200, slz), (slx-200, slz), (slx, slz+200), (slx, slz-200)))
            for pt in scan_pts:
                scan_x, scan_z = pt
                scan_y = get_math_depth(scan_x, scan_z)
                if scan_y > -50.0:
                    c_dist = float(np.linalg.norm(np.array((scan_x-slx, scan_y-sly, scan_z-slz))))
                    if c_dist < 400.0: active_contacts.append(("MASSIVE STRUCTURE", c_dist, TARGET_GREEN))
            
            # Add dynamically injected/mock threats
            for c in strike_entities:
                if c.active:
                    c_dist = float(np.linalg.norm(c.pos - np.array((rx, ry, rz))))
                    active_contacts.append((f"[{c.name}] THREAT INBOUND", c_dist, VECTOR_RED))
            
            # Add Live AIS to the list too
            for ais in live_ais_contacts:
                a_lat, a_lon = ais.get("lat"), ais.get("lon")
                if a_lat and a_lon:
                    a_x = (a_lat - b_lat) * 111320.0 + rx
                    a_z = (a_lon - b_lon) * 111320.0 * math.cos(math.radians(b_lat)) + rz
                    c_dist = float(np.linalg.norm([a_x - rx, a_z - rz]))
                    # Only show on radar list if within 25km (approx 13.5nm)
                    if c_dist < 25000:
                        active_contacts.append((f"[AIS] {ais.get('name', 'UNK')}", c_dist, (100, 200, 255)))
                        
        active_contacts.sort(key=get_contact_dist)
        if not active_contacts:
            ui_surface.blit(font.render("NO CONTACTS" if sys_state.get("sonar", True) else "SONAR OFFLINE", True, SONAR_DIM if sys_state.get("sonar", True) else VECTOR_RED), (contact_x + 10, contact_y + 40))
        else:
            for idx, contact in enumerate(active_contacts):
                if idx > 4: break
                name, c_dist, color = contact
                ui_surface.blit(font.render(f"{name}", True, color), (contact_x + 10, contact_y + 40 + idx*18))
                ui_surface.blit(font.render(f"{c_dist:.0f}m", True, color), (contact_x + 190, contact_y + 40 + idx*18))

        cx, cy = WIDTH-280, HEIGHT-280
        pygame.draw.rect(ui_surface, (0,10,25,200), (cx, cy, 250, 250))
        pygame.draw.rect(ui_surface, SONAR_CYAN, (cx, cy, 250, 250), 2)
        
        yaw_deg = int(sonar_yaw) % 360
        ui_surface.blit(title_font.render(f"CHIRP MAP ({yaw_deg:03d}°)", True, SONAR_CYAN), (cx+10, cy-25))
        
        rad_yaw = np.radians(sonar_yaw)
        cy_yaw, sy_yaw = math.cos(rad_yaw), math.sin(rad_yaw)
        rel_gx = int(np.floor(slx / SCALE)) - terrain.last_gx + int(GRID_DIM / 2)
        rel_gz = int(np.floor(slz / SCALE)) - terrain.last_gz + int(GRID_DIM / 2)
        rel_gx, rel_gz = int(np.clip(rel_gx, 0, GRID_DIM-1)), int(np.clip(rel_gz, 0, GRID_DIM-1))
        
        if sys_state.get("sonar", True):
            for pz in range(max(0,rel_gz-30), min(GRID_DIM,rel_gz+30), 2):
                for px in range(max(0,rel_gx-30), min(GRID_DIM,rel_gx+30), 2):
                    if terrain.discovered[pz,px]:
                        ox, oz = (px - rel_gx) * 6, (pz - rel_gz) * 6
                        rot_x, rot_z = ox * cy_yaw - oz * sy_yaw, ox * sy_yaw + oz * cy_yaw
                        screen_px, screen_py = cx + 125 + rot_x, cy + 125 + rot_z
                        if cx < screen_px < cx+250 and cy < screen_py < cy+250: 
                            pygame.draw.circle(ui_surface, SONAR_CYAN, (int(screen_px), int(screen_py)), 1)
        
        pygame.draw.circle(ui_surface, VECTOR_RED if sys_state.get("sonar", True) else SONAR_DIM, (cx+125, cy+125), 5)
        pygame.draw.line(ui_surface, VECTOR_RED if sys_state.get("sonar", True) else SONAR_DIM, (cx+125, cy+115), (cx+125, cy+135), 1)
        pygame.draw.line(ui_surface, VECTOR_RED if sys_state.get("sonar", True) else SONAR_DIM, (cx+115, cy+125), (cx+135, cy+125), 1)

        # Draw sweeping radar CRT pulse ring
        if sys_state.get("sonar", True):
            pulse_rad = 20 + int((t * 50) % 105)
            alpha_fade = max(0, min(255, 105 - pulse_rad))
            pygame.draw.circle(ui_surface, (0, 150, 255, alpha_fade), (cx+125, cy+125), pulse_rad, 2)

        if show_schematic:
            sys_hitboxes = draw_schematic(ui_surface, screen_w, screen_h, sys_state)
            
        sys_buttons = draw_sys_buttons(ui_surface, screen_w, screen_h, sys_state)

        # Draw Scenario Dropdown UI (Draw Last so it is on top)
        pygame.draw.rect(ui_surface, (0, 150, 150) if scenario_dropdown_open else (0, 50, 80), scenario_btn_rect, border_radius=4)
        pygame.draw.rect(ui_surface, SONAR_CYAN, scenario_btn_rect, 1, border_radius=4)
        ui_surface.blit(title_font.render("SELECT SCENARIO ▼", True, SLED_WHITE), (scenario_btn_rect.x + 8, scenario_btn_rect.y + 6))
        
        # Display Current Scenario text underneath instead
        ui_surface.blit(title_font.render(SCENARIOS[current_scenario_idx]["name"].upper(), True, TARGET_GREEN), (455, 52))

        if scenario_dropdown_open:
            pygame.draw.rect(ui_surface, (0, 20, 40, 240), dropdown_rect)
            pygame.draw.rect(ui_surface, SONAR_CYAN, dropdown_rect, 2)
            
            for i in range(10): # Show 10 items at a time
                idx = scenario_scroll_offset + i
                if idx >= len(SCENARIOS): break
                item_y = dropdown_rect.y + (i * 40)
                item_rect = pygame.Rect(dropdown_rect.x, item_y, dropdown_rect.width, 40)
                
                # Hover effect
                if item_rect.collidepoint(pygame.mouse.get_pos()):
                    pygame.draw.rect(ui_surface, (0, 80, 120, 255), item_rect)
                
                scen = SCENARIOS[idx]
                threat_col = TARGET_GREEN if scen["threat"] == "LOW" else (255, 200, 50) if scen["threat"] == "MEDIUM" else VECTOR_RED if scen["threat"] == "HIGH" else (255, 0, 255)
                ui_surface.blit(font.render(scen["name"], True, SLED_WHITE), (item_rect.x + 10, item_rect.y + 5))
                ui_surface.blit(font.render(f"LAT: {scen['lat']} LON: {scen['lon']} [{scen['threat']}]", True, threat_col), (item_rect.x + 10, item_rect.y + 20))
                pygame.draw.line(ui_surface, (0, 50, 80), (item_rect.x, item_rect.bottom), (item_rect.right, item_rect.bottom))

            # Scrollbar
            scroll_h = max(20, int(dropdown_rect.height * (10 / len(SCENARIOS))))
            scroll_y = dropdown_rect.y + int((scenario_scroll_offset / (len(SCENARIOS) - 10)) * (dropdown_rect.height - scroll_h)) if len(SCENARIOS) > 10 else dropdown_rect.y
            pygame.draw.rect(ui_surface, SONAR_CYAN, (dropdown_rect.right - 8, scroll_y, 8, scroll_h))

        # Screen Dimming Inactivity Timeout (60 Seconds)
        if time.time() - last_interaction_time > 60.0:
            dim_overlay = pygame.Surface((screen_w, screen_h), pygame.SRCALPHA)
            dim_overlay.fill((0, 0, 0, 200)) # 80% opacity black
            ui_surface.blit(dim_overlay, (0, 0))

        # Scanline Overlay
        scanline_surf = pygame.Surface((screen_w, screen_h), pygame.SRCALPHA)
        for y_scan in range(0, screen_h, 4):
            pygame.draw.line(scanline_surf, (0, 0, 0, 40), (0, y_scan), (screen_w, y_scan))
        ui_surface.blit(scanline_surf, (0, 0))
        # === AUTOPILOT PANEL (bottom-centre) ===
        _ap_path = AUTOPILOT_PATHS[current_path_idx]
        _ap_col  = _ap_path["color"]
        _ap_pw, _ap_ph = 420, 120
        _ap_px = int(WIDTH/2) - int(_ap_pw/2)
        _ap_py = HEIGHT - _ap_ph - 8
        pygame.draw.rect(ui_surface, (0, 15, 30, 210), (_ap_px, _ap_py, _ap_pw, _ap_ph))
        pygame.draw.rect(ui_surface, _ap_col, (_ap_px, _ap_py, _ap_pw, _ap_ph), 2)
        _ap_head_c = (100, 255, 100) if autopilot_active else SONAR_CYAN
        _ap_status = "ENGAGED" if autopilot_active else "STANDBY"
        ui_surface.blit(title_font.render(f"AUTOPILOT  [A] {_ap_status}", True, _ap_head_c), (_ap_px+10, _ap_py+8))
        ui_surface.blit(font.render(f"PATH: {_ap_path['name']}  ([P] cycle)", True, _ap_col), (_ap_px+10, _ap_py+28))
        if current_wp_idx < len(_ap_path["waypoints"]):
            _ap_wp_scen = SCENARIOS[_ap_path["waypoints"][current_wp_idx]]
            if autopilot_active:
                _, _ap_dnm = compute_autopilot(b_lat, b_lon, current_path_idx, current_wp_idx)
                _eta_min   = (_ap_dnm / 10.0) * 60.0 / SIM_SPEED_MULT
                ui_surface.blit(font.render(f"WPT {current_wp_idx+1}/{len(_ap_path['waypoints'])}: {_ap_wp_scen['name'][:28]}", True, SLED_WHITE), (_ap_px+10, _ap_py+48))
                ui_surface.blit(font.render(f"DIST: {_ap_dnm:.1f}nm   ETA: {_eta_min:.1f} min real-time", True, SONAR_CYAN), (_ap_px+10, _ap_py+66))
            else:
                ui_surface.blit(font.render(f"NEXT WPT: {_ap_wp_scen['name'][:32]}", True, SONAR_DIM), (_ap_px+10, _ap_py+48))
        ui_surface.blit(font.render(f"SIM SPEED: {int(SIM_SPEED_MULT)}x  [PgUp/PgDn]", True, CABLE_ORANGE), (_ap_px+10, _ap_py+95))

        # === LIVE PROXY FEED PANEL (top-right corner) ===
        _lp_w, _lp_h = 265, 118
        _lp_x = WIDTH - _lp_w - 10
        _lp_y = 93
        with proxy_lock:
            _lpd = dict(live_proxy_data)
        _online_c   = (80, 255, 80) if proxy_online else (255, 80, 80)
        _online_txt = "ONLINE" if proxy_online else "OFFLINE"
        _age = int(time.time() - _lpd["last_update"]) if _lpd["last_update"] else 0
        pygame.draw.rect(ui_surface, (0, 15, 30, 210), (_lp_x, _lp_y, _lp_w, _lp_h))
        pygame.draw.rect(ui_surface, _online_c, (_lp_x, _lp_y, _lp_w, _lp_h), 2)
        ui_surface.blit(title_font.render(f"LIVE PROXY  {_online_txt}", True, _online_c), (_lp_x+8, _lp_y+6))
        _wh_txt  = f"{_lpd['wave_h']:.1f}m"    if _lpd["wave_h"]      is not None else "n/a"
        _wp_txt  = f"@ {_lpd['wave_period']:.0f}s" if _lpd["wave_period"] is not None else ""
        _sst_txt = f"{_lpd['sst_c']:.1f}C"     if _lpd["sst_c"]       is not None else "n/a"
        _cv_txt  = f"{_lpd['curr_v']:.2f}kn"   if _lpd["curr_v"]      is not None else "n/a"
        _cd_txt  = f"{_lpd['curr_dir']:.0f}deg" if _lpd["curr_dir"]   is not None else ""
        ui_surface.blit(font.render(f"WAVE: {_wh_txt} {_wp_txt}", True, SONAR_CYAN), (_lp_x+8, _lp_y+28))
        ui_surface.blit(font.render(f"CURR: {_cv_txt}  {_cd_txt}", True, SONAR_CYAN), (_lp_x+8, _lp_y+46))
        ui_surface.blit(font.render(f"SST : {_sst_txt}", True, SONAR_CYAN), (_lp_x+8, _lp_y+64))
        _age_str = f"Updated: {_age}s ago" if _lpd["last_update"] else "Awaiting CMEMS data..."
        ui_surface.blit(font.render(_age_str, True, (120,120,120)), (_lp_x+8, _lp_y+90))

        render_ui_to_opengl(ui_surface, ui_tex_id, screen_w, screen_h)
        pygame.display.flip()

if __name__ == "__main__": 
    run_unified_sim()