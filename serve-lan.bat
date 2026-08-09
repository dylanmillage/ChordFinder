@echo off
rem Starts ChordFinder so other devices on your network (iPad, phone) can
rem reach it. The console prints the address to open on the other device.
rem Windows may ask to allow Python through the firewall - choose "Private".
pushd "%~dp0"
echo Starting ChordFinder for this network...
echo Open the "On this network" address below on your iPad.
echo Press Ctrl+C (or close this window) to stop the server.
python serve.py 8321 --lan
popd
