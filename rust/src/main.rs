//! `housekeeper-core` — the optional data-plane accelerator.
//!
//! A persistent JSONL request loop on stdin/stdout. Python is always the reference: every digest
//! here must be byte-identical to `housekeeper.hashing`, because a "faster" hash that disagrees
//! with the stored one silently invalidates the inventory.
//!
//! Three divergences from that reference were fixed here, all of them silent:
//!
//! * **Short reads.** `Read::read` may return fewer bytes than requested — on pipes, network
//!   filesystems, or after a signal — where Python's `read(n)` loops until it has `n` bytes or
//!   EOF. The sampled quick hash called `read` once per offset, so a short read produced a
//!   *different digest for the same bytes*. `read_up_to` now loops.
//! * **Sampling policy.** Python skips sampling entirely when the samples would cover the whole
//!   file anyway (`size <= (samples + 2) * chunk`), and sorts and deduplicates the offsets. This
//!   did neither, so every small file's quick hash disagreed with Python's.
//! * **Buffer allocated inside the offsets loop** — a 1 MiB allocation per sample.

use serde_json::{json, Value};
use sha2::{Digest, Sha256, Sha512};
use std::fs::File;
use std::io::{self, BufRead, Read, Seek, Write};
use std::path::Path;

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Fill `buf` until it holds `want` bytes or the file ends. Mirrors Python's `read(n)`.
fn read_up_to(file: &mut File, buf: &mut [u8], want: usize) -> Result<usize, String> {
    let want = want.min(buf.len());
    let mut filled = 0;
    while filled < want {
        match file.read(&mut buf[filled..want]) {
            Ok(0) => break,
            Ok(n) => filled += n,
            Err(ref e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e.to_string()),
        }
    }
    Ok(filled)
}

enum Hasher {
    S256(Sha256),
    S512(Sha512),
}

impl Hasher {
    fn new(algorithm: &str) -> Result<Self, String> {
        if algorithm.eq_ignore_ascii_case("sha256") {
            Ok(Hasher::S256(Sha256::new()))
        } else if algorithm.eq_ignore_ascii_case("sha512") {
            Ok(Hasher::S512(Sha512::new()))
        } else {
            Err(format!("unsupported algorithm: {algorithm}"))
        }
    }
    fn update(&mut self, data: &[u8]) {
        match self {
            Hasher::S256(h) => h.update(data),
            Hasher::S512(h) => h.update(data),
        }
    }
    fn finish(self) -> String {
        match self {
            Hasher::S256(h) => hex(&h.finalize()),
            Hasher::S512(h) => hex(&h.finalize()),
        }
    }
}

/// `housekeeper.hashing._quick_offsets`: sorted and deduplicated, so reads move forward once.
fn quick_offsets(size: u64, chunk: u64, samples: u64) -> Vec<u64> {
    let mut offsets = vec![0u64, size.saturating_sub(chunk)];
    for index in 0..samples {
        // u128 so the multiply cannot overflow; equals Python's int(size * (i+1) / (samples+1))
        // for every file size below 2^53 bytes, which is every file size.
        offsets.push(((size as u128 * (index as u128 + 1)) / (samples as u128 + 1)) as u64);
    }
    offsets.sort_unstable();
    offsets.dedup();
    offsets
}

/// `housekeeper.hashing._samples_whole_file`.
fn samples_whole_file(size: u64, chunk: u64, samples: u64) -> bool {
    size != 0 && size <= (samples + 2).saturating_mul(chunk)
}

fn unchanged(before: &std::fs::Metadata, after: &std::fs::Metadata) -> bool {
    before.len() == after.len() && before.modified().ok() == after.modified().ok()
}

fn hash_whole_file(file: &mut File, algorithm: &str, block_size: usize) -> Result<String, String> {
    let mut hasher = Hasher::new(algorithm)?;
    let mut buf = vec![0u8; block_size.max(4096)];
    loop {
        let want = buf.len();
        let n = read_up_to(file, &mut buf, want)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hasher.finish())
}

fn full_hash(
    path: &Path,
    algorithm: &str,
    block_size: usize,
) -> Result<(String, u64, bool), String> {
    let before = std::fs::metadata(path).map_err(|e| e.to_string())?;
    let mut file = File::open(path).map_err(|e| e.to_string())?;
    let digest = hash_whole_file(&mut file, algorithm, block_size)?;
    let after = std::fs::metadata(path).map_err(|e| e.to_string())?;
    Ok((digest, after.len(), unchanged(&before, &after)))
}

