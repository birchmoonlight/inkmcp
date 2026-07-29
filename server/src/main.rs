mod mcp;
mod worker;

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::TcpListener;
use tokio::signal;
use tokio::sync::Mutex;
use tracing::{debug, error, info, warn};

use mcp::McpHandler;
use worker::WorkerProcess;

/// Default TCP port for direct client connections.
const DEFAULT_TCP_PORT: u16 = 9999;

#[tokio::main]
async fn main() {
    // ── Parse CLI args ──────────────────────────────────────────
    let args: Vec<String> = std::env::args().collect();

    let mut python_cmd = if cfg!(target_os = "windows") {
        "python".to_string()
    } else {
        "python3".to_string()
    };
    let mut worker_path: Option<PathBuf> = None;
    let mut tcp_port: u16 = DEFAULT_TCP_PORT;
    let mut disable_tcp = false;

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--python" | "-p" => {
                i += 1;
                if i < args.len() {
                    python_cmd = args[i].clone();
                }
            }
            "--worker" | "-w" => {
                i += 1;
                if i < args.len() {
                    worker_path = Some(PathBuf::from(&args[i]));
                }
            }
            "--tcp-port" => {
                i += 1;
                if i < args.len() {
                    tcp_port = args[i].parse().unwrap_or(DEFAULT_TCP_PORT);
                }
            }
            "--no-tcp" => {
                disable_tcp = true;
            }
            "--help" | "-h" => {
                print_help(&args[0]);
                return;
            }
            _ => {}
        }
        i += 1;
    }

    // ── Initialize logging (to stderr) ──────────────────────────
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_env_filter(
            tracing_subscriber::EnvFilter::from_default_env()
                .add_directive(tracing::Level::INFO.into()),
        )
        .init();

    info!("🖌️  Inkmcp MCP Server v{}", env!("CARGO_PKG_VERSION"));

    // ── Find worker script ──────────────────────────────────────
    let worker_script = worker_path
        .or_else(find_worker_script)
        .unwrap_or_else(|| {
            error!(
                "Could not find inkmcp_worker.py. \
                 Specify the path with --worker /path/to/inkmcp_worker.py"
            );
            std::process::exit(1);
        });

    if !worker_script.exists() {
        error!(
            "Worker script not found: {}",
            worker_script.display()
        );
        std::process::exit(1);
    }

    // ── Auto-detect venv Python if no explicit --python flag ──────
    // If the user hasn't explicitly set --python, look for a venv
    // next to the binary or worker script.
    let python_explicit = args.iter().position(|a| a == "--python" || a == "-p");
    if python_explicit.is_none() {
        if let Some(venv_python) = find_venv_python(&worker_script) {
            info!("Using venv Python: {}", venv_python.display());
            python_cmd = venv_python.to_string_lossy().to_string();
        }
    }

    // ── Spawn Python worker ─────────────────────────────────────
    let worker = match WorkerProcess::spawn(&python_cmd, &worker_script).await {
        Ok(w) => Arc::new(Mutex::new(w)),
        Err(e) => {
            error!("Failed to start Python worker: {e}");
            std::process::exit(1);
        }
    };

    // Verify worker is alive
    {
        let mut w = worker.lock().await;
        if !w.is_alive() {
            error!("Python worker died immediately after starting");
            std::process::exit(1);
        }
    }
    info!("✅ Python worker is alive and ready");

    // ── Optional TCP listener for direct CLI connections ─────────
    if !disable_tcp && tcp_port > 0 {
        let tcp_worker = worker.clone();
        tokio::spawn(async move {
            if let Err(e) = run_tcp_server(tcp_port, tcp_worker).await {
                error!("TCP server error: {e}");
            }
        });
        info!("TCP listener started on 127.0.0.1:{tcp_port}");
    } else {
        info!("TCP listener disabled");
    }

    // ── Run MCP stdio handler and TCP server concurrently ──────
    let processed = Arc::new(AtomicBool::new(false));

    {
        let mp_flag = processed.clone();
        let mcp_worker = worker.clone();
        tokio::spawn(async move {
            info!("Entering MCP stdio event loop");
            let mut handler = McpHandler::new(mcp_worker);
            let _ = handler.run().await;
            mp_flag.store(handler.processed_messages, Ordering::SeqCst);
            info!("MCP handler finished");
        });
    }

    // Wait for exit conditions:
    //   MCP mode: handler processed messages and finished (client disconnected)
    //   Daemon mode: Ctrl+C received
    loop {
        let finished = tokio::time::timeout(
            std::time::Duration::from_secs(1),
            signal::ctrl_c(),
        ).await;

        match finished {
            Ok(Ok(())) => {
                info!("Received Ctrl+C, shutting down...");
                break;
            }
            Ok(Err(e)) => {
                error!("Failed to listen for Ctrl+C: {e}");
                break;
            }
            Err(_timeout) => {
                // Check every second if MCP handler finished with real messages
                if processed.load(Ordering::SeqCst) {
                    info!("MCP handler finished after processing messages — exiting");
                    break;
                }
            }
        }
    }

    {
        let mut w = worker.lock().await;
        w.shutdown().await;
    }

    info!("👋  Inkmcpd shut down");
}

