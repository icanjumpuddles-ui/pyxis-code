#!/usr/bin/env python3
"""
wargame_blockade_au.py — PYXIS WAR GAMES ENGINE v1.0
============================================================
SCENARIO: OPERATION SOUTHERN CROSS
Chinese Naval Blockade of Australia — Hybrid AIS + Simulated Force Picture

ALL simulated contacts are prefixed [SIM-PLAN] or [SIM-RAN].
Live AIS traffic is passed through unmodified from the proxy.

Run on VM alongside mantasim_headless.py:
  nohup python3 wargame_blockade_au.py >> wargame.log 2>&1 &
============================================================
"""
import math, time, random, threading, requests, json, os
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────────────────────────
PROXY_BASE    = "https://benfishmanta.duckdns.org"
STATUS_URL    = f"{PROXY_BASE}/status_api"
INGRESS_URL   = f"{PROXY_BASE}/sim_ingress"
AUTH_TOKEN    = os.getenv("PYXIS_AUTH_TOKEN")
if not AUTH_TOKEN:
    raise ValueError("PYXIS_AUTH_TOKEN environment variable not set")
HEADERS       = {"X-Garmin-Auth": AUTH_TOKEN, "Content-Type": "application/json"}
PUSH_INTERVAL = 3.0   # seconds between telemetry pushes
HZ            = 5.0   # physics update rate

