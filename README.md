# claude-code-tools

Tools that make long Claude Code sessions cheaper to run and keep their memory across compactions.

| Tool | What it does |
|---|---|
| [textimg/](textimg/README.md) | Reads text files as compact page images, for about half the tokens of plain Read. It can also edit files by exact text without reading them first, and run as an MCP server. A single Rust binary with no dependencies. |
| [chronicle/](chronicle/README.md) | Gives one long session full-fidelity memory across compactions. A separate model records each part from the raw transcript, with user messages verbatim. At each compaction a background fork writes the session's own summary, and after it the session reads everything back through textimg. |

Each folder's README covers building, setup and how it works.
