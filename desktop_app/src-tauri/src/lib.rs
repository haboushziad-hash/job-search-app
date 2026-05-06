use std::fs;
use std::path::PathBuf;
use tauri::Manager;
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;
use uuid::Uuid;


/// Hardcoded URL of Ziad's Cloudflare Worker that collects audit JSONs.
/// Empty string = uploads disabled (local-only mode for dev).
/// Production tester builds: paste your deployed Worker URL here, then
/// rebuild the .msi via `npm run tauri build`. See cf_worker/DEPLOY.md.
///
/// Format: "https://fmsdj-worker.YOUR-SUBDOMAIN.workers.dev/v1/runs"
const AUDIT_UPLOAD_URL: &str = "https://api.findmesomedamnjobz.com/v1/runs";

/// Hardcoded base URL of the LLM proxy Worker. When set, the bundled
/// Python backend routes ALL Gemini + Anthropic API calls through this
/// Worker, which holds the real keys server-side. Testers don't need to
/// configure anything — the Worker takes care of it.
///
/// Empty string = direct mode (backend reads keys from local .env). Used
/// in dev when you have keys locally and want to bypass the proxy.
///
/// Format: "https://api.findmesomedamnjobz.com" (no trailing slash, no path)
const LLM_PROXY_URL: &str = "https://api.findmesomedamnjobz.com";

// Holds the FastAPI backend's child process so we can kill it cleanly on
// app quit. Stored in Tauri's managed state.
struct BackendProcess(std::sync::Mutex<Option<CommandChild>>);


