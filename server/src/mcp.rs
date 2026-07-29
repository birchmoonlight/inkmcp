use std::sync::Arc;
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::sync::Mutex;
use serde_json::{json, Value};
use tracing::{debug, error, info, warn};

use crate::worker::WorkerProcess;

/// The MCP protocol version we support.
const PROTOCOL_VERSION: &str = "2024-11-05";

/// JSON-RPC error codes
mod jsonrpc_error {
    pub const PARSE: i64 = -32700;
    pub const INVALID_REQUEST: i64 = -32600;
    pub const METHOD_NOT_FOUND: i64 = -32601;
    pub const INVALID_PARAMS: i64 = -32602;
    pub const INTERNAL: i64 = -32603;
}

/// Handles MCP stdio protocol (JSON-RPC 2.0 with Content-Length framing).
pub struct McpHandler {
    worker: Arc<Mutex<WorkerProcess>>,
    initialized: bool,
    /// Set to true if at least one MCP request was processed (not immediate EOF).
    pub processed_messages: bool,
}

impl McpHandler {
    pub fn new(worker: Arc<Mutex<WorkerProcess>>) -> Self {
        Self {
            worker,
            initialized: false,
            processed_messages: false,
        }
    }

    /// Run the MCP stdio event loop.
    /// Reads JSON-RPC requests from stdin, dispatches, writes responses to stdout.
    pub async fn run(&mut self) -> Result<(), Box<dyn std::error::Error>> {
        let stdin = tokio::io::stdin();
        let stdout = tokio::io::stdout();
        let mut reader = BufReader::new(stdin);
        let mut writer = stdout;

        info!("MCP server listening on stdio");

        loop {
            let raw = match read_mcp_message(&mut reader).await {
                Ok(Some(msg)) => msg,
                Ok(None) => {
                    info!("MCP client disconnected (EOF on stdin)");
                    break;
                }
                Err(e) => {
                    error!("Error reading MCP message: {e}");
                    break;
                }
            };

            debug!("Received MCP message ({} bytes)", raw.len());

            // Mark that we successfully received a real MCP message (not just EOF)
            self.processed_messages = true;

            // Parse JSON-RPC
            let parsed: Value = match serde_json::from_str(&raw) {
                Ok(v) => v,
                Err(e) => {
                    error!("Failed to parse JSON-RPC: {e}");
                    let err = make_error(&Value::Null, jsonrpc_error::PARSE, &format!("Parse error: {e}"));
                    write_mcp_message(&mut writer, &err.to_string()).await?;
                    continue;
                }
            };

            let method = parsed["method"].as_str().unwrap_or("").to_string();
            let id = parsed.get("id").cloned();

            // Notifications have no "id" field — don't respond
            let is_notification = id.is_none() || id == Some(Value::Null);
            let id_val = id.unwrap_or(Value::Null);
            let params = parsed.get("params").cloned().unwrap_or(Value::Null);

            // Route request
            let response = self.dispatch(&method, &params, is_notification).await;

            // For notifications, we don't send a response
            if is_notification {
                if method == "exit" || method == "shutdown" {
                    info!("Received exit notification, shutting down");
                    break;
                }
                continue;
            }

            // Send response
            let response_str = serde_json::to_string(&with_id(&response, &id_val))?;
            write_mcp_message(&mut writer, &response_str).await?;
            writer.flush().await?;
        }

        // NOTE: worker shutdown is handled by main.rs, not here.
        // In daemon mode the MCP handler may finish early (EOF on stdin
        // when run in background), but the worker must survive for TCP.
        Ok(())
    }

    /// Dispatch a JSON-RPC method.
    async fn dispatch(
        &mut self,
        method: &str,
        params: &Value,
        _is_notification: bool,
    ) -> Value {
        match method {
            "initialize" => self.handle_initialize(params).await,
            "ping" => json!({}),
            "tools/list" => self.handle_tools_list(),
            "tools/call" => self.handle_tools_call(params).await,
            "notifications/initialized" => {
                self.initialized = true;
                info!("MCP client initialized");
                json!({})
            }
            "notifications/cancelled" => json!({}),
            "shutdown" => json!({}),
            "exit" => json!({}),
            _ => {
                warn!("Unknown method: {method}");
                jsonrpc_error_response(jsonrpc_error::METHOD_NOT_FOUND, &format!("Method not found: {method}"))
            }
        }
    }

    /// Handle `initialize` request.
    async fn handle_initialize(&self, _params: &Value) -> Value {
        info!("Handling MCP initialize");
        json!({
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {
                "tools": {}
            },
            "serverInfo": {
                "name": "inkmcpd",
                "version": env!("CARGO_PKG_VERSION")
            }
        })
    }

