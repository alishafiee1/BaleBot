# Searching the code with Graphify

[Graphify](https://graphify.com) (PyPI package **`graphifyy`**, with two y) turns the source into a knowledge graph you can query
instead of grepping. For code it uses tree-sitter locally: **no LLM, nothing leaves your machine.**

## One-time install
```sh
uv tool install graphifyy      # the command is `graphify`
```
On Windows make sure `~/.local/bin` is on `PATH`; if the output shows garbled characters set `PYTHONIOENCODING=utf-8`.

## Build and update the graph
```sh
graphify update .              # re-extracts code, no LLM needed; writes graphify-out/
```
`graphify-out/` (graph.json, graph.html, GRAPH_REPORT.md) is **generated and git-ignored**. Rebuild it after pulling.
`.graphifyignore` (committed) keeps `.venv`, caches and databases out of it.
Current size: ~550 nodes, ~1500 edges, 22 communities.

## Ask questions
```sh
graphify explain "ChatService"                                  # what a thing is and what touches it
graphify path "ChatService" "SafeHttp"                          # how two things connect
graphify query "how does the free daily quota get consumed and refunded"
graphify affected "SafeHttp"                                    # what breaks if this changes
```
- Each edge is tagged `EXTRACTED` (written in the code) or `INFERRED` (resolved by Graphify); trust `EXTRACTED` more.
- `query` is capped at about 2000 tokens; raise it with `--budget 6000` or narrow the question.
- Open `graphify-out/graph.html` in a browser to click around and filter.

## In an AI assistant
`graphify install` registers a `/graphify` skill for Claude Code, Cursor, Codex and others.
Docs and images need that skill (they use the assistant's model); code does not.

## Good to know
- Graphify also sells a hosted product (Graphify Cloud). **We do not use it** and nothing here needs an account or an API key.
- The graph is a search aid, not a source of truth. Confirm important answers in the code and the tests.
