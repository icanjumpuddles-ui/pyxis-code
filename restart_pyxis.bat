@echo off
echo Stopping existing Pyxis Proxy and Headless Simulator...
taskkill /F /IM python.exe /T >nul 2>&1

echo Starting Pyxis Proxy in the background...
start /b python proxy_v3.9.3_FINAL.py > proxy.log 2>&1

timeout /t 2 /nobreak >nul

echo Starting Headless Simulator in the background...
start /b python headless_sim.py > sim.log 2>&1

echo Both services are now running!
echo To view logs, open proxy.log and sim.log
