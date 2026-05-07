@echo off
title UMIGURI LED Bridge - Windows Builder
echo.
echo  ================================================
echo   UMIGURI LED Bridge - Windows EXE Builder
echo  ================================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found.
    echo         Install Python 3.10+ from https://python.org
    echo         Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)
echo [OK] Python found:
python --version

:: Install dependencies  (tkinter is built into Python - no extra GUI lib needed)
echo.
echo [*] Installing dependencies...
pip install websockets pyserial pyinstaller --quiet
if errorlevel 1 (
    echo [ERROR] pip install failed
    pause
    exit /b 1
)
echo [OK] Dependencies installed

:: Check for icon file
set ICON_FLAG=
set DATA_FLAG=
if exist "icon.ico" (
    set ICON_FLAG=--icon "icon.ico"
    set DATA_FLAG=--add-data "icon.ico;."
    echo [OK] icon.ico found - will embed icon
) else (
    echo [WARN] icon.ico not found - EXE will use default Python icon
    echo        Place a 256x256 icon.ico next to this script to add one
)

:: Generate version metadata file (shows up in EXE Properties on Windows)
echo VSVersionInfo(                                                         > version_info.txt
echo   ffi=FixedFileInfo(                                                  >> version_info.txt
echo     filevers=(3,5,0,0),                                               >> version_info.txt
echo     prodvers=(3,5,0,0),                                               >> version_info.txt
echo     mask=0x3f,                                                        >> version_info.txt
echo     flags=0x0,                                                        >> version_info.txt
echo     OS=0x4,                                                           >> version_info.txt
echo     fileType=0x1,                                                     >> version_info.txt
echo     subtype=0x0,                                                      >> version_info.txt
echo     date=(0,0)                                                        >> version_info.txt
echo   ),                                                                  >> version_info.txt
echo   kids=[                                                              >> version_info.txt
echo     StringFileInfo([                                                  >> version_info.txt
echo       StringTable(                                                    >> version_info.txt
echo         u'040904B0',                                                  >> version_info.txt
echo         [StringStruct(u'CompanyName',      u''),                     >> version_info.txt
echo          StringStruct(u'FileDescription',  u'UMIGURI LED Bridge'),   >> version_info.txt
echo          StringStruct(u'FileVersion',      u'3.5.0.0'),              >> version_info.txt
echo          StringStruct(u'InternalName',     u'umiguri-led-bridge'),   >> version_info.txt
echo          StringStruct(u'LegalCopyright',   u''),                     >> version_info.txt
echo          StringStruct(u'OriginalFilename', u'umiguri-led-bridge.exe'), >> version_info.txt
echo          StringStruct(u'ProductName',      u'UMIGURI LED Bridge'),   >> version_info.txt
echo          StringStruct(u'ProductVersion',   u'3.5.0.0')])             >> version_info.txt
echo     ]),                                                               >> version_info.txt
echo     VarFileInfo([VarStruct(u'Translation', [1033, 1200])])           >> version_info.txt
echo   ]                                                                   >> version_info.txt
echo )                                                                     >> version_info.txt

:: Build
echo.
echo [*] Building EXE (this may take 30-60 seconds)...
pyinstaller ^
    --onefile ^
    --windowed ^
    --name "umiguri-led-bridge" ^
    %ICON_FLAG% ^
    %DATA_FLAG% ^
    --version-file "version_info.txt" ^
    --hidden-import websockets ^
    --hidden-import websockets.server ^
    --hidden-import serial ^
    --hidden-import serial.tools.list_ports ^
    bridge_gui.py

:: Clean up temp version file
del version_info.txt 2>nul

if errorlevel 1 (
    echo [ERROR] Build failed. See output above.
    pause
    exit /b 1
)

echo.
echo  ================================================
echo   BUILD COMPLETE!
echo   EXE is at:  dist\umiguri-led-bridge.exe
echo  ================================================
echo.
pause