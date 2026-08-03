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
    B3(blake3::Hasher),
    S256(Sha256),
    S512(Sha512),
}

impl Hasher {
    fn new(algorithm: &str) -> Result<Self, String> {
        if algorithm.eq_ignore_ascii_case("blake3") {
            Ok(Hasher::B3(blake3::Hasher::new()))
        } else if algorithm.eq_ignore_ascii_case("sha256") {
            Ok(Hasher::S256(Sha256::new()))
        } else if algorithm.eq_ignore_ascii_case("sha512") {
            Ok(Hasher::S512(Sha512::new()))
        } else {
            Err(format!("unsupported algorithm: {algorithm}"))
        }
    }
    fn update(&mut self, data: &[u8]) {
        match self {
            Hasher::B3(h) => {
                h.update(data);
            }
            Hasher::S256(h) => h.update(data),
            Hasher::S512(h) => h.update(data),
        }
    }
    fn finish(self) -> String {
        match self {
            // 32 bytes of the extendable output — the same 64 hex characters the Python
            // `blake3.blake3().hexdigest()` produces, so the two backends stay interchangeable.
            Hasher::B3(h) => hex(h.finalize().as_bytes()),
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
    let mut buf = vec![0u8; block_size.max(1)];
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
    let chunk = chunk_size.max(1);
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

/// Full and sampled digests from one sequential read, matching Python's `compute_identity`.
fn identity_hash(
    path: &Path,
    algorithm: &str,
    block_size: usize,
    quick_chunk_size: usize,
    samples: usize,
) -> Result<(String, String, u64, bool), String> {
    let before = std::fs::metadata(path).map_err(|e| e.to_string())?;
    let size = before.len();
    let block = block_size.max(1);
    let quick_chunk = quick_chunk_size.max(1);
    let offsets = if samples_whole_file(size, quick_chunk as u64, samples as u64) {
        Vec::new()
    } else {
        quick_offsets(size, quick_chunk as u64, samples as u64)
    };
    let mut captured: Vec<Vec<u8>> = offsets
        .iter()
        .map(|_| Vec::with_capacity(quick_chunk))
        .collect();
    let mut full = Hasher::new(algorithm)?;
    let mut file = File::open(path).map_err(|e| e.to_string())?;
    let mut buf = vec![0u8; block];
    let mut position = 0u64;
    loop {
        let want = buf.len();
        let n = read_up_to(&mut file, &mut buf, want)?;
        if n == 0 {
            break;
        }
        full.update(&buf[..n]);
        let end = position.saturating_add(n as u64);
        for (index, offset) in offsets.iter().copied().enumerate() {
            let low = offset.max(position);
            let high = offset.saturating_add(quick_chunk as u64).min(end);
            if low < high {
                captured[index]
                    .extend_from_slice(&buf[(low - position) as usize..(high - position) as usize]);
            }
        }
        position = end;
    }
    let full_digest = full.finish();
    let quick_digest = if offsets.is_empty() {
        full_digest.clone()
    } else {
        let mut quick = Hasher::new(algorithm)?;
        for sample in captured {
            quick.update(&sample);
        }
        quick.finish()
    };
    let after = std::fs::metadata(path).map_err(|e| e.to_string())?;
    Ok((
        full_digest,
        quick_digest,
        after.len(),
        unchanged(&before, &after),
    ))
}

/// Gear table: byte-identical to housekeeper.chunking.python_backend._gear_table()
/// (Python `random.Random(0xC0FFEE).getrandbits(64)` x256). The parity test is the guarantee.
const GEAR: [u64; 256] = [
    0x950e87d7f5606615,
    0x2c61275c9e6b6cf8,
    0x1f00bca0042db923,
    0x6dbca290a9eab706,
    0x4c10a4fe30cffdda,
    0xf26fff4cc4fd394d,
    0x6814a2bc786a6d2d,
    0xa26b351e6c8042c5,
    0x54760e7fbc051c6c,
    0xd4c08880a5a4666d,
    0x29610ae0eed8f1e7,
    0xc34bd8e2fe5213e5,
    0x6c50afb6e9fb123d,
    0x6f28d015a2aa0b9d,
    0x4e385994ebac94af,
    0x194f9545adba52ce,
    0xc675ce05588f882f,
    0x57de8c051d4b7ef2,
    0xd998efd82733e933,
    0x6df216c33f8f3201,
    0x11dc6f3fcb57d5d8,
    0x8860a84722025e05,
    0x33176469aa6ef630,
    0x607507ebc5b864d7,
    0x7a2f11088d29b146,
    0xda10faaa6fc24b83,
    0x2de288f12fcb9940,
    0xb98937dfef041066,
    0xdd4b712ed355871e,
    0xc5b790314a2e3224,
    0x07fdc889fa017ed7,
    0x81eeadd71198bf15,
    0x3a46305c425a7de1,
    0xaaabc8d366e0440d,
    0x3371364fc51d1a5e,
    0x4763dd191ac44b70,
    0x016590c55646e6d0,
    0x0b7a6e1d81e4b9e7,
    0xe5a2a8bef16e981a,
    0x1167fba4a2927979,
    0x3d01ac0f1b534b87,
    0xd27a5f0f5532c867,
    0xee26cbc0358b24d3,
    0x9bdb39b2ca3c6a00,
    0x8de06fbe1a741555,
    0xd6257b492186c8b5,
    0xdee7539c539445f3,
    0x4307513f1ec1b0b1,
    0x1d790bcaeffd4d2d,
    0xde18f50a43cf423a,
    0xd36c78ab3537a844,
    0x64b5e3f81a293b3b,
    0xe8eef3d67646f8a9,
    0xa88d379db047719d,
    0xf177d49f03ddc3bf,
    0xa745fdd552965bca,
    0xd0b6a46a7048daca,
    0xfce79398852e0400,
    0x760c9b756320dbe3,
    0x4e52b41980271e94,
    0x293f65848aa18f43,
    0x520e015e444ed0f2,
    0x793ff51bb0baf029,
    0x7ad955568f86a26a,
    0x1c720603ec8602d9,
    0xd08e7565d487d342,
    0x310288290b43dbfb,
    0xd50ca99e8e59ea07,
    0x6c24e82c6dbbac73,
    0xb7a13dce8e4595df,
    0xe91b8ec1f011e633,
    0x9293bf4aed9a76b9,
    0x75c33f8fcb8031fe,
    0x1e7c31d385989296,
    0x5574e314ddfc20fe,
    0xd17dad339930e76e,
    0xacfbba2a3f8666ee,
    0xa4e307830deef007,
    0x8fcd110ce94f47b0,
    0xe1660a4195d74835,
    0xd6d91d39227d512d,
    0x2abb018969cbe6eb,
    0x09cea2a86a921843,
    0x3fe9e76493a8b5d8,
    0x602f8e87d16bc8be,
    0xe376bd78d7304cb6,
    0x748781c961ef7dfc,
    0xff5e243c496a590b,
    0x089934a93d71d058,
    0x3deadc7d1d2e1a2e,
    0xe443e6031233f1e0,
    0x5ab59d10b4a20569,
    0x658141e73ede6f12,
    0xf5d46d8127762b7b,
    0xad1dd1408b87cfcb,
    0xf9afa64760083c7d,
    0xb7a68aa8611b9b59,
    0xd828056ea86fc09c,
    0x1c0ae9a87893032b,
    0x34c8a05ca34be96a,
    0xc966aed65a10eeaf,
    0x6b7e21f0921082df,
    0x6e5d9a3007c331a3,
    0x3a0806a754f57983,
    0x0a07a198f7767fd6,
    0xf0723a8383f43dc4,
    0xfb65e62582414d3f,
    0x504516f2106025b5,
    0xa0d72f15feb859eb,
    0x115600523ea6fb4d,
    0x1be3ae0c3b97b6c9,
    0x5fe2b11364b97756,
    0x5a8a944097dea5e8,
    0xc330642bbf1317f8,
    0xf0b02956ff594f79,
    0xa4002d902b1b1e58,
    0xba351d1d2912ab9f,
    0x56761e8879073c59,
    0x3912a0fca373e01b,
    0xec004af1d0efd4ff,
    0x8919551203d33d87,
    0x64f85da91a44dfa0,
    0x21d287d8efb4cad1,
    0x1732b75d08d75496,
    0x27623245c6251a5c,
    0x987abb69ec5093da,
    0xea45cdaf628e21c8,
    0x0272834f4d8a9084,
    0xab699ad2c231185b,
    0x6ff327f4119ee914,
    0x6b06b34098ca4c3f,
    0x725461191d5d7302,
    0x511173b251af8015,
    0xebbfbb2bc3846ece,
    0xed8b79ed1d74a080,
    0x9736b29f0b03d0e1,
    0xceaf0df42de3540c,
    0x576c473aecbeb26f,
    0x6782e42f80a0f27d,
    0xf39f015e2cafb91c,
    0x293c27e425e74da2,
    0x1a18b9b1c2c8b502,
    0x731535ecb7b2a53b,
    0x4f7d9b08c0f76e59,
    0x3e115e3e75118be1,
    0x689db40cdd801db4,
    0x399246294d8fc042,
    0xc018ee73ff8f5cff,
    0xa364f1b057f4865e,
    0xbd5993b1f9f2dce0,
    0x1fb37062a68f65c1,
    0x2a5f2d8aca707a92,
    0x3ff1295c1d296c14,
    0x4ea7feaa1455fcad,
    0xb484b8d3f354db28,
    0xdef5e3507a2ee034,
    0x1a46b9e3a2663f03,
    0x5665aca3177d70d6,
    0x36a208e01b1b4ee3,
    0x00822ed4e33a0336,
    0x9d3bd30e22749e54,
    0x703666d165265fe5,
    0xebe4418c6286ef71,
    0xe07f915527fcb0f2,
    0xcfedc87950868c9c,
    0x95825097784ecbbb,
    0x106572c92038d12e,
    0x79b713272176822e,
    0x810287a90cffae31,
    0x7c8f5a44b03c1008,
    0x113167635255aa79,
    0x9f0600356aab79e5,
    0x559ccfb8c80ce420,
    0x33fc57dd263695f9,
    0xc2299345df0b305d,
    0x3519cb88dac97abb,
    0xed1137eb3e5e1046,
    0x22b6ce988e5e8733,
    0xe3bd76bf57cec991,
    0x402117a53e2681d1,
    0xeee4852d330c2394,
    0x854773512f3334bf,
    0xcfe680854c95ea72,
    0xe3aab3ddc209f79d,
    0xa2842cb2fb44c6a2,
    0x32442b01a0f4dd5a,
    0xe5fbc6d02bd667d6,
    0x343c5382621d123a,
    0x6cb5b7d2782a1890,
    0xef04a4a598411feb,
    0x31afaa01fdc2dbd7,
    0x5762032f27aa949b,
    0x332508b2d1c97795,
    0xb93ad7dfcba7ddcd,
    0x4930986a215c9b8b,
    0x3caf648a3fe36a17,
    0x4e1309a0fc447a7f,
    0x019d6ac5fe7f773e,
    0x637118bb0b0e773c,
    0xba17e7bd0a7a8b0c,
    0x20b9122fca694c79,
    0xb0773e1b8ea50117,
    0xa544b6d2cf823377,
    0x3e2e21041529057c,
    0x01d6aedaa22e88e8,
    0x673bb9153bc7eead,
    0xf332dec5058c062b,
    0x802df2eef9537531,
    0x26dd7c451562a836,
    0x0c72e5f1f03cde37,
    0xeae27c2bcf28335a,
    0x9482faca03ac665d,
    0x6774a90031d2ba09,
    0xe6b37c203fbd6d30,
    0xc958935b157304b1,
    0x9ef80467a8e636c6,
    0xa7d73426f0aee715,
    0x4ac05557bdca343f,
    0x65c2195389de9f30,
    0x7b4afcc0a8108c27,
    0x938f35b2dc04bbfc,
    0x642e484600cdfa67,
    0x890c62927989d7e6,
    0x11d0bc174b47a18b,
    0xd0ae2b468f227e2f,
    0xb9f409d40d3832c1,
    0xa37579c44c86abf9,
    0xcc69f35beecff786,
    0x3cd64d14ac521437,
    0xb860c5a45b4be237,
    0x3d1791cf2b9550bc,
    0x4c5b4726a89a476e,
    0x12e2992b24380fb6,
    0x0fb88164ccc14927,
    0x9dca0bdcdd3a68c5,
    0xeb0e37f4d6290f03,
    0x0e8936d8133fee34,
    0x2e778e78671eaa35,
    0x616eb2a9fb09b28d,
    0xaac0c22e5d235cab,
    0xad4cf62c94a4f317,
    0xcf3b5ee99ca944bb,
    0xc1f007cd2413872a,
    0x18fde7a7091e9247,
    0xe8ed59599a0e9c30,
    0xb036bade9e716b3d,
    0x92852160c8b912b1,
    0x59ad98498ff5b11b,
    0xd41339c948a6e7cb,
    0x3c79a0009f140b4e,
    0x34186cdd3c3c5140,
    0x919b6a673343fd70,
    0xbab5120ef942a0f6,
    0x3c8016d006c1ec71,
    0x28e208906796f59f,
    0xfbd9efbb76c9773a,
];

/// FastCDC-style gear-hash chunker — byte-identical to
/// `housekeeper.chunking.python_backend.chunk_file`. Returns `(sequence, offset, size, sha256_hex)`
/// per chunk. The chunk digest is always SHA-256, matching the reference (which ignores the
/// profile's hash-algorithm label for the chunk hash itself).
fn chunk_file_cdc(
    path: &Path,
    minimum: u64,
    average: u64,
    maximum: u64,
) -> Result<Vec<(u64, u64, u64, String)>, String> {
    let average = average.max(2);
    let bits = average.ilog2(); // == Python average.bit_length() - 1 for average >= 1
    let mask_s = (1u64 << (bits + 2).min(63)) - 1; // harder to cut below the average
    let mask_l = (1u64 << bits.saturating_sub(2).max(1)) - 1; // easier to cut above it
    let mut file = File::open(path).map_err(|e| e.to_string())?;
    let mut fingerprint: u64 = 0;
    let mut buffer: Vec<u8> = Vec::new();
    let mut offset: u64 = 0;
    let mut sequence: u64 = 0;
    let mut records: Vec<(u64, u64, u64, String)> = Vec::new();
    let mut block = vec![0u8; 1 << 20];
    let emit = |buffer: &[u8], sequence: u64, offset: u64| -> (u64, u64, u64, String) {
        let mut hasher = Sha256::new();
        hasher.update(buffer);
        (
            sequence,
            offset,
            buffer.len() as u64,
            hex(&hasher.finalize()),
        )
    };
    loop {
        let want = block.len();
        let n = read_up_to(&mut file, &mut block, want)?;
        if n == 0 {
            break;
        }
        for &byte in &block[..n] {
            buffer.push(byte);
            // Python: ((fingerprint << 1) + GEAR[byte]) & (2^64 - 1). u64 shift drops the top bit
            // and wrapping_add stays mod 2^64, so the two agree exactly.
            fingerprint = (fingerprint << 1).wrapping_add(GEAR[byte as usize]);
            let size = buffer.len() as u64;
            if size < minimum {
                continue;
            }
            let cut = size >= maximum
                || (size <= average && (fingerprint & mask_s) == 0)
                || (size > average && (fingerprint & mask_l) == 0);
            if cut {
                records.push(emit(&buffer, sequence, offset));
                offset += size;
                sequence += 1;
                buffer.clear();
                fingerprint = 0;
            }
        }
    }
    if !buffer.is_empty() {
        records.push(emit(&buffer, sequence, offset));
    }
    Ok(records)
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

fn identity_reply(result: (String, String, u64, bool)) -> Value {
    let (full, quick, size, ok) = result;
    json!({
        "status": if ok { "ok" } else { "error" },
        "full_hash": if ok { Value::from(full) } else { Value::Null },
        "quick_hash": if ok { Value::from(quick) } else { Value::Null },
        "size_bytes": size,
        "bytes_read": size,
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
                json!({"status":"ok","capabilities":{"backend":"rust","protocol_version":"1","operations":["capabilities","full_hash","quick_hash","identity_hash","chunk_file"]}}),
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
            "identity_hash" => {
                let path = args["path"].as_str().unwrap_or("");
                let algorithm = args["algorithm"].as_str().unwrap_or("blake3");
                let block = args["block_size"].as_u64().unwrap_or(8_388_608) as usize;
                let chunk = args["quick_chunk_size"].as_u64().unwrap_or(1_048_576) as usize;
                let samples = args["middle_samples"].as_u64().unwrap_or(2) as usize;
                match identity_hash(Path::new(path), algorithm, block, chunk, samples) {
                    Ok(result) => respond(request_id, identity_reply(result)),
                    Err(error) => respond(request_id, json!({"status":"error","error":error})),
                }
            }
            "chunk_file" => {
                let path = args["path"].as_str().unwrap_or("");
                let minimum = args["minimum_chunk_size"].as_u64().unwrap_or(16_384);
                let average = args["average_chunk_size"].as_u64().unwrap_or(65_536);
                let maximum = args["maximum_chunk_size"].as_u64().unwrap_or(262_144);
                match chunk_file_cdc(Path::new(path), minimum, average, maximum) {
                    Ok(records) => {
                        let chunks: Vec<Value> = records
                            .into_iter()
                            .map(|(sequence, offset, size, digest)| {
                                json!({
                                    "sequence_index": sequence,
                                    "byte_offset": offset,
                                    "size_bytes": size,
                                    "chunk_hash": digest,
                                })
                            })
                            .collect();
                        respond(
                            request_id,
                            json!({"status":"ok","count": chunks.len(), "chunks": chunks}),
                        );
                    }
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
