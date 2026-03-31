#!/usr/bin/env python3
"""Pyxis VM API Diagnostic - plain text output, no colour codes"""
import requests, json, time, os, sys

la, lo = -38.487, 145.620   # read from state file if possible
PROXY = "https://127.0.0.1:443"

# Read actual vessel position from state file
B = "/home/icanjumpuddles/manta-comms"
DT = os.path.join(B, "pyxis_state.json")
for candidate in [DT, os.path.join(B, "data.json"), os.path.join(B, "state.json")]:
    if os.path.exists(candidate):
        try:
            with open(candidate) as f:
                st = json.load(f)
            la = st.get("lat", la)
            lo = st.get("lon", lo)
            print(f"[STATE] Read vessel pos from {candidate}: lat={la} lon={lo}")
        except: pass
        break

print(f"\n=== PYXIS VM API DIAGNOSTIC ===")
print(f"Vessel: lat={la} lon={lo}")
print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
print()

def test(label, url, method="GET", headers=None, body=None, ac_key=None, timeout=15):
    h = headers or {"User-Agent": "PyxisDiag/1.0"}
    try:
        t0 = time.time()
        r = requests.post(url, json=body, headers=h, timeout=timeout, verify=False) if method=="POST" else \
            requests.get(url, headers=h, timeout=timeout, verify=False)
        ms = int((time.time()-t0)*1000)
        if r.status_code == 200:
            try:
                d = r.json()
                ac = d.get(ac_key, []) if ac_key else []
                n = len(ac) if isinstance(ac, list) else ("null" if ac is None else "?")
                suffix = f"  [{ac_key}={n}]" if ac_key else f"  [{len(r.content)}B]"
                status = "!! ZERO" if ac_key and (not ac or ac is None) else "OK"
                print(f"  [{status}] {label}: HTTP 200  {ms}ms{suffix}")
                return d
            except:
                print(f"  [OK] {label}: HTTP 200  {len(r.content)}B  {ms}ms  (binary/image)")
                return {}
        else:
            print(f"  [FAIL] {label}: HTTP {r.status_code}  {r.text[:100]}  {ms}ms")
            return {}
    except requests.exceptions.Timeout:
        print(f"  [FAIL] {label}: TIMEOUT {timeout}s")
        return {}
    except Exception as e:
        print(f"  [FAIL] {label}: {type(e).__name__}: {e}")
        return {}

# 1. ADSB SOURCES
print("[1] ADSB UPSTREAM APIs")
test("OpenSky (3deg)",
     f"https://opensky-network.org/api/states/all?lamin={la-3}&lomin={lo-3}&lamax={la+3}&lomax={lo+3}",
     ac_key="states")
test("adsb.fi v2",
     f"https://api.adsb.fi/v2/lat/{la}/lon/{lo}/dist/250",
     ac_key="ac")
test("adsb.lol v2",
     f"https://api.adsb.lol/v2/lat/{la}/lon/{lo}/dist/250",
     ac_key="ac")

# 2. PROXY LOCAL
print("\n[2] PROXY ENDPOINTS (local)")
auth = {"Authorization": "Basic YWRtaW46bWFudGE=", "User-Agent": "PyxisDiag/1.0"}

d = test("adsb_contacts", f"{PROXY}/adsb_contacts", headers=auth, ac_key="contacts")
if d:
    contacts = d.get("contacts", [])
    total = d.get("total", 0)
    age = d.get("cache_age_s", "?")
    vlat = d.get("vessel_lat", "?")
    vlon = d.get("vessel_lon", "?")
    print(f"       total={total}  cache_age_s={age}  vessel_lat={vlat}  vessel_lon={vlon}")
    if contacts:
        c = contacts[0]
        print(f"       nearest: {c.get('callsign')} range={c.get('range_nm')}nm lat={c.get('lat')} lon={c.get('lon')}")

sa = test("status_api", f"{PROXY}/status_api", headers=auth)
if sa:
    rc = sa.get("radar_contacts", [])
    print(f"       radar_contacts={len(rc)}  lat={sa.get('lat')}  lon={sa.get('lon')}")

test("ais_radar_map/500", f"{PROXY}/ais_radar_map/500")
test("adsb_radar_map z=6", f"{PROXY}/adsb_radar_map?z=6&lat={la}&lon={lo}")
test("health", f"{PROXY}/health")
test("scenario", f"{PROXY}/scenario")

# 3. CACHE FILES
print("\n[3] CACHE FILES")
for fname in ["adsb_cache.json", "cmems_cache.json", "geo_cache.json",
              "pyxis_state.json", "data.json", "state.json", "sim_state.json"]:
    path = os.path.join(B, fname)
    if os.path.exists(path):
        age = int(time.time() - os.path.getmtime(path))
        sz = os.path.getsize(path)
        try:
            with open(path) as f: d = json.load(f)
            n = len(d) if isinstance(d, list) else len(d.keys())
            print(f"  [OK] {fname}: {sz}B  age={age}s  entries={n}")
            if fname == "adsb_cache.json" and isinstance(d, list) and d:
                print(f"       sample: {d[0].get('callsign','?')} lat={d[0].get('lat')} lon={d[0].get('lon')}")
        except Exception as e:
            print(f"  [WARN] {fname}: {sz}B  age={age}s  parse={e}")
    else:
        print(f"  [--] {fname}: not found")

# 4. ALL FILES IN manta-comms
print(f"\n[4] FILES IN {B}")
try:
    files = sorted(os.listdir(B))
    for f in files:
        p = os.path.join(B, f)
        sz = os.path.getsize(p) if os.path.isfile(p) else 0
        age = int(time.time() - os.path.getmtime(p)) if os.path.exists(p) else 0
        print(f"  {f:40s}  {sz:>10,}B  age={age}s")
except Exception as e:
    print(f"  ERROR: {e}")

# 5. ENV KEYS
print("\n[5] API KEYS")
for k in ["GEMINI_API_KEY", "AISSTREAM_API_KEY", "OPENSKY_USER", "OPENSKY_PASS"]:
    v = os.getenv(k, "")
    print(f"  {k}: {'SET ('+v[:4]+'...)' if v else 'NOT SET'}")

print("\n=== DONE ===")
