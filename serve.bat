@echo off
rem Starts the ChordFinder practice view, and syncs the hosted (iPad) copy
rem in a second window at the same time.
rem
rem Usage:
rem   serve.bat                start the server + sync the web copy
rem   serve.bat <video_id>     same, opening that song
rem   serve.bat nosync         start the server only (offline / in a hurry)
rem
rem The sync pulls any imports you did on the iPad down to this PC first,
rem then publishes local changes back up. It runs in its own window so the
rem server (and your practice) starts immediately.
pushd "%~dp0"

set ID=%1
set SYNC=1
if /I "%1"=="nosync" (
  set SYNC=0
  set ID=
)
if /I "%2"=="nosync" set SYNC=0
if "%ID%"=="" set ID=QWIcz3ab358

if "%SYNC%"=="1" (
  echo Syncing the web copy in a separate window...
  start "ChordFinder web sync" cmd /c ""%~dp0cloud\deploy.bat" auto"
)

start "" "http://localhost:8321/frontend/player.html?v=%ID%"
echo Practice view opening in your browser. Keep this window open while playing.
echo Press Ctrl+C (or close this window) to stop the server.
python serve.py 8321
popd
