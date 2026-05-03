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
        .manage(BackendProcess(std::sync::Mutex::new(None)))
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

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
            // Kill the backend process when the main window closes,
            // otherwise the Python process can linger as a zombie.
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
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