    /// Handle `tools/list` request.
    fn handle_tools_list(&self) -> Value {
        json!({
            "tools": [
                {
                    "name": "inkscape_operation",
                    "description": "Execute any Inkscape operation.\n\n\
                        Usage: \"tag key=val key=val children=[{child_tag attr=val}]\n\n\
                        Examples:\n\
                        - \"rect x=100 y=100 width=200 height=100 fill=blue\"\n\
                        - \"circle cx=150 cy=150 r=75 fill=#ff0000\"\n\
                        - \"text x=50 y=50 content='Hello' font-size=16\"\n\
                        - \"execute-code code='circle = Circle(); ...'\"\n\
                        - \"get-info\"\n\
                        - \"export-document-image format=png return_base64=true\"\n\n\
                        See the project README for full documentation.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "Command string in format: tag key=value key=value"
                            }
                        },
                        "required": ["command"]
                    }
                }
            ]
        })
    }

    /// Handle `tools/call` request.
    async fn handle_tools_call(&self, params: &Value) -> Value {
        let tool_name = params["name"].as_str().unwrap_or("");
        let arguments = &params["arguments"];

        match tool_name {
            "inkscape_operation" => {
                let command = arguments["command"]
                    .as_str()
                    .unwrap_or("");

                if command.is_empty() {
                    return error_content("Empty command. Provide a 'command' argument.");
                }

                debug!("Executing command: {command}");

                let mut worker = self.worker.lock().await;

                // Check worker health
                if !worker.is_alive() {
                    return error_content("Python worker is not running. Please restart the server.");
                }

                // Handle execute-code specially: extract code to avoid quoting issues
                let result = if command.starts_with("execute-code ") || command == "execute-code" {
                    let code = extract_execute_code(command);
                    worker.execute_json(&serde_json::json!({
                        "tag": "execute-code",
                        "attributes": {
                            "code": code,
                            "return_output": true
                        }
                    })).await
                } else if command == "get-info" || command == "get-selection" {
                    worker.execute_json(&serde_json::json!({
                        "tag": command,
                        "attributes": {}
                    })).await
                } else {
                    worker.execute(command).await
                };

                match result {
                    Ok(response) => {
                        // Extract the inner result from the worker response
                        let success = response.get("success").and_then(|v| v.as_bool()).unwrap_or(false);
                        let data = response.get("data");

                        if success {
                            build_success_content(data.unwrap_or(&json!({})))
                        } else {
                            let err = data
                                .and_then(|d| d.get("error"))
                                .and_then(|v| v.as_str())
                                .unwrap_or("Unknown error");
                            error_content(err)
                        }
                    }
                    Err(e) => {
                        error!("Worker execution failed: {e}");
                        error_content(&format!("Operation failed: {e}"))
                    }
                }
            }
            _ => {
                error_content(&format!("Unknown tool: {tool_name}"))
            }
        }
    }
}

/// Extract the `code` value from an "execute-code code=..." command string.
///
/// Instead of parsing with regex (which breaks on embedded quotes), we
/// find everything after `code=`, handling one level of quote stripping.
fn extract_execute_code(command: &str) -> String {
    // Find the code= part
    if let Some(pos) = command.find("code=") {
        let after = &command[pos + 5..];
        let trimmed = after.trim();

        // Handle quoted values — strip the outer quotes but keep inner content as-is
        if let Some(stripped) = trimmed.strip_prefix('"') {
            // Find the closing double quote
            let mut result = String::new();
            for ch in stripped.chars() {
                if ch == '"' {
                    break;
                }
                result.push(ch);
            }
            return result;
        }
        if let Some(stripped) = trimmed.strip_prefix('\'') {
            let mut result = String::new();
            for ch in stripped.chars() {
                if ch == '\'' {
                    break;
                }
                result.push(ch);
            }
            return result;
        }

        // Unquoted: return the rest
        trimmed.to_string()
    } else {
        String::new()
    }
}

// ─── MCP stdio framing ─────────────────────────────────────────────

/// Read one MCP message from a reader.
/// Returns `None` on EOF.
async fn read_mcp_message<R>(
    reader: &mut R,
) -> Result<Option<String>, Box<dyn std::error::Error>>
where
    R: AsyncBufReadExt + AsyncReadExt + Unpin,
{
    let mut content_length: usize = 0;

    // Read HTTP-style headers (Content-Length, etc.)
    loop {
        let mut line = String::new();
        let n = reader.read_line(&mut line).await?;
        if n == 0 {
            return Ok(None); // EOF
        }

        let trimmed = line.trim();
        if trimmed.is_empty() {
            break; // End of headers (\r\n\r\n)
        }

        // Normalize: strip trailing \r if present
        let clean = trimmed.trim_end_matches('\r');

        if let Some(len_str) = clean
            .to_lowercase()
            .strip_prefix("content-length:")
        {
            content_length = len_str.trim().parse::<usize>()?;
        }
        // Other headers (Content-Type, etc.) are ignored
    }

    // Read the exact JSON payload
    let mut buf = vec![0u8; content_length];
    if content_length > 0 {
        reader.read_exact(&mut buf).await?;
    }

    let json_str = String::from_utf8(buf)?;
    Ok(Some(json_str))
}

