//! `textimg edit`: exact-text file edits from the shell, safe for any characters (no escapes are interpreted).

use std::fs;
use std::io::Read;
use std::path::Path;

pub const HELP: &str = r#"textimg edit FILE [--all] [--patch PATCHFILE] <<'EOF'
@@@OLD@@@
exact old text (copied from TEXT output: grep -n, sed -n 'A,Bp', Read; never from a page image)
@@@NEW@@@
new text (leave empty to delete the old text with its line break)
@@@END@@@
EOF

Several OLD/NEW/END blocks may follow each other. Each OLD must occur exactly once (--all: every occurrence is
replaced). Every OLD is matched against the file as it was before this call, and blocks that overlap are refused
(merge them into one block): a block never sees what another block of the same call wrote. All blocks are checked
before anything is written: one failure writes nothing. A line that is exactly a marker cannot appear inside a block.

  textimg edit FILE --append     <<'EOF' ... EOF   the text goes at the end, on a new line
  textimg edit FILE --create     <<'EOF' ... EOF   a new file (refused if FILE exists)
  textimg edit FILE --overwrite  <<'EOF' ... EOF   replaces the whole file
For these three the whole input is the text: no markers (a marker line in it is written as text).

Use a QUOTED heredoc marker (<<'EOF') so the shell leaves $ and backticks alone.
BACKSLASHES: Claude Code's Bash tool turns \\ into \ before the shell sees a command, so inline text holding any
backslash is refused. Write the same input to a NEW file with the Write tool (it keeps backslashes) and pass
--patch PATCHFILE; the patch file is deleted after it is read, so the next patch is again a new file.
Line endings (CRLF or LF) and a UTF-8 BOM are kept. On success it prints each change's line numbers and new lines."#;

const M_OLD: &str = "@@@OLD@@@";
const M_NEW: &str = "@@@NEW@@@";
const M_END: &str = "@@@END@@@";

fn die(msg: &str) -> ! {
    eprintln!("textimg edit: {}", msg);
    std::process::exit(1)
}

fn parse_blocks(text: &str) -> Vec<(String, Option<String>)> {
    let mut blocks = Vec::new();
    let (mut state, mut old, mut new) = (0u8, Vec::<&str>::new(), Vec::<&str>::new()); // 0 outside, 1 old, 2 new
    for (n, line) in text.split('\n').enumerate() {
        let s = line.strip_suffix('\r').unwrap_or(line);
        let n = n + 1;
        if s == M_OLD {
            if state != 0 { die(&format!("input line {}: {} before the previous block ended with {}", n, M_OLD, M_END)); }
            state = 1; old.clear(); new.clear();
        } else if s == M_NEW {
            if state != 1 { die(&format!("input line {}: {} without {} before it", n, M_NEW, M_OLD)); }
            state = 2;
        } else if s == M_END {
            if state != 2 { die(&format!("input line {}: {} without {} before it", n, M_END, M_NEW)); }
            blocks.push((old.join("\n"), if new.is_empty() { None } else { Some(new.join("\n")) }));
            state = 0;
        } else if state == 1 { old.push(s); }
        else if state == 2 { new.push(s); }
        else if !s.trim().is_empty() { die(&format!("input line {}: text outside a block: {:?}", n, s.chars().take(60).collect::<String>())); }
    }
    if state != 0 { die(&format!("the last block has no {}", M_END)); }
    if blocks.is_empty() { die(&format!("no blocks given (each is {} / old text / {} / new text / {})", M_OLD, M_NEW, M_END)); }
    blocks
}

/// Similarity of two strings by shared character bigrams (Dice coefficient).
fn alike(a: &str, b: &str) -> f64 {
    let bg = |s: &str| { let c: Vec<char> = s.chars().collect(); c.windows(2).map(|w| (w[0], w[1])).collect::<Vec<_>>() };
    let (x, mut y) = (bg(a), bg(b));
    if x.is_empty() || y.is_empty() { return if a == b { 1.0 } else { 0.0 }; }
    let total = x.len() + y.len();
    let mut hit = 0;
    for p in &x { if let Some(i) = y.iter().position(|q| q == p) { y.swap_remove(i); hit += 1; } }
    2.0 * hit as f64 / total as f64
}

