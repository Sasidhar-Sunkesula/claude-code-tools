#!/usr/bin/env python3
"""chronicle: a separate model's full-fidelity record of a long Claude Code session, part by part.

While the session works, every ~chunk_tokens of new transcript is handed (complete: thinking, every tool call and
result, every image) to a separate summarizer (claude -p, Opus 5.5, xhigh effort), which writes a chronological
record. User messages and the assistant's messages to the user are inserted verbatim by this program, never by the
model. Before each compaction the remaining tail is recorded too. docs/chronicle/now.md, imported by the project's
CLAUDE.md, is rebuilt after every record, so after a compaction the session wakes with the records loaded.

Commands:
  chronicle.py hook                      (Claude Code hooks: PostToolUse, UserPromptSubmit, Stop, PreCompact, SessionStart)
  chronicle.py record TRANSCRIPT [--final] [--start-segments N]
  chronicle.py rebuild PROJECT_DIR       (rewrite now.md from the records)
  chronicle.py import-log PROJECT_DIR LOG.md [--dry]   (an old lab log's entries before the first transcript record,
                                         copied verbatim as records, one per stretch between wakings and per day)
  chronicle.py status PROJECT_DIR
Config: PROJECT_DIR/.claude/chronicle.json (its presence turns chronicle on for that project).
Standard library only.
"""
import base64, datetime, glob, json, os, re, shutil, struct, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = {
    "title_match": "",            # record only sessions whose title contains this (case-insensitive); "" = any
    "min_context": 150000,        # ...and only once their context has grown past this many tokens
    "chunk_tokens": 120000,       # transcript tokens per recorded part
    "model": "claude-opus-5-5",
    "effort": "xhigh",
    "max_output_tokens": 128000,
    "budget_tokens": 150000,      # size of now.md
    "full_tokens": 110000,        # of which: the most recent records in full
    "days": 7,                    # older records appear in now.md's index only
    "precompact_wait": 90,        # seconds PreCompact waits for the tail's record (0 when another recording runs)
    "records_dir": "docs/chronicle",
    "state_dir": "docs/chronicle/.tool",
    "claude": "",                 # path of the claude CLI ("" = from PATH)
    "inject": "import",           # import: CLAUDE.md imports now.md as text; images: the session reads now.md through textimg
    "textimg": os.path.join(os.path.dirname(HERE), "textimg", "textimg.exe" if os.name == "nt" else "textimg"),  # the repo's build
}
SKIP_ATTACH = {"output_style", "deferred_tools_record", "deferred_tools_delta", "skill_listing", "agent_listing_delta",
               "prompt_snapshot", "environment", "model", "output_style_instructions", "session_context", "date",
               "credential_org", "remote_session_change", "silent_turn_reminder", "todo_reminder", "mode"}
CHARS_PER_TOKEN = 3.3
MAX_PARTS = [10 ** 9]  # record at most this many parts in one run (--max-parts)


# ---------------------------------------------------------------- basics
def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def local(ts, fmt="%Y-%m-%d %H:%M"):
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime(fmt)
    except Exception:
        return ts or "?"


def find_root(start):
    d = os.path.abspath(start or ".")
    while True:
        if os.path.isfile(os.path.join(d, ".claude", "chronicle.json")):
            return d
        p = os.path.dirname(d)
        if p == d:
            return None
        d = p


class Project:
    def __init__(self, root):
        self.root = root
        cfg = dict(DEFAULTS)
        try:
            cfg.update(json.load(open(os.path.join(root, ".claude", "chronicle.json"), encoding="utf-8")))
        except Exception:
            pass
        self.cfg = cfg
        self.rec_dir = os.path.join(root, cfg["records_dir"])
        self.st_dir = os.path.join(root, cfg["state_dir"])
        os.makedirs(self.rec_dir, exist_ok=True)
        os.makedirs(self.st_dir, exist_ok=True)
        self.state_path = os.path.join(self.st_dir, "state.json")
        self.lock_path = os.path.join(self.st_dir, "lock.json")

    def log(self, msg):
        with open(os.path.join(self.st_dir, "log.txt"), "a", encoding="utf-8") as f:
            f.write(f"{now_iso()} [{os.getpid()}] {msg}\n")

    def load(self):
        try:
            return json.load(open(self.state_path, encoding="utf-8"))
        except Exception:
            return {"records": [], "last_uuid": None, "files": {}, "notes": {}}

    def save(self, st):
        tmp = self.state_path + f".{os.getpid()}.tmp"
        json.dump(st, open(tmp, "w", encoding="utf-8"), indent=1)
        os.replace(tmp, self.state_path)

    # the hooks keep their own state (scan offsets, pending marks, notes); only the recorder writes state.json
    def load_hook(self):
        try:
            return json.load(open(os.path.join(self.st_dir, "hook.json"), encoding="utf-8"))
        except Exception:
            return {"files": {}, "notes": {}}

    def save_hook(self, hs):
        p = os.path.join(self.st_dir, "hook.json")
        tmp = p + f".{os.getpid()}.tmp"
        json.dump(hs, open(tmp, "w", encoding="utf-8"), indent=1)
        os.replace(tmp, p)


def pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def take_lock(pj, wait_s):
    end = time.time() + wait_s
    while True:
        try:
            fd = os.open(pj.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, json.dumps({"pid": os.getpid(), "since": now_iso()}).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                held = json.load(open(pj.lock_path, encoding="utf-8"))
            except Exception:
                held = {}
            if not pid_alive(held.get("pid")):
                try:
                    os.remove(pj.lock_path)
                except OSError:
                    pass
                continue
            if time.time() > end:
                return False
            time.sleep(3)


def drop_lock(pj):
    try:
        if json.load(open(pj.lock_path, encoding="utf-8")).get("pid") == os.getpid():
            os.remove(pj.lock_path)
    except Exception:
        pass


def spawn(args, pj):
    """Start a detached background process (it outlives the hook)."""
    out = open(os.path.join(pj.st_dir, "job.log"), "a", encoding="utf-8")
    kw = dict(stdin=subprocess.DEVNULL, stdout=out, stderr=out, close_fds=True, cwd=pj.root)
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
    else:
        kw["start_new_session"] = True
    return subprocess.Popen([sys.executable, os.path.abspath(__file__)] + args, **kw).pid


# ---------------------------------------------------------------- transcript
def index_file(path):
    """uuid -> (offset, length, type, parent, logicalParent, sidechain, subtype, ts) for chain entries; plus file order."""
    idx, order, title = {}, [], None
    with open(path, "rb") as f:
        off = 0
        for raw in f:
            n = len(raw)
            head = raw[:300]
            if b'"custom-title"' in head:
                try:
                    title = json.loads(raw).get("customTitle") or title
                except Exception:
                    pass
            elif b'"uuid"' in raw and (b'"parentUuid"' in head or b'"type":"system"' in head or b'"type": "system"' in head):
                try:
                    d = json.loads(raw)
                except Exception:
                    off += n
                    continue
                u = d.get("uuid")
                if u and d.get("type") in ("user", "assistant", "system", "attachment"):
                    idx[u] = (off, n, d.get("type"), d.get("parentUuid"), d.get("logicalParentUuid"),
                              bool(d.get("isSidechain")), d.get("subtype"), d.get("timestamp"))
                    order.append(u)
            off += n
    return idx, order, title


def read_entry(path, off, n):
    with open(path, "rb") as f:
        f.seek(off)
        return json.loads(f.read(n))


def main_chain(idx, order):
    """uuids of the main line, oldest first, following compaction boundaries to the segment before them."""
    leaf = next((u for u in reversed(order) if not idx[u][5]), None)
    chain, seen, u = [], set(), leaf
    while u and u in idx and u not in seen:
        seen.add(u)
        chain.append(u)
        _, _, typ, parent, lparent, _, sub, _ = idx[u]
        u = parent or (lparent if sub == "compact_boundary" else None)
    chain.reverse()
    return chain


def img_tokens(block):
    src = block.get("source") or {}
    data = src.get("data") or ""
    try:
        head = base64.b64decode(data[:65536] + "=" * (-len(data[:65536]) % 4))
    except Exception:
        return 1600
    w = h = 0
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", head[16:24])
    elif head[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(head):
            if head[i] != 0xFF:
                i += 1
                continue
            m = head[i + 1]
            if m in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", head[i + 5:i + 9])
                break
            i += 2 + struct.unpack(">H", head[i + 2:i + 4])[0]
    if not w:
        return 1600
    s = min(1.0, (1932 * 1932 / (w * h)) ** 0.5, 2576 / max(w, h))  # the API's own downscaling bound
    return int(w * s * h * s / 750) + 70


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text")
    return ""


def strip_tags(s):
    return re.sub(r"</?(local-command-stdout|local-command-stderr|command-message|command-name|command-args)>", "", s).strip()


def items_of(e, calls):
    """Render one transcript entry into items: (kind, ts, payload). kinds: user, other, reply, thinking, call,
    result, event, summary, boundary."""
    t, ts = e.get("type"), e.get("timestamp")
    out = []
    if t == "system":
        sub = e.get("subtype")
        if sub == "compact_boundary":
            out.append(("boundary", ts, f"compaction ({(e.get('compactMetadata') or {}).get('trigger', '?')}, "
                                        f"{(e.get('compactMetadata') or {}).get('preTokens', '?')} tokens before it)"))
        elif sub == "stop_hook_summary":
            extra = e.get("hookAdditionalContext") or e.get("stopReason")
            if extra:
                out.append(("event", ts, "stop hook: " + json.dumps(extra, ensure_ascii=False)[:3000]))
        else:
            c = strip_tags(str(e.get("content") or ""))
            if c:
                out.append(("event", ts, f"{sub}: {c[:3000]}"))
    elif t == "attachment":
        a = e.get("attachment") or {}
        k = a.get("type")
        if k in SKIP_ATTACH:
            return out
        if k == "queued_command":
            p = a.get("prompt")
            p = p if isinstance(p, str) else text_of(p)
            if (a.get("origin") or {}).get("kind") == "human" or a.get("commandMode") == "prompt":
                out.append(("user", ts, p))
            else:
                out.append(("event", ts, f"{a.get('commandMode', 'queued')}: {p[:4000]}"))
        elif k == "file":
            c = a.get("content")
            body = ""
            if isinstance(c, dict):
                body = ((c.get("file") or {}).get("content")) or json.dumps(c, ensure_ascii=False)
            out.append(("event", ts, f"file attached to the context after compaction: {a.get('filename')}\n{body}"))
        elif k == "compact_file_reference":
            out.append(("event", ts, f"file referenced after compaction: {a.get('filename')}"))
        elif k == "goal_status":
            out.append(("event", ts, f"goal check: met={a.get('met')}"))
        elif k == "instructions":
            out.append(("event", ts, "instruction files loaded: " + ", ".join(x.get("path", "?") for x in a.get("files") or [])))
        elif k == "invoked_skills":
            out.append(("event", ts, "skills loaded: " + ", ".join(x.get("name", "?") for x in a.get("skills") or [])))
        else:
            out.append(("event", ts, f"{k}: " + json.dumps(a, ensure_ascii=False)[:1500]))
    elif t == "user":
        c = (e.get("message") or {}).get("content")
        if e.get("isCompactSummary"):
            out.append(("summary", ts, text_of(c)))
            return out
        if isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
            for x in c:
                if x.get("type") != "tool_result":
                    continue
                cc = x.get("content")
                parts = [cc] if isinstance(cc, str) else [y for y in (cc or []) if isinstance(y, dict)]
                # a written act (bench/act.sh prints it between "ACT" and "END ACT") is kept verbatim, like a user message
                if "act.sh" in calls.get("_cmd", {}).get(x.get("tool_use_id"), ""):
                    rest = []
                    for y in parts:
                        t = y if isinstance(y, str) else (y.get("text", "") if y.get("type") == "text" else None)
                        if t is None:
                            rest.append(y)
                            continue
                        for m in re.finditer(r"^ACT [^\n]*\n(.*?)\nEND ACT$", t, re.S | re.M):
                            out.append(("act", ts, m.group(1).strip("\n")))
                        t = re.sub(r"^ACT [^\n]*\n.*?\nEND ACT$", "[the act, kept verbatim above]", t, flags=re.S | re.M)
                        rest.append(t)
                    parts = rest
                out.append(("result", ts, {"id": calls.get(x.get("tool_use_id"), "?"), "error": bool(x.get("is_error")), "parts": parts}))
            return out
        txt = text_of(c)
        imgs = [y for y in c if isinstance(y, dict) and y.get("type") == "image"] if isinstance(c, list) else []
        if e.get("isMeta"):
            if txt.strip() and not txt.startswith("<local-command-caveat>"):
                out.append(("event", ts, "harness note: " + txt[:3000]))
            return out
        s = txt.strip()
        if s.startswith("<command-name>"):
            name = re.search(r"<command-name>(.*?)</command-name>", s, re.S)
            args = re.search(r"<command-args>(.*?)</command-args>", s, re.S)
            out.append(("user", ts, ((name.group(1).strip() if name else "/?") + " " + (args.group(1).strip() if args else "")).strip()))
        elif s.startswith("<local-command-stdout>") or s.startswith("<local-command-stderr>"):
            out.append(("event", ts, "command output: " + strip_tags(s)[:3000]))
        elif s.startswith("[Request interrupted"):
            out.append(("event", ts, "the user interrupted the assistant"))
        elif e.get("turnOrigin") == "host_synthetic":
            out.append(("event", ts, "harness: " + s[:2000]))
        elif "<cross-session-message" in s:
            out.append(("other", ts, s))
        elif s.startswith("<task-notification>"):
            out.append(("event", ts, "task notification: " + s[:4000]))
        elif s or imgs:
            out.append(("user", ts, {"text": txt, "images": imgs} if imgs else txt))
    elif t == "assistant":
        for b in (e.get("message") or {}).get("content") or []:
            k = b.get("type")
            if k == "text" and b.get("text", "").strip():
                out.append(("reply", ts, b["text"]))
            elif k == "tool_use":
                calls["_n"] = calls.get("_n", 0) + 1
                cid = f"C{calls['_n']}"
                calls[b.get("id")] = cid
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                calls.setdefault("_cmd", {})[b.get("id")] = str(inp.get("command", ""))
                out.append(("call", ts, {"id": cid, "name": b.get("name"), "input": b.get("input")}))
    return out


def est(item):
    k, _, p = item
    if k == "result":
        n = 0
        for x in p["parts"]:
            if isinstance(x, str):
                n += len(x) / CHARS_PER_TOKEN
            elif x.get("type") == "image":
                n += img_tokens(x)
            else:
                n += len(x.get("text", "")) / CHARS_PER_TOKEN
        return int(n) + 10
    if k == "call":
        return int(len(json.dumps(p["input"], ensure_ascii=False)) / CHARS_PER_TOKEN) + 10
    if k == "user" and isinstance(p, dict):
        return int(len(p["text"]) / CHARS_PER_TOKEN) + sum(img_tokens(i) for i in p["images"])
    return int(len(p if isinstance(p, str) else json.dumps(p)) / CHARS_PER_TOKEN) + 5


# ---------------------------------------------------------------- summarizer input
def render_call(p):
    inp = p["input"] if isinstance(p["input"], dict) else {"input": p["input"]}
    name = p["name"]
    if name == "Bash":
        body = inp.get("command", "") + (f"\n# {inp['description']}" if inp.get("description") else "")
    elif name in ("Read", "Glob", "Grep"):
        body = json.dumps(inp, ensure_ascii=False)
    elif name == "Write":
        body = f"file_path: {inp.get('file_path')}\ncontent:\n{inp.get('content', '')}"
    elif name == "Edit":
        body = (f"file_path: {inp.get('file_path')}{' (replace_all)' if inp.get('replace_all') else ''}\n"
                f"old_string:\n{inp.get('old_string', '')}\nnew_string:\n{inp.get('new_string', '')}")
    else:
        body = json.dumps(inp, ensure_ascii=False, indent=1)
    return f"[{p['id']} call {name}]\n{body}"


def build_content(items, labels, context_blocks, part_info, seen=None, fresh=None):
    """Anthropic content blocks for the summarizer's single user message. An image or long tool result that is
    byte-identical to one already recorded (seen: hash -> record file) is replaced by a pointer to that record;
    fresh collects the hashes of what this part sends in full."""
    import hashlib
    seen = seen if seen is not None else {}
    fresh = fresh if fresh is not None else {}
    blocks = []

    def known(data):
        h = hashlib.sha1(data.encode("utf-8", "ignore")).hexdigest()
        if h in seen:
            return seen[h]
        if h in fresh:
            return "an earlier result in this same part"
        fresh[h] = None
        return None

    def text(s):
        if blocks and blocks[-1]["type"] == "text":
            blocks[-1]["text"] += s
        else:
            blocks.append({"type": "text", "text": s})

    text("CONTEXT (for continuity only; already recorded or summarized; do not record again)\n\n")
    for title, body in context_blocks:
        text(f"=== {title} ===\n{body}\n\n")
    text(f"RAW ({part_info})\n\n")
    for i, (k, ts, p) in enumerate(items):
        when = local(ts, "%Y-%m-%d %H:%M:%S")
        if k == "user":
            if isinstance(p, dict):
                text(f"[{labels[i]} USER {when}]\n{p['text']}\n")
                for im in p["images"]:
                    blocks.append({"type": "image", "source": im["source"]})
                text("\n")
            else:
                text(f"[{labels[i]} USER {when}]\n{p}\n\n")
        elif k == "other":
            text(f"[{labels[i]} MESSAGE FROM ANOTHER SESSION, not the user {when}]\n{p}\n\n")
        elif k == "reply":
            text(f"[{labels[i]} ASSISTANT TO USER {when}]\n{p}\n\n")
        elif k == "act":
            text(f"[{labels[i]} ACT WRITTEN BY THE ASSISTANT (a C, D, F or H) {when}]\n{p}\n\n")
        elif k == "thinking":
            text(f"[assistant thinking {when}]\n{p}\n\n")
        elif k == "call":
            text(render_call(p) + f"   ({when})\n\n")
        elif k == "result":
            text(f"[{p['id']} result{' ERROR' if p['error'] else ''}]\n")
            for x in p["parts"]:
                if isinstance(x, dict) and x.get("type") == "text":
                    x = x.get("text", "")
                if isinstance(x, str):
                    where = known(x) if len(x) > 3000 else None
                    text((f"[this result's text ({len(x)} chars) is byte-identical to one already recorded in {where}]"
                          if where else x) + "\n")
                elif x.get("type") == "image" and x.get("source"):
                    where = known((x["source"].get("data") or "")[:200000])
                    if where:
                        text(f"[image byte-identical to one already recorded in {where}: the same page, unchanged]\n")
                    else:
                        blocks.append({"type": "image", "source": x["source"]})
                        text("\n")
            text("\n")
        elif k == "event":
            text(f"[event {when}] {p}\n\n")
        elif k == "boundary":
            text(f"[event {when}] {p}\n\n")
    text("END OF RAW. Write the record now, following the format exactly, starting with '## Timeline'.")
    return blocks


def summarize(pj, blocks, tag):
    cfg = pj.cfg
    claude = cfg["claude"] or shutil.which("claude") or shutil.which("claude.exe") or "claude"
    work = os.path.join(tempfile.gettempdir(), "chronicle_work")  # throwaway: the system temp folder, not the project
    os.makedirs(work, exist_ok=True)
    msg = json.dumps({"type": "user", "message": {"role": "user", "content": blocks}}, ensure_ascii=False)
    env = dict(os.environ, CLAUDE_CODE_MAX_OUTPUT_TOKENS=str(cfg["max_output_tokens"]))
    args = [claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--model", cfg["model"], "--effort", cfg["effort"], "--setting-sources", "", "--tools", "",
            "--strict-mcp-config", "--system-prompt-file", os.path.join(HERE, "prompt.md"), "--no-session-persistence"]
    last_err = ""
    for attempt in range(3):
        t0 = time.time()
        try:
            r = subprocess.run(args, input=msg + "\n", capture_output=True, text=True, encoding="utf-8",
                               env=env, cwd=work, timeout=3600)
        except subprocess.TimeoutExpired:
            last_err = "timeout"
            continue
        res = None
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") == "result":
                    res = d
        if res and not res.get("is_error") and (res.get("result") or "").strip():
            pj.log(f"{tag}: summarized in {time.time() - t0:.0f}s, usage {json.dumps(res.get('usage', {}))[:300]}")
            return res["result"]
        last_err = (json.dumps(res)[:600] if res else (r.stderr or r.stdout)[-600:])
        pj.log(f"{tag}: attempt {attempt + 1} failed: {last_err}")
        time.sleep(20 * (attempt + 1))
    raise RuntimeError(f"summarizer failed: {last_err}")


# ---------------------------------------------------------------- record assembly
def quote(s):
    return "\n".join("> " + l if l.strip() else ">" for l in s.rstrip().split("\n"))


def assemble(raw, items, labels, header):
    """Insert verbatim user messages and replies at the model's placeholders; repair missing ones."""
    s = raw.strip()
    s = re.sub(r"^```(?:markdown|md)?\s*\n", "", s)
    s = re.sub(r"\n```\s*$", "", s)
    i = s.find("## Timeline")
    s = s[i:] if i >= 0 else "## Timeline\n" + s
    seq = [labels[i] for i in range(len(items)) if labels[i]]
    body = {labels[i]: (i, items[i]) for i in range(len(items)) if labels[i]}

    def verbatim(lab):
        i, (k, ts, p) = body[lab]
        when = local(ts)
        if k == "user":
            txt = p["text"] + f"\n[{len(p['images'])} image(s) attached]" if isinstance(p, dict) else p
            return f"**[{lab}] User · {when}**\n{quote(txt)}"
        if k == "other":
            return f"**[{lab}] Message from another session (not the user) · {when}**\n{quote(p)}"
        if k == "act":
            return f"**[{lab}] Act · {when}**\n{quote(p)}"
        return f"**[{lab}] Assistant to the user · {when}**\n{quote(p)}"

    lines = s.split("\n")
    placed, out = set(), []
    pat = re.compile(r"\{\{([TRA]\d+)\}\}")
    for ln in lines:
        m = pat.fullmatch(ln.strip())
        if m:
            lab = m.group(1)
            if lab in body and lab not in placed:
                # anything earlier in the sequence that the model skipped goes in first, in order
                for prev in seq[:seq.index(lab)]:
                    if prev not in placed:
                        out.append(verbatim(prev) + "\n\n*(placed by the program: the summarizer omitted it)*")
                        placed.add(prev)
                out.append(verbatim(lab))
                placed.add(lab)
            continue
        out.append(pat.sub(lambda mm: f"[{mm.group(1)}]", ln))
    missing = [lab for lab in seq if lab not in placed]
    if missing:
        j = next((n for n, ln in enumerate(out) if ln.startswith("## State at the end")), len(out))
        add = [verbatim(lab) + "\n\n*(placed by the program: the summarizer omitted it)*" for lab in missing]
        out[j:j] = add + [""]
    return header + "\n" + "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------- recording
def record(transcript, final=False, start_segments=1, project=None):
    root = find_root(project or os.getcwd())
    if not root:
        print("chronicle: no .claude/chronicle.json above the project directory", file=sys.stderr)
        return 1
    pj = Project(root)
    if not take_lock(pj, 3600 if final else 5):
        pj.log("record: another recorder holds the lock; exiting")
        return 0
    try:
        rc = _record(pj, transcript, final, start_segments)
        if final:
            finish_final(pj)
        return rc
    except Exception as ex:
        import traceback
        pj.log("record failed: " + "".join(traceback.format_exception(ex))[-3000:])
        return 1
    finally:
        drop_lock(pj)


def _record(pj, transcript, final, start_segments):
    st = pj.load()
    idx, order, title = index_file(transcript)
    chain = main_chain(idx, order)
    if not chain:
        return 0
    # Results of parallel tool calls hang off their call's entry as side branches; the model saw them, so they are
    # taken in (only tool results: a rewound branch's messages are not). Everything is ordered by file position.
    fpos = {u: i for i, u in enumerate(order)}
    on = set(chain)
    side = {u for u in order if u not in on and idx[u][2] == "user" and idx[u][3] in on and not idx[u][5]}
    last = st.get("last_uuid")
    if last in fpos and (last in on or last in side):
        lp = fpos[last]
    else:
        bounds = [i for i, u in enumerate(chain) if idx[u][6] == "compact_boundary"]
        if last:
            pj.log(f"record: last recorded entry {last} is not on the current line (rewind or new file); "
                   f"starting at the latest compaction")
            start_segments = 1
        start = bounds[-start_segments] if len(bounds) >= start_segments else 0
        lp = fpos[chain[start]] - 1
    todo = sorted((u for u in on | side if fpos[u] > lp), key=fpos.get)
    if not todo:
        return 0
    entries = []
    for u in todo:
        off, n = idx[u][0], idx[u][1]
        e = read_entry(transcript, off, n)
        if u in side:
            c = (e.get("message") or {}).get("content")
            if not (isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c)):
                continue
        entries.append(e)
    # split at compaction boundaries: each segment is recorded on its own
    segs, cur = [], []
    for e in entries:
        if e.get("type") == "system" and e.get("subtype") == "compact_boundary" and cur:
            segs.append(cur)
            cur = []
        cur.append(e)
    segs.append(cur)
    for si, seg in enumerate(segs):
        closed = si < len(segs) - 1  # a later compaction exists: this segment is complete
        if seg[0].get("type") == "system" and seg[0].get("subtype") == "compact_boundary":
            st["segment"] = seg[0].get("timestamp")
        elif not st.get("segment"):
            st["segment"] = seg[0].get("timestamp")
        calls = {}
        units = []  # (entry uuid, [items])
        for e in seg:
            units.append((e.get("uuid"), items_of(e, calls)))
        # cut into parts at entry boundaries; a call and its result stay together
        parts, cur, tok = [], [], 0
        for k, (u, its) in enumerate(units):
            cur.append((u, its))
            tok += sum(est(x) for x in its)
            nxt = units[k + 1][1] if k + 1 < len(units) else []
            ends_with_call = any(x[0] == "call" for x in its) and not any(x[0] == "result" for x in its)
            if tok >= pj.cfg["chunk_tokens"] and not ends_with_call and not (nxt and nxt[0][0] == "result"):
                parts.append(cur)
                cur, tok = [], 0
        if cur:
            if final or closed:
                parts.append(cur)
            elif sum(sum(est(x) for x in its) for _, its in cur) >= pj.cfg["chunk_tokens"]:
                parts.append(cur)
        for part in parts:
            if MAX_PARTS[0] <= 0:
                return 0
            MAX_PARTS[0] -= 1
            items = [x for _, its in part for x in its]
            if not any(k in ("user", "reply", "thinking", "call", "result", "other") for k, _, _ in items):
                st["last_uuid"] = part[-1][0]
                pj.save(st)
                continue
            write_part(pj, st, items, part, title)
            st["last_uuid"] = part[-1][0]
            pj.save(st)
            rebuild(pj)
    return 0


FORK_PROMPT = ("Write a summary of this conversation's work since the last compaction only; everything before that "
               "compaction is already recorded elsewhere. Cover: the user's requests in this stretch; what was done and "
               "found, with exact numbers, files and commands; the decisions made and why; the approaches considered and "
               "set aside. Do not use any tools. Output only the summary.")


def forksum_snapshot(pj, transcript):
    """A copy of the transcript under a new session id, beside it (taken before the compaction changes anything)."""
    import uuid
    cid = str(uuid.uuid4())
    shutil.copyfile(transcript, os.path.join(os.path.dirname(transcript), cid + ".jsonl"))
    return cid


def forksum(transcript, cid, project=None):
    """The chat's own model, in a fork of the copied session (its full context, from cache), writes a summary of the
    stretch since the last compaction; it is saved verbatim as a record and the copy and the fork are removed."""
    root = find_root(project or os.getcwd())
    if not root:
        return 1
    pj = Project(root)
    folder = os.path.dirname(transcript)
    made = [os.path.join(folder, cid + ".jsonl"), os.path.join(folder, cid)]
    try:
        model, end = None, None
        size = os.path.getsize(made[0])
        with open(made[0], "rb") as fh:
            fh.seek(max(0, size - 30_000_000))
            for raw in fh:
                if b'"type":"assistant"' in raw[:4000]:
                    try:
                        d = json.loads(raw)
                        model = d["message"].get("model") or model
                        end = d.get("timestamp") or end
                    except Exception:
                        pass
        claude = pj.cfg["claude"] or shutil.which("claude") or "claude"
        env = dict(os.environ, CHRONICLE_OFF="1")
        args = [claude, "-p", FORK_PROMPT, "--resume", cid, "--fork-session", "--permission-mode", "dontAsk",
                "--max-turns", "1", "--output-format", "json", "--settings", json.dumps({"disableAllHooks": True})]
        if model:
            args += ["--model", model]
        t0 = time.time()
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", env=env, cwd=root,
                           stdin=subprocess.DEVNULL, timeout=3600)
        res = {}
        for line in reversed(r.stdout.strip().splitlines()):
            try:
                res = json.loads(line)
                break
            except Exception:
                continue
        fork = res.get("session_id")
        if fork:
            made += [os.path.join(folder, fork + ".jsonl"), os.path.join(folder, fork)]
        text = (res.get("result") or "").strip()
        if not text and fork and os.path.exists(made[-2]):
            # the summary is the fork's first message after our prompt (a hook or the turn limit may end the run later)
            seen_prompt = False
            for line in open(made[-2], encoding="utf-8"):
                d = json.loads(line)
                if d.get("type") == "user" and d.get("message", {}).get("content") == FORK_PROMPT:
                    seen_prompt = True
                elif seen_prompt and d.get("type") == "assistant":
                    t = "".join(b.get("text", "") for b in d["message"]["content"] if b.get("type") == "text").strip()
                    if len(t) > 200:
                        text = t
                        break
        u = res.get("usage", {})
        pj.log(f"forksum: {model} in {time.time() - t0:.0f}s, cache read {u.get('cache_read_input_tokens')}, "
               f"written {u.get('cache_creation_input_tokens')}, out {u.get('output_tokens')}, summary {len(text)} chars")
        if not text:
            pj.log("forksum: no summary came back: " + (r.stderr or r.stdout)[-400:])
            return 1
        now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        name = "sessions/" + datetime.datetime.now().strftime("%Y-%m-%d_%H%M") + "_own-summary.md"
        body = (f"# The chat's own summary of its work before the compaction at {now_iso()[:16].replace('T', ' ')}\n"
                f"- Written by {model}, the chat's own model, in a background copy of the session with its whole context; "
                f"saved verbatim.\n\n{text}\n")
        if not take_lock(pj, 7200):
            pj.log("forksum: the lock stayed held; the summary was not saved")
            return 1
        try:
            os.makedirs(os.path.join(pj.rec_dir, "sessions"), exist_ok=True)
            with open(os.path.join(pj.rec_dir, name), "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            st = pj.load()
            st.setdefault("records", []).append({"file": name, "segment": "own-summary", "kind": "own-summary",
                                                 "start": now_utc, "end": now_utc, "tokens_in": 0, "chars": len(body),
                                                 "first_user": "the chat's own summary before the compaction"})
            pj.save(st)
            rebuild(pj)
        finally:
            drop_lock(pj)
        return 0
    finally:
        for p in made:
            try:
                shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
            except OSError:
                pass


def finish_final(pj):
    """The tail before a compaction is recorded: drop the 'not yet recorded' mark at once (now.md is rebuilt without it)
    and queue the one-time pointer to the new record(s) for a session that read now.md before they existed."""
    hs = pj.load_hook()
    aw = hs.setdefault("notes", {}).pop("await", None)
    hs.pop("pending", None)
    if aw:
        recs = pj.load().get("records", [])
        files = [r["file"] for r in recs if r["end"][:19] > (aw.get("after") or "")[:19]
                 and r["start"][:19] <= (aw.get("until") or "9")[:19]]
        if files:
            hs["notes"]["ready"] = {"files": files, "end": recs[-1]["end"], "told": []}
    pj.save_hook(hs)
    rebuild(pj)


def write_part(pj, st, items, part, title):
    # labels: T# for user/other messages, R# for messages to the user, A# for written acts (numbered within the record)
    labels, nt, nr, na = [], 0, 0, 0
    for k, _, _ in items:
        if k in ("user", "other"):
            nt += 1
            labels.append(f"T{nt}")
        elif k == "reply":
            nr += 1
            labels.append(f"R{nr}")
        elif k == "act":
            na += 1
            labels.append(f"A{na}")
        else:
            labels.append(None)
    summary = next((p for k, _, p in items if k == "summary"), None)
    ctx = []
    segment = st.get("segment")
    recs = st.get("records", [])
    prev_same = [r for r in recs if r.get("segment") == segment]
    if summary or not prev_same:
        if recs:
            ctx.append((f"the previous record ({recs[-1]['file']})", read_text(pj, recs[-1]["file"])))
        if summary:
            ctx.append(("the session's own compaction summary, at the start of this part (written by the working model)", summary))
    else:
        budget = 60000 * CHARS_PER_TOKEN
        chosen = []
        for r in reversed(prev_same):
            t = read_text(pj, r["file"])
            if sum(len(x[1]) for x in chosen) + len(t) > budget and chosen:
                break
            chosen.insert(0, (f"earlier record of this segment ({r['file']})", t))
        ctx.extend(chosen)
    raw_items = [x for x in items if x[0] != "summary"]
    raw_labels = [labels[i] for i in range(len(items)) if items[i][0] != "summary"]
    ts0 = next((ts for _, ts, _ in raw_items if ts), None) or now_iso()
    ts1 = next((ts for _, ts, _ in reversed(raw_items) if ts), None) or ts0
    tok = sum(est(x) for x in raw_items)
    n_img = sum(1 for k, _, p in raw_items if k == "result" for x in p["parts"] if isinstance(x, dict) and x.get("type") == "image")
    info = (f"{title or 'session'}: {local(ts0)} to {local(ts1)}, about {tok // 1000}k tokens, "
            f"{sum(1 for k, _, _ in raw_items if k in ('user', 'other'))} user message(s), "
            f"{sum(1 for k, _, _ in raw_items if k == 'reply')} message(s) to the user, "
            f"{sum(1 for k, _, _ in raw_items if k == 'call')} tool call(s), {n_img} image(s)")
    raw_items = drop_own_reads(raw_items)
    seen = st.setdefault("seen", {})
    fresh = {}
    blocks = build_content(raw_items, raw_labels, ctx, info, seen, fresh)
    name = "sessions/" + local(ts0, "%Y-%m-%d_%H%M") + ".md"
    k = 2
    while os.path.exists(os.path.join(pj.rec_dir, name)):
        name = "sessions/" + local(ts0, "%Y-%m-%d_%H%M") + f"-{k}.md"
        k += 1
    n_dedup = sum(1 for b in blocks if b["type"] == "text" for _ in re.finditer("byte-identical to one already recorded", b["text"]))
    info += f"; {n_dedup} unchanged page(s)/result(s) pointed to earlier records" if n_dedup else ""
    pj.log(f"recording {name}: {info}")
    if os.environ.get("CHRONICLE_DRY"):
        txt = "".join(b.get("text", "") if b["type"] == "text" else "\n[IMAGE]\n" for b in blocks)
        with open(os.path.join(pj.st_dir, "dry_" + os.path.basename(name).replace(".md", ".txt")), "w", encoding="utf-8") as f:
            f.write(txt)
        print(f"DRY {name}: {info}; context {len(ctx)} block(s); input text {len(txt) // 1000}k chars, "
              f"{sum(1 for b in blocks if b['type'] == 'image')} images")
        recs.append({"file": name, "segment": segment, "start": ts0, "end": ts1, "tokens_in": tok, "chars": 0, "first_user": ""})
        seen.update({h: name for h in fresh})
        return
    out = summarize(pj, blocks, name)
    header = (f"# Chronicle · {local(ts0)} → {local(ts1, '%H:%M') if local(ts0)[:10] == local(ts1)[:10] else local(ts1)}\n"
              f"- {info}.\n"
              f"- Entries {part[0][0]} … {part[-1][0]}. Written by {pj.cfg['model']} ({pj.cfg['effort']}) at {now_iso()}; "
              f"user messages and messages to the user are verbatim, inserted by the program.\n")
    text = assemble(out, raw_items, raw_labels, header)
    path = os.path.join(pj.rec_dir, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    first_user = next((p if isinstance(p, str) else p.get("text", "") for k, _, p in raw_items if k == "user"), "")
    recs.append({"file": name, "segment": segment, "start": ts0, "end": ts1, "tokens_in": tok,
                 "chars": len(text), "first_user": first_user.strip().replace("\n", " ")[:120]})
    st["records"] = recs
    seen.update({h: f"{pj.cfg['records_dir']}/{name}" for h in fresh})
    pj.save(st)


OWN = re.compile(r"(runs|docs)[\\/]+chronicle([\\/]|$)", re.I)


def drop_own_reads(items):
    """The session's reads of its own chronicle (the page images, or records rendered by textimg) are not sent again:
    their content is the earlier records. A one-line marker stays in their place."""
    # only Read calls on the chronicle's pages or records, and on page images textimg rendered from a chronicle record
    # (a Bash result is kept whole: a search or a status line that merely names the chronicle is real work)
    own, out = set(), []
    pages = set()
    for k, ts, p in items:
        if k == "call":
            inp = p["input"] if isinstance(p["input"], dict) else {}
            fp = str(inp.get("file_path", ""))
            if p["name"] == "Read" and fp and (OWN.search(fp) or os.path.normcase(fp) in pages):
                own.add(p["id"])
        elif k == "result":
            for x in p["parts"]:
                t = x if isinstance(x, str) else (x.get("text", "") if isinstance(x, dict) else "")
                head = re.match(r"textimg: (.+?) \|", t)
                if head and OWN.search(head.group(1)):
                    pages.update(os.path.normcase(m.group(1)) for m in re.finditer(r"^PAGE (\S+)", t, re.M))
            if p["id"] in own:
                p = {"id": p["id"], "error": p["error"], "parts": ["[the session read its own chronicle (its memory of "
                     "earlier parts) here; that content is the earlier records and is not sent again]"]}
        out.append((k, ts, p))
    return out


def read_text(pj, name):
    try:
        return open(os.path.join(pj.rec_dir, name), encoding="utf-8").read()
    except OSError:
        return ""


# ---------------------------------------------------------------- now.md
def condensed(text):
    """User messages and written acts (verbatim) and the carried-forward list of a record."""
    out = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        if re.match(r"\*\*\[(T\d+\] (User|Message from another session)|A\d+\] Act)", lines[i]):
            out.append(lines[i])
            i += 1
            while i < len(lines) and lines[i].startswith(">"):
                out.append(lines[i])
                i += 1
            out.append("")
            continue
        i += 1
    m = re.search(r"^## Carry forward\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    if m:
        out.append("Carried forward:")
        out.append(m.group(1).strip())
    if not out and text.startswith("# The chat's own summary"):
        return text.split("\n\n", 1)[-1].strip()   # short already: kept whole
    if not out and text.startswith("# Chronicle (from the lab log)"):
        # a record copied from the lab log has no [T#] blocks or carried list: its entry headings stand for it
        out = ["Entries (headings; the full text is in the record):"] + [l for l in text.split("\n") if l.startswith("## ")]
    return "\n".join(out).strip()


def rebuild(pj):
    st = pj.load()
    recs = sorted(st.get("records", []), key=lambda r: r["start"])
    cfg = pj.cfg
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=cfg["days"])).isoformat()
    recent = [r for r in recs if r["end"] >= cutoff[:19] and r.get("era") != "transcript"]
    old = [r for r in recs if r not in recent]
    texts = {r["file"]: read_text(pj, r["file"]) for r in recent}
    tok = lambda s: int(len(s) / CHARS_PER_TOKEN)
    full, used = [], 0
    for r in reversed(recent):
        t = tok(texts[r["file"]])
        if full and used + t > cfg["full_tokens"]:
            break
        full.insert(0, r)
        used += t
    cond = []
    for r in reversed([r for r in recent if r not in full]):
        c = condensed(texts[r["file"]])
        if used + tok(c) > cfg["budget_tokens"]:
            break
        cond.insert(0, (r, c))
        used += tok(c)
    indexed = [r for r in recs if r not in full and r["file"] not in {c[0]["file"] for c in cond}]
    rel = cfg["records_dir"].replace("\\", "/")
    pend = pj.load_hook().get("pending")
    out = ["# Session chronicle (generated by tools/chronicle; do not edit)", "",
           "These are records of this session's own transcript, written part by part by a separate model (Opus 5.5) "
           "from the complete raw transcript: every user message, reply, tool call and result. User messages and "
           "the assistant's messages to the user are verbatim. Oldest first; the last record ends where the most "
           "recent recorded part ended. They are the session's memory across compactions: read them as what this "
           "session itself did and was told. Any record's full text is in " + rel + "/ (index at the end).", ""]
    if cond:
        out.append("## Earlier parts: the user's messages verbatim and what was carried forward")
        for r, c in cond:
            out += ["", f"### {local(r['start'])} → {local(r['end'])} ({rel}/{r['file']})", "", c]
        out.append("")
    if full:
        out.append("## Most recent parts in full")
        for r in full:
            t = texts[r["file"]]
            t = re.sub(r"^# ", "### ", t, count=1)
            t = re.sub(r"^## ", "#### ", t, flags=re.M)
            out += ["", t.strip()]
        out.append("")
    if pend:
        out += [f"## Not yet recorded", "", f"The part from {local(pend.get('from', ''))} to the compaction is still being "
                f"recorded; until then the compaction summary covers it.", ""]
    out.append("## Index of all records")
    for r in recs:
        out.append(f"- {local(r['start'])} → {local(r['end'], '%H:%M')} · {rel}/{r['file']} · {r.get('first_user') or '(continuing work)'}")
    text = "\n".join(out) + "\n"
    tmp = os.path.join(pj.rec_dir, "now.md.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    now = os.path.join(pj.rec_dir, "now.md")
    os.replace(tmp, now)
    return len(text)


def log_entries(text):
    """Split a lab log into (heading datetime local, raw text) entries, byte for byte. The text before the first
    heading goes with the first entry; a heading without a clock time takes its date's part of day or the time of the
    entry before it."""
    lines = text.splitlines(keepends=True)
    starts = [i for i, l in enumerate(lines) if l.startswith("## ")]
    out, prev = [], None
    for k, i in enumerate(starts):
        a = 0 if k == 0 else i
        b = starts[k + 1] if k + 1 < len(starts) else len(lines)
        m = LOG_HEAD.match(lines[i])
        when = prev
        if m:
            d = datetime.datetime.strptime(m.group(1), "%Y-%m-%d")
            if m.group(2):
                when = d.replace(hour=int(m.group(2)), minute=int(m.group(3)))
            else:
                word = lines[i][len(m.group(0)):].lstrip(", ").split(":")[0].strip()
                hm = LOG_PART.get(word)
                when = d.replace(hour=hm[0], minute=hm[1]) if hm else (prev if prev and prev.date() == d.date() else d)
                if prev and when < prev:
                    when = prev
        out.append((when, "".join(lines[a:b]), lines[i].rstrip("\n")))
        prev = when
    return out


def import_log(root, log_path, dry=False):
    """Turn the lab log into chronicle records: entries before the first transcript record are copied verbatim, one
    record per stretch between wakings (A entries); entries in the transcript era are kept only if no transcript record
    mentions their stamp. Nothing is rewritten."""
    pj = Project(root)
    if not dry and not take_lock(pj, 7200):
        print("chronicle: the recorder holds the lock; try again later")
        return 1
    try:
        st = pj.load()
        tr = sorted([r for r in st.get("records", []) if r.get("kind") != "log"], key=lambda r: r["start"])
        cut = (datetime.datetime.fromisoformat(tr[0]["start"].replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
               if tr else datetime.datetime.max)
        text = open(log_path, encoding="utf-8").read()
        ents = log_entries(text)
        assert "".join(e[1] for e in ents) == text, "the log's entries do not join back to the file"
        before = [e for e in ents if e[0] is None or e[0] < cut]
        after = [e for e in ents if e[0] is not None and e[0] >= cut]
        # group the entries into stretches that each begin at a waking or a new day. The transcript era's entries are
        # copied too, as index-only records: the transcript records stay what loads, and these keep the log's own text
        # and stamps, which pointers and act stamps resolve against.
        def group(es):
            gs, cur = [], []
            for e in es:
                if cur and (LOG_WAKE.match(e[2]) or (e[0] and cur[0][0] and e[0].date() != cur[-1][0].date())):
                    gs.append(cur)
                    cur = []
                cur.append(e)
            if cur:
                gs.append(cur)
            return gs
        groups, late = group(before), group(after)
        new = []
        utc = lambda dt: dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        note = ("- The lab log's own entries for this stretch, copied verbatim from docs/log.md (written at the time by the "
                "working model; not made from the transcript). A pointer \"log <date>, <time>\" resolves to the heading "
                "\"## <date>, <time>\" below.\n\n")
        for g in groups + late:
            t0, t1 = g[0][0], g[-1][0]
            era = "transcript" if any(g is x for x in late) else "log"
            name = "sessions/" + t0.strftime("%Y-%m-%d_%H%M") + ".md"
            head = (f"# Chronicle (from the lab log) · {t0:%Y-%m-%d %H:%M} → {t1:%Y-%m-%d %H:%M}\n"
                    + (note if era == "log" else note.replace("for this stretch", "for this stretch, a time the transcript "
                       "records also cover (index only: those records are what loads)")))
            body = "".join(e[1] for e in g)
            new.append(({"file": name, "segment": "log", "kind": "log", "era": era, "start": utc(t0), "end": utc(t1),
                         "tokens_in": 0, "chars": len(head) + len(body), "first_user": g[0][2][3:][:120]}, head + body))
        print(f"log: {len(ents)} entries; {len(before)} before the first transcript record ({cut:%Y-%m-%d %H:%M}) in "
              f"{len(groups)} records; {len(after)} in the transcript era in {len(late)} index-only records")
        for r, _ in new[:3] + new[-3:]:
            print(f"  {r['file']}  {r['chars'] // 1000} KB  {r['first_user'][:70]}")
        if dry:
            return 0
        for r, body in new:
            os.makedirs(os.path.join(pj.rec_dir, "sessions"), exist_ok=True)
            with open(os.path.join(pj.rec_dir, r["file"]), "w", encoding="utf-8", newline="") as f:
                f.write(body)
        st = pj.load()
        st["records"] = sorted([r for r in st.get("records", []) if r.get("kind") != "log"] + [r for r, _ in new],
                               key=lambda r: r["start"])
        pj.save(st)
        # verify: the copied bodies join back to the log, byte for byte
        got = "".join(open(os.path.join(pj.rec_dir, r["file"]), encoding="utf-8", newline="").read().split("\n\n", 1)[1]
                      for r, _ in new)
        want = text
        print("verbatim check:", "OK" if got == want else "MISMATCH")
        rebuild(pj)
        return 0 if got == want else 1
    finally:
        if not dry:
            drop_lock(pj)


# ---------------------------------------------------------------- hooks
def tracked(pj, st, transcript):
    """Whether this session is one the project records (title match and size)."""
    f = st.setdefault("files", {}).setdefault(transcript, {})
    if "title" not in f:
        title = None
        try:
            size = os.path.getsize(transcript)
            with open(transcript, "rb") as fh:
                fh.seek(max(0, size - 8_000_000))
                for raw in fh:
                    if b'"custom-title"' in raw[:200]:
                        try:
                            title = json.loads(raw).get("customTitle")
                        except Exception:
                            pass
        except OSError:
            pass
        f["title"] = title
    want = (pj.cfg.get("title_match") or "").lower()
    if want:
        return want in (f.get("title") or "").lower()
    return f.get("ctx", 0) >= pj.cfg["min_context"] or bool(st.get("has_records"))


def scan_growth(st, transcript):
    """Tokens of new transcript since the last scan (cheap, incremental), and the latest context size."""
    f = st["files"][transcript]
    try:
        size = os.path.getsize(transcript)
    except OSError:
        return 0, 0
    off = f.get("scan")
    if off is None or off > size:
        f["scan"] = size
        return 0, f.get("ctx", 0)
    add, ctx = 0, f.get("ctx", 0)
    with open(transcript, "rb") as fh:
        fh.seek(off)
        data = fh.read(size - off)
    end = data.rfind(b"\n") + 1
    for raw in data[:end].split(b"\n"):
        head = raw[:300]
        if not raw or b'"api-request' in head or b'"isSidechain":true' in head:
            continue
        if b'"type":"assistant"' in raw[:4000] or b'"type":"user"' in raw[:4000] or b'"type":"attachment"' in raw[:4000]:
            try:
                d = json.loads(raw)
            except Exception:
                continue
            calls = {}
            for it in items_of(d, calls):
                add += est(it)
            u = (d.get("message") or {}).get("usage") if d.get("type") == "assistant" else None
            if u:
                ctx = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
    f["scan"] = off + end
    f["ctx"] = ctx
    return add, ctx


def hook():
    if os.environ.get("CHRONICLE_OFF"):  # a background copy of the session (the summary fork) is never recorded itself
        return 0
    try:
        inp = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    except Exception:
        return 0
    root = find_root(inp.get("cwd") or os.getcwd())
    if not root:
        return 0
    pj = Project(root)
    ev = inp.get("hook_event_name", "")
    transcript = inp.get("transcript_path") or ""
    sid = inp.get("session_id", "")
    if not transcript or not os.path.isfile(transcript):
        return 0
    st = pj.load()  # recorder state: read only here
    hs = pj.load_hook()
    hs.setdefault("files", {}).setdefault(transcript, {})
    hs.setdefault("notes", {})
    hs["has_records"] = bool(st.get("records"))
    add = 0
    if ev in ("PostToolUse", "UserPromptSubmit", "Stop", "SubagentStop"):
        add, _ = scan_growth(hs, transcript)
    if not tracked(pj, hs, transcript):
        pj.save_hook(hs)
        return 0
    f = hs["files"][transcript]
    out = None
    if ev in ("PostToolUse", "UserPromptSubmit", "Stop", "SubagentStop"):
        f["pending"] = f.get("pending", 0) + add
        if f["pending"] >= pj.cfg["chunk_tokens"] and not os.path.exists(pj.lock_path):
            pid = spawn(["record", transcript, "--project", pj.root], pj)
            pj.log(f"hook {ev}: {f['pending']} new tokens; started recorder pid {pid}")
            f["pending"] = 0
        # a record finished after the last compaction's context was built: point the session to it, once
        note = hs["notes"].get("ready")
        if note and ev in ("PostToolUse", "UserPromptSubmit") and sid not in note.get("told", []):
            note.setdefault("told", []).append(sid)
            files = note.get("files") or []
            out = {"hookSpecificOutput": {"hookEventName": ev, "additionalContext":
                   "Chronicle: the record(s) of the work just before the last compaction were finished after it: "
                   + " ; ".join(f"{pj.cfg['records_dir']}/{x}" for x in files)
                   + ". If the now.md you read after the compaction already held them (its index lists them), nothing is "
                   "owed; otherwise read them in full now (textimg), before continuing: they are your memory of that work."}}
    elif ev == "PreCompact":
        before = len(st.get("records", []))
        hs["pending"] = {"from": (st["records"][-1]["end"] if st.get("records") else ""), "since": now_iso()}
        f["pending"] = 0
        f["scan"] = os.path.getsize(transcript)
        hs["notes"].pop("ready", None)
        # another recording in progress (a part, or a backfill): never hold the compaction for it
        busy = False
        try:
            busy = pid_alive(json.load(open(pj.lock_path, encoding="utf-8")).get("pid"))
        except Exception:
            pass
        # the chat's own summary of this stretch: a copy of the transcript as it stands now, summarized in the background
        # by the chat's own model in a fork of the session (its whole context, thinking included, read from cache)
        try:
            cid = forksum_snapshot(pj, transcript)
            spawn(["forksum", transcript, cid, "--project", pj.root], pj)
        except Exception as ex:
            pj.log(f"PreCompact: the fork summary could not start: {ex}")
        pid = spawn(["record", transcript, "--final", "--project", pj.root], pj)
        # the pointer is queued before any wait, so a hook cut short still leaves it
        hs["notes"]["await"] = {"pid": pid, "after": (st["records"][-1]["end"] if st.get("records") else ""),
                                "until": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
        pj.save_hook(hs)
        wait = 0 if busy else pj.cfg["precompact_wait"]
        pj.log(f"PreCompact: started the final recorder (pid {pid}); waiting up to {wait}s"
               + (" (another recording holds the lock)" if busy else ""))
        t_end = time.time() + wait
        while time.time() < t_end and pid_alive(pid):
            time.sleep(2)
        hs = pj.load_hook()
        if not pid_alive(pid):
            hs.get("notes", {}).pop("await", None)
            hs.pop("pending", None)
        else:
            pj.log("PreCompact: the tail is still being recorded; compaction proceeds, the session is told when it is ready")
        pj.save_hook(hs)
        rebuild(pj)
        return 0
    elif ev == "SessionStart":
        recs = st.get("records", [])
        if recs and pj.cfg.get("inject") == "images":
            now = os.path.join(pj.rec_dir, "now.md").replace("\\", "/")
            msg = (f"CHRONICLE, your memory of this session before this point: records written by a separate model from "
                   f"the complete transcript (user messages verbatim), up to {local(recs[-1]['end'])}; fuller than the "
                   f"compaction summary. Before anything else, read ALL of {pj.cfg['records_dir']}/now.md with textimg: "
                   f"run `{pj.cfg['textimg'].replace(chr(92), '/')} {now} --max-pages 60` and open every PAGE it prints "
                   f"with the Read tool, in one parallel call. For an exact quote or number, Grep {pj.cfg['records_dir']}/ "
                   f"(the same records as text).")
            if hs.get("pending"):
                msg += (f" The part after {local(hs['pending'].get('from', ''))} is still being recorded; you will be told "
                        f"when it is ready.")
            out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": msg}}
        elif recs:
            msg = (f"Chronicle: {pj.cfg['records_dir']}/now.md, loaded through CLAUDE.md, holds this session's own records "
                   f"(written from the complete transcript) up to {local(recs[-1]['end'])}: your memory of the work before "
                   f"this point, fuller than the compaction summary.")
            if hs.get("pending"):
                msg += (f" The part after {local(hs['pending'].get('from', ''))} is still being recorded; you will be told "
                        f"when it is ready.")
            out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": msg}}
    # an awaited tail record has finished: queue the one-time pointer and drop the pending mark
    aw = hs["notes"].get("await")
    if aw and not pid_alive(aw.get("pid")):
        hs["notes"].pop("await")
        hs.pop("pending", None)
        recs = pj.load().get("records", [])
        files = [r["file"] for r in recs if r["end"][:19] > (aw.get("after") or "")[:19] and r["start"][:19] <= (aw.get("until") or "9")[:19]]
        if files:
            hs["notes"]["ready"] = {"files": files, "told": []}
        pj.save_hook(hs)
        rebuild(pj)
    else:
        pj.save_hook(hs)
    if out:
        sys.stdout.write(json.dumps(out))
    return 0


# ---------------------------------------------------------------- cli
def status(root):
    pj = Project(root)
    st = pj.load()
    print(f"project {root}; records {len(st.get('records', []))}; last entry {st.get('last_uuid')}")
    for r in st.get("records", [])[-10:]:
        print(f"  {r['file']}  {local(r['start'])} -> {local(r['end'])}  in ~{r['tokens_in'] // 1000}k  out {r['chars'] // 1000}k chars")
    for p, f in pj.load_hook().get("files", {}).items():
        print(f"  file {os.path.basename(p)}: title={f.get('title')!r} pending={f.get('pending', 0)} ctx={f.get('ctx')}")
    if os.path.exists(pj.lock_path):
        print("  recorder running:", open(pj.lock_path).read())
    now = os.path.join(pj.rec_dir, "now.md")
    if os.path.exists(now):
        print(f"  now.md: {os.path.getsize(now) // 1000} KB, ~{int(os.path.getsize(now) / CHARS_PER_TOKEN) // 1000}k tokens")


def main():
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if a[0] == "hook":
        return hook()
    if a[0] == "record":
        final = "--final" in a
        n = int(a[a.index("--start-segments") + 1]) if "--start-segments" in a else 1
        proj = a[a.index("--project") + 1] if "--project" in a else None
        if "--max-parts" in a:
            MAX_PARTS[0] = int(a[a.index("--max-parts") + 1])
        return record(a[1], final, n, proj)
    if a[0] == "forksum":
        proj = a[a.index("--project") + 1] if "--project" in a else None
        return forksum(a[1], a[2], proj)
    if a[0] == "import-log":
        return import_log(find_root(a[1]) or a[1], a[2], "--dry" in a)
    if a[0] == "rebuild":
        pj = Project(find_root(a[1]) or a[1])
        print(f"now.md: {rebuild(pj)} chars")
        return 0
    if a[0] == "status":
        status(find_root(a[1]) or a[1])
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
