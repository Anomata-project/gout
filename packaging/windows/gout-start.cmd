@echo off
rem gout's Start menu entry: a terminal in %USERPROFILE%\gout, with the first commands to type.
set "PATH=%~dp0;%PATH%"
if not exist "%USERPROFILE%\gout" mkdir "%USERPROFILE%\gout"
cd /d "%USERPROFILE%\gout"
title gout
gout version
echo.
echo   gout new song              make a project here, in %USERPROFILE%\gout
echo   cd song
echo   gout add "%USERPROFILE%\Music\drums.wav"
echo   gout                       open gout; ctrl-k shows every key
echo   gout help                  everything else
echo.