# ── Vessel Definitions ───────────────────────────────────────────────────────
# Each ship: id, name, type, lat, lon, patrol list [(lat,lon)...], speed_kn, symbol
PLAN_FORCES = [
    # Carrier Strike Group — Torres Strait northern approach
    {
        "id": "PLAN-CSG1-CV",
        "name": "[SIM-PLAN] CNS Liaoning (CV-16)",
        "type": "WARSHIP",
        "patrol": [(-8.0, 143.5), (-9.5, 141.5), (-10.5, 142.0), (-9.0, 144.5)],
        "speed_kn": 18.0,
        "symbol": "▲",
        "threat": "EXTREME",
        "detail": "PLAN Carrier Strike Group flagship. Aircraft carrier, J-15 fighters embarked."
    },
    {
        "id": "PLAN-CSG1-DD1",
        "name": "[SIM-PLAN] CNS Nanchang (DDG-101)",
        "type": "WARSHIP",
        "patrol": [(-8.5, 144.0), (-10.0, 142.5), (-9.5, 143.0)],
        "speed_kn": 22.0,
        "symbol": "▲",
        "threat": "EXTREME",
        "detail": "Type 055 destroyer. Escort for Liaoning CSG. YJ-18 ASCMs, HQ-9 SAMs."
    },
    {
        "id": "PLAN-CSG1-DD2",
        "name": "[SIM-PLAN] CNS Kunming (DDG-172)",
        "type": "WARSHIP",
        "patrol": [(-9.0, 143.8), (-10.2, 141.8), (-8.8, 142.5)],
        "speed_kn": 20.0,
        "symbol": "▲",
        "threat": "EXTREME",
        "detail": "Type 052D destroyer. Anti-air warfare screen for CSG."
    },
    {
        "id": "PLAN-CSG1-FF1",
        "name": "[SIM-PLAN] CNS Yueyang (FFG-575)",
        "type": "WARSHIP",
        "patrol": [(-9.2, 144.2), (-10.5, 143.5), (-9.8, 142.8)],
        "speed_kn": 19.0,
        "symbol": "▲",
        "threat": "HIGH",
        "detail": "Type 054A frigate. ASW escort. Yu-8 torpedo-armed helicopter embarked."
    },

    # Bass Strait Blockade Group — sealing southern approach
    {
        "id": "PLAN-SAG1-DD1",
        "name": "[SIM-PLAN] CNS Guiyang (DDG-119)",
        "type": "WARSHIP",
        "patrol": [(-39.0, 146.5), (-40.5, 148.0), (-40.0, 145.0), (-38.5, 147.0)],
        "speed_kn": 20.0,
        "symbol": "▲",
        "threat": "EXTREME",
        "detail": "Type 052D destroyer. Bass Strait blockade line. Tracking all eastbound shipping."
    },
    {
        "id": "PLAN-SAG1-DD2",
        "name": "[SIM-PLAN] CNS Hainan (DDG-101)",
        "type": "WARSHIP",
        "patrol": [(-40.0, 147.0), (-39.5, 149.5), (-41.0, 148.5)],
        "speed_kn": 22.0,
        "symbol": "▲",
        "threat": "EXTREME",
        "detail": "Type 055 destroyer. Bass Strait blockade southern anchor. Surface strike role."
    },

    # Lombok/Sunda Strait interdiction — cutting off Asian shipping lanes
    {
        "id": "PLAN-INT1-FF1",
        "name": "[SIM-PLAN] CNS Zhoushan (FFG-529)",
        "type": "WARSHIP",
        "patrol": [(-8.7, 115.5), (-8.5, 116.2), (-9.0, 115.8)],
        "speed_kn": 16.0,
        "symbol": "▲",
        "threat": "HIGH",
        "detail": "Type 054A frigate. Lombok Strait interdiction patrol. Boarding operations."
    },
    {
        "id": "PLAN-INT1-FF2",
        "name": "[SIM-PLAN] CNS Jingzhou (FFG-532)",
        "type": "WARSHIP",
        "patrol": [(-5.9, 105.5), (-5.5, 106.2), (-6.2, 105.9)],
        "speed_kn": 15.0,
        "symbol": "▲",
        "threat": "HIGH",
        "detail": "Type 054A frigate. Sunda Strait patrol. Intercepting fuel tankers."
    },

    # Submarine contacts (acoustic detections — uncertain positions)
    {
        "id": "PLAN-SUB1",
        "name": "[SIM-PLAN] PLAN 093B SSN (ACOUSTIC CONTACT)",
        "type": "SUBMARINE",
        "patrol": [(-32.0, 114.5), (-32.5, 115.8), (-33.0, 114.0)],
        "speed_kn": 8.0,
        "symbol": "◆",
        "threat": "EXTREME",
        "detail": "Type 093B SSN. Acoustic contact tracking HMAS Stirling (Garden Island). Uncertain position +/-15nm."
    },
    {
        "id": "PLAN-SUB2",
        "name": "[SIM-PLAN] PLAN 093B SSN (ACOUSTIC CONTACT)",
        "type": "SUBMARINE",
        "patrol": [(-35.0, 152.0), (-34.5, 151.0), (-33.8, 152.5)],
        "speed_kn": 7.0,
        "symbol": "◆",
        "threat": "EXTREME",
        "detail": "Type 093B SSN. Tracking Sydney harbour approaches. Possible mining intent."
    },
    {
        "id": "PLAN-SUB3",
        "name": "[SIM-PLAN] PLAN 041 AIP SSK",
        "type": "SUBMARINE",
        "patrol": [(-30.0, 115.0), (-31.5, 114.0), (-30.5, 113.5)],
        "speed_kn": 5.0,
        "symbol": "◆",
        "threat": "HIGH",
        "detail": "Type 041 (Yuan class) AIP submarine. Patrol off Perth. Anti-ship mines deployed."
    },

    # Air assets — show as fast-movers on radar
    {
        "id": "PLAN-AIR1",
        "name": "[SIM-PLAN] PLAN H-6K BOMBER (4-SHIP)",
        "type": "AIRCRAFT",
        "patrol": [(-15.0, 130.0), (-20.0, 135.0), (-18.0, 125.0)],
        "speed_kn": 450.0,
        "symbol": "✈",
        "threat": "EXTREME",
        "detail": "4x H-6K cruise missile carriers. 24x CJ-10A ALCMs. Targeting Tindal RAAF."
    },
]