/// Where the old text's first line almost matches, to show why it was not found.
fn nearest(content: &str, old: &str) -> String {
    let first = old.split('\n').find(|l| !l.trim().is_empty()).unwrap_or("").trim();
    let mut best = (0.0, 0usize, "");
    for (i, l) in content.split('\n').enumerate() {
        let r = alike(l.trim(), first);
        if r > best.0 { best = (r, i, l); }
    }
    if best.0 > 0.5 { format!("; closest line {} ({:.0}% alike): {:?}", best.1 + 1, best.0 * 100.0, best.2.chars().take(120).collect::<String>()) } else { String::new() }
}

fn line_of(s: &str, at: usize) -> usize { s[..at].matches('\n').count() + 1 }

fn write(path: &Path, content: &str, bom: bool, crlf: bool) {
    let body = if crlf { content.replace('\n', "\r\n") } else { content.to_string() };
    let mut data = Vec::with_capacity(body.len() + 3);
    if bom { data.extend_from_slice(&[0xEF, 0xBB, 0xBF]); }
    data.extend_from_slice(body.as_bytes());
    let dir = path.parent().filter(|d| !d.as_os_str().is_empty()).unwrap_or(Path::new("."));
    let tmp = dir.join(format!(".textimg_edit_{}", std::process::id()));
    fs::write(&tmp, &data).unwrap_or_else(|e| die(&format!("cannot write {}: {}", tmp.display(), e)));
    if let Err(e) = fs::rename(&tmp, path) { let _ = fs::remove_file(&tmp); die(&format!("cannot replace {}: {}", path.display(), e)); }
}

