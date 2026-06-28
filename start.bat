@echo off
title Quick YouTube Downloader
echo.
echo  Installing / updating dependencies...
pip install -r requirements.txt -q
echo.
echo  Starting server...
echo  Open http://localhost:5000 in your browser
echo.
python app.py
pause