RAN_FORCES = [
    # HQ — Fleet disposition
    {
        "id": "RAN-LHD01",
        "name": "[SIM-RAN] HMAS Canberra (LHD-02)",
        "type": "WARSHIP",
        "patrol": [(-34.0, 151.5), (-33.5, 152.0), (-34.5, 152.5)],
        "speed_kn": 18.0,
        "symbol": "▽",
        "threat": "LOW",
        "detail": "RAN LHD. Task Group flagship. Tiger ARH helicopters embarked. Sydney area."
    },
    {
        "id": "RAN-FFH02",
        "name": "[SIM-RAN] HMAS Perth (FFH-157)",
        "type": "WARSHIP",
        "patrol": [(-12.0, 130.5), (-11.5, 131.5), (-12.5, 130.0)],
        "speed_kn": 25.0,
        "symbol": "▽",
        "threat": "LOW",
        "detail": "Anzac-class FFH. Darwin patrol group. SM-2 SAM, MH-60R embarked."
    },
    {
        "id": "RAN-FFH03",
        "name": "[SIM-RAN] HMAS Toowoomba (FFH-156)",
        "type": "WARSHIP",
        "patrol": [(-31.5, 115.5), (-32.5, 114.5), (-32.0, 116.0)],
        "speed_kn": 22.0,
        "symbol": "▽",
        "threat": "LOW",
        "detail": "Anzac-class FFH. HMAS Stirling escort. Tracking PLAN SSN contact."
    },
    {
        "id": "RAN-DDG41",
        "name": "[SIM-RAN] HMAS Sydney (DDG-42)",
        "type": "WARSHIP",
        "patrol": [(-33.8, 151.2), (-34.5, 150.5), (-33.2, 151.8)],
        "speed_kn": 28.0,
        "symbol": "▽",
        "threat": "LOW",
        "detail": "Hobart-class DDG. Area air defence. SM-6 capable. Sydney outer harbour."
    },
    {
        "id": "RAN-DDG43",
        "name": "[SIM-RAN] HMAS Brisbane (DDG-43)",
        "type": "WARSHIP",
        "patrol": [(-27.0, 154.0), (-27.5, 153.5), (-26.5, 153.8)],
        "speed_kn": 28.0,
        "symbol": "▽",
        "threat": "LOW",
        "detail": "Hobart-class DDG. Deployed NE of Brisbane. Theatre air defence picket."
    },
    # Australian submarines — opposing the PLAN SSNs
    {
        "id": "RAN-SSK71",
        "name": "[SIM-RAN] HMAS Rankin (SSK-78) (POSITION CLASSIFIED)",
        "type": "SUBMARINE",
        "patrol": [(-9.5, 143.2), (-10.0, 142.0), (-8.8, 142.8)],
        "speed_kn": 10.0,
        "symbol": "◇",
        "threat": "LOW",
        "detail": "Collins-class SSK. Hunt mission against PLAN CSG near Torres Strait."
    },
    {
        "id": "RAN-SSK72",
        "name": "[SIM-RAN] HMAS Waller (SSK-75) (POSITION CLASSIFIED)",
        "type": "SUBMARINE",
        "patrol": [(-40.0, 146.2), (-39.5, 148.0), (-40.5, 147.5)],
        "speed_kn": 8.0,
        "symbol": "◇",
        "threat": "LOW",
        "detail": "Collins-class SSK. Bass Strait picket. Counter-blockade mission."
    },
    # Air defence
    {
        "id": "RAN-MPA01",
        "name": "[SIM-RAN] AP-8A POSEIDON (MPA PATROL)",
        "type": "AIRCRAFT",
        "patrol": [(-15.0, 143.0), (-12.0, 145.0), (-10.0, 142.0), (-13.0, 140.0)],
        "speed_kn": 380.0,
        "symbol": "✈",
        "threat": "LOW",
        "detail": "RAAF P-8A Poseidon. Torres Strait ASW sweep. Sonobuoy pattern. Tracking PLAN CSG."
    },
    {
        "id": "RAN-MPA02",
        "name": "[SIM-RAN] AP-8A POSEIDON (MPA PATROL)",
        "type": "AIRCRAFT",
        "patrol": [(-38.0, 148.0), (-40.0, 149.0), (-41.0, 147.0), (-39.0, 145.5)],
        "speed_kn": 380.0,
        "symbol": "✈",
        "threat": "LOW",
        "detail": "RAAF P-8A Poseidon. Bass Strait ASW pattern. Opposing PLAN blockade DDGs."
    },
]

