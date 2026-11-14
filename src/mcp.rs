//! `textimg mcp`: a stdio MCP server, so an agent reads a file's pages (header and every image) in ONE tool call
//! and edits without a prior Read. Newline-delimited JSON-RPC 2.0; no dependencies (own JSON and base64).

use std::collections::BTreeMap;
use std::io::{BufRead, Write};
use std::time::Instant;

// ---------- minimal JSON ----------
#[derive(Clone, Debug)]
pub enum J { Null, Bool(bool), Num(f64), Str(String), Arr(Vec<J>), Obj(BTreeMap<String, J>) }

impl J {
    fn get(&self, k: &str) -> Option<&J> { if let J::Obj(m) = self { m.get(k) } else { None } }
    fn str(&self) -> Option<&str> { if let J::Str(s) = self { Some(s) } else { None } }
    fn num(&self) -> Option<f64> { if let J::Num(n) = self { Some(*n) } else { None } }
    fn boolean(&self) -> Option<bool> { if let J::Bool(b) = self { Some(*b) } else { None } }
}

struct P<'a> { s: &'a [u8], i: usize }
impl<'a> P<'a> {
    fn ws(&mut self) { while self.i < self.s.len() && matches!(self.s[self.i], b' ' | b'\t' | b'\n' | b'\r') { self.i += 1; } }
    fn val(&mut self) -> Result<J, String> {
        self.ws();
        let c = *self.s.get(self.i).ok_or("unexpected end")?;
        match c {
            b'{' => {
                self.i += 1; let mut m = BTreeMap::new();
                self.ws(); if self.s.get(self.i) == Some(&b'}') { self.i += 1; return Ok(J::Obj(m)); }
                loop {
                    self.ws(); let k = match self.val()? { J::Str(k) => k, _ => return Err("object key".into()) };
                    self.ws(); if self.s.get(self.i) != Some(&b':') { return Err("expected :".into()); } self.i += 1;
                    let v = self.val()?; m.insert(k, v); self.ws();
                    match self.s.get(self.i) { Some(b',') => self.i += 1, Some(b'}') => { self.i += 1; return Ok(J::Obj(m)); } _ => return Err("expected , or }".into()) }
                }
            }
            b'[' => {
                self.i += 1; let mut a = Vec::new();
                self.ws(); if self.s.get(self.i) == Some(&b']') { self.i += 1; return Ok(J::Arr(a)); }
                loop {
                    a.push(self.val()?); self.ws();
                    match self.s.get(self.i) { Some(b',') => self.i += 1, Some(b']') => { self.i += 1; return Ok(J::Arr(a)); } _ => return Err("expected , or ]".into()) }
                }
            }
            b'"' => {
                self.i += 1; let mut out: Vec<u8> = Vec::new();
                loop {
                    let c = *self.s.get(self.i).ok_or("unterminated string")?; self.i += 1;
                    match c {
                        b'"' => return String::from_utf8(out).map(J::Str).map_err(|_| "bad utf-8".into()),
                        b'\\' => {
                            let e = *self.s.get(self.i).ok_or("bad escape")?; self.i += 1;
                            let ch = match e {
                                b'"' => '"', b'\\' => '\\', b'/' => '/', b'b' => '\u{8}', b'f' => '\u{c}', b'n' => '\n', b'r' => '\r', b't' => '\t',
                                b'u' => {
                                    let hex = |p: &mut P| -> Result<u32, String> { let h = std::str::from_utf8(p.s.get(p.i..p.i + 4).ok_or("bad \\u")?).map_err(|_| "bad \\u")?; p.i += 4; u32::from_str_radix(h, 16).map_err(|_| "bad \\u".to_string()) };
                                    let mut u = hex(self)?;
                                    if (0xD800..0xDC00).contains(&u) && self.s.get(self.i..self.i + 2) == Some(b"\\u") { self.i += 2; let lo = hex(self)?; u = 0x10000 + ((u - 0xD800) << 10) + (lo.wrapping_sub(0xDC00) & 0x3FF); }
                                    char::from_u32(u).unwrap_or('\u{fffd}')
                                }
                                _ => return Err("bad escape".into()),
                            };
                            let mut b = [0u8; 4]; out.extend_from_slice(ch.encode_utf8(&mut b).as_bytes());
                        }
                        _ => out.push(c),
                    }
                }
            }
            b't' if self.s[self.i..].starts_with(b"true") => { self.i += 4; Ok(J::Bool(true)) }
            b'f' if self.s[self.i..].starts_with(b"false") => { self.i += 5; Ok(J::Bool(false)) }
            b'n' if self.s[self.i..].starts_with(b"null") => { self.i += 4; Ok(J::Null) }
            _ => {
                let st = self.i;
                while self.i < self.s.len() && matches!(self.s[self.i], b'-' | b'+' | b'.' | b'e' | b'E' | b'0'..=b'9') { self.i += 1; }
                std::str::from_utf8(&self.s[st..self.i]).ok().and_then(|t| t.parse().ok()).map(J::Num).ok_or_else(|| "bad value".into())
            }
        }
    }
}

