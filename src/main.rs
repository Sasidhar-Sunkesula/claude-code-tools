//! textimg: read a text file as compact page images (about a third of the tokens of plain text for prose).
//! No dependencies: glyphs come from an embedded atlas (build_atlas.py: Verdana and Consolas 14px plus fallback
//! fonts for symbols, CJK and Hangul); PNGs are written directly.
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

const ATLAS: &[u8] = include_bytes!("atlas.bin");
const W: usize = 1932;
const H: usize = 1932;
const MARGIN: usize = 4;
const DOT: char = '\u{b7}';
const PILCROW: char = '\u{b6}';
const BAR: char = '\u{a6}';

const HELP: &str = "textimg FILE [-o OFFSET] [-n LIMIT] [--layout auto|packed|lines] [--max-pages N]

Renders a text file as compact page images, then prints their paths. Open every PAGE path with the Read tool
(all in one parallel call). About a third of the tokens of plain text for prose; reading is reliable for
meaning but not character-perfect, so use the normal text Read for anything you will edit or quote exactly.

  -o OFFSET    first line, 1-based (like Read's offset); default 1
  -n LIMIT     number of lines; default: as many as fit in --max-pages
  --layout     auto/packed (default): lines flow edge to edge, each starts with the marker \u{b6}N; code blocks,
               tables, indented code and aligned or box-drawn lines each start their own row in a fixed-width font;
               empty lines outside code get no marker
               lines: every source line starts its own fixed-width row as N\u{2502} (keeps all layout, costs more)
  --max-pages  pages per call, default 8 (about 40k tokens); MORE: names the next offset when it stops early
Leading spaces and runs of 2+ spaces are drawn as \u{b7}, one per space; tabs expand to 4 columns; ANSI color
codes are dropped. Pages render fresh on every call.";

// ---------- glyph atlas (see build_atlas.py for the format) ----------
struct Glyph { adv: f32, left: i32, top: i32, w: usize, h: usize, off: usize }
struct Face { glyphs: Vec<Glyph>, idx: Vec<u32>, q: usize }
impl Face {
    fn find(&self, c: char) -> Option<&Glyph> {
        let i = *self.idx.get(c as usize)?;
        if i == 0 { None } else { Some(&self.glyphs[i as usize - 1]) }
    }
    fn glyph(&self, c: char) -> &Glyph { self.find(c).unwrap_or(&self.glyphs[self.q]) }
    fn width(&self, s: &str) -> f32 { s.chars().map(|c| self.glyph(c).adv).sum() }
}

fn rd<const N: usize>(p: usize) -> [u8; N] { ATLAS[p..p + N].try_into().unwrap() }

/// Returns (line height, [prose face, fixed-width face]); bitmaps stay in the embedded atlas.
fn load_atlas() -> (usize, [Face; 2]) {
    assert!(&ATLAS[0..4] == b"TXA3", "atlas.bin is not TXA3; rerun build_atlas.py");
    let lh = u16::from_le_bytes(rd(4)) as usize;
    let pool = 10;
    let mut p = pool + u32::from_le_bytes(rd(6)) as usize;
    let mut face = || {
        let n = u32::from_le_bytes(rd(p)) as usize;
        p += 4;
        let mut glyphs = Vec::with_capacity(n);
        let mut idx = vec![0u32; 0x20000];
        for _ in 0..n {
            let cp = u32::from_le_bytes(rd(p)) as usize;
            glyphs.push(Glyph {
                adv: f32::from_le_bytes(rd(p + 4)),
                left: i16::from_le_bytes(rd(p + 8)) as i32,
                top: i16::from_le_bytes(rd(p + 10)) as i32,
                w: u16::from_le_bytes(rd(p + 12)) as usize,
                h: u16::from_le_bytes(rd(p + 14)) as usize,
                off: pool + u32::from_le_bytes(rd(p + 16)) as usize,
            });
            p += 20;
            if cp < idx.len() { idx[cp] = glyphs.len() as u32; }
        }
        let q = idx['?' as usize] as usize - 1;
        Face { glyphs, idx, q }
    };
    let prose = face();
    let mono = face();
    (lh, [prose, mono])
}

// ---------- text preparation ----------
fn invisible(c: char) -> bool {
    c.is_control() || matches!(c as u32, 0xAD | 0x200B..=0x200F | 0x2028..=0x202E | 0x2060..=0x206F | 0xFEFF)
}

/// Drops ANSI escapes and invisible characters, expands tabs to 4 columns, and draws leading spaces and runs of
/// two or more spaces as middle dots (one per space).
fn visible(s: &str) -> String {
    let mut t = String::with_capacity(s.len());
    let mut col = 0;
    let mut it = s.chars().peekable();
    while let Some(c) = it.next() {
        if c == '\u{1b}' {
            match it.next() {
                Some('[') => { for d in it.by_ref() { if ('\u{40}'..='\u{7e}').contains(&d) { break; } } }
                Some(']') => { while let Some(d) = it.next() { if d == '\u{7}' { break; } if d == '\u{1b}' { it.next(); break; } } }
                _ => {}
            }
        } else if c == '\t' {
            let k = 4 - col % 4;
            for _ in 0..k { t.push(' '); }
            col += k;
        } else if !invisible(c) { t.push(c); col += 1; }
    }
    let mut out = String::with_capacity(t.len() + 8);
    let mut run = 0;
    let mut leading = true;
    for c in t.chars().chain(std::iter::once('\0')) {
        if c == ' ' { run += 1; continue; }
        if run > 0 {
            if leading || run >= 2 { for _ in 0..run { out.push(DOT); } } else { out.push(' '); }
            run = 0;
        }
        if c != '\0' { leading = false; out.push(c); }
    }
    out
}

/// Greedy word wrap to `maxw` pixels; returns byte ranges of the rows (the space at each break is dropped).
/// Words wider than a row are split between characters. Stops after `cap` rows.
fn wrap(f: &Face, text: &str, maxw: f32, cap: usize) -> Vec<(usize, usize)> {
    let sp = f.glyph(' ').adv;
    let mut out = Vec::new();
    let (mut rs, mut re, mut cw, mut open) = (0usize, 0usize, 0f32, false);
    let mut pos = 0;
    for word in text.split(' ') {
        let (ws, we) = (pos, pos + word.len());
        pos = we + 1;
        let ww = f.width(word);
        if open {
            if cw + sp + ww <= maxw { re = we; cw += sp + ww; continue; }
            if re > rs { out.push((rs, re)); if out.len() >= cap { return out; } }
        }
        if ww <= maxw { rs = ws; re = we; cw = ww; open = true; continue; }
        rs = ws;
        cw = 0.0;
        for (bi, c) in word.char_indices() {
            let a = f.glyph(c).adv;
            if cw + a > maxw && ws + bi > rs {
                out.push((rs, ws + bi));
                if out.len() >= cap { return out; }
                rs = ws + bi;
                cw = 0.0;
            }
            cw += a;
        }
        re = we;
        open = true;
    }
    if open { out.push((rs, re)); }
    out
}

/// Lines whose layout carries meaning: in Markdown, fenced code (with its fences), table rows and indented code;
/// in any file, box drawing and column-aligned text (three spaces inside the line). Each gets its own fixed-width rows.
fn structured_flags(lines: &[String], markdown: bool) -> Vec<bool> {
    let mut out = Vec::with_capacity(lines.len());
    let mut fence: Option<String> = None;
    for l in lines {
        let t = l.trim_start();
        let lead = l.len() - t.len();
        let is_fence = lead <= 3 && (t.starts_with("```") || t.starts_with("~~~"));
        let mut s = false;
        if markdown {
            if let Some(f) = &fence {
                s = true;
                if is_fence && t.starts_with(f.as_str()) && t.trim_end().chars().all(|c| c == f.chars().next().unwrap()) { fence = None; }
            } else if is_fence {
                s = true;
                let ch = t.chars().next().unwrap();
                fence = Some(t.chars().take_while(|&c| c == ch).collect());
            } else if t.starts_with('|') || ((l.starts_with('\t') || lead >= 4) && !t.is_empty() && !list_item(t)) {
                s = true;
            }
        }
        if !s && (l.chars().any(|c| ('\u{2500}'..='\u{259f}').contains(&c)) || l.trim().contains("   ")) { s = true; }
        out.push(s);
    }
    out
}

/// A nested Markdown list item ("- x", "* x", "+ x", "12. x", "3) x"): indented, but not code.
fn list_item(t: &str) -> bool {
    let d = t.chars().take_while(|c| c.is_ascii_digit()).count();
    let rest = &t[d..];
    if d > 0 { rest.starts_with(". ") || rest.starts_with(") ") } else { rest.starts_with("- ") || rest.starts_with("* ") || rest.starts_with("+ ") }
}

struct Row { text: String, first: usize, last: usize, mono: bool }

/// Turns wrapped ranges of `text` into rows. `marks` are (byte, line) where a \u{b6}N marker starts; `head` is the line
/// that owns byte 0. A row that does not start at a marker gets the label \u{a6}N naming the line it continues.
/// `pad` > 0 pads labels to that many digits (fixed-width rows keep their columns aligned).
fn emit(text: &str, marks: &[(usize, usize)], head: usize, ranges: &[(usize, usize)], mono: bool, pad: usize, rows: &mut Vec<Row>) {
    for &(a, b) in ranges {
        let i = marks.partition_point(|m| m.0 <= a);
        let owner = if i > 0 { marks[i - 1].1 } else { head };
        let at_marker = i > 0 && marks[i - 1].0 == a;
        let j = marks.partition_point(|m| m.0 < b);
        let last = if j > 0 { marks[j - 1].1.max(owner) } else { owner };
        let body = &text[a..b];
        let text = if at_marker { body.to_string() } else { label(BAR, owner, pad) + body };
        rows.push(Row { text, first: owner, last, mono });
    }
}

/// "\u{b6}12 " / "\u{a6}12 ": the symbol, the line number padded to `pad` digits, and a separator. No-break spaces
/// keep a marker on the same row as the first word of its line.
fn label(sym: char, n: usize, pad: usize) -> String {
    let d = n.to_string();
    let mut s = String::with_capacity(d.len() + pad + 4);
    s.push(sym);
    s.push_str(&d);
    for _ in d.len()..pad { s.push('\u{a0}'); }
    s.push('\u{a0}');
    s
}

/// Builds at most max_rows+1 rows. Also returns (line, count) for lines with characters the atlas cannot draw.
fn build_rows(faces: &[Face; 2], lines: &[String], start: usize, packed: bool, markdown: bool, max_rows: usize) -> (Vec<Row>, Vec<(usize, usize)>) {
    let maxw = (W - 2 * MARGIN - 2) as f32;
    let mut rows: Vec<Row> = Vec::new();
    let mut missing = Vec::new();
    let check = |f: &Face, body: &str, i: usize, missing: &mut Vec<(usize, usize)>| {
        let n = body.chars().filter(|&c| f.find(c).is_none()).count();
        if n > 0 { missing.push((i, n)); }
    };
    if packed {
        let pad = (start + lines.len()).to_string().len();
        let lab = label(BAR, start + lines.len(), pad);
        let lab_w = [faces[0].width(&lab), faces[1].width(&lab)];
        let flags = structured_flags(lines, markdown);
        let mut flow = String::new();
        let mut marks: Vec<(usize, usize)> = Vec::new();
        let mut head = start;
        for (k, l) in lines.iter().enumerate() {
            let cap = (max_rows + 1).saturating_sub(rows.len());
            if cap == 0 { break; }
            let i = start + k;
            let body = visible(l);
            if flags[k] {
                check(&faces[1], &body, i, &mut missing);
                let item = label(PILCROW, i, pad) + &body;
                let r = wrap(&faces[1], &item, maxw - lab_w[1], cap);
                emit(&item, &[(0, i)], i, &r, true, pad, &mut rows);
                continue;
            }
            check(&faces[0], &body, i, &mut missing);
            let next_breaks = k + 1 == lines.len() || flags[k + 1];
            // an empty line outside code gets no marker at all (the header says so): its number is simply skipped
            let item = if body.is_empty() { String::new() } else { label(PILCROW, i, 0) + &body };
            if !item.is_empty() {
                if !flow.is_empty() { flow.push(' '); }
                marks.push((flow.len(), i));
                flow.push_str(&item);
            }
            if flow.is_empty() { continue; }
            if next_breaks || flow.len() > 100_000 {
                let r = wrap(&faces[0], &flow, maxw - lab_w[0], cap);
                if !next_breaks && r.len() > 1 && r.len() < cap {
                    // keep the last, partly filled row in the flow so the text keeps running edge to edge
                    let (a, _) = *r.last().unwrap();
                    emit(&flow, &marks, head, &r[..r.len() - 1], false, 0, &mut rows);
                    let n = marks.partition_point(|m| m.0 <= a);
                    if n > 0 { head = marks[n - 1].1; }
                    let keep = marks.partition_point(|m| m.0 < a);
                    marks = marks[keep..].iter().map(|&(p, l)| (p - a, l)).collect();
                    flow = flow[a..].to_string();
                } else {
                    emit(&flow, &marks, head, &r, false, 0, &mut rows);
                    flow.clear();
                    marks.clear();
                }
            }
        }
    } else {
        let width = (start + lines.len().saturating_sub(1)).to_string().len();
        let ind = " ".repeat(width + 1);
        let ind_w = faces[1].width(&ind);
        for (k, l) in lines.iter().enumerate() {
            let cap = (max_rows + 1).saturating_sub(rows.len());
            if cap == 0 { break; }
            let i = start + k;
            let body = visible(l);
            check(&faces[1], &body, i, &mut missing);
            let pre = format!("{:>width$}\u{2502}{}", i, body, width = width);
            let r = wrap(&faces[1], &pre, maxw - ind_w, cap);
            for (j, &(a, b)) in r.iter().enumerate() {
                let text = if j == 0 { pre[a..b].to_string() } else { format!("{}{}", ind, &pre[a..b]) };
                rows.push(Row { text, first: i, last: i, mono: true });
            }
        }
    }
    (rows, missing)
}

fn draw(faces: &[Face; 2], rows: &[Row], lh: usize, h: usize) -> Vec<u8> {
    let mut buf = vec![255u8; W * h];
    for (j, r) in rows.iter().enumerate() {
        let f = &faces[r.mono as usize];
        let top = (MARGIN + j * lh) as i32;
        let mut x = MARGIN as f32;
        for c in r.text.chars() {
            let g = f.glyph(c);
            let bits = &ATLAS[g.off..g.off + g.w * g.h];
            let gx = x.round() as i32 + g.left;
            let gy = top + g.top;
            for yy in 0..g.h {
                let py = gy + yy as i32;
                if py < 0 || py as usize >= h { continue; }
                let row = py as usize * W;
                for xx in 0..g.w {
                    let cov = bits[yy * g.w + xx];
                    if cov == 0 { continue; }
                    let px = gx + xx as i32;
                    if px < 0 || px as usize >= W { continue; }
                    let p = &mut buf[row + px as usize];
                    let v = 255 - cov;
                    if v < *p { *p = v; }
                }
            }
            x += g.adv;
        }
    }
    buf
}

// ---------- PNG writer: 8-bit gray, filter none, zlib with fixed-Huffman deflate and run/row-repeat matches ----------
struct Bits { out: Vec<u8>, acc: u64, n: u32 }
impl Bits {
    fn put(&mut self, v: u32, len: u32) { self.acc |= (v as u64) << self.n; self.n += len; while self.n >= 8 { self.out.push(self.acc as u8); self.acc >>= 8; self.n -= 8; } }
    fn huff(&mut self, code: u32, len: u32) { let mut r = 0; for i in 0..len { r |= ((code >> i) & 1) << (len - 1 - i); } self.put(r, len); }
    fn flush(&mut self) { if self.n > 0 { self.out.push(self.acc as u8); self.acc = 0; self.n = 0; } }
}
const LBASE: [u32; 29] = [3,4,5,6,7,8,9,10,11,13,15,17,19,23,27,31,35,43,51,59,67,83,99,115,131,163,195,227,258];
const LEXT: [u32; 29] = [0,0,0,0,0,0,0,0,1,1,1,1,2,2,2,2,3,3,3,3,4,4,4,4,5,5,5,5,0];
const DBASE: [u32; 30] = [1,2,3,4,5,7,9,13,17,25,33,49,65,97,129,193,257,385,513,769,1025,1537,2049,3073,4097,6145,8193,12289,16385,24577];
const DEXT: [u32; 30] = [0,0,0,0,1,1,2,2,3,3,4,4,5,5,6,6,7,7,8,8,9,9,10,10,11,11,12,12,13,13];
fn lit(b: &mut Bits, v: u32) {
    if v <= 143 { b.huff(0x30 + v, 8) } else if v <= 255 { b.huff(0x190 + v - 144, 9) } else if v <= 279 { b.huff(v - 256, 7) } else { b.huff(0xC0 + v - 280, 8) }
}
fn deflate(data: &[u8], stride: usize) -> Vec<u8> {
    let mut b = Bits { out: Vec::with_capacity(data.len() / 8), acc: 0, n: 0 };
    b.put(1, 1); b.put(1, 2); // final block, fixed Huffman
    let mut i = 0;
    while i < data.len() {
        let mut best = 0; let mut dist = 0;
        for &d in &[1usize, stride] {
            if i >= d {
                let mut l = 0;
                while l < 258 && i + l < data.len() && data[i + l] == data[i + l - d] { l += 1; }
                if l > best { best = l; dist = d; }
            }
        }
        if best >= 3 {
            let li = LBASE.iter().rposition(|&x| x as usize <= best).unwrap();
            lit(&mut b, 257 + li as u32); b.put(best as u32 - LBASE[li], LEXT[li]);
            let di = DBASE.iter().rposition(|&x| x as usize <= dist).unwrap();
            b.huff(di as u32, 5); b.put(dist as u32 - DBASE[di], DEXT[di]);
            i += best;
        } else { lit(&mut b, data[i] as u32); i += 1; }
    }
    lit(&mut b, 256); b.flush();
    b.out
}
fn crc_table() -> [u32; 256] { let mut t = [0u32; 256]; for n in 0..256 { let mut c = n as u32; for _ in 0..8 { c = if c & 1 != 0 { 0xEDB88320 ^ (c >> 1) } else { c >> 1 }; } t[n] = c; } t }
fn chunk(out: &mut Vec<u8>, ty: &[u8], data: &[u8], t: &[u32; 256]) {
    out.extend_from_slice(&(data.len() as u32).to_be_bytes());
    let mut c = 0xFFFFFFFFu32;
    for &x in ty.iter().chain(data.iter()) { c = t[((c ^ x as u32) & 0xFF) as usize] ^ (c >> 8); }
    out.extend_from_slice(ty); out.extend_from_slice(data); out.extend_from_slice(&(c ^ 0xFFFFFFFF).to_be_bytes());
}
fn png(buf: &[u8], h: usize) -> Vec<u8> {
    let mut raw = Vec::with_capacity((W + 1) * h);
    for y in 0..h { raw.push(0); raw.extend_from_slice(&buf[y * W..(y + 1) * W]); }
    let (mut a, mut bsum) = (1u32, 0u32);
    for &x in &raw { a = (a + x as u32) % 65521; bsum = (bsum + a) % 65521; }
    let mut z = vec![0x78, 0x01]; z.extend(deflate(&raw, W + 1)); z.extend_from_slice(&((bsum << 16) | a).to_be_bytes());
    let t = crc_table();
    let mut out = vec![0x89, b'P', b'N', b'G', 0x0D, 0x0A, 0x1A, 0x0A];
    let mut ih = Vec::new(); ih.extend_from_slice(&(W as u32).to_be_bytes()); ih.extend_from_slice(&(h as u32).to_be_bytes()); ih.extend_from_slice(&[8, 0, 0, 0, 0]);
    chunk(&mut out, b"IHDR", &ih, &t); chunk(&mut out, b"IDAT", &z, &t); chunk(&mut out, b"IEND", &[], &t);
    out
}

// ---------- input ----------
fn decode(b: &[u8]) -> String {
    if b.starts_with(&[0xEF, 0xBB, 0xBF]) { return String::from_utf8_lossy(&b[3..]).into_owned(); }
    if b.starts_with(&[0xFF, 0xFE]) || b.starts_with(&[0xFE, 0xFF]) {
        let le = b[0] == 0xFF;
        let u: Vec<u16> = b[2..].chunks(2).filter(|c| c.len() == 2).map(|c| if le { u16::from_le_bytes([c[0], c[1]]) } else { u16::from_be_bytes([c[0], c[1]]) }).collect();
        return String::from_utf16_lossy(&u);
    }
    match std::str::from_utf8(b) { Ok(s) => s.to_string(), Err(_) => b.iter().map(|&c| c as char).collect() } // not UTF-8: Latin-1
}

fn clean(p: &Path) -> String { p.display().to_string().trim_start_matches(r"\\?\").to_string() }

fn pct_decode(s: &str) -> String {
    let b = s.as_bytes();
    let hex = |c: u8| (c as char).to_digit(16).map(|d| d as u8);
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            if let (Some(h), Some(l)) = (hex(b[i + 1]), hex(b[i + 2])) { out.push(h * 16 + l); i += 3; continue; }
        }
        out.push(b[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Local images that Markdown/HTML image links in `lines` point to (marked when missing), and the count of remote ones.
fn image_links(lines: &[String], dir: &Path) -> (Vec<String>, usize) {
    let mut found: Vec<String> = Vec::new();
    let mut seen: Vec<PathBuf> = Vec::new();
    let mut remote = 0;
    for l in lines {
        let mut rest = l.as_str();
        loop {
            let p = [rest.find("]("), rest.find("src=\""), rest.find("src='")].into_iter().flatten().min();
            let Some(p) = p else { break };
            let is_md = rest[p..].starts_with("](");
            if is_md {
                let is_img = rest[..p].rfind('[').map_or(false, |b| rest[..b].ends_with('!'));
                if !is_img { rest = &rest[p + 2..]; continue; }
            }
            let tail = &rest[p + if is_md { 2 } else { 5 }..];
            let (target, after) = if is_md {
                if let Some(t) = tail.strip_prefix('<') { let e = t.find('>').unwrap_or(t.len()); (&t[..e], &t[e..]) }
                else { let e = tail.find(|c: char| c == ')' || c == ' ').unwrap_or(tail.len()); (&tail[..e], &tail[e..]) }
            } else {
                let q = rest.as_bytes()[p + 4] as char;
                let e = tail.find(q).unwrap_or(tail.len());
                (&tail[..e], &tail[e..])
            };
            rest = after;
            let t = target.trim();
            if t.is_empty() || t.starts_with("data:") { continue; }
            if t.starts_with("http://") || t.starts_with("https://") || t.starts_with("//") { remote += 1; continue; }
            let t = pct_decode(t.split(['#', '?']).next().unwrap_or(""));
            let t = t.strip_prefix("file:///").unwrap_or(&t).to_string();
            let full = dir.join(t.replace('/', std::path::MAIN_SEPARATOR_STR));
            let full = fs::canonicalize(&full).unwrap_or(full);
            if seen.contains(&full) { continue; }
            found.push(format!("{}{}", clean(&full), if full.exists() { "" } else { " (missing)" }));
            seen.push(full);
        }
    }
    (found, remote)
}

/// "3, 7-9, 12" from sorted line numbers, at most `max` items.
fn ranges(v: &[usize], max: usize) -> String {
    let mut parts: Vec<String> = Vec::new();
    let mut i = 0;
    while i < v.len() {
        let mut j = i;
        while j + 1 < v.len() && v[j + 1] == v[j] + 1 { j += 1; }
        parts.push(if j > i { format!("{}-{}", v[i], v[j]) } else { v[i].to_string() });
        i = j + 1;
    }
    if parts.len() > max { let n = parts.len() - max; parts.truncate(max); parts.push(format!("and {} more", n)); }
    parts.join(", ")
}

fn die(msg: &str) -> ! { eprintln!("textimg: {}", msg); std::process::exit(1) }

fn main() {
    let t0 = Instant::now();
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() || args.iter().any(|a| a == "-h" || a == "--help") { println!("{}", HELP); return; }
    let (mut file, mut offset, mut limit, mut layout, mut max_pages) = (None, 1usize, 0usize, "auto".to_string(), 8usize);
    let mut i = 0;
    while i < args.len() {
        let a = &args[i];
        let mut val = || { i += 1; args.get(i).cloned().unwrap_or_else(|| die(&format!("{} needs a value", a))) };
        match a.as_str() {
            "-o" | "--offset" => offset = val().parse().unwrap_or_else(|_| die("offset must be a line number (1 or more)")),
            "-n" | "--limit" => limit = val().parse().unwrap_or_else(|_| die("limit must be a number of lines")),
            "--layout" => layout = val(),
            "--max-pages" => max_pages = val().parse::<usize>().unwrap_or_else(|_| die("max-pages must be a number")).max(1),
            _ if file.is_none() => file = Some(a.clone()),
            _ => die(&format!("unexpected argument {}", a)),
        }
        i += 1;
    }
    let packed = match layout.as_str() { "packed" | "auto" => true, "lines" => false, _ => die("layout must be auto, packed or lines") };
    let path = PathBuf::from(file.unwrap_or_else(|| die("no file given")));
    if !path.exists() { die(&format!("File does not exist: {}", path.display())); }
    if path.is_dir() { die(&format!("{} is a directory, not a file; list it with ls or Glob.", path.display())); }
    let ap = fs::canonicalize(&path).unwrap_or_else(|e| die(&format!("{}: {}", path.display(), e)));
    let disp = clean(&ap);
    let ext = ap.extension().and_then(|e| e.to_str()).unwrap_or("").to_ascii_lowercase();
    const USE_READ: [&str; 26] = ["png", "jpg", "jpeg", "gif", "bmp", "webp", "tif", "tiff", "ico", "pdf", "ipynb", "docx", "xlsx", "pptx",
        "doc", "xls", "ppt", "zip", "gz", "7z", "rar", "tar", "exe", "dll", "so", "bin"];
    if USE_READ.contains(&ext.as_str()) { die(&format!("{} is not a text file; use the Read tool (it shows images, PDFs and notebooks).", disp)); }
    let raw = fs::read(&ap).unwrap_or_else(|e| die(&format!("{}: {}", disp, e)));
    let utf16 = raw.starts_with(&[0xFF, 0xFE]) || raw.starts_with(&[0xFE, 0xFF]);
    if !utf16 && raw[..raw.len().min(8192)].contains(&0) { die(&format!("{} looks binary; use the Read tool.", disp)); }
    let text = decode(&raw);
    let mut lines: Vec<String> = text.split('\n').map(|l| l.strip_suffix('\r').unwrap_or(l).to_string()).collect();
    if lines.last().map_or(false, |l| l.is_empty()) { lines.pop(); }
    let total = lines.len();
    if total == 0 { println!("textimg: {} exists but is empty (0 lines).", disp); return; }
    let s = offset.max(1);
    if s > total { die(&format!("{} has only {} lines; offset {} is past the end.", disp, total, s)); }
    let end = if limit > 0 { (s - 1 + limit).min(total) } else { total };
    let markdown = ["md", "markdown", "mdx", "rst"].contains(&ext.as_str());
    let (lh, faces) = load_atlas();
    let per = (H - 2 * MARGIN) / lh;
    let cut = per * max_pages;
    let (rows, missing) = build_rows(&faces, &lines[s - 1..end], s, packed, markdown, cut);
    // when the rows overflow max-pages, the next call starts at the line that the first unshown row belongs to
    let mut long_first = false;
    let more = if rows.len() > cut {
        let m = rows[cut].first;
        if m <= s { long_first = true; Some(s + 1) } else { Some(m) }
    } else { None };
    let shown = &rows[..rows.len().min(cut)];
    if shown.is_empty() { println!("textimg: {} | lines {}-{} of {} are all empty.", disp, s, end, total); return; }
    // fresh output folder; folders older than two hours are removed
    let base = std::env::temp_dir().join("textimg");
    if let Ok(rd) = fs::read_dir(&base) {
        for e in rd.flatten() {
            if let Ok(m) = e.metadata() { if m.modified().ok().and_then(|t| t.elapsed().ok()).map_or(false, |d| d.as_secs() > 7200) { let _ = fs::remove_dir_all(e.path()); } }
        }
    }
    let stamp = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis();
    let out = base.join(format!("{}_{}", stamp, std::process::id()));
    fs::create_dir_all(&out).unwrap_or_else(|e| die(&format!("cannot create {}: {}", out.display(), e)));
    let mut px = 0usize;
    let mut listed = Vec::new();
    let pages: Vec<&[Row]> = shown.chunks(per).collect();
    for (n, p) in pages.iter().enumerate() {
        let h = if n + 1 < pages.len() || p.len() == per { H } else { 2 * MARGIN + lh * p.len() };
        let buf = draw(&faces, p, lh, h);
        let f = out.join(format!("p{:02}.png", n + 1));
        fs::write(&f, png(&buf, h)).unwrap_or_else(|e| die(&format!("cannot write {}: {}", f.display(), e)));
        px += W * h;
        listed.push((f, p[0].first, p.iter().map(|r| r.last).max().unwrap()));
    }
    let shown_end = match more { Some(m) => m - 1, None => end };
    let last_drawn = listed.last().map_or(s, |l| l.2);
    let est = px / 750 + 70 * listed.len();
    let shown_bytes: usize = lines[s - 1..shown_end].iter().map(|l| l.len() + 1).sum::<usize>()
        .min(shown.iter().map(|r| r.text.len()).sum()); // a cut-off long line counts only its shown part
    let text_est = shown_bytes * 10 / 28; // plain text runs about 2.8 bytes per token on these files
    let so = std::io::stdout();
    let mut o = so.lock();
    let _ = writeln!(o, "textimg: {} | lines {}-{} of {} | {} layout | {} page(s) | ~{} tokens (plain text ~{}) | {} ms",
        disp, s, shown_end, total, if packed { "packed" } else { "lines" }, listed.len(), est, text_est, t0.elapsed().as_millis());
    if est * 10 > text_est * 9 { let _ = writeln!(o, "NOTE: images save little or nothing here; the plain Read tool is as cheap and exact."); }
    let marker = if packed { "\u{b6}N starts line N; a row starting with \u{a6}N continues line N (the label is not text); a line number that never appears is an empty line" } else { "N\u{2502} starts line N" };
    let _ = writeln!(o, "Read every PAGE below with the Read tool (one parallel call). {}; \u{b7} = one space.", marker);
    for (f, a, b) in &listed { let _ = writeln!(o, "PAGE {}  (lines {}-{})", f.display(), a, b); }
    let miss: Vec<&(usize, usize)> = missing.iter().filter(|m| m.0 <= last_drawn).collect();
    if !miss.is_empty() {
        let n: usize = miss.iter().map(|m| m.1).sum();
        let at: Vec<usize> = miss.iter().map(|m| m.0).collect();
        let _ = writeln!(o, "NOTE: {} character(s) this font set cannot draw are shown as ? on line(s) {}; read those lines with the text Read tool.", n, ranges(&at, 12));
    }
    if markdown {
        let dir = ap.parent().map(|p| p.to_path_buf()).unwrap_or_default();
        let (found, remote) = image_links(&lines[s - 1..last_drawn.min(total)], &dir);
        if !found.is_empty() { let _ = writeln!(o, "IMAGES linked in these lines (not drawn; open with Read if needed): {}", found.join(" ; ")); }
        if remote > 0 { let _ = writeln!(o, "NOTE: {} remote image link(s) not fetched.", remote); }
    }
    if long_first { let _ = writeln!(o, "NOTE: line {} is longer than {} page(s); only its start is shown (raise --max-pages, or use the text Read).", s, max_pages); }
    if let Some(m) = more { if m <= end { let _ = writeln!(o, "MORE: run again with -o {} to continue.", m); } }
}
