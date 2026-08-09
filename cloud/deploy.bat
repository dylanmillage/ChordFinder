@echo off
rem Publish the library to Firestore, then deploy the app to Firebase Hosting.
rem One-time setup is in the README (Firebase console steps).
rem
rem   deploy.bat        run it and wait for a keypress at the end
rem   deploy.bat auto   same, but close automatically (used by serve.bat)
pushd "%~dp0"
set AUTO=%1

echo [1/3] Uploading songs to Firestore...
C:\Users\dylan\.venvs\chordfinder\Scripts\python.exe ..\pipeline\publish.py
if errorlevel 1 goto :fail

echo.
echo [2/3] Staging the app files...
if not exist public mkdir public
C:\Users\dylan\.venvs\chordfinder\Scripts\python.exe make_icons.py
copy /Y ..\frontend\player.html          public\ >nul
copy /Y ..\frontend\songs.html           public\ >nul
copy /Y ..\frontend\cloud.js             public\ >nul
copy /Y ..\frontend\import-sheet.js      public\ >nul
copy /Y ..\frontend\firebase-config.js   public\ >nul
copy /Y ..\frontend\chord-shapes.json    public\ >nul
copy /Y ..\frontend\manifest.webmanifest public\ >nul

echo.
echo [3/3] Deploying to Firebase Hosting...
call firebase deploy --only hosting,firestore:rules
if errorlevel 1 goto :fail

echo.
echo Done. Open the Hosting URL above on your iPad and sign in with Google.
goto :end

:fail
echo.
echo Deploy failed - see the message above.

:end
popd
if /I "%AUTO%"=="auto" (
  timeout /t 4 >nul
) else (
  pause
)
