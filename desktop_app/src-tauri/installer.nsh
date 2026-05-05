; ----------------------------------------------------------------------------
; NSIS install hooks for findmesomedamnjobz auto-update
; ----------------------------------------------------------------------------
;
; Critical fix shipped in v0.1.7: auto-updates from v0.1.5 → v0.1.6 surfaced
; an "Error opening file for writing: backend.exe" dialog in the middle of
; the install. Root cause: the Python backend sidecar (`backend.exe`,
; PyInstaller-bundled) holds an exclusive file lock on its own .exe while
; running. Tauri 2's auto-updater downloads the new installer and invokes
; it WITHOUT first stopping the sidecar — so Windows refuses to overwrite
; the running binary and prompts Abort/Retry/Ignore. Most users hit Ignore,
; ending up with new frontend + old backend (silent feature breakage).
;
; This hook runs BEFORE NSIS copies any files. It taskkill's the running
; sidecar + main exe (Tauri usually closes the main exe itself, but we
; belt-and-suspenders that too), then sleeps 1s so Windows fully releases
; file handles before file copy starts. End result: silent, prompt-free
; auto-update.
;
; The hook is wired in via `tauri.conf.json` bundle.windows.nsis.installerHooks
; pointing at this file. Macro names (NSIS_HOOK_PREINSTALL etc) are part
; of Tauri 2's NSIS template contract.

!macro NSIS_HOOK_PREINSTALL
  DetailPrint "Stopping running app processes before install..."

  ; Kill the Python backend sidecar. taskkill returns nonzero if the
  ; process isn't running, which is fine — we ignore the exit code so
  ; clean installs (where nothing's running) don't fail spuriously.
  nsExec::ExecToLog 'taskkill /F /IM backend.exe'
  Pop $0

  ; Kill the Tauri main exe too. Tauri's updater normally does this
  ; itself before launching the installer, but we guard against the
  ; edge case where it didn't (e.g., manual installer run).
  nsExec::ExecToLog 'taskkill /F /IM findmesomedamnjobz.exe'
  Pop $0

  ; Sleep 1 second so Windows fully releases file handles. Without
  ; this brief pause, the file copy step can race the kernel and
  ; still see "file in use" on the very next instruction.
  Sleep 1000
!macroend

; Same protection for uninstall — if the user uninstalls via Add/Remove
; Programs while the app is open, we want to kill it cleanly rather than
; show the same locked-file dialog.
!macro NSIS_HOOK_PREUNINSTALL
  DetailPrint "Stopping running app processes before uninstall..."

  nsExec::ExecToLog 'taskkill /F /IM backend.exe'
  Pop $0

  nsExec::ExecToLog 'taskkill /F /IM findmesomedamnjobz.exe'
  Pop $0

  Sleep 1000
!macroend