fn esc(s: &str) -> String {
    let mut o = String::with_capacity(s.len() + 2);
    o.push('"');
    for c in s.chars() {
        match c { '"' => o.push_str("\\\""), '\\' => o.push_str("\\\\"), '\n' => o.push_str("\\n"), '\r' => o.push_str("\\r"), '\t' => o.push_str("\\t"),
            c if (c as u32) < 0x20 => o.push_str(&format!("\\u{:04x}", c as u32)), c => o.push(c) }
    }
    o.push('"');
    o
}

fn b64(d: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut o = String::with_capacity(d.len() / 3 * 4 + 4);
    for c in d.chunks(3) {
        let n = (c[0] as u32) << 16 | (*c.get(1).unwrap_or(&0) as u32) << 8 | *c.get(2).unwrap_or(&0) as u32;
        o.push(T[(n >> 18) as usize & 63] as char); o.push(T[(n >> 12) as usize & 63] as char);
        o.push(if c.len() > 1 { T[(n >> 6) as usize & 63] as char } else { '=' });
        o.push(if c.len() > 2 { T[n as usize & 63] as char } else { '=' });
    }
    o
}

// ---------- tools ----------
const TOOLS: &str = r#"[
{"name":"read","description":"Read a text file as compact page images: about half the tokens of the plain Read tool, header and every page in this one call. Each source line starts with the marker ¶N (N = its line number); a row starting with ¦N continues line N (a label, not text); a line number that never appears is an empty line; · is one space (leading spaces and runs of 2+); Markdown code, tables and aligned/box lines sit on their own fixed-width rows. Image reading slips about one character per 15 long lines: use the plain Read tool or Grep for exact quotes, numbers you copy, and small files. If the result ends with MORE, call again with that offset. Refuses images, PDFs, notebooks and binaries (use Read).","inputSchema":{"type":"object","properties":{"file_path":{"type":"string","description":"Path of the text file"},"offset":{"type":"integer","description":"First line, 1-based (default 1)"},"limit":{"type":"integer","description":"Number of lines (default: as many as fit in max_pages)"},"max_pages":{"type":"integer","description":"Pages per call (default 4; each full page is about 5k tokens)"},"layout":{"type":"string","enum":["auto","lines"],"description":"auto (default): prose flows edge to edge; lines: one fixed-width row per source line"}},"required":["file_path"]}},
{"name":"edit","description":"Change a file by exact text replacement without reading it first. Each edit's old_string must occur exactly once in the file as it was before this call (replace_all: every occurrence); edits may not overlap; all edits are checked before anything is written, so one failure writes nothing and names the closest line. Copy old_string from text output (Grep, the plain Read tool), never from a page image. An empty new_string deletes the old text (with its line break when it is whole lines). mode append/create/overwrite takes the whole file text in `text` instead of edits. Keeps CRLF/LF and a UTF-8 BOM; prints the changed lines with their numbers.","inputSchema":{"type":"object","properties":{"file_path":{"type":"string"},"edits":{"type":"array","items":{"type":"object","properties":{"old_string":{"type":"string"},"new_string":{"type":"string"}},"required":["old_string","new_string"]}},"replace_all":{"type":"boolean"},"mode":{"type":"string","enum":["replace","append","create","overwrite"],"description":"replace (default) uses edits; the others use text"},"text":{"type":"string"}},"required":["file_path"]}}
]"#;

fn text_item(t: &str) -> String { format!("{{\"type\":\"text\",\"text\":{}}}", esc(t)) }

fn call_read(a: &J) -> Result<String, String> {
    let file = a.get("file_path").and_then(J::str).ok_or("file_path is required")?;
    let n = |k: &str| a.get(k).and_then(J::num).map(|v| v.max(0.0) as usize);
    let layout = a.get("layout").and_then(J::str).unwrap_or("auto");
    let r = crate::render(file, n("offset").unwrap_or(1), n("limit").unwrap_or(0), layout, n("max_pages").unwrap_or(4), Instant::now())?;
    if let Some(m) = r.info { return Ok(format!("[{}]", text_item(&m))); }
    let mut t = r.head.join("\n");
    t.push_str(&format!("\n{} pages follow. {}", r.pages.len(), r.legend));
    for (k, (_, a, b)) in r.pages.iter().enumerate() { t.push_str(&format!("\npage {}: lines {}-{}", k + 1, a, b)); }
    let mut items = vec![text_item(&t)];
    for (png, _, _) in &r.pages { items.push(format!("{{\"type\":\"image\",\"data\":\"{}\",\"mimeType\":\"image/png\"}}", b64(png))); }
    let mut tail = r.notes.join("\n");
    if let Some(m) = r.more { if !tail.is_empty() { tail.push('\n'); } tail.push_str(&format!("MORE: call read again with offset {} to continue.", m)); }
    if !tail.is_empty() { items.push(text_item(&tail)); }
    Ok(format!("[{}]", items.join(",")))
}

