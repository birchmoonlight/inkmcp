use std::path::Path;
use std::process::Stdio;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, ChildStdout, Command};
use tracing::{info, warn};

/// Manages the Python worker subprocess.
/// Communication uses a simple JSON-line protocol over stdin/stdout.
pub struct WorkerProcess {
    child: Option<Child>,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    next_id: u64,
}

impl WorkerProcess {
    /// Spawn the Python worker subprocess.
    ///
    /// `python_cmd`: Python interpreter (e.g. "python3", "python")
    /// `script_path`: Path to `inkmcp_worker.py`
    pub async fn spawn(python_cmd: &str, script_path: &Path) -> Result<Self, String> {
        if !script_path.exists() {
            return Err(format!(
                "Worker script not found: {}",
                script_path.display()
            ));
        }

        info!(
            "Spawning Python worker: {} {}",
            python_cmd,
            script_path.display()
        );

        let mut child = Command::new(python_cmd)
            .arg(script_path)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit()) // Let worker stderr flow through to user's stderr
            .kill_on_drop(true)
            .spawn()
            .map_err(|e| format!("Failed to spawn Python worker: {e}"))?;

        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| "Failed to capture worker stdin".to_string())?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| "Failed to capture worker stdout".to_string())?;

        info!("Python worker started (PID: {})", child.id().unwrap_or(0));

        Ok(Self {
            child: Some(child),
            stdin,
            stdout: BufReader::new(stdout),
            next_id: 1,
        })
    }

    /// Send a command as a raw JSON object to the worker (bypasses string parsing).
    ///
    /// The JSON object should have "tag" and "attributes" fields directly,
    /// e.g. {"tag": "execute-code", "attributes": {"code": "..."}}
    pub async fn execute_json(&mut self, request: &serde_json::Value) -> Result<serde_json::Value, String> {
        let req_id = self.next_id;
        self.next_id += 1;

        let mut full = request.clone();
        full["id"] = serde_json::json!(req_id);

        let line = serde_json::to_string(&full)
            .map_err(|e| format!("Serialize error: {e}"))?
            + "\n";

        self.stdin
            .write_all(line.as_bytes())
            .await
            .map_err(|e| format!("Write to worker failed: {e}"))?;
        self.stdin
            .flush()
            .await
            .map_err(|e| format!("Flush worker stdin failed: {e}"))?;

        self.read_response(req_id).await
    }

    /// Internal: read the JSON response for a given request ID.
    async fn read_response(&mut self, req_id: u64) -> Result<serde_json::Value, String> {
        let mut raw = String::new();
        loop {
            raw.clear();
            let n = self
                .stdout
                .read_line(&mut raw)
                .await
                .map_err(|e| format!("Read from worker failed: {e}"))?;

            if n == 0 {
                let exit_status = if let Some(ref mut child) = self.child {
                    child
                        .try_wait()
                        .unwrap_or(None)
                        .map(|s| s.to_string())
                        .unwrap_or_else(|| "unknown".to_string())
                } else {
                    "unknown".to_string()
                };
                return Err(format!("Worker process exited (status: {exit_status})"));
            }

            if raw.trim().is_empty() {
                continue;
            }

            break;
        }

        let response: serde_json::Value = serde_json::from_str(raw.trim())
            .map_err(|e| format!("Parse worker response failed: {e}\nRaw: {raw}"))?;

        let rid = response["id"].as_u64().unwrap_or(0);
        if rid != req_id {
            warn!("Worker response ID mismatch: expected {req_id}, got {rid}");
        }

        if let Some(err) = response.get("error") {
            return Err(err.as_str().unwrap_or("Unknown worker error").to_string());
        }

        Ok(response)
    }
    pub async fn execute(&mut self, command: &str) -> Result<serde_json::Value, String> {
        let req_id = self.next_id;
        self.next_id += 1;

        let request = serde_json::json!({
            "id": req_id,
            "command": command
        });

        let line = serde_json::to_string(&request)
            .map_err(|e| format!("Serialize error: {e}"))?
            + "\n";

        self.stdin
            .write_all(line.as_bytes())
            .await
            .map_err(|e| format!("Write to worker failed: {e}"))?;
        self.stdin
            .flush()
            .await
            .map_err(|e| format!("Flush worker stdin failed: {e}"))?;

        self.read_response(req_id).await
    }

    /// Gracefully shut down the worker.
    pub async fn shutdown(&mut self) {
        // Send shutdown command
        let request = serde_json::json!({
            "id": 0,
            "command": "__shutdown__"
        });
        let line = serde_json::to_string(&request).unwrap_or_default() + "\n";
        let _ = self.stdin.write_all(line.as_bytes()).await;
        let _ = self.stdin.flush().await;

        // Give it a moment, then kill
        tokio::time::sleep(tokio::time::Duration::from_millis(200)).await;
        if let Some(mut child) = self.child.take() {
            let _ = child.start_kill();
            let _ = child.wait().await;
        }
    }

    /// Check if the worker is still alive.
    pub fn is_alive(&mut self) -> bool {
        if let Some(ref mut child) = self.child {
            matches!(child.try_wait(), Ok(None))
        } else {
            false
        }
    }
}

impl Drop for WorkerProcess {
    fn drop(&mut self) {
        if let Some(mut child) = self.child.take() {
            let _ = child.start_kill();
        }
    }
}
