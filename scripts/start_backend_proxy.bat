@echo off
REM Launch backend.exe in PROXY mode (routes through Cloudflare Worker)
REM Used to verify v0.1.4 Worker proxy + scraper key proxies actually fire
REM in production-equivalent mode.

cd /d "C:\Users\habou\OneDrive\Desktop\Job Search App\desktop_app\src-tauri\binaries"
set LLM_PROXY_URL=https://api.findmesomedamnjobz.com
backend-x86_64-pc-windows-msvc.exe
