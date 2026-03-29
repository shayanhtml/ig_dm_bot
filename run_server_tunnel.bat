@echo off
setlocal

cd /d "%~dp0"

start "Model DM Bot Server" cmd /k "python server.py"

cloudflared tunnel run --token eyJhIjoiMTQ0NWVjMzBkY2M2MGI2NmRkNWQ4ZTAzMGMzNzkxZTIiLCJ0IjoiNTA3ZTQyNWEtZjQzOS00Zjc5LWExYTgtZjFhZmE0ZmIwYjZkIiwicyI6Ik5qQmpObU0yWXpZdE5ETmxZeTAwTjJReUxXSXdOalV0WVdFd01qRTBZbUV5WVRCaCJ9
