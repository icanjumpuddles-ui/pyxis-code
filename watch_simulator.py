"""
Pyxis Watch Simulator v3.0 — Grounded Edition
Mirrors the REAL PyxisMainMenu and ALL sub-views from Garminbenfish.mc v4.3.3
Correct routing for every menu item:
  wind_rose  → GET /sea_state_json/<cb>  → animated vector compass (WindRoseView)
  sea_state  → ActionMenu confirm → GET /seastate/<cb>/z<z>/meteo_map (image)
               ENTER sub-button → GET /sea_state_json/<cb> (SeaStateDataView)
  wind_map   → GET /wind_map/<cb>/z<z>/wind_map (image)
  wx_radar   → ActionMenu confirm → GET /weather_radar/<cb>/weather_map.jpg?z=<z>&lat=&lon= (image)
  topo_map   → GET /topo_map/<cb>/topo.jpg (image)
  nautical   → GET /nautical_map/<cb>/chart.jpg (image)
  map/osint  → POST /gemini MAP_REQ → GET /ais_radar_map/<r>/<hdg>/<cb> (image)
  adsb       → GET /adsb_contacts (json list)
  adsb_geo   → GET /adsb_radar_map?z=<z> (image)
"""

import tkinter as tk
from tkinter import ttk, font
import threading
import requests
from io import BytesIO
from PIL import Image, ImageTk, ImageDraw, ImageFont
import json
import time
import math
import os

PROXY = os.environ.get("PYXIS_PROXY_URL", "https://benfishmanta.duckdns.org")

# ── Exact menu tree from PyxisMainMenu in Garminbenfish.mc ───────────────────
MENU = [
    ("── SITUATION ──",  None,            "sep"),
    ("Live Radar",       "map",           "action"),
    ("AIS Geo Map",      "ais_geo_map",   "action"),
    ("ADSB Track",       "adsb",          "action"),
    ("ADSB Geo Map",     "adsb_geo",      "action"),
    ("OSINT Radar",      "osint_radar",   "action"),
    ("OSINT Geo Map",    "osint_geo_map", "action"),
    ("── WEATHER ──",    None,            "sep"),
    ("WX Radar",         "wx_radar",      "confirm"),   # → ActionMenu confirm
    ("Sea State",        "sea_state",     "confirm"),   # → ActionMenu confirm
    ("Wave Rose",        "wind_rose",     "action"),    # → direct WindRoseView
    ("Wind Map",         "wind_map",      "action"),    # → direct WindMapView
    ("── INTEL ──",      None,            "sep"),
    ("Message Inbox",    "inbox",         "action"),
    ("Regional OSINT",   "intel",         "action"),
    ("Morning Brief",    "day_brief",     "action"),
    ("Evening Brief",    "night_brief",   "action"),
    ("── NAVIGATION ──", None,            "sep"),
    ("Active Route",     "item_nav",      "action"),
    ("Req Routing",      "nav",           "submenu"),
    ("Nav Importer",     "pull_nav",      "confirm"),
    ("3D Sonar",         "sonar",         "action"),
    ("Topo Map",         "topo_map",      "action"),
    ("Naut Chart",       "nautical_map",  "action"),
    ("── SYSTEMS ──",    None,            "sep"),
    ("Engine Room",      "engine",        "submenu"),
    ("UUV Squadron",     "uuv",           "submenu"),
    ("Environment",      "sensor",        "action"),
    ("── CREW OPS ──",   None,            "sep"),
    ("M.O.B Search",     "mob",           "action"),
    ("AI Voice",         "voice",         "action"),
    ("Onboard Pyxis",    "onboard_pyxis", "toggle"),
    ("LIVE Source",      "toggle_sim",    "toggle"),
]

# Sub-menus from mc source
SUBMENUS = {
    "engine": [("Start Generator", "sys_gen_start"),
               ("Stop Generator",  "sys_gen_stop"),
               ("Bilge Override",  "sys_bilge_auto")],
    "nav":    [("Nearest Anchorage", "nav_ANCHORAGE"),
               ("Nearest Port",      "nav_PORT"),
               ("RED ALERT (SOS)",   "nav_MAYDAY")],
    "uuv":    [("Deploy Patrol", "uuv_DEPLOY"),
               ("Recall Drone",  "uuv_RECALL")],
}

# ── Garmin AMOLED colour palette ─────────────────────────────────────────────
BG      = "#000000"
PANEL   = "#0d0d0d"
SEP_FG  = "#223344"
SEP_BG  = "#111111"
ACT_FG  = "#00d2ff"
ACT_BG  = "#0a1a2a"
HOV_BG  = "#001f3f"
SUB_FG  = "#7fd7ff"
WARN_FG = "#ff6600"
OK_FG   = "#00ff88"
RED_FG  = "#ff0033"
LOG_FG  = "#10b981"
LOG_BG  = "#020617"
BTN_BG  = "#1a2a3a"
CONF_FG = "#ffcc00"


