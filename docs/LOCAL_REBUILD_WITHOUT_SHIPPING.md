# Local Rebuild Without Shipping

**When to use this:** any time you edit Python code in `backend/` and want to verify the change works in the actual desktop app — without bumping the version, signing, or pushing to R2 for testers.

This is the dev-iteration loop. The opposite flow ("I've verified everything, now ship to testers") is documented separately in the version-bump + signing checklist.

The auto-update channel and your dev build are independent. Your installed v0.3.x from the auto-update keeps running normally; the dev `app.exe` you launch from `desktop_app\src-tauri\target\release\` is a separate process that reuses the same `%APPDATA%\app.jobsearch.desktop\` data dir.

---

## The known-good sequence

Run these from the project root (`C:\Users\habou\OneDrive\Desktop\Job Search App`) in PowerShell. Verified working 2026-05-09 against v0.3.15.

### 1. Edit your Python code

Make the changes in `backend/` you want to test. Save the files.

### 2. Kill anything that might hold the old binary open

```powershell
Get-Process backend -ErrorAction SilentlyContinue | Stop-Process -Force
Get-Process app -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2
```

### 3. Wipe stale `__pycache__` directories

PyInstaller's `--clean` flag does NOT touch these. If you skip this step, PyInstaller can bundle the OLD bytecode and your changes silently won't activate. This was the bug that ate ~90 minutes during the v0.3.15 dev cycle.

```powershell
Get-ChildItem -Path .\backend -Recurse -Force -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
```

Verify it's clean:

```powershell
Get-ChildItem -Path .\backend -Recurse -Directory -Filter __pycache__ | Select-Object FullName
```

Should print nothing. If anything prints, run the delete again.

### 4. Wipe PyInstaller's build artifacts

```powershell
Remove-Item -Recurse -Force .\build, .\dist -ErrorAction SilentlyContinue
```

### 5. Rebuild the sidecar

```powershell
python -m PyInstaller backend.spec --clean --noconfirm
```

Note: capital P, capital I. Takes ~1-2 minutes. Output lands at `dist\backend.exe` (~113 MB single-file).

### 6. Smoke-test the sidecar in isolation BEFORE bundling into Tauri

This catches missing dependencies early. If the sidecar can't even boot standalone, no point rebuilding the Tauri app on top of it.

```powershell
.\dist\backend.exe
```

You want to see something like:

```
INFO:     Started server process [...]
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8765
```

If it crashes with a Python traceback, the message will tell you what's missing. Common culprits:

| Error | Fix |
| --- | --- |
| `ModuleNotFoundError: No module named 'X'` | Add `X` to system Python: `python -m pip install --user X`. Then add `"X"` to `hidden_imports` in `backend.spec`. |
| `Form data requires "python-multipart"` | Already in `backend.spec` as of 2026-05-09. If you see this, confirm the spec change is committed. |
| `RuntimeError: ... at backend\api.py line N` | Real bug in your code; fix and rebuild. |

**Open a SECOND PowerShell window** (don't kill the running backend) and verify any endpoint you care about works:

```powershell
cd "C:\Users\habou\OneDrive\Desktop\Job Search App"
Invoke-RestMethod -Uri "http://127.0.0.1:8765/applications/event" -Method POST -ContentType "application/json" -Body '{"event_type":"viewed","role_url":"https://example.com/test","role_id":1}'
```

A `200 OK` here means the sidecar itself is good. If you get a 422 with field-validation errors, the endpoint exists but you sent the wrong shape — which is also a pass for "is this endpoint compiled in?"

Stop the backend (Ctrl+C in the first window, or `Get-Process backend | Stop-Process -Force` from the second).

### 7. Copy the sidecar into the Tauri slot

```powershell
Copy-Item -Path .\dist\backend.exe -Destination .\desktop_app\src-tauri\binaries\backend-x86_64-pc-windows-msvc.exe -Force
```

Sanity-check the copy:

```powershell
Get-Item .\dist\backend.exe, .\desktop_app\src-tauri\binaries\backend-x86_64-pc-windows-msvc.exe | Select-Object FullName, Length, LastWriteTime
```

Both rows must show the same `Length` and `LastWriteTime`.

### 8. Rebuild the Tauri app

```powershell
Remove-Item -Recurse -Force .\desktop_app\src-tauri\target\release\bundle -ErrorAction SilentlyContinue
cd .\desktop_app
npm run tauri build
cd ..
```

Takes 3-5 minutes. Ignore the warning about `TAURI_SIGNING_PRIVATE_KEY` — that only blocks the auto-update manifest, not the local build.

Confirm fresh timestamp:

```powershell
Get-Item .\desktop_app\src-tauri\target\release\app.exe | Select-Object FullName, Length, LastWriteTime
```

`LastWriteTime` should be within the last few minutes.

### 9. Launch the dev build

```powershell
Get-Process app -ErrorAction SilentlyContinue | Stop-Process -Force
Get-Process backend -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2
Start-Process .\desktop_app\src-tauri\target\release\app.exe
```

The version shown in the app's UI must match the source code's version. If it shows the OLD version, you launched the auto-update install by mistake — kill it and re-launch from the explicit path above.

### 10. Verify the change actually activated

Whatever feature you changed, exercise it in the app and check the relevant log/output. For click-tracking work specifically:

```powershell
Get-Content "$env:APPDATA\app.jobsearch.desktop\archive\role_events.jsonl" | Select-Object -Last 10
```

For other features, check `%APPDATA%\JobSearchApp\backend.log` or wherever the feature writes.

---

## Common pitfalls

- **Stale `__pycache__`.** Always delete before rebuilding. `--clean` does not touch them. Symptom: your code change doesn't activate even though PyInstaller reports success.
- **Wrong app instance.** You may have two app.exe processes running: the auto-update install AND the dev build. Always kill both before launching, then launch by explicit path.
- **Tauri incremental bundle cache.** If the sidecar EXE timestamp hasn't changed, Tauri can re-bundle the previous artifact. The `Remove-Item ...\bundle` step in (8) prevents this.
- **System Python missing deps.** If you set up a fresh machine, you'll need: `python -m pip install --user fastapi uvicorn pydantic google-genai anthropic tenacity httpx python-dotenv pyinstaller pydantic-settings dnspython email-validator orjson pypdf docx2txt rapidfuzz aiosqlite python-multipart`.
- **Missing `hidden_imports` in `backend.spec`.** PyInstaller's static analyzer can miss dynamically-loaded modules. If a runtime ImportError appears in (6) but the dep is installed, add the package to `hidden_imports` in `backend.spec`.
- **Don't bump the version when iterating.** Only bump `package.json` / `tauri.conf.json` / `Cargo.toml` versions when you're shipping. For local iteration, keep the version stable so SemVer checks don't false-fail.

---

## Why this is faster than shipping

| Step | Local rebuild | Ship to testers |
| --- | --- | --- |
| Code edit | Yes | Yes |
| PyInstaller rebuild | Yes | Yes |
| Tauri rebuild | Yes | Yes |
| Version bump | No | Yes |
| `scripts/check_versions.py` | No | Yes |
| Tauri signing key in env | No | Yes |
| Sign installer + sig file | No | Yes |
| Upload to R2 bucket | No | Yes |
| Update `latest.json` manifest | No | Yes |
| Tester auto-update wait window | No | Yes (varies) |

A full local rebuild loop is ~5-7 minutes. A full ship cycle is ~25-40 minutes plus tester sync time. Use this doc for everything except the final "I'm confident, push to testers" moment.