# US allied forces — forward deployed
US_FORCES = [
    {
        "id": "USN-CSG70-CV",
        "name": "[SIM-USN] USS Ronald Reagan (CVN-76)",
        "type": "WARSHIP",
        "patrol": [(-20.0, 167.0), (-18.0, 165.0), (-22.0, 166.0)],
        "speed_kn": 25.0,
        "symbol": "★",
        "threat": "LOW",
        "detail": "USN Carrier Strike Group 5. Coral Sea positioning. Deterrence deployment."
    },
    {
        "id": "USN-CSG70-DD1",
        "name": "[SIM-USN] USS Howard (DDG-83)",
        "type": "WARSHIP",
        "patrol": [(-19.5, 167.5), (-21.0, 165.5), (-20.5, 166.2)],
        "speed_kn": 28.0,
        "symbol": "★",
        "threat": "LOW",
        "detail": "Arleigh Burke DDG. CSG screen. THAAD capable."
    },
]

# ── Ship State ───────────────────────────────────────────────────────────────
class WarshipContact:
    def __init__(self, spec):
        self.id = spec["id"]
        self.name = spec["name"]
        self.type = spec["type"]
        self.patrol = spec["patrol"]
        self.speed_kn = spec["speed_kn"]
        self.symbol = spec.get("symbol", "▲")
        self.threat = spec.get("threat", "MEDIUM")
        self.detail = spec.get("detail", "")
        self.wp_idx = 0
        # Start at first waypoint with small random offset
        self.lat = self.patrol[0][0] + random.uniform(-0.1, 0.1)
        self.lon = self.patrol[0][1] + random.uniform(-0.1, 0.1)
        self.heading = 0.0
        # Aircraft move faster — reduce uncertainty noise
        noise = 0.02 if self.speed_kn < 50 else 0.0
        self.lat += random.uniform(-noise, noise)
        self.lon += random.uniform(-noise, noise)

    def update(self, dt):
        target = self.patrol[self.wp_idx]
        tlat, tlon = target
        dlat = tlat - self.lat
        dlon = tlon - self.lon
        dist_deg = math.hypot(dlat, dlon)
        dist_nm = dist_deg * 60.0
        
        self.heading = (math.degrees(math.atan2(dlon, dlat)) + 360) % 360
        
        speed_deg_per_sec = (self.speed_kn / 3600.0) / 60.0  # knots -> deg/s
        move = speed_deg_per_sec * dt
        
        if dist_nm < 1.0:  # reached waypoint
            self.wp_idx = (self.wp_idx + 1) % len(self.patrol)
        else:
            self.lat += (dlat / dist_deg) * move
            self.lon += (dlon / dist_deg) * move
            
        # Submarines: add small random drift to simulate position uncertainty
        if self.type == "SUBMARINE":
            self.lat += random.gauss(0, 0.002)
            self.lon += random.gauss(0, 0.002)

    def to_contact(self, ref_lat, ref_lon):
        dlat = (self.lat - ref_lat) * 60.0
        dlon = (self.lon - ref_lon) * 60.0 * math.cos(math.radians(ref_lat))
        rng = math.sqrt(dlat**2 + dlon**2)
        bearing = (math.degrees(math.atan2(dlon, dlat)) + 360) % 360
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "lat": round(self.lat, 4),
            "lon": round(self.lon, 4),
            "bearing": round(bearing, 1),
            "range_nm": round(rng, 1),
            "speed": round(self.speed_kn, 1),
            "heading": round(self.heading, 1),
            "threat": self.threat,
            "detail": self.detail,
            "simulated": True,  # ← always flag as simulated
        }