# ─────────────────────────────────────────────────────────────────────────────
# Live Radar — mirrors LiveRadarView in Garminbenfish.mc
# POSTs to /gemini prompt=MAP_REQ every 3 s, renders contacts as vector scope.
# ─────────────────────────────────────────────────────────────────────────────
class LiveRadarCanvas:
    """Pulsing AIS vector scope. Mirrors LiveRadarView auto-refresh + drawVector."""

    TYPE_STYLE = {
        "DRONE":   ("circle",   "#0066FF"),
        "ALARM":   ("square",   "#FF6600"),
        "HOSTILE": ("tri",      "#8800CC"),
        "MARKER":  ("cross",    "#FFFF00"),
        "SHOAL":   ("dot",      "#FF6600"),
    }
    DEFAULT_STYLE = ("tri", "#00FF00")  # AIS vessel

    def __init__(self, canvas: tk.Canvas, proxy, lat, lon, log_fn):
        self.canvas = canvas
        self.proxy  = proxy
        self.lat    = lat
        self.lon    = lon
        self._log   = log_fn
        self.W, self.H  = 454, 454
        self.cx = self.W // 2
        self.cy = self.H // 2
        self.r  = min(self.W, self.H) // 2 - 12
        self._contacts  = []
        self._radius_nm = 5.0
        self._sel_id    = None
        self._history   = {}
        self._tick      = 0
        self._ping_r    = 0
        self._loading   = True
        self._timer_id  = None
        self._refresh_id= None

    def start(self):
        self._fetch()
        self._animate()

    def stop(self):
        for attr in ("_timer_id", "_refresh_id"):
            tid = getattr(self, attr, None)
            if tid:
                self.canvas.after_cancel(tid)
                setattr(self, attr, None)

    def zoom_in(self):
        self._radius_nm = max(1.0, self._radius_nm * 0.5)
        self._draw()

    def zoom_out(self):
        self._radius_nm = min(100.0, self._radius_nm * 2.0)
        self._draw()

    def next_contact(self):
        if self._contacts:
            idx = self._cur_idx()
            idx = (idx + 1) % len(self._contacts)
            self._sel_id = self._contacts[idx].get("id")
            self._draw()

    def prev_contact(self):
        if self._contacts:
            idx = self._cur_idx()
            idx = (idx - 1) % len(self._contacts)
            self._sel_id = self._contacts[idx].get("id")
            self._draw()

    def _cur_idx(self):
        for i, c in enumerate(self._contacts):
            if c.get("id") == self._sel_id:
                return i
        return 0

    def _fetch(self):
        def _do():
            try:
                params = {"prompt": "MAP_REQ", "lat": self.lat,
                          "lon": self.lon, "onboard": False}
                self._log(f"POST /gemini  prompt=MAP_REQ  lat={self.lat} lon={self.lon}")
                r = requests.post(f"{self.proxy}/gemini", json=params, timeout=12)
                self._log(f"  HTTP {r.status_code}  {len(r.content):,}B")
                if r.status_code == 200:
                    data = r.json()
                    # Full response to debug log
                    self._log("  RESPONSE KEYS: " + str(list(data.keys())))
                    contacts = data.get("radar_contacts", [])
                    self._log(f"  radar_contacts: {len(contacts)} contacts")
                    if contacts:
                        self._log(f"  sample[0]: {json.dumps(contacts[0])[:120]}")
                    else:
                        self._log(f"  watch_summary: {data.get('watch_summary','?')}")
                    self.canvas.after(0, self._on_contacts, contacts)
                else:
                    self._log(f"  BODY: {r.text[:300]}")
                    self.canvas.after(0, self._on_contacts, [])
            except Exception as e:
                self._log(f"  MAP_REQ error: {e}")
                self.canvas.after(0, self._on_contacts, [])
            # 3-second refresh (matches Garminbenfish.mc timer)
            self._refresh_id = self.canvas.after(3000, self._fetch)
        threading.Thread(target=_do, daemon=True).start()

    def _on_contacts(self, contacts):
        self._contacts = contacts
        self._loading  = False
        self._ping_r   = 2   # start expanding ping ring
        if contacts and self._sel_id is None:
            self._sel_id = contacts[0].get("id")

    def _animate(self):
        self._tick += 1
        if self._ping_r > 0:
            self._ping_r += 10
            if self._ping_r > self.r:
                self._ping_r = 0
        self._draw()
        self._timer_id = self.canvas.after(100, self._animate)  # 10fps

    def _draw(self):
        S = self.canvas
        S.delete("all")
        cx, cy, r = self.cx, self.cy, self.r

        S.create_rectangle(0, 0, self.W, self.H, fill="#000000", outline="")

        # Scope rings (blue)
        for frac in (1.0, 0.66, 0.33):
            rr = int(r * frac)
            S.create_oval(cx-rr, cy-rr, cx+rr, cy+rr, outline="#0000AA", width=1)

        # Crosshairs (dark blue)
        S.create_line(cx, cy-r, cx, cy+r, fill="#000044", width=1)
        S.create_line(cx-r, cy, cx+r, cy, fill="#000044", width=1)

        # Expanding ping ring (data refresh pulse)
        if self._ping_r > 0:
            pr = self._ping_r
            alpha = max(0, 1.0 - pr / r)
            g = int(alpha * 140); b = int(alpha * 220)
            S.create_oval(cx-pr, cy-pr, cx+pr, cy+pr,
                          outline=f"#00{g:02x}{b:02x}", width=2)

        # Heading line + Pyxis triangle
        S.create_line(cx, cy-6, cx, cy-42, fill="#FFFFFF", width=2)
        S.create_polygon(cx, cy-6, cx-4, cy+4, cx+4, cy+4,
                         fill="#FFFFFF", outline="")

        # Plot contacts
        sel_contact = None
        for c in self._contacts:
            brg = c.get("bearing"); dist = c.get("range_nm")
            if brg is None or dist is None:
                continue
            try:
                bd = float(brg); dd = float(dist)
            except (TypeError, ValueError):
                continue
            if dd > self._radius_nm:
                continue

            theta = math.radians(bd - 90)
            pd = (dd / self._radius_nm) * r
            tx = cx + math.cos(theta) * pd
            ty = cy + math.sin(theta) * pd
            cid = c.get("id"); typ = c.get("type")

            # Comet trail
            if cid:
                hist = self._history.get(cid, [])
                hist.append((tx, ty))
                if len(hist) > 4:
                    hist = hist[-4:]
                self._history[cid] = hist
                for i in range(1, len(hist)):
                    S.create_line(*hist[i-1], *hist[i], fill="#440088", width=1)

            shape, col = self.TYPE_STYLE.get(typ, self.DEFAULT_STYLE)
            tx_i, ty_i = int(tx), int(ty)
            if shape == "circle":
                S.create_oval(tx_i-6, ty_i-6, tx_i+6, ty_i+6, outline=col, width=2)
            elif shape == "square":
                S.create_rectangle(tx_i-4, ty_i-4, tx_i+4, ty_i+4, fill=col, outline="")
            elif shape == "tri":
                S.create_polygon(tx_i, ty_i-5, tx_i-3, ty_i+4, tx_i+3, ty_i+4,
                                 fill=col, outline="")
            elif shape == "cross":
                S.create_line(tx_i-4, ty_i, tx_i+4, ty_i, fill=col)
                S.create_line(tx_i, ty_i-4, tx_i, ty_i+4, fill=col)
            elif shape == "dot":
                S.create_oval(tx_i-3, ty_i-3, tx_i+3, ty_i+3, fill=col, outline="")

            # CPA line (contact < 1nm and moving)
            try:
                if dd < 1.0 and float(c.get("speed", 0) or 0) > 0.5:
                    S.create_line(cx, cy, tx_i, ty_i, fill="#FF0000", width=3)
            except Exception:
                pass

            # Selection bracket
            if cid and cid == self._sel_id:
                sel_contact = c
                bw = 12
                for dx, dy in ((-bw,-bw),(bw,-bw),(-bw,bw),(bw,bw)):
                    ix, iy = tx_i+dx, ty_i+dy
                    ex = 5 if dx < 0 else -5
                    ey = 5 if dy < 0 else -5
                    S.create_line(ix, iy, ix-ex, iy, fill="#0099FF", width=2)
                    S.create_line(ix, iy, ix, iy-ey, fill="#0099FF", width=2)

        # MARPA readout for selected contact
        if sel_contact:
            name = str(sel_contact.get("name") or sel_contact.get("id") or "UNK")[:10]
            def fv(k, fmt=".1f"):
                v = sel_contact.get(k)
                try: return format(float(v), fmt)
                except Exception: return "?"
            y0 = self.H - 150
            for txt in (f"TGT: {name}",
                        f"RNG:{fv('range_nm')}nm  BRG:{fv('bearing','.0f')}",
                        f"CRSE:{fv('heading','.0f')}  SOG:{fv('speed')}kn"):
                S.create_text(cx, y0, text=txt, fill="#0099FF",
                              font=("Consolas", 7, "bold"))
                y0 += 18

        # Radar range label
        S.create_text(cx, 14, text=f"RADAR: {self._radius_nm:.0f}nm",
                      fill="#00FF00", font=("Consolas", 8, "bold"))

        if self._loading:
            S.create_text(cx, cy+30, text="LOADING CONTACTS...",
                          fill="#00AADD", font=("Consolas", 9))
        elif not self._contacts:
            S.create_text(cx, cy+30, text="NO CONTACTS IN RANGE",
                          fill="#333333", font=("Consolas", 9))


