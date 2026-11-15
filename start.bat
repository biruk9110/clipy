@echo off
title Clipy - AI Auto Clipper
echo ========================================================
echo               STARTING CLIPY WEB & MOBILE APP
echo ========================================================
echo.
echo Open this URL on your PC or phone's browser:
echo http://localhost:8000
echo.
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
pause