pub fn main(args: &[String]) {
    if args.is_empty() || args.iter().any(|a| a == "-h" || a == "--help") { println!("{}", HELP); return; }
    let mut path = None;
    let mut patch = None;
    let (mut all, mut append, mut create, mut overwrite) = (false, false, false, false);
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--all" => all = true,
            "--append" => append = true,
            "--create" => create = true,
            "--overwrite" => overwrite = true,
            "--patch" => { i += 1; patch = Some(args.get(i).cloned().unwrap_or_else(|| die("--patch needs a file"))); }
            a if a.starts_with("--") => die(&format!("unknown option {}", a)),
            a if path.is_none() => path = Some(a.to_string()),
            a => die(&format!("unexpected argument {}", a)),
        }
        i += 1;
    }
    let path = path.unwrap_or_else(|| die("no file given"));
    let p = Path::new(&path);
    let text = match &patch {
        Some(pf) => {
            let b = fs::read(pf).unwrap_or_else(|e| die(&format!("patch file {}: {}", pf, e)));
            let _ = fs::remove_file(pf); // read once: the next patch is written as a new file
            String::from_utf8(b).unwrap_or_else(|_| die("patch file is not UTF-8"))
        }
        None => {
            let mut b = Vec::new();
            std::io::stdin().read_to_end(&mut b).unwrap_or_else(|e| die(&format!("reading stdin: {}", e)));
            let t = String::from_utf8(b).unwrap_or_else(|_| die("input is not UTF-8"));
            if t.contains('\\') {
                die("inline text holds a backslash, which the Bash tool may have changed (\\\\ arrives as \\); nothing written. Write the same input to a new file with the Write tool and pass --patch FILE.");
            }
            t
        }
    };
    if create || overwrite {
        if create && p.exists() { die(&format!("{} exists; edit it with blocks, or use --overwrite", path)); }
        let crlf = p.exists() && fs::read(p).map_or(false, |b| b.windows(2).any(|w| w == b"\r\n"));
        write(p, &text, false, crlf);
        println!("wrote {}: {} lines", path, text.matches('\n').count());
        return;
    }
    if !p.exists() { die(&format!("file does not exist: {} (use --create for a new file)", path)); }
    if p.is_dir() { die(&format!("{} is a directory", path)); }
    let raw = fs::read(p).unwrap_or_else(|e| die(&format!("{}: {}", path, e)));
    let bom = raw.starts_with(&[0xEF, 0xBB, 0xBF]);
    let mut content = String::from_utf8(raw[if bom { 3 } else { 0 }..].to_vec()).unwrap_or_else(|_| die(&format!("{} is not UTF-8 text", path)));
    let crlf = content.contains("\r\n");
    if crlf { content = content.replace("\r\n", "\n"); }
    if append {
        let body = if text.ends_with('\n') { text.clone() } else { format!("{}\n", text) };
        let sep = !content.is_empty() && !content.ends_with('\n');
        let start = content.matches('\n').count() + 1 + sep as usize;
        if sep { content.push('\n'); }
        content.push_str(&body);
        write(p, &content, bom, crlf);
        println!("appended {} lines to {} at line {}", body.matches('\n').count(), path, start);
        return;
    }
    // every OLD is located in the file as it was before this call; the blocks' regions may not overlap, so no block
    // can match or undo text another block of the same call writes
    let blocks = parse_blocks(&text);
    let bytes = content.as_bytes();
    let mut regions: Vec<(usize, usize, usize)> = Vec::new(); // (start, end, block index)
    let mut counts = Vec::with_capacity(blocks.len());
    for (k, (old, new)) in blocks.iter().enumerate() {
        if old.is_empty() { die(&format!("block {}: empty OLD text", k + 1)); }
        let hits: Vec<usize> = content.match_indices(old.as_str()).map(|(i, _)| i).collect();
        if hits.is_empty() { die(&format!("block {}: OLD text not found in {}{}; nothing written", k + 1, path, nearest(&content, old))); }
        if hits.len() > 1 && !all { die(&format!("block {}: OLD text found {} times in {}; add context to make it unique, or pass --all; nothing written", k + 1, hits.len(), path)); }
        counts.push(hits.len());
        for &at in &hits {
            let mut end = at + old.len();
            // a whole-line removal takes its line break with it
            if new.is_none() && bytes.get(end) == Some(&b'\n') && (at == 0 || bytes[at - 1] == b'\n') { end += 1; }
            regions.push((at, end, k));
        }
    }
    regions.sort();
    for w in regions.windows(2) {
        if w[1].0 < w[0].1 {
            let (a, b) = (w[0].2.min(w[1].2) + 1, w[0].2.max(w[1].2) + 1);
            die(&format!("blocks {} and {} overlap at line {} of the original file (every OLD is matched against the file before this call); merge them into one block; nothing written",
                a, b, line_of(&content, w[1].0)));
        }
    }
    let mut out = String::with_capacity(content.len());
    let mut first_line: Vec<Option<usize>> = vec![None; blocks.len()];
    let (mut pos, mut lines) = (0usize, 0usize);
    for &(a, b, k) in &regions {
        lines += content[pos..a].matches('\n').count();
        out.push_str(&content[pos..a]);
        if first_line[k].is_none() { first_line[k] = Some(lines + 1); }
        if let Some(new) = &blocks[k].1 { out.push_str(new); lines += new.matches('\n').count(); }
        pos = b;
    }
    out.push_str(&content[pos..]);
    let mut report = Vec::new();
    for (k, (old, new)) in blocks.iter().enumerate() {
        let (line, n) = (first_line[k].unwrap(), counts[k]);
        let places = if n > 1 { format!(" ({} places; first shown)", n) } else { String::new() };
        match new {
            None => report.push(format!("block {}: deleted {} line(s) at line {}{}", k + 1, old.matches('\n').count() + 1, line, places)),
            Some(new) => {
                let shown: Vec<&str> = new.split('\n').collect();
                let mut r = format!("block {}: lines {}-{}{}:", k + 1, line, line + shown.len() - 1, places);
                for (j, l) in shown.iter().take(12).enumerate() { r.push_str(&format!("\n  {}: {}", line + j, l)); }
                if shown.len() > 12 { r.push_str(&format!("\n  ... ({} more lines)", shown.len() - 12)); }
                report.push(r);
            }
        }
    }
    write(p, &out, bom, crlf);
    println!("edited {}\n{}", path, report.join("\n"));
}
