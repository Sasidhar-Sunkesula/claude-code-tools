# chronicle

**Memory across compactions for one long Claude Code session, written by a separate model from the complete
transcript.**

Claude Code's own compaction re-summarizes everything, including its previous summary, into a single ~25k-character
summary written by the working model. Detail fades with each compaction, and the session starts working in loops.
chronicle records the session part by part, at full fidelity, as it goes. After a compaction the session wakes with
those records, not just the summary.

## How it works

1. **While the session works.** A `PostToolUse` / `UserPromptSubmit` / `Stop` hook counts new transcript tokens. The
   count reads only the bytes added since the last check, so it takes milliseconds. Every `chunk_tokens` (default 120k),
   the hook starts a detached recorder.
2. **The recorder** walks the session's main line, following compaction boundaries, from the last recorded entry. It
   renders the new part in order and sends it in one call to `claude -p --model claude-opus-5-5 --effort xhigh`:
   - every user message, message to the user, tool call with its full input, and tool result with full text and every image;
   - harness events.
   That call has no settings, tools, CLAUDE.md or hooks, so it cannot recurse. A byte-identical image or long result
   already recorded is sent as a pointer to the record that holds it; on a waking re-read of unchanged docs, that removes
   most of the pages. Continuity context goes in as well: the previous records of the same segment, or the previous
   record plus the compaction summary at a segment's start.
3. **The record** (`docs/chronicle/<date>_<time>.md`) has three sections:
   - **Timeline**: every event in strict order.
   - **State at the end**: the task, the next step, running jobs, uncommitted files, open questions, promises.
   - **Carry forward**: instructions in force, decisions with reasons, numbers, dead ends.
   User messages and the assistant's messages to the user are inserted **verbatim by the program** at the model's
   `{{T#}}` / `{{R#}}` placeholders, so they can't be paraphrased; any the model skips are inserted anyway.
4. **Before a compaction**, the `PreCompact` hook starts recording the remaining tail and waits up to `precompact_wait`
   (default 90 s). If another recording is already running, it doesn't wait at all, so a compaction is never held
   for long. If the tail isn't done in time, it finishes in the background, and the next tool call tells the session
   to read it.
5. **`now.md`** is rebuilt after every record, within `budget_tokens`:
   - the most recent records in full (up to `full_tokens`);
   - earlier records of the last `days` as their user messages (verbatim) plus their carried-forward lists;
   - an index of every record.
   It reaches the session in one of two ways:
   - `inject: "import"`: the project's CLAUDE.md has `@docs/chronicle/now.md`. It is re-read on every compaction and
     start, so it can't be skipped.
   - `inject: "images"`: `now.md` is rendered by textimg into `docs/chronicle/pages/`, at about half the tokens. The
     `SessionStart` hook names the pages and tells the session to read them all first. Exact text can still be grepped
     from the records.

## Written acts, verbatim

A project can have its session write short acts: a decision, a prediction before a run, a result after it. In mind
these are written through `bench/act.sh`, which prints the act whole between `ACT` and `END ACT`. The recorder takes
that printout out of the tool result and labels it `[A#]`. The summarizer places `{{A#}}` where the act happened, and
the program inserts the text verbatim, exactly as it does user messages. So acts need no files of their own. In
`now.md`, an older record's condensed form keeps its acts along with the user's messages.

## The chat's own summary at each compaction

The summarizer can't see the working model's thinking. So at `PreCompact` the hook also copies the transcript as it
stands and starts `chronicle.py forksum`. That job runs a `claude -p --resume <copy> --fork-session` with the chat's
own model, one turn, no tools and all hooks off (`disableAllHooks`, `CHRONICLE_OFF`), in the project folder. Because the
prefix is identical, the fork reads the session's whole context from cache, thinking included. It is asked only for a
summary of the stretch since the last compaction: requests, what was done and found, decisions and why, approaches set
aside. The answer is saved verbatim as `sessions/<date>_<time>_own-summary.md`, and the copy and the fork are deleted.
In a test on a 605k-token context, it read 605,651 tokens from cache and wrote a 13k-character summary.

## Scope of each record

Records are read one after another, so each one covers only its own part. The prompt forbids restating CONTEXT
(earlier records, the compaction summary) in any section; Carry forward lists only what that part added or changed.
The session's reads of its own chronicle (its page images, or records rendered through textimg) are replaced in the
input by a one-line marker, so memory is never re-summarized into memory.

## An old lab log

`chronicle.py import-log PROJECT LOG.md` turns a hand-kept log into records without any model:
- entries before the first transcript record are copied verbatim, one record per stretch between wakings (A entries)
  and per day;
- the bodies are checked to join back to the log byte for byte;
- entries in the transcript era are copied too, as index-only records: the transcript records stay what loads, and
  these keep the log's own text and stamps for pointers.

The mind project's docs/log.md (1,811 entries) became 74 such records, plus 4 index-only ones, this way.

## What is not passed

- **The model's thinking.** Opus 5.5's safeguards refuse requests to extract a model's reasoning, so the assistant's
  stored thinking blocks are left out. Decisions and their reasons are recorded from what the assistant said, ran and
  wrote.
- **Harness noise.** Tool lists, skill listings, output-style reminders and prompt snapshots are dropped.

## Setup for a project

- `PROJECT/.claude/chronicle.json`, for example `{"title_match": "first principles", "inject": "images"}`. Its keys are
  those in `DEFAULTS` in `chronicle.py`.
- Hooks in `PROJECT/.claude/settings.local.json`:

```json
"hooks": {
  "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "python /path/to/textimg/chronicle/chronicle.py hook", "timeout": 30}]}],
  "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python /path/to/textimg/chronicle/chronicle.py hook", "timeout": 30}]}],
  "Stop": [{"hooks": [{"type": "command", "command": "python /path/to/textimg/chronicle/chronicle.py hook", "timeout": 30}]}],
  "PreCompact": [{"hooks": [{"type": "command", "command": "python /path/to/textimg/chronicle/chronicle.py hook", "timeout": 1600}]}],
  "SessionStart": [{"hooks": [{"type": "command", "command": "python /path/to/textimg/chronicle/chronicle.py hook", "timeout": 60}]}]
}
```

- **Backfill**, run from the project: `python chronicle.py record TRANSCRIPT --final --start-segments N --project .`
  records the last N segments in order.
- **Status**: `python chronicle.py status PROJECT`.
- **Logs**: `docs/chronicle/log.txt` and `job.log`. State: `state.json` (recorder) and `hook.json` (hooks), beside the records and kept out of git.

## Limits found in testing

- **Hook-injected text** above about 10,000 characters is replaced by a 2 KB preview and a file path. That's why
  records go through a CLAUDE.md import (tested complete at 382 KB) or page images.
- **Hooks** run inside Claude Code's job object. A detached child cannot break away, but it does outlive the hook.