/// Runs `textimg edit` in a child process from a patch file, so its checks, messages and exit codes are the CLI's own.
fn call_edit(a: &J) -> Result<String, String> {
    let file = a.get("file_path").and_then(J::str).ok_or("file_path is required")?;
    let mode = a.get("mode").and_then(J::str).unwrap_or("replace");
    let mut args = vec!["edit".to_string(), file.to_string()];
    let body = match mode {
        "append" | "create" | "overwrite" => { args.push(format!("--{}", mode)); a.get("text").and_then(J::str).ok_or("text is required for this mode")?.to_string() }
        "replace" => {
            let edits = match a.get("edits") { Some(J::Arr(v)) if !v.is_empty() => v, _ => return Err("edits (a non-empty array) is required".into()) };
            if a.get("replace_all").and_then(J::boolean).unwrap_or(false) { args.push("--all".into()); }
            let mut s = String::new();
            for (k, e) in edits.iter().enumerate() {
                let old = e.get("old_string").and_then(J::str).ok_or(format!("edit {}: old_string is required", k + 1))?;
                let new = e.get("new_string").and_then(J::str).ok_or(format!("edit {}: new_string is required", k + 1))?;
                for t in [old, new] {
                    if t.split('\n').any(|l| matches!(l.trim_end_matches('\r'), "@@@OLD@@@" | "@@@NEW@@@" | "@@@END@@@")) {
                        return Err(format!("edit {}: a line that is exactly @@@OLD@@@, @@@NEW@@@ or @@@END@@@ cannot be edited this way; use mode overwrite", k + 1));
                    }
                }
                s.push_str("@@@OLD@@@\n"); s.push_str(old); s.push_str("\n@@@NEW@@@\n");
                if !new.is_empty() { s.push_str(new); s.push('\n'); }
                s.push_str("@@@END@@@\n");
            }
            s
        }
        _ => return Err("mode must be replace, append, create or overwrite".into()),
    };
    let patch = std::env::temp_dir().join(format!("textimg_mcp_{}_{}.patch", std::process::id(), Instant::now().elapsed().as_nanos()));
    std::fs::write(&patch, body.as_bytes()).map_err(|e| format!("cannot write a patch file: {}", e))?;
    args.push("--patch".into()); args.push(patch.display().to_string());
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let out = std::process::Command::new(exe).args(&args).output().map_err(|e| e.to_string());
    let _ = std::fs::remove_file(&patch);
    let out = out?;
    let msg = format!("{}{}", String::from_utf8_lossy(&out.stdout), String::from_utf8_lossy(&out.stderr)).trim_end().to_string();
    if out.status.success() { Ok(format!("[{}]", text_item(&msg))) } else { Err(msg) }
}

pub fn main() {
    let stdin = std::io::stdin();
    let stdout = std::io::stdout();
    for line in stdin.lock().lines() {
        let Ok(line) = line else { break };
        if line.trim().is_empty() { continue; }
        let msg = match (P { s: line.as_bytes(), i: 0 }).val() { Ok(m) => m, Err(_) => continue };
        let id = match msg.get("id") { Some(J::Num(n)) => format!("{}", n), Some(J::Str(s)) => esc(s), _ => continue }; // notifications get no reply
        let method = msg.get("method").and_then(J::str).unwrap_or("");
        let result: Result<String, (i32, String)> = match method {
            "initialize" => {
                let v = msg.get("params").and_then(|p| p.get("protocolVersion")).and_then(J::str).unwrap_or("2025-06-18");
                Ok(format!("{{\"protocolVersion\":{},\"capabilities\":{{\"tools\":{{}}}},\"serverInfo\":{{\"name\":\"textimg\",\"version\":\"{}\"}}}}", esc(v), env!("CARGO_PKG_VERSION")))
            }
            "ping" => Ok("{}".into()),
            "tools/list" => Ok(format!("{{\"tools\":{}}}", TOOLS.replace(['\n', '\r'], ""))),
            "tools/call" => {
                let p = msg.get("params");
                let name = p.and_then(|p| p.get("name")).and_then(J::str).unwrap_or("");
                let empty = J::Obj(BTreeMap::new());
                let a = p.and_then(|p| p.get("arguments")).unwrap_or(&empty);
                let r = match name { "read" => call_read(a), "edit" => call_edit(a), _ => Err(format!("unknown tool {}", name)) };
                Ok(match r {
                    Ok(content) => format!("{{\"content\":{}}}", content),
                    Err(e) => format!("{{\"content\":[{}],\"isError\":true}}", text_item(&format!("textimg: {}", e))),
                })
            }
            _ => Err((-32601, format!("method not found: {}", method))),
        };
        let resp = match result {
            Ok(r) => format!("{{\"jsonrpc\":\"2.0\",\"id\":{},\"result\":{}}}", id, r),
            Err((c, m)) => format!("{{\"jsonrpc\":\"2.0\",\"id\":{},\"error\":{{\"code\":{},\"message\":{}}}}}", id, c, esc(&m)),
        };
        let mut o = stdout.lock();
        let _ = o.write_all(resp.as_bytes());
        let _ = o.write_all(b"\n");
        let _ = o.flush();
    }
}