/// Write one MCP message to a writer.
async fn write_mcp_message<W>(
    writer: &mut W,
    json: &str,
) -> Result<(), Box<dyn std::error::Error>>
where
    W: AsyncWriteExt + Unpin,
{
    let header = format!("Content-Length: {}\r\n\r\n", json.len());
    writer.write_all(header.as_bytes()).await?;
    writer.write_all(json.as_bytes()).await?;
    writer.flush().await?;
    Ok(())
}

// ─── JSON-RPC helpers ──────────────────────────────────────────────

/// Build a JSON-RPC success response envelope.
fn with_id(result: &Value, id: &Value) -> Value {
    json!({
        "jsonrpc": "2.0",
        "id": id,
        "result": result
    })
}

/// Build a JSON-RPC error response.
fn jsonrpc_error_response(code: i64, message: &str) -> Value {
    json!({
        "jsonrpc": "2.0",
        "error": {
            "code": code,
            "message": message
        }
    })
}

/// Build a method-level error response (not JSON-RPC level).
fn make_error(id: &Value, code: i64, message: &str) -> Value {
    json!({
        "jsonrpc": "2.0",
        "id": id,
        "error": {
            "code": code,
            "message": message
        }
    })
}

// ─── Content builders ──────────────────────────────────────────────

/// Build a success content array from worker response data.
fn build_success_content(data: &Value) -> Value {
    let mut contents = Vec::new();

    // Check for base64 image data (from export operations)
    if let Some(base64) = data.get("base64_data").and_then(|v| v.as_str()) {
        if !base64.is_empty() {
            contents.push(json!({
                "type": "image",
                "data": base64,
                "mimeType": "image/png"
            }));
        }
    }

    // Build text content
    let message = data["message"].as_str().unwrap_or("Operation completed");
    let mut text = format!("✅ {}", message);

    // Append additional details
    if let Some(id_val) = data.get("id").and_then(|v| v.as_str()) {
        text.push_str(&format!("\n**ID**: `{id_val}`"));
    }
    if let Some(tag) = data.get("tag").and_then(|v| v.as_str()) {
        text.push_str(&format!("\n**Type**: {tag}"));
    }
    if let Some(id_mapping) = data.get("id_mapping").and_then(|v| v.as_object()) {
        if !id_mapping.is_empty() {
            text.push_str("\n**Element IDs** (requested → actual):");
            for (req, act) in id_mapping {
                let act_str = act.as_str().unwrap_or("?");
                if req == act_str {
                    text.push_str(&format!("\n  {req} ✓"));
                } else {
                    text.push_str(&format!("\n  {req} → {act_str}"));
                }
            }
        }
    }
    if let Some(generated) = data.get("generated_ids").and_then(|v| v.as_array()) {
        if !generated.is_empty() {
            text.push_str("\n⚠️  **WARNING: Elements created without IDs**");
            for g in generated {
                if let Some(g_id) = g.as_str() {
                    text.push_str(&format!("\n  {g_id}"));
                }
            }
        }
    }
    if let Some(count) = data.get("count").and_then(|v| v.as_u64()) {
        text.push_str(&format!("\n**Count**: {count}"));
    }
    if let Some(output) = data.get("output").and_then(|v| v.as_str()) {
        if !output.is_empty() {
            text.push_str(&format!("\n{output}"));
        }
    }
    if let Some(file_size) = data.get("file_size").and_then(|v| v.as_u64()) {
        text.push_str(&format!("\n**Size**: {file_size} bytes"));
    }
    if let Some(export_path) = data.get("export_path").and_then(|v| v.as_str()) {
        text.push_str(&format!("\n**File**: {export_path}"));
    }

    if let Some(exec_success) = data.get("execution_successful").and_then(|v| v.as_bool()) {
        if exec_success {
            text.push_str("\n**Execution**: ✅ Success");
        } else {
            text.push_str("\n**Execution**: ❌ Failed");
            // Include error details
            if let Some(errors) = data.get("errors").and_then(|v| v.as_str()) {
                if !errors.is_empty() {
                    text.push_str(&format!("\n**Errors**:\n{errors}"));
                }
            }
            return error_content(&format!("Code execution failed:\n{}", data["errors"].as_str().unwrap_or("")));
        }
    }

    if let Some(errors) = data.get("errors").and_then(|v| v.as_str()) {
        if !errors.is_empty() {
            text.push_str(&format!("\n**Errors**:\n{errors}"));
        }
    }

    contents.push(json!({
        "type": "text",
        "text": text
    }));

    json!({
        "content": contents,
        "isError": false
    })
}

/// Build an error content item.
fn error_content(message: &str) -> Value {
    json!({
        "content": [
            {
                "type": "text",
                "text": format!("❌ {message}")
            }
        ],
        "isError": true
    })
}
