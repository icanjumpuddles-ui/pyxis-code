import time
import requests
import concurrent.futures

la_r = 52.52
lo_r = 13.41

def fetch_sequential():
    try:
        t0 = time.time()
        w_res = requests.get(f"https://api.open-meteo.com/v1/forecast?latitude={la_r}&longitude={lo_r}&current=wind_speed_10m,wind_direction_10m,temperature_2m,surface_pressure", timeout=3.0).json()
        m_res = requests.get(f"https://marine-api.open-meteo.com/v1/marine?latitude={la_r}&longitude={lo_r}&current=wave_height,wave_direction,wave_period", timeout=3.0).json()
        t1 = time.time()
        print(f"Sequential Execution Time: {t1 - t0:.4f} seconds")
    except Exception as e:
        print(f"Error: {e}")

def fetch_concurrent():
    try:
        t0 = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future_w = executor.submit(requests.get, f"https://api.open-meteo.com/v1/forecast?latitude={la_r}&longitude={lo_r}&current=wind_speed_10m,wind_direction_10m,temperature_2m,surface_pressure", timeout=3.0)
            future_m = executor.submit(requests.get, f"https://marine-api.open-meteo.com/v1/marine?latitude={la_r}&longitude={lo_r}&current=wave_height,wave_direction,wave_period", timeout=3.0)

            w_res = future_w.result().json()
            m_res = future_m.result().json()
        t1 = time.time()
        print(f"Concurrent Execution Time: {t1 - t0:.4f} seconds")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    fetch_sequential()
    fetch_concurrent()
