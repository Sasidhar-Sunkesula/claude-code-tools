# textimg

**Read text files as compact page images for about half the tokens, and edit them without reading them first.**

Vision models charge for an image by its area (roughly `width × height / 750` tokens for Claude), not by how much text
is in it. A 1932×1932 page of small, dense text holds far more characters than the same number of text tokens.
textimg renders any text file into such pages so an agent (Claude Code, or anything with an image-reading tool)
can read large docs, logs and code at a fraction of the cost, while keeping exact line numbers.

| File | Plain Read (numbered lines) | textimg | Saved |
|---|---|---|---|
| 570-line Rust source | 15.0k tokens | 5.7k | 62% |
| 113-line Python script | 3.6k tokens | 1.3k | 64% |
| 35 KB Markdown report | ~12.5k tokens | 6.2k | ~50% |

It is a single dependency-free Rust binary (its own glyph atlas and PNG encoder): about 35 ms a page; the first 8 pages
of a 23 MB, 300k-line log in 0.33 s. No cache, no server: every call renders fresh.

## Usage

```
textimg [read] FILE [-o OFFSET] [-n LIMIT] [--layout auto|packed|lines] [--max-pages N]
textimg edit FILE [--all | --append | --create | --overwrite] [--patch PATCHFILE]   (text on stdin)
```

It prints a header and one `PAGE` path per image; open them all with your image-reading tool (in Claude Code: the Read
tool, all pages in one parallel call).

```
textimg: C:\work\report.md | lines 1-122 of 122 | packed layout | 2 page(s) | ~6188 tokens (plain text ~12518) | 45 ms
Read every PAGE below with the Read tool (one parallel call). ¶N starts line N; ...
PAGE C:\Users\me\AppData\Local\Temp\textimg\1794647317736_16096\p01.png  (lines 1-102)
PAGE C:\Users\me\AppData\Local\Temp\textimg\1794647317736_16096\p02.png  (lines 102-122)
```

- `-o` / `-n`: 1-based first line and line count, like Claude Code's Read offset and limit.
- `--max-pages` (default 8, about 40k tokens): when output stops early, `MORE: run again with -o N` gives the next offset.
- `--layout lines`: one source line per fixed-width row as `N│` (keeps every layout, costs more).

### What a page looks like

- Every source line starts with `¶N`; a row that continues a long line starts with the label `¦N`.
- Prose flows edge to edge in Verdana 14px; empty lines get no marker (a skipped number is an empty line).
- Markdown code blocks, tables, indented code, and box-drawn or column-aligned lines in any file get their own rows in a
  fixed-width face (Consolas 14px), so columns stay aligned.
- `·` marks one space (leading spaces and runs of two or more); tabs expand to 4 columns; ANSI color codes and invisible
  characters (zero-width spaces, soft hyphens, BOMs) are dropped.

### Behaviour

It follows the contract of Claude Code's Read tool:

| Input | Result |
|---|---|
| Missing path / directory | `File does not exist: …` / `… is a directory` (exit 1) |
| Images, PDF, notebooks, Office files, archives, executables | refused, pointing to the normal Read tool |
| Binary content (NUL bytes in the first 8 KB) | refused: `looks binary` |
| Empty file, or a range holding only empty lines | says so, no pages |
| Offset past the end | `has only N lines; offset X is past the end` |
| UTF-8 (with or without BOM), UTF-16 LE/BE with BOM, anything else as Latin-1 | decoded; CRLF handled |
| One line longer than all the pages (minified JSON) | shows its start and says so |
| Characters no bundled font has (Arabic, Hebrew, Indic, Thai) | drawn as `?`; a NOTE lists the lines to read as text |
| Markdown image links (`![](…)`, `<img src>`) | not drawn; the local files are listed (marked missing if absent), remote ones counted |

The header shows the image cost next to a plain-text estimate, and a `NOTE:` says when plain text is as cheap (short
files, code-heavy Markdown). Pages go to `%TEMP%\textimg\<stamp>\`; folders older than two hours are removed.

## Editing without reading: `textimg edit`

Claude Code's Edit and Write tools refuse to change a file that was not first read as text, which brings back the
token cost. `textimg edit` is an exact find-and-replace run from the shell, so an agent takes the exact lines from
`grep -n` or `sed -n 'A,Bp'` and changes them without reading the whole file:

```
textimg edit FILE <<'EOF'
@@@OLD@@@
exact old text
@@@NEW@@@
new text (leave empty to delete the old lines)
@@@END@@@
EOF
```

- Several blocks per call. All of them are checked before anything is written, so one failure writes nothing.
- Each OLD must occur exactly once (`--all` replaces every occurrence).
- Every OLD is matched against the file as it was before the call, and blocks whose regions overlap are refused, so one
  block can never match or undo what another block of the same call wrote. Merge overlapping edits into one block.
- On a miss it names the closest line.
- On success it prints the changed lines with their numbers.
- It keeps CRLF and LF line endings and a UTF-8 BOM, and replaces the file in one step, so a failed write never leaves it
  half-written.
- `--append`, `--create` and `--overwrite`: the whole input is the text, with no markers.
- A line that is exactly a marker cannot appear inside a block (write such a file with `--overwrite`).
- **Backslashes:** Claude Code's Bash tool turns `\\` into `\` before the shell sees a command. So inline text that holds
  a backslash is refused: write the same input to a new file with the Write tool (which keeps backslashes) and pass
  `--patch FILE`. The patch file is deleted after it is read.

## Accuracy

Opus 5.5 copied 30 hard lines back character for character (plain text scores 30/30):

| Test | Exact lines |
|---|---|
| Prose report, 2 pages | 27, 28 |
| Markdown with a table, Rust code block, indented code and a box diagram | 30, 30 |

Use it to read for understanding. For text you will edit, quote or copy numbers from, use plain text (image reading
slips about one character per 15 long lines).

## Build

Needs Rust and Python 3 with Pillow. The glyph atlas is rasterized from fonts installed on your machine (it is not
distributed here):

```
pip install pillow
python build_atlas.py              # writes src/atlas.bin (~7.8 MB) from C:\Windows\Fonts
cargo build --release              # target/release/textimg(.exe), atlas embedded
```

Fonts: Verdana and Consolas, with fallbacks Segoe UI, Segoe UI Symbol, Arial, Cambria, Lucida Console, Tahoma,
Microsoft YaHei (CJK), Malgun Gothic (Hangul) and Segoe UI Emoji, each character taken from the first font whose cmap
has it (about 39k characters per face). On another OS, copy those font files into a folder and set `TEXTIMG_FONTS` to
it; missing fallbacks are skipped (their characters render as `?`).

## How it works

- `build_atlas.py` renders each character once into a coverage bitmap, aligned to its face's baseline, and stores them
  deduplicated with their advances (`TXA3` format, described in the script).
- `src/main.rs` reads the file, lays out rows (greedy word wrap by pixel width; `¶N` markers glued to their first word),
  composites glyphs into an 8-bit grayscale page, and writes PNGs with its own zlib encoder (fixed-Huffman deflate with
  run and row-repeat matches, ~1.2 MB a full page).

## Using it from Claude Code

Add something like this to your `CLAUDE.md`:

```
Read large text files with textimg: run `textimg FILE [-o N] [-n N]`, then open every PAGE it prints with the
Read tool in one parallel call; on MORE, rerun with -o N. Use plain Read for small files, exact quotes, numbers you
will copy, and the text you are about to edit.
```