/// Returns this tester's anonymous UUID — generated once on first launch,
/// persisted to the OS app-data folder so it survives reinstalls within
/// the same user account.
///
/// Path: %APPDATA%\com.findmesomedamnjobz.app\tester_id.txt (Windows)
///       ~/Library/Application Support/com.findmesomedamnjobz.app/tester_id.txt (macOS)
///       ~/.local/share/com.findmesomedamnjobz.app/tester_id.txt (Linux)
fn get_or_create_tester_uuid(app: &tauri::AppHandle) -> String {
    let dir: PathBuf = match app.path().app_data_dir() {
        Ok(p) => p,
        Err(_) => return Uuid::new_v4().to_string(),
    };
    if let Err(e) = fs::create_dir_all(&dir) {
        log::warn!("could not create app data dir: {e}");
        return Uuid::new_v4().to_string();
    }
    let path = dir.join("tester_id.txt");

    // Read existing UUID if present and well-formed
    if let Ok(s) = fs::read_to_string(&path) {
        let trimmed = s.trim();
        if Uuid::parse_str(trimmed).is_ok() {
            log::info!("[uuid] loaded existing tester UUID");
            return trimmed.to_string();
        }
    }

    // Generate new UUID + persist
    let new_uuid = Uuid::new_v4().to_string();
    if let Err(e) = fs::write(&path, &new_uuid) {
        log::warn!("could not persist tester UUID: {e}");
    } else {
        log::info!("[uuid] generated and saved new tester UUID");
    }
    new_uuid
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        // Auto-updater plugin — exposes JS APIs to check + apply updates.
        // The manifest URL and pubkey are configured in tauri.conf.json.
        // The frontend (App.tsx) calls check() shortly after startup and
        // shows a toast when a new version is available.
        .plugin(tauri_plugin_updater::Builder::new().build())
        // Save As dialog + arbitrary-path file writes for the export feature.
        // The fs plugin's default scope is restrictive (app data dir only);
        // capabilities/default.json grants `**` scope so users can save
        // exports anywhere they choose. Security-wise this is fine — the
        // user explicitly picks the path via the OS-native dialog.
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        // Process plugin — backs JS-side relaunch() / exit() calls. Used by
        // the auto-updater and the Settings reset flow.
        .plugin(tauri_plugin_process::init())
        .manage(BackendProcess(std::sync::Mutex::new(None)))
        .setup(|app| {
            // v0.3.1: enable the log plugin in BOTH debug and release builds.
            // Previously only debug had logging, which meant production
            // installs (what testers run) had zero observability — every
            // log::info!("[backend] ...") silently dropped, so when the
            // sidecar misbehaved on Mac, no log file existed anywhere on
            // disk to diagnose from. Now logs land at:
            //   macOS:   ~/Library/Logs/<bundle-id>/<app>.log
            //   Windows: %LOCALAPPDATA%\<bundle-id>\logs\<app>.log
            //   Linux:   ~/.local/share/<bundle-id>/logs/<app>.log
            // Stdout target stays on too so `./app` from Terminal still
            // streams live output for active debugging sessions.
            app.handle().plugin(
                tauri_plugin_log::Builder::default()
                    .level(log::LevelFilter::Info)
                    .targets([
                        tauri_plugin_log::Target::new(
                            tauri_plugin_log::TargetKind::LogDir { file_name: None },
                        ),
                        tauri_plugin_log::Target::new(
                            tauri_plugin_log::TargetKind::Stdout,
                        ),
                    ])
                    .build(),
            )?;

            // Spawn the bundled FastAPI backend sidecar. The "backend" name
            // is mapped via tauri.conf.json's bundle.externalBin to the
            // platform-specific binary (backend-x86_64-pc-windows-msvc.exe
            // on Windows).
            //
            // In dev (cargo run) the binary path is resolved from
            // src-tauri/binaries/. In production (.msi install) it comes
            // from the app's resource directory.
            // Anonymous tester UUID — generated once, persisted to app data.
            // Sent to backend as TESTER_UUID env so audit JSONs upload keyed
            // by this stable-but-anonymous identifier.
            let tester_uuid = get_or_create_tester_uuid(app.handle());

            let mut sidecar = app.shell().sidecar("backend").map_err(|e| {
                log::error!("failed to resolve backend sidecar: {e}");
                e
            })?
            .env("TESTER_UUID", &tester_uuid);

            // If a Worker URL is baked into this build, pass it to the
            // backend so audit JSONs auto-upload after every run.
            if !AUDIT_UPLOAD_URL.is_empty() {
                sidecar = sidecar.env("AUDIT_UPLOAD_URL", AUDIT_UPLOAD_URL);
            }

            // LLM_PROXY_URL: route all Gemini/Anthropic calls through Ziad's
            // Worker so testers don't need their own API keys. The Worker
            // holds the real keys server-side and substitutes them per call.
            if !LLM_PROXY_URL.is_empty() {
                sidecar = sidecar.env("LLM_PROXY_URL", LLM_PROXY_URL);
            }

            let (mut rx, child) = sidecar.spawn().map_err(|e| {
                log::error!("failed to spawn backend sidecar: {e}");
                e
            })?;

            // Stash the process handle so on_window_event can kill it on quit
            app.state::<BackendProcess>()
                .0
                .lock()
                .unwrap()
                .replace(child);

            // Drain stdout/stderr from the backend so the OS pipe doesn't
            // fill up. Log them at debug level for diagnostics.
            tauri::async_runtime::spawn(async move {
                use tauri_plugin_shell::process::CommandEvent;
                while let Some(event) = rx.recv().await {
                    match event {
                        CommandEvent::Stdout(line_bytes) => {
                            if let Ok(s) = std::str::from_utf8(&line_bytes) {
                                log::info!("[backend] {}", s.trim_end());
                            }
                        }
                        CommandEvent::Stderr(line_bytes) => {
                            if let Ok(s) = std::str::from_utf8(&line_bytes) {
                                log::warn!("[backend] {}", s.trim_end());
                            }
                        }
                        CommandEvent::Terminated(payload) => {
                            log::warn!("[backend] terminated: code={:?}", payload.code);
                            break;
                        }
                        _ => {}
                    }
                }
            });

            Ok(())
        })
        .on_window_event(|window, event| {
            // Primary cleanup path — fires when user clicks the window X.
            // This is the normal end-user flow on packaged builds.
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                if let Some(state) = window.app_handle().try_state::<BackendProcess>() {
                    if let Ok(mut guard) = state.0.lock() {
                        if let Some(child) = guard.take() {
                            let _ = child.kill();
                        }
                    }
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            // Secondary cleanup path — fires on ANY app shutdown (graceful
            // X-close, OS shutdown, Ctrl+C in dev terminal, app crash that
            // unwinds cleanly). Belt-and-suspenders so backend.exe doesn't
            // linger as a zombie holding port 8765.
            //
            // The .take() above already drained the BackendProcess on a
            // normal close, so this no-ops in that case. It only does
            // real work when CloseRequested didn't fire (abnormal exits).
            if let tauri::RunEvent::Exit = event {
                if let Some(state) = app_handle.try_state::<BackendProcess>() {
                    if let Ok(mut guard) = state.0.lock() {
                        if let Some(child) = guard.take() {
                            let _ = child.kill();
                        }
                    }
                }
            }
        });
}