# ── Main Loop ────────────────────────────────────────────────────────────────
def run_blockade_wargame():
    print("=" * 60)
    print("  PYXIS WAR GAMES: OPERATION SOUTHERN CROSS")
    print("  Chinese Naval Blockade of Australia")
    print("  All PLAN / RAN / USN contacts are SIMULATED")
    print("=" * 60)
    
    # Instantiate all ships
    all_ships = (
        [WarshipContact(s) for s in PLAN_FORCES] +
        [WarshipContact(s) for s in RAN_FORCES] +
        [WarshipContact(s) for s in US_FORCES]
    )
    
    # Get vessel position from proxy (defaults to Port Phillip Bay)
    ref_lat, ref_lon = -38.2, 144.9
    try:
        r = requests.get(STATUS_URL, headers={"X-Garmin-Auth": AUTH_TOKEN}, timeout=5, verify=False)
        if r.status_code == 200:
            d = r.json()
            rl = float(d.get("BOAT_LAT") or d.get("lat") or ref_lat)
            rlo = float(d.get("BOAT_LON") or d.get("lon") or ref_lon)
            if rl != 0 and rlo != 0:
                ref_lat, ref_lon = rl, rlo
    except Exception as e:
        print(f"  Proxy offline, using default position: {e}")
    
    print(f"  Observer position: {ref_lat:.3f}, {ref_lon:.3f}")
    print(f"  Tracking {len(all_ships)} simulated military contacts")
    print(f"  Fusing with live AIS feed...")
    print()

    base_dt = 1.0 / HZ
    last_push = 0.0
    live_ais_snapshot = []

    def refresh_ais():
        """Pull live AIS from proxy every 10s in background."""
        nonlocal live_ais_snapshot, ref_lat, ref_lon
        while True:
            try:
                r = requests.get(STATUS_URL, headers={"X-Garmin-Auth": AUTH_TOKEN}, timeout=5, verify=False)
                if r.status_code == 200:
                    d = r.json()
                    rl = float(d.get("BOAT_LAT") or d.get("lat") or ref_lat)
                    rlo = float(d.get("BOAT_LON") or d.get("lon") or ref_lon)
                    if rl != 0 and rlo != 0:
                        ref_lat, ref_lon = rl, rlo
                    live_ais_snapshot = d.get("radar_contacts", [])
            except Exception:
                pass
            time.sleep(10)

    threading.Thread(target=refresh_ais, daemon=True).start()
    time.sleep(1.0)  # let AIS populate

    while True:
        t0 = time.time()

        # Physics update
        for ship in all_ships:
            ship.update(base_dt)

        now = time.time()
        if now - last_push >= PUSH_INTERVAL:
            # Build radar picture: live AIS + simulated military
            simulated_contacts = [s.to_contact(ref_lat, ref_lon) for s in all_ships]
            
            # Merge: live AIS first (real), then simulated (flagged)
            all_contacts = list(live_ais_snapshot) + simulated_contacts

            payload = {
                "BOAT_LAT": round(ref_lat, 5),
                "BOAT_LON": round(ref_lon, 5),
                "CREW_LAT": round(ref_lat, 5),
                "CREW_LON": round(ref_lon, 5),
                "speed_kn": 0.0,
                "sea_state": 2.5,
                "sled_depth": 0.0,
                "radar_contacts": all_contacts,
                "wargame_active": True,
                "wargame_scenario": "OPERATION SOUTHERN CROSS: Chinese Blockade of Australia",
                "wargame_ts": now,
                "metrics": {
                    "fuel_pct": 100.0,
                    "water_pct": 100.0,
                    "sea_temp_c": 22.0,
                    "wind_tws": 15.0,
                    "wind_aws": 18.0,
                    "ap_engaged": False,
                    "ble_count": 0,
                },
            }

            try:
                requests.post(INGRESS_URL, json=payload, headers=HEADERS, timeout=3, verify=False)
                plan_count = len([s for s in simulated_contacts if "PLAN" in s["id"]])
                ran_count  = len([s for s in simulated_contacts if "RAN"  in s["id"]])
                usn_count  = len([s for s in simulated_contacts if "USN"  in s["id"]])
                ais_count  = len(live_ais_snapshot)
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"[{ts}] WAR GAME PUSH >> {plan_count}x PLAN | {ran_count}x RAN | {usn_count}x USN | {ais_count}x LIVE AIS | Ref: {ref_lat:.3f},{ref_lon:.3f}")
            except Exception as e:
                print(f"  POST failed: {e}")

            last_push = now

        elapsed = time.time() - t0
        if elapsed < base_dt:
            time.sleep(base_dt - elapsed)

if __name__ == "__main__":
    run_blockade_wargame()