fn quick_hash(
    path: &Path,
    algorithm: &str,
    chunk_size: usize,
    samples: usize,
) -> Result<(String, u64, bool), String> {
    let before = std::fs::metadata(path).map_err(|e| e.to_string())?;
    let size = before.len();
    let chunk = chunk_size.max(4096);
    let mut file = File::open(path).map_err(|e| e.to_string())?;
    // A file the samples would cover anyway is read once, and its quick digest *is* its full
    // digest — exactly as Python does it, so the two agree on every small file.
    let digest = if size == 0 || samples_whole_file(size, chunk as u64, samples as u64) {
        hash_whole_file(&mut file, algorithm, chunk)?
    } else {
        let mut hasher = Hasher::new(algorithm)?;
        let mut buf = vec![0u8; chunk]; // hoisted: one allocation, not one per offset
        for offset in quick_offsets(size, chunk as u64, samples as u64) {
            file.seek(io::SeekFrom::Start(offset))
                .map_err(|e| e.to_string())?;
            let n = read_up_to(&mut file, &mut buf, chunk)?;
            hasher.update(&buf[..n]);
        }
        hasher.finish()
    };
    let after = std::fs::metadata(path).map_err(|e| e.to_string())?;
    Ok((digest, after.len(), unchanged(&before, &after)))
}

/// Shape the reply exactly as the Python backend does, including on an unstable read.
fn hash_reply(field: &str, result: (String, u64, bool)) -> Value {
    let (digest, size, ok) = result;
    json!({
        "status": if ok { "ok" } else { "error" },
        field: if ok { Value::from(digest) } else { Value::Null },
        "size_bytes": size,
        "stable": ok,
        "error": if ok { Value::Null } else { Value::from("file changed during hashing") },
    })
}

fn respond(request_id: &str, body: Value) {
    let mut output = body;
    output["request_id"] = json!(request_id);
    output["event"] = json!("result");
    println!("{}", output);
    let _ = io::stdout().flush();
}

fn main() {
    let stdin = io::stdin();
    for line in stdin.lock().lines() {
        let line = match line {
            Ok(value) => value,
            Err(_) => break,
        };
        if line.trim().is_empty() {
            continue;
        }
        let request: Value = match serde_json::from_str(&line) {
            Ok(value) => value,
            Err(e) => {
                println!(
                    "{}",
                    json!({"event":"result","status":"error","error":e.to_string()})
                );
                let _ = io::stdout().flush();
                continue;
            }
        };
        let request_id = request["request_id"].as_str().unwrap_or("");
        let args = &request["arguments"];
        match request["operation"].as_str().unwrap_or("") {
            "capabilities" => respond(
                request_id,
                json!({"status":"ok","capabilities":{"backend":"rust","protocol_version":"1","operations":["capabilities","full_hash","quick_hash"]}}),
            ),
            "full_hash" => {
                let path = args["path"].as_str().unwrap_or("");
                let algorithm = args["algorithm"].as_str().unwrap_or("sha256");
                let block = args["block_size"].as_u64().unwrap_or(8_388_608) as usize;
                match full_hash(Path::new(path), algorithm, block) {
                    Ok(result) => respond(request_id, hash_reply("full_hash", result)),
                    Err(error) => respond(request_id, json!({"status":"error","error":error})),
                }
            }
            "quick_hash" => {
                let path = args["path"].as_str().unwrap_or("");
                let algorithm = args["algorithm"].as_str().unwrap_or("sha256");
                let chunk = args["chunk_size"].as_u64().unwrap_or(1_048_576) as usize;
                let samples = args["middle_samples"].as_u64().unwrap_or(2) as usize;
                match quick_hash(Path::new(path), algorithm, chunk, samples) {
                    Ok(result) => respond(request_id, hash_reply("quick_hash", result)),
                    Err(error) => respond(request_id, json!({"status":"error","error":error})),
                }
            }
            operation => respond(
                request_id,
                json!({"status":"error","error":format!("unsupported operation: {operation}")}),
            ),
        }
    }
}