/// Run a simple TCP server on `127.0.0.1:port` for direct JSON-line
/// protocol access (used by CLI tools, Blender addon, etc.).
async fn run_tcp_server(
    port: u16,
    worker: Arc<Mutex<WorkerProcess>>,
) -> Result<(), Box<dyn std::error::Error>> {
    let addr = format!("127.0.0.1:{port}");
    let listener = TcpListener::bind(&addr).await?;
    info!("TCP server listening on {addr}");

    loop {
        let (mut socket, peer) = match listener.accept().await {
            Ok(s) => s,
            Err(e) => {
                warn!("TCP accept error: {e}");
                continue;
            }
        };

        let w = worker.clone();
        tokio::spawn(async move {
            debug!("TCP client connected: {peer}");
            let (reader, mut writer) = socket.split();
            let mut buf_reader = BufReader::new(reader);
            let mut line = String::new();

            loop {
                line.clear();
                match buf_reader.read_line(&mut line).await {
                    Ok(0) => break, // EOF
                    Ok(_) => {}
                    Err(e) => {
                        debug!("TCP read error from {peer}: {e}");
                        break;
                    }
                }

                let trimmed = line.trim();
                if trimmed.is_empty() {
                    continue;
                }

                // Parse incoming command — two forms accepted:
                //   {"command": "rect x=100 ..."}
                //   or a plain string: "rect x=100 ..."
                let command = match serde_json::from_str::<serde_json::Value>(trimmed) {
                    Ok(v) => v["command"]
                        .as_str()
                        .unwrap_or(trimmed)
                        .to_string(),
                    Err(_) => trimmed.to_string(), // Plain string
                };

                let mut w_lock = w.lock().await;
                let response = match w_lock.execute(&command).await {
                    Ok(r) => r,
                    Err(e) => serde_json::json!({
                        "success": false,
                        "data": {"error": e.to_string()}
                    }),
                };
                drop(w_lock);

                let resp_str =
                    serde_json::to_string(&response).unwrap_or_default()
                        + "\n";
                if let Err(e) = writer.write_all(resp_str.as_bytes()).await {
                    debug!("TCP write error to {peer}: {e}");
                    break;
                }
                let _ = writer.flush().await;
            }

            debug!("TCP client disconnected: {peer}");
        });
    }
}

/// Find the worker script relative to the binary or in common locations.
fn find_worker_script() -> Option<PathBuf> {
    // 1. Check INKMCP_WORKER environment variable
    if let Ok(path) = std::env::var("INKMCP_WORKER") {
        let p = PathBuf::from(&path);
        if p.exists() {
            return Some(p);
        }
    }

    // 2. Check relative to the binary
    if let Ok(exe_path) = std::env::current_exe() {
        if let Some(exe_dir) = exe_path.parent() {
            // Same directory
            let candidates = [
                exe_dir.join("inkmcp_worker.py"),
                exe_dir.join("../inkmcp/inkmcp_worker.py"),
                exe_dir.join("inkmcp/inkmcp_worker.py"),
            ];
            for c in candidates {
                if c.exists() {
                    return Some(c.canonicalize().unwrap_or(c.clone()));
                }
            }
        }
    }

    // 3. Check current directory
    if let Ok(cwd) = std::env::current_dir() {
        let candidates = [
            cwd.join("inkmcp_worker.py"),
            cwd.join("inkmcp/inkmcp_worker.py"),
        ];
        for c in candidates {
            if c.exists() {
                return Some(c.to_path_buf());
            }
        }
    }

    None
}

/// Find a venv Python interpreter near the worker script or binary.
/// Looks for `venv/bin/python` relative to common locations.
fn find_venv_python(worker_script: &Path) -> Option<PathBuf> {
    let binary_dir = std::env::current_exe().ok()?.parent()?.to_path_buf();

    // Collect directories to search — must own values to avoid lifetime issues
    let search_dirs = vec![
        worker_script.parent()?.to_path_buf(),
        binary_dir,
    ];

    for dir in &search_dirs {
        // Walk up looking for a venv directory
        let mut current: Option<&Path> = Some(dir.as_path());
        while let Some(d) = current {
            let venv_python = d.join("venv").join("bin").join("python");
            if venv_python.exists() {
                return Some(venv_python);
            }
            // Also check for a .venv directory (alternative naming)
            let dot_venv_python = d.join(".venv").join("bin").join("python");
            if dot_venv_python.exists() {
                return Some(dot_venv_python);
            }
            current = d.parent();
            // Stop at filesystem root or a home directory
            if let Some(parent) = d.parent() {
                let stem = parent.to_string_lossy().to_string();
                if stem == "/" {
                    break;
                }
            }
        }
    }

    None
}

/// Print help text.
fn print_help(program: &str) {
    eprintln!(
        "Inkmcp MCP Server v{}

Usage:
    {program} [options]

Options:
    --python | -p <PATH>    Python interpreter path (default: python3)
    --worker | -w <PATH>    Path to inkmcp_worker.py
    --tcp-port <PORT>       TCP port for direct connections (default: 9999, 0 to disable)
    --no-tcp                Disable TCP server
    --help | -h             Show this help

Environment:
    INKMCP_WORKER           Path to inkmcp_worker.py
    RUST_LOG                Log level filter (e.g. debug, info, warn)

The server handles the MCP protocol on stdin/stdout for AI assistant
integration, and optionally listens on TCP 127.0.0.1:<PORT> for
direct CLI tool connections.
",
        env!("CARGO_PKG_VERSION"),
    );
}
