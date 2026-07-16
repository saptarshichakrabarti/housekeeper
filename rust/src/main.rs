use serde_json::{json, Value};
use sha2::{Digest, Sha256, Sha512};
use std::fs::File;
use std::io::{self, BufRead, Read, Seek, Write};
use std::path::Path;

fn hex(bytes: &[u8]) -> String { bytes.iter().map(|b| format!("{b:02x}")).collect() }

fn full_hash(path: &Path, algorithm: &str, block_size: usize) -> Result<(String, u64, bool), String> {
    let before = std::fs::metadata(path).map_err(|e| e.to_string())?;
    let mut file = File::open(path).map_err(|e| e.to_string())?;
    let mut total = 0u64;
    let digest = if algorithm.eq_ignore_ascii_case("sha512") {
        let mut h = Sha512::new(); let mut buf = vec![0u8; block_size.max(4096)];
        loop { let n = file.read(&mut buf).map_err(|e| e.to_string())?; if n == 0 { break; } h.update(&buf[..n]); total += n as u64; }
        hex(&h.finalize())
    } else if algorithm.eq_ignore_ascii_case("sha256") {
        let mut h = Sha256::new(); let mut buf = vec![0u8; block_size.max(4096)];
        loop { let n = file.read(&mut buf).map_err(|e| e.to_string())?; if n == 0 { break; } h.update(&buf[..n]); total += n as u64; }
        hex(&h.finalize())
    } else { return Err(format!("unsupported algorithm: {algorithm}")); };
    let after = std::fs::metadata(path).map_err(|e| e.to_string())?;
    Ok((digest, total, before.len() == after.len() && before.modified().ok() == after.modified().ok()))
}

fn quick_hash(path: &Path, algorithm: &str, chunk_size: usize, samples: usize) -> Result<(String, u64, bool), String> {
    let before = std::fs::metadata(path).map_err(|e| e.to_string())?;
    let size = before.len(); let mut file = File::open(path).map_err(|e| e.to_string())?;
    let mut offsets = vec![0, size.saturating_sub(chunk_size as u64)];
    for i in 0..samples { offsets.push(size.saturating_mul((i + 1) as u64) / (samples as u64 + 1)); }
    let mut apply = |mut hasher: Sha256| -> Result<String, String> { for offset in &offsets { file.seek(std::io::SeekFrom::Start(*offset)).map_err(|e| e.to_string())?; let mut buf = vec![0u8; chunk_size.max(4096)]; let n = file.read(&mut buf).map_err(|e| e.to_string())?; hasher.update(&buf[..n]); } Ok(hex(&hasher.finalize())) };
    let digest = if algorithm.eq_ignore_ascii_case("sha256") { apply(Sha256::new())? } else { return Err(format!("unsupported quick-hash algorithm: {algorithm}")); };
    let after = std::fs::metadata(path).map_err(|e| e.to_string())?;
    Ok((digest, after.len(), before.len() == after.len() && before.modified().ok() == after.modified().ok()))
}

fn respond(request_id: &str, body: Value) { let mut output = body; output["request_id"] = json!(request_id); output["event"] = json!("result"); println!("{}", output); let _ = io::stdout().flush(); }

fn main() {
    let stdin = io::stdin();
    for line in stdin.lock().lines() {
        let line = match line { Ok(value) => value, Err(_) => break };
        let request: Value = match serde_json::from_str(&line) { Ok(value) => value, Err(e) => { println!("{}", json!({"event":"result","status":"error","error":e.to_string()})); continue; } };
        let request_id = request["request_id"].as_str().unwrap_or("");
        match request["operation"].as_str().unwrap_or("") {
            "capabilities" => respond(request_id, json!({"status":"ok","capabilities":{"backend":"rust","protocol_version":"1","operations":["capabilities","full_hash","quick_hash"]}})),
            "full_hash" => {
                let args = &request["arguments"]; let path = args["path"].as_str().unwrap_or(""); let algorithm = args["algorithm"].as_str().unwrap_or("sha256"); let block = args["block_size"].as_u64().unwrap_or(8_388_608) as usize;
                match full_hash(Path::new(path), algorithm, block) { Ok((digest, size, stable)) => respond(request_id, json!({"status":"ok","full_hash":digest,"size_bytes":size,"stable":stable})), Err(error) => respond(request_id, json!({"status":"error","error":error})) }
            }
            "quick_hash" => {
                let args = &request["arguments"]; let path = args["path"].as_str().unwrap_or(""); let algorithm = args["algorithm"].as_str().unwrap_or("sha256"); let chunk = args["chunk_size"].as_u64().unwrap_or(1_048_576) as usize; let samples = args["middle_samples"].as_u64().unwrap_or(2) as usize;
                match quick_hash(Path::new(path), algorithm, chunk, samples) { Ok((digest, size, stable)) => respond(request_id, json!({"status":"ok","quick_hash":digest,"size_bytes":size,"stable":stable})), Err(error) => respond(request_id, json!({"status":"error","error":error})) }
            }
            operation => respond(request_id, json!({"status":"error","error":format!("unsupported operation: {operation}")})),
        }
    }
}