# ─────────────────────────────────────────────────────────────────────────────
# Wave Rose renderer — mirrors WindRoseView + drawVector from Garminbenfish.mc
# ─────────────────────────────────────────────────────────────────────────────
class WindRoseCanvas:
    """Draws the animated multi-vector environmental compass on a tkinter Canvas."""

    COLORS = {
        "wind":    "#00FFFF",   # Cyan
        "wave":    "#DDDDDD",   # White/grey
        "swell":   "#FF8800",   # Orange
        "current": "#00CC00",   # Green
        "wind_ww": "#AAAAAA",   # Grey (CMEMS wind-wave)
    }

    def __init__(self, canvas: tk.Canvas, W=454, H=454):
        self.canvas = canvas
        self.W = W
        self.H = H
        self.cx = W // 2
        self.cy = H // 2
        self.r  = min(W, H) // 2 - 32
        self._tick = 0
        self._data = {}
        self._timer_id = None

    def load(self, data: dict):
        self._data = data
        self._tick = 0
        self._start_animation()

    def stop(self):
        if self._timer_id:
            self.canvas.after_cancel(self._timer_id)
            self._timer_id = None

    def _start_animation(self):
        self.stop()
        self._animate()

    def _animate(self):
        self._draw()
        self._tick += 1
        self._timer_id = self.canvas.after(250, self._animate)  # 4 fps, matches mc 250ms timer

    def _parse_brg(self, s) -> float:
        """Parse bearing from proxy value. Handles '204', 'SSW (204°)', 204.0, None."""
        if s is None:
            return -1.0
        s = str(s).strip()
        if not s or s in ("n/a", "None", ""):
            return -1.0
        # Look for (204°) pattern
        i = s.find("(")
        j = s.find("°")
        if i >= 0 and j > i:
            try:
                return float(s[i+1:j])
            except ValueError:
                pass
        # Plain numeric
        try:
            v = float(s)
            return v if v >= 0 else -1.0
        except ValueError:
            return -1.0

    def _draw_vector(self, brg: float, color: str, thick: int,
                     radius: int, tail_radius: int, label: str):
        """Mirror drawVector() from Garminbenfish.mc with animated walking ticks."""
        if brg < 0:
            return
        rad = math.radians(brg)
        cx, cy = self.cx, self.cy

        tip_x = cx + radius * math.sin(rad)
        tip_y = cy - radius * math.cos(rad)
        start_x = cx - tail_radius * math.sin(rad)
        start_y = cy + tail_radius * math.cos(rad)

        # Main shaft
        self.canvas.create_line(start_x, start_y, tip_x, tip_y,
                                fill=color, width=thick)

        # Arrowhead
        perp = math.radians(brg + 90)
        hs = 9
        hx1 = tip_x - hs*math.sin(rad) + (hs*0.5)*math.sin(perp)
        hy1 = tip_y + hs*math.cos(rad) - (hs*0.5)*math.cos(perp)
        hx2 = tip_x - hs*math.sin(rad) - (hs*0.5)*math.sin(perp)
        hy2 = tip_y + hs*math.cos(rad) + (hs*0.5)*math.cos(perp)
        self.canvas.create_polygon(tip_x, tip_y, hx1, hy1, hx2, hy2,
                                   fill=color, outline=color)

        # Feather at tail
        tfx = start_x + 6*math.sin(perp)
        tfy = start_y - 6*math.cos(perp)
        self.canvas.create_line(start_x, start_y, tfx, tfy, fill=color, width=1)

        # Animated walking ticks along shaft
        spacing = 16
        anim_offset = self._tick % spacing
        d = anim_offset
        while d < radius - 12:
            if d >= 8:
                px = cx + d * math.sin(rad)
                py = cy - d * math.cos(rad)
                fsize = 6
                fx = px + fsize * math.sin(perp)
                fy = py - fsize * math.cos(perp)
                self.canvas.create_line(int(px), int(py), int(fx), int(fy),
                                       fill=color, width=1)
            d += spacing

        # Tip label
        lx = cx + (radius + 16) * math.sin(rad)
        ly = cy - (radius + 16) * math.cos(rad)
        self.canvas.create_text(int(lx), int(ly), text=label,
                                fill=color, font=("Consolas", 7, "bold"))

    def _draw(self):
        S = self.canvas
        S.delete("all")
        cx, cy, r = self.cx, self.cy, self.r

        # Background
        S.create_rectangle(0, 0, self.W, self.H, fill=BG, outline="")

        # Compass rings (static)
        S.create_oval(cx-r, cy-r, cx+r, cy+r, outline="#003300", width=1)
        S.create_oval(cx-r//2, cy-r//2, cx+r//2, cy+r//2, outline="#002200", width=1)
        S.create_line(cx, cy-r, cx, cy+r, fill="#224422", width=1)
        S.create_line(cx-r, cy, cx+r, cy, fill="#224422", width=1)

        # NSEW labels
        S.create_text(cx,     cy-r+10, text="N", fill="#446644", font=("Consolas", 8))
        S.create_text(cx+r-8, cy,      text="E", fill="#446644", font=("Consolas", 8))
        S.create_text(cx,     cy+r-14, text="S", fill="#446644", font=("Consolas", 8))
        S.create_text(cx-r+6, cy,      text="W", fill="#446644", font=("Consolas", 8))

        d = self._data
        if not d:
            S.create_text(cx, cy, text="NO DATA", fill=RED_FG,
                          font=("Consolas", 11, "bold"))
            return

        # ── Bearing resolution (numeric deg field → string fallback) ───────────
        def brg(deg_key, str_key):
            v = d.get(deg_key)
            if v is not None:
                try:
                    return float(v)
                except Exception:
                    pass
            return self._parse_brg(d.get(str_key))

        wd  = brg("wind_dir_deg",  "wind_dir")
        wv  = brg("wave_dir_deg",  "wave_dir")
        sw  = brg("swell_dir_deg", "swell_dir")
        cd  = brg("curr_dir_deg",  "curr_dir")
        wwd = self._parse_brg(d.get("wind_ww_deg"))

        def fmt(key, unit=""):
            v = d.get(key)
            return f"{v}{unit}" if v is not None else "?"

        wsp_s  = fmt("wind_sp")
        wvh_s  = fmt("wave_h")
        swh_s  = fmt("swell_h")
        cv_s   = fmt("curr_v")

        # 1. Wind (Cyan) — blowing direction = from + 180
        if wd >= 0:
            blow = (int(wd) + 180) % 360
            self._draw_vector(float(blow), self.COLORS["wind"],
                              3, r, 110, f"W:{wsp_s}")

        # 2. Wave (White/grey)
        if wv >= 0:
            blow = (int(wv) + 180) % 360
            self._draw_vector(float(blow), self.COLORS["wave"],
                              2, r-30, 100, f"Wv:{wvh_s}")

        # 3. Swell (Orange)
        if sw >= 0:
            blow = (int(sw) + 180) % 360
            self._draw_vector(float(blow), self.COLORS["swell"],
                              2, r-60, 90, f"Sw:{swh_s}")

        # 4. Current (Green)
        if cd >= 0:
            blow = (int(cd) + 180) % 360
            self._draw_vector(float(blow), self.COLORS["current"],
                              2, r//2-20, 80, f"C:{cv_s}")

        # 5. CMEMS wind-wave (grey, thinner)
        if wwd >= 0:
            blow = (int(wwd) + 180) % 360
            self._draw_vector(float(blow), self.COLORS["wind_ww"],
                              1, r-10, 105, "WW")

        # Bottom strip — summary values (mirrors watch bottom strip)
        row1y = self.H - 36
        row2y = self.H - 18
        S.create_text(cx//2,    row1y, text=f"W:{wsp_s}",  fill=self.COLORS["wind"],    font=("Consolas", 8, "bold"))
        S.create_text(cx+cx//2, row1y, text=f"Wv:{wvh_s}", fill=self.COLORS["wave"],    font=("Consolas", 8, "bold"))
        S.create_text(cx//2,    row2y, text=f"Sw:{swh_s}", fill=self.COLORS["swell"],   font=("Consolas", 8, "bold"))
        S.create_text(cx+cx//2, row2y, text=f"C:{cv_s}",   fill=self.COLORS["current"], font=("Consolas", 8, "bold"))

        # Header label
        S.create_text(cx, 14, text="WAVE ROSE  (WIND/WAVE/SWELL/CURR)",
                      fill="#003300", font=("Consolas", 7))


# ─────────────────────────────────────────────────────────────────────────────
# Main simulator app
# ─────────────────────────────────────────────────────────────────────────────
class PyxisWatchSim(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Pyxis v4.3.3 — Watch Protocol Simulator")
        self.configure(bg=BG)
        self.resizable(True, True)

        self.lat_var   = tk.StringVar(value="-38.487")
        self.lon_var   = tk.StringVar(value="145.620")
        self.zoom_var  = tk.IntVar(value=10)
        self.sys_var   = tk.StringVar(value="remote")
        self.onboard   = False
        self.sim_mode  = False

        self.current_img  = None   # prevent PhotoImage GC
        self._wind_rose   = None   # active WindRoseCanvas instance
        self._live_radar  = None   # active LiveRadarCanvas instance
        self._cur_view    = None   # string: 'radar','wind_rose','image','json'
        self._build_ui()
        self._draw_standby()

    # ── Layout ────────────────────────────────────────────────────────────────
    def _build_ui(self):
        bar = tk.Frame(self, bg="#0a0a0a", pady=6, padx=12)
        bar.pack(fill=tk.X, side=tk.TOP)

        for label, var, w in [("CREW LAT", self.lat_var, 11),
                               ("CREW LON", self.lon_var, 11)]:
            tk.Label(bar, text=label+":", bg="#0a0a0a", fg=ACT_FG,
                     font=("Consolas", 9, "bold")).pack(side=tk.LEFT, padx=(8,1))
            tk.Entry(bar, textvariable=var, width=w, bg="#111", fg="white",
                     insertbackground="white", font=("Consolas", 9),
                     relief=tk.FLAT, bd=1).pack(side=tk.LEFT, padx=(0,6))

        tk.Label(bar, text="ZOOM:", bg="#0a0a0a", fg="#ffcc00",
                 font=("Consolas", 9, "bold")).pack(side=tk.LEFT, padx=(8,1))
        tk.Spinbox(bar, from_=2, to=15, textvariable=self.zoom_var, width=3,
                   bg="#111", fg="white", insertbackground="white",
                   font=("Consolas", 9), relief=tk.FLAT).pack(side=tk.LEFT, padx=(0,6))

        self.sys_btn = tk.Button(bar, text="SYS: REMOTE", bg="#1a0000", fg=RED_FG,
                                 font=("Consolas", 9, "bold"), relief=tk.FLAT,
                                 command=self._toggle_sys, cursor="hand2")
        self.sys_btn.pack(side=tk.LEFT, padx=8)

        self.conn_lbl = tk.Label(bar, text="⬤ benfishmanta.duckdns.org",
                                 bg="#0a0a0a", fg="#334455",
                                 font=("Consolas", 8))
        self.conn_lbl.pack(side=tk.RIGHT, padx=8)

        body = tk.Frame(self, bg=BG)
        body.pack(fill=tk.BOTH, expand=True)

        # Left: scrollable menu
        left = tk.Frame(body, bg=PANEL, width=200)
        left.pack(side=tk.LEFT, fill=tk.Y)
        left.pack_propagate(False)

        tk.Label(left, text="PYXIS C3", bg=PANEL, fg=ACT_FG,
                 font=("Consolas", 12, "bold"), pady=8).pack(fill=tk.X)

        scroll_frame = tk.Frame(left, bg=PANEL)
        scroll_frame.pack(fill=tk.BOTH, expand=True)

        canvas_m = tk.Canvas(scroll_frame, bg=PANEL, highlightthickness=0)
        sb = ttk.Scrollbar(scroll_frame, orient="vertical", command=canvas_m.yview)
        self.menu_inner = tk.Frame(canvas_m, bg=PANEL)
        self.menu_inner.bind("<Configure>",
            lambda e: canvas_m.configure(scrollregion=canvas_m.bbox("all")))
        canvas_m.create_window((0,0), window=self.menu_inner, anchor="nw")
        canvas_m.configure(yscrollcommand=sb.set)
        canvas_m.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas_m.bind_all("<MouseWheel>",
            lambda e: canvas_m.yview_scroll(-1*(e.delta//120), "units"))

        self._build_menu()

        # Centre: 454×454 watch face
        watch_frame = tk.Frame(body, bg=BG)
        watch_frame.pack(side=tk.LEFT, padx=20, pady=12)

        bezel = tk.Frame(watch_frame, bg="#1a1a1a",
                         highlightthickness=3, highlightbackground="#2a3a4a")
        bezel.pack()
        self.screen = tk.Canvas(bezel, bg=BG, width=454, height=454,
                                highlightthickness=0)
        self.screen.pack()

        # Control bar — contextual buttons for the active view (below bezel)
        self._ctrl_bar = tk.Frame(watch_frame, bg="#0a0a0a", pady=4)
        self._ctrl_bar.pack(fill=tk.X)
        # Populated dynamically by _set_controls()

        # Right: log console
        self.log = tk.Text(body, bg=LOG_BG, fg=LOG_FG,
                           font=("Consolas", 8), wrap=tk.WORD,
                           state=tk.DISABLED, relief=tk.FLAT)
        self.log.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True,
                      padx=10, pady=10)

        self._log("Pyxis Watch Simulator v3.0 ready.")
        self._log(f"Proxy: {PROXY}")

    def _build_menu(self):
        for label, item_id, kind in MENU:
            if kind == "sep":
                f = tk.Frame(self.menu_inner, bg=SEP_BG, pady=2)
                f.pack(fill=tk.X, padx=4, pady=1)
                tk.Label(f, text=label, bg=SEP_BG, fg=SEP_FG,
                         font=("Consolas", 7, "bold")).pack()
            elif kind == "toggle":
                b = tk.Button(self.menu_inner, text=label,
                              bg="#1a0a00", fg=WARN_FG,
                              font=("Consolas", 8), relief=tk.FLAT,
                              activebackground="#2a1500",
                              activeforeground=WARN_FG,
                              cursor="hand2", anchor="w", padx=8, pady=4)
                b.pack(fill=tk.X, padx=4, pady=1)
                b.configure(command=lambda i=item_id, btn=b: self._on_toggle(i, btn))
            elif kind == "submenu":
                b = tk.Button(self.menu_inner, text=f"▶  {label}",
                              bg=ACT_BG, fg=SUB_FG,
                              font=("Consolas", 8), relief=tk.FLAT,
                              activebackground=HOV_BG, activeforeground=ACT_FG,
                              cursor="hand2", anchor="w", padx=8, pady=4)
                b.pack(fill=tk.X, padx=4, pady=1)
                b.configure(command=lambda i=item_id, lbl=label: self._open_submenu(i, lbl))
            elif kind == "confirm":
                # Mirrors ActionMenu: two-step Select/Cancel before firing
                b = tk.Button(self.menu_inner, text=f"⚠  {label}",
                              bg="#0a0a00", fg=CONF_FG,
                              font=("Consolas", 8), relief=tk.FLAT,
                              activebackground="#1a1a00", activeforeground="white",
                              cursor="hand2", anchor="w", padx=8, pady=4)
                b.pack(fill=tk.X, padx=4, pady=1)
                b.configure(command=lambda i=item_id, lbl=label: self._open_confirm(i, lbl))
            else:  # "action"
                b = tk.Button(self.menu_inner, text=label,
                              bg=ACT_BG, fg=ACT_FG,
                              font=("Consolas", 8), relief=tk.FLAT,
                              activebackground=HOV_BG, activeforeground="white",
                              cursor="hand2", anchor="w", padx=8, pady=4)
                b.pack(fill=tk.X, padx=4, pady=1)
                b.configure(command=lambda i=item_id, lbl=label: self._dispatch(i, lbl))

    # ── ActionMenu confirm dialog (mirrors real watch Select/Cancel) ───────────
    def _open_confirm(self, item_id, title):
        win = tk.Toplevel(self, bg=BG)
        win.title(f"PYXIS ▶ {title.upper()}")
        win.configure(bg=BG)
        win.resizable(False, False)

        tk.Label(win, text=title.upper(), bg=BG, fg=CONF_FG,
                 font=("Consolas", 11, "bold"), pady=12).pack()
        tk.Label(win, text="Confirm Action", bg=BG, fg="#888888",
                 font=("Consolas", 8), pady=2).pack()

        tk.Button(win, text="✔  Select", bg="#001a00", fg=OK_FG,
                  font=("Consolas", 10, "bold"), relief=tk.FLAT,
                  cursor="hand2", width=20, pady=8,
                  command=lambda: [win.destroy(), self._dispatch(item_id, title)]).pack(padx=20, pady=6)

        tk.Button(win, text="✘  Cancel", bg="#1a0000", fg="#554444",
                  font=("Consolas", 9), relief=tk.FLAT,
                  cursor="hand2", width=20, pady=6,
                  command=win.destroy).pack(padx=20, pady=2)

    # ── Sub-menu popup ────────────────────────────────────────────────────────
    def _open_submenu(self, menu_id, title):
        items = SUBMENUS.get(menu_id, [])
        if not items:
            self._dispatch(menu_id, title)
            return

        win = tk.Toplevel(self, bg=BG)
        win.title(f"PYXIS ▶ {title.upper()}")
        win.configure(bg=BG)
        win.resizable(False, False)

        tk.Label(win, text=title.upper(), bg=BG, fg=ACT_FG,
                 font=("Consolas", 11, "bold"), pady=10).pack()

        for lbl, cmd_id in items:
            b = tk.Button(win, text=lbl, bg=BTN_BG, fg=ACT_FG,
                          font=("Consolas", 9), relief=tk.FLAT,
                          activebackground=HOV_BG, activeforeground="white",
                          cursor="hand2", width=24, pady=6,
                          command=lambda c=cmd_id, w=win: self._submenu_action(c, w))
            b.pack(fill=tk.X, padx=20, pady=3)

        tk.Button(win, text="◀  BACK", bg="#0a0a0a", fg="#445566",
                  font=("Consolas", 8), relief=tk.FLAT,
                  command=win.destroy, pady=6).pack(pady=8)

    def _submenu_action(self, cmd_id, win):
        win.destroy()
        self._post_command(cmd_id)

    # ── Toggle items ──────────────────────────────────────────────────────────
    def _on_toggle(self, item_id, btn):
        if item_id == "onboard_pyxis":
            self.onboard = not self.onboard
            if self.onboard:
                btn.configure(bg="#001a00", fg=OK_FG, text="Onboard Pyxis  ✔ ON")
                self.sys_var.set("onboard")
                self.sys_btn.configure(text="SYS: ONBOARD", bg="#001a00", fg=OK_FG)
            else:
                btn.configure(bg="#1a0000", fg=WARN_FG, text="Onboard Pyxis  ✘ OFF")
                self.sys_var.set("remote")
                self.sys_btn.configure(text="SYS: REMOTE", bg="#1a0000", fg=RED_FG)
        elif item_id == "toggle_sim":
            self.sim_mode = not self.sim_mode
            btn.configure(text="SIM Source  ✔ SIM" if self.sim_mode else "LIVE Source  ✘ SIM")
        self._post_command(item_id)

    def _toggle_sys(self):
        self.onboard = not self.onboard
        if self.onboard:
            self.sys_var.set("onboard")
            self.sys_btn.configure(text="SYS: ONBOARD", bg="#001a00", fg=OK_FG)
        else:
            self.sys_var.set("remote")
            self.sys_btn.configure(text="SYS: REMOTE", bg="#1a0000", fg=RED_FG)
        self._log(f"SYS MODE → {'ONBOARD' if self.onboard else 'REMOTE'}")

    # ── Main dispatch — maps item_id to correct proxy call ────────────────────
    def _dispatch(self, item_id, label):
        self._stop_all()
        self._set_controls([])   # clear controls while loading
        self._log(f"\n{'─'*40}")
        self._log(f"MENU → {label}  [{item_id}]")

        cb   = int(time.time())
        lat  = self.lat_var.get()
        lon  = self.lon_var.get()
        zoom = self.zoom_var.get()

        # ── Live Radar pulsing AIS scope — LiveRadarView in Garminbenfish.mc
        if item_id in ("map", "osint_radar"):
            self._log("→ LiveRadarView: POST /gemini MAP_REQ every 3s, local vector scope")
            self.screen.delete("all")
            self.screen.create_text(227, 227, text="LOADING RADAR...",
                                    fill="#00AADD", font=("Consolas", 12, "bold"))
            lr = LiveRadarCanvas(self.screen, PROXY, lat, lon, self._log)
            self._live_radar = lr
            lr.start()
            # Control bar: zoom + contact cycling + zoom out
            self._set_controls([
                ("◀ PREV",   ACT_FG,  lr.prev_contact),
                ("ZOOM +",   OK_FG,   lr.zoom_in),
                ("ZOOM -",   OK_FG,   lr.zoom_out),
                ("NEXT ▶",   ACT_FG,  lr.next_contact),
            ])

        # ── AIS Geo Map / OSINT Geo Map
        elif item_id in ("ais_geo_map", "osint_geo_map"):
            url = f"{PROXY}/ais_radar_map/50/0.0/{cb}"
            threading.Thread(target=self._fetch_image,
                             args=(url, label, {"lat": lat, "lon": lon}),
                             daemon=True).start()
            self._set_controls([
                ("ZOOM +", OK_FG, lambda: self._rezoom_image(item_id, label, -1)),
                ("ZOOM -", OK_FG, lambda: self._rezoom_image(item_id, label, +1)),
            ])

        # ── ADSB Track — JSON contacts list
        elif item_id == "adsb":
            url = f"{PROXY}/adsb_contacts"
            threading.Thread(target=self._fetch_json,
                             args=(url, label, {"Authorization": "Basic YWRtaW46bWFudGE="}),
                             daemon=True).start()

        # ── ADSB Geo Map — server-rendered image
        elif item_id == "adsb_geo":
            url = f"{PROXY}/adsb_radar_map"
            threading.Thread(target=self._fetch_image,
                             args=(url, label, {"z": zoom, "bust": cb}),
                             daemon=True).start()

        # ── WX Radar — /weather_radar/<cb>/weather_map.jpg
        elif item_id == "wx_radar":
            url = f"{PROXY}/weather_radar/{cb}/weather_map.jpg"
            threading.Thread(target=self._fetch_image,
                             args=(url, label, {"z": zoom, "lat": lat, "lon": lon}),
                             daemon=True).start()

        # ── Sea State Map — /seastate/<cb>/z<z>/meteo_map
        elif item_id == "sea_state":
            z = max(1, min(4, zoom // 3))
            url = f"{PROXY}/seastate/{cb}/z{z}/meteo_map"
            self._log(f"  Using z={z} (sea state 1-4 scale)")
            threading.Thread(target=self._fetch_image,
                             args=(url, label, {}),
                             daemon=True).start()
            # After image loads, show Data Brief sub-button
            self._set_controls([
                ("DATA BRIEF →", OK_FG,
                 lambda: self._fetch_and_show_json(f"{PROXY}/sea_state_json/{int(time.time())}", "Sea State Data")),
            ])

        # ── Wave Rose — /sea_state_json/<cb> → animated vector compass
        elif item_id == "wind_rose":
            url = f"{PROXY}/sea_state_json/{cb}"
            self._log("→ WindRoseView: fetching sea_state_json for vector compass")
            threading.Thread(target=self._fetch_wind_rose,
                             args=(url,), daemon=True).start()

        # ── Wind Map — /wind_map/<cb>/z<z>/wind_map
        elif item_id == "wind_map":
            z = max(1, min(4, zoom // 3))
            url = f"{PROXY}/wind_map/{cb}/z{z}/wind_map"
            threading.Thread(target=self._fetch_image,
                             args=(url, label, {}),
                             daemon=True).start()

        # ── Topo Map — /topo_map/<cb>/topo.jpg
        elif item_id == "topo_map":
            url = f"{PROXY}/topo_map/{cb}/topo.jpg"
            threading.Thread(target=self._fetch_image,
                             args=(url, label, {"z": zoom, "lat": lat, "lon": lon,
                                               "w": 320, "h": 320}),
                             daemon=True).start()

        # ── Nautical Chart — /nautical_map/<cb>/chart.jpg
        elif item_id == "nautical_map":
            url = f"{PROXY}/nautical_map/{cb}/chart.jpg"
            threading.Thread(target=self._fetch_image,
                             args=(url, label, {"z": zoom, "lat": lat, "lon": lon,
                                               "w": 320, "h": 320}),
                             daemon=True).start()

        # ── Environment — JSON /sea_state_json
        elif item_id == "sensor":
            url = f"{PROXY}/sea_state_json/{cb}"
            threading.Thread(target=self._fetch_json,
                             args=(url, label, {}),
                             daemon=True).start()

        # ── All other cmds — POST to /gemini or /scenario
        else:
            self._post_command(item_id)

    # ── Wind Rose fetch + render ──────────────────────────────────────────────
    def _fetch_wind_rose(self, url: str):
        self._log(f"  GET {url}")
        t0 = time.time()
        try:
            r = requests.get(url, timeout=15)
            elapsed = (time.time() - t0) * 1000
            self._log(f"  HTTP {r.status_code}  {len(r.content):,}B  {elapsed:.0f}ms")
            self.after(0, self._update_conn, r.status_code == 200)
            if r.status_code == 200:
                data = r.json()
                self._log(f"  Keys: {list(data.keys())[:12]}")
                self.after(0, self._show_wind_rose, data)
            else:
                self.after(0, self._show_error, r.status_code, url)
        except Exception as e:
            self._log(f"  NETWORK ERROR: {e}")
            self.after(0, self._show_error, 0, str(e))

    def _show_wind_rose(self, data: dict):
        """Render the animated WindRoseView — exact visual match to Garminbenfish.mc."""
        self._stop_wind_rose()
        self.current_img = None
        self.screen.delete("all")

        wr = WindRoseCanvas(self.screen)
        wr.load(data)
        self._wind_rose = wr

        # SELECT button overlay (mirrors WindRoseDelegate.onSelect → WaveRoseDataView)
        sel_btn = tk.Button(self.screen, text="SELECT → Data Brief",
                            bg="#001a00", fg=OK_FG,
                            font=("Consolas", 7, "bold"), relief=tk.FLAT,
                            cursor="hand2",
                            command=lambda d=data: self._show_wave_rose_data(d))
        self.screen.create_window(227, 454-14, window=sel_btn)

        self._log("✓ Wave Rose rendering (animated). SELECT btn → WaveRoseDataView")
        self._log("  Wind=cyan  Wave=white  Swell=orange  Curr=green  WW=grey")

    def _show_wave_rose_data(self, data: dict):
        """WaveRoseDataView — mirrors the SELECT sub-panel plain language brief."""
        win = tk.Toplevel(self, bg=BG)
        win.title("PYXIS ▶ TACTICAL ENVIRONMENTAL BRIEF")
        win.configure(bg=BG)
        win.resizable(False, False)

        tk.Label(win, text="TACTICAL ENVIRONMENTAL BRIEF",
                 bg=BG, fg=OK_FG, font=("Consolas", 10, "bold"), pady=10).pack()

        rows = [
            ("MET WIND",  f"{data.get('wind_sp','n/a')} @ {data.get('wind_dir','n/a')}"),
            ("SEA WIND",  f"{data.get('wind_ww_h','n/a')} @ {data.get('wind_ww_dir','n/a')}"),
            ("WAVE",      f"{data.get('wave_h','n/a')} @ {data.get('wave_dir','n/a')}"),
            ("SWELL",     f"{data.get('swell_h','n/a')} @ {data.get('swell_dir','n/a')}"),
            ("CURRENT",   f"{data.get('curr_v','n/a')} to {data.get('curr_dir','n/a')}"),
            ("SEA TEMP",  str(data.get('sst_c','n/a'))),
            ("SEA STATE", str(data.get('sea_state','n/a'))),
            ("WAVE PD",   str(data.get('wave_period','n/a'))),
            ("DEPTH",     str(data.get('depth','n/a'))),
            ("WARNINGS",  str(data.get('warnings','NONE'))),
            ("LAT",       str(data.get('pyxis_lat', data.get('lat','n/a')))),
            ("LON",       str(data.get('pyxis_lon', data.get('lon','n/a')))),
            ("HDG",       str(data.get('heading','n/a'))),
            ("SOG",       str(data.get('sog','n/a'))),
            ("UPDATED",   str(data.get('updated','n/a'))),
        ]

        fr = tk.Frame(win, bg=BG)
        fr.pack(padx=20, pady=4)
        for k, v in rows:
            row = tk.Frame(fr, bg=BG)
            row.pack(fill=tk.X, pady=2)
            tk.Label(row, text=f"{k}:", width=12, anchor="w",
                     bg=BG, fg="#888888", font=("Consolas", 9)).pack(side=tk.LEFT)
            warn = "warnings" in k.lower() and v not in ("NONE", "n/a")
            tk.Label(row, text=v, anchor="w",
                     bg=BG, fg=RED_FG if warn else OK_FG,
                     font=("Consolas", 9, "bold")).pack(side=tk.LEFT)

        tk.Button(win, text="◀  BACK", bg="#0a0a0a", fg="#445566",
                  font=("Consolas", 9), relief=tk.FLAT,
                  command=win.destroy, pady=8).pack(pady=10)

    def _stop_wind_rose(self):
        if self._wind_rose:
            self._wind_rose.stop()
            self._wind_rose = None

    def _stop_live_radar(self):
        if self._live_radar:
            self._live_radar.stop()
            self._live_radar = None

    def _stop_all(self):
        """Stop all animated views before switching."""
        self._stop_wind_rose()
        self._stop_live_radar()
        self.current_img = None

    def _set_controls(self, buttons):
        """
        Rebuild the contextual control bar below the watch screen.
        buttons = list of (label, fg_color, callback) tuples.
        An empty list clears the bar.
        """
        for w in self._ctrl_bar.winfo_children():
            w.destroy()
        for lbl, fg, cb in buttons:
            tk.Button(self._ctrl_bar, text=lbl, fg=fg, bg="#0d0d0d",
                      font=("Consolas", 8, "bold"), relief=tk.FLAT,
                      activebackground="#1a2a3a", activeforeground="white",
                      cursor="hand2", padx=10, pady=4,
                      command=cb).pack(side=tk.LEFT, padx=4)


    def _post_command(self, cmd):
        url    = f"{PROXY}/scenario"
        params = self._params()
        params["cmd"] = cmd
        threading.Thread(target=self._do_post, args=(url, params, cmd),
                         daemon=True).start()

    def _do_post(self, url, params, cmd):
        try:
            r = requests.get(url, params=params, timeout=10)
            self._log(f"CMD {cmd} → HTTP {r.status_code}")
        except Exception as e:
            self._log(f"CMD {cmd} FAILED: {e}")

    def _params(self):
        return {
            "lat":  self.lat_var.get(),
            "lon":  self.lon_var.get(),
            "z":    self.zoom_var.get(),
            "sys":  self.sys_var.get(),
            "w":    454, "h": 454,
            "bust": int(time.time()),
        }

    def _fetch_image(self, url: str, label: str, params: dict):
        self._log(f"  GET {url}")
        if params:
            self._log(f"  params={params}")
        t0 = time.time()
        try:
            r = requests.get(url, params=params, timeout=20)
            elapsed = (time.time() - t0) * 1000
            ct = r.headers.get("Content-Type", "?")
            self._log(f"  HTTP {r.status_code}  {len(r.content):,}B  {elapsed:.0f}ms  ct={ct}")
            self.after(0, self._update_conn, r.status_code == 200)
            if r.status_code == 200:
                img = Image.open(BytesIO(r.content)).resize((454, 454), Image.LANCZOS)
                self.after(0, self._show_image, img, label)
            else:
                # Full error body to debug log
                body = r.text[:600]
                self._log(f"  ERROR BODY: {body}")
                self.after(0, self._show_error, r.status_code, url)
        except Exception as e:
            self._log(f"  NETWORK ERROR: {e}")
            self.after(0, self._update_conn, False)
            self.after(0, self._show_error, 0, str(e))

    def _fetch_json(self, url: str, label: str, headers: dict):
        self._log(f"  GET {url}")
        t0 = time.time()
        try:
            r = requests.get(url, headers=headers, timeout=15)
            elapsed = (time.time() - t0) * 1000
            self._log(f"  HTTP {r.status_code}  {len(r.content):,}B  {elapsed:.0f}ms")
            self.after(0, self._update_conn, r.status_code == 200)
            if r.status_code == 200:
                data = r.json()
                # Full JSON to debug log
                self._log(f"  KEYS: {list(data.keys()) if isinstance(data, dict) else type(data).__name__}")
                self._log(json.dumps(data, indent=2, default=str)[:1200])
                self.after(0, self._show_json, data, label)
            else:
                self._log(f"  ERROR BODY: {r.text[:600]}")
                self.after(0, self._show_error, r.status_code, url)
        except Exception as e:
            self._log(f"  NETWORK ERROR: {e}")
            self.after(0, self._show_error, 0, str(e))

    def _fetch_and_show_json(self, url: str, label: str):
        """Fire-and-forget JSON fetch that opens a popup — used by sub-action buttons."""
        def _do():
            self._log(f"\n  [SUB] GET {url}")
            t0 = time.time()
            try:
                r = requests.get(url, timeout=15)
                elapsed = (time.time() - t0) * 1000
                self._log(f"  HTTP {r.status_code}  {len(r.content):,}B  {elapsed:.0f}ms")
                if r.status_code == 200:
                    data = r.json()
                    self._log(json.dumps(data, indent=2, default=str)[:1200])
                    self.after(0, self._show_json_popup, data, label)
                else:
                    self._log(f"  ERROR: {r.text[:400]}")
            except Exception as e:
                self._log(f"  ERROR: {e}")
        threading.Thread(target=_do, daemon=True).start()

    def _show_json_popup(self, data: dict, label: str):
        """Open a Toplevel with the full JSON data (mirrors SeaStateDataView etc.)"""
        win = tk.Toplevel(self, bg=BG)
        win.title(f"PYXIS ▶ {label.upper()}")
        win.configure(bg=BG)
        win.resizable(True, True)

        tk.Label(win, text=label.upper(), bg=BG, fg=OK_FG,
                 font=("Consolas", 10, "bold"), pady=8).pack()

        fr = tk.Frame(win, bg=BG)
        fr.pack(fill=tk.BOTH, expand=True, padx=16, pady=4)

        flat = {}
        for k, v in (data.items() if isinstance(data, dict) else enumerate(data)):
            flat[str(k)] = v if not isinstance(v, (dict, list)) else json.dumps(v, default=str)[:60]

        for k, v in flat.items():
            row = tk.Frame(fr, bg=BG)
            row.pack(fill=tk.X, pady=1)
            tk.Label(row, text=f"{str(k).upper()[:18]}:", width=20, anchor="w",
                     bg=BG, fg="#888888", font=("Consolas", 8)).pack(side=tk.LEFT)
            warn = "warn" in k.lower() and str(v) not in ("NONE", "none", "n/a", "")
            tk.Label(row, text=str(v)[:40], anchor="w",
                     bg=BG, fg=RED_FG if warn else OK_FG,
                     font=("Consolas", 8, "bold")).pack(side=tk.LEFT)

        tk.Button(win, text="◀  CLOSE", bg="#0a0a0a", fg="#445566",
                  font=("Consolas", 9), relief=tk.FLAT,
                  command=win.destroy, pady=8).pack(pady=8)

    def _rezoom_image(self, item_id: str, label: str, zoom_delta: int):
        """Re-fetch an image view with adjusted zoom."""
        self.zoom_var.set(max(1, min(15, self.zoom_var.get() + zoom_delta)))
        self._dispatch(item_id, label)

    def _update_conn(self, ok):
        self.conn_lbl.configure(fg=OK_FG if ok else RED_FG)

    # ── Screen renderers ──────────────────────────────────────────────────────
    def _draw_standby(self):
        S = self.screen
        S.delete("all")
        W, H = 454, 454
        cx, cy = W//2, H//2

        S.create_line(cx, 0, cx, H, fill="#001100", width=1)
        S.create_line(0, cy, W, cy, fill="#001100", width=1)

        self._draw_arc_gauge(S, cx, cy, cx-4, 225, 135, "#222222", pct=0.0,
                             color="#004488", label="BATT", side="left")
        self._draw_arc_gauge(S, cx, cy, cx-4, 315, 45,  "#222222", pct=0.0,
                             color="#004488", label="FUEL", side="right", ccw=True)

        self._rounded_rect(S, cx-70, 36, cx+70, 68, r=16,
                           fill="#050505", outline=RED_FG, width=2)
        S.create_text(cx, 52, text="SYS: REMOTE",
                      fill=RED_FG, font=("Consolas", 9, "bold"))

        self._rounded_rect(S, cx-70, H-68, cx+70, H-36, r=16,
                           fill="#050505", outline=WARN_FG, width=2)
        S.create_text(cx, H-52, text="PULL SITREP",
                      fill=WARN_FG, font=("Consolas", 9, "bold"))

        timestr = time.strftime("%H:%M:%S")
        S.create_text(cx, cy-30, text=timestr,
                      fill="white", font=("Consolas", 24, "bold"))

        datestr = time.strftime("%Y-%m-%d")
        S.create_text(cx, cy-70, text=datestr,
                      fill="#666666", font=("Consolas", 9))

        S.create_text(cx, cy+22, text="AWAITING CMD",
                      fill=OK_FG, font=("Consolas", 10, "bold"))

        S.create_text(cx, cy+60, text="[SELECT MENU ITEM]",
                      fill="#334455", font=("Consolas", 8))

        self._rounded_rect(S, cx-24, cy+80, cx+24, cy+96, r=4,
                           fill="#001122", outline="#004488", width=1)
        S.create_text(cx, cy+88, text="v4.3.3",
                      fill="#004488", font=("Consolas", 7))

        self.after(1000, self._tick_clock)

    def _tick_clock(self):
        try:
            if self.current_img is None and self._wind_rose is None:
                self._draw_standby()
        except Exception:
            pass

    def _draw_arc_gauge(self, S, cx, cy, r, start, end, bg_col, pct,
                        color, label, side, ccw=False):
        S.create_arc(cx-r, cy-r, cx+r, cy+r,
                     start=start, extent=-(start-end) if not ccw else (end-start),
                     style=tk.ARC, outline=bg_col, width=8)

    def _show_image(self, img, label):
        self._stop_wind_rose()
        self.current_img = ImageTk.PhotoImage(img)
        self.screen.delete("all")
        self.screen.create_image(0, 0, anchor=tk.NW, image=self.current_img)
        cx, cy = 227, 227
        self.screen.create_line(cx-10, cy, cx+10, cy, fill=RED_FG, width=1)
        self.screen.create_line(cx, cy-10, cx, cy+10, fill=RED_FG, width=1)
        self.screen.create_rectangle(0, 424, 454, 454, fill="#000000", outline="")
        self.screen.create_text(227, 437, text=label.upper(),
                                fill=OK_FG, font=("Consolas", 8, "bold"))

    def _show_json(self, data, label):
        self._stop_wind_rose()
        self.current_img = None
        self.screen.delete("all")
        S = self.screen
        W, H = 454, 454
        cx = W//2

        S.create_text(cx, 18, text=label.upper(),
                      fill=OK_FG, font=("Consolas", 11, "bold"))
        S.create_line(20, 34, W-20, 34, fill="#222222")

        y = 48
        spacing = 22

        # Flatten one level of nesting (contacts list etc.)
        flat = {}
        for k, v in data.items():
            if isinstance(v, list):
                flat[k] = f"[{len(v)} items]"
            elif isinstance(v, dict):
                flat[k] = "{...}"
            else:
                flat[k] = v

        for k, v in list(flat.items())[:16]:
            short_k = str(k)[:16].upper()
            short_v = str(v)[:20]
            S.create_text(24, y, text=short_k+":", fill="#888888",
                          font=("Consolas", 8), anchor="w")
            color = OK_FG
            try:
                fv = float(v)
                if fv < 0: color = RED_FG
                elif fv > 100: color = WARN_FG
            except Exception:
                pass
            S.create_text(W-24, y, text=short_v, fill=color,
                          font=("Consolas", 8, "bold"), anchor="e")
            y += spacing
            if y > H - 30:
                break

        S.create_text(cx, H-15, text=f"{len(data)} keys received",
                      fill="#334455", font=("Consolas", 7))

        self._log(json.dumps(data, indent=2)[:800])

    def _show_error(self, code, detail):
        self._stop_wind_rose()
        self.current_img = None
        self.screen.delete("all")
        S = self.screen
        cx, cy = 227, 227
        S.create_text(cx, cy-20, text="COMMS FAILED",
                      fill=RED_FG, font=("Consolas", 14, "bold"))
        S.create_text(cx, cy+20, text=f"HTTP {code}",
                      fill=WARN_FG, font=("Consolas", 11))
        S.create_text(cx, cy+50, text=str(detail)[:50],
                      fill="#555555", font=("Consolas", 8))

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _rounded_rect(self, canvas, x1, y1, x2, y2, r=10, **kwargs):
        pts = [x1+r, y1, x2-r, y1, x2, y1, x2, y1+r,
               x2, y2-r, x2, y2, x2-r, y2, x1+r, y2,
               x1, y2, x1, y2-r, x1, y1+r, x1, y1]
        canvas.create_polygon(pts, smooth=True, **kwargs)

    def _log(self, msg):
        def _do():
            self.log.configure(state=tk.NORMAL)
            t = time.strftime("%H:%M:%S")
            self.log.insert(tk.END, f"[{t}] {msg}\n")
            self.log.see(tk.END)
            self.log.configure(state=tk.DISABLED)
        self.after(0, _do)


if __name__ == "__main__":
    app = PyxisWatchSim()
    app.mainloop()
