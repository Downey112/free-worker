# free-worker

A read-only MCP worker for Claude Code. Claude hands it drafting, file reading, web lookups and reviews; it sends each task to the first **free** model that still has quota, and moves to the next when one runs out.

## Providers, in fallback order

| Name | What | Needs | Web search |
| --- | --- | --- | --- |
| `agy-flash` | Antigravity CLI · Gemini 3.8 Flash (High) | agy installed and logged in | yes |
| `agy-sonnet` | Antigravity CLI · Claude Sonnet 4.6 (Thinking) | same | yes |
| `copilot` | GitHub Copilot CLI (Auto model) | `copilot` installed and logged in | no |
| `gemini` | Gemini API free key | `setup gemini` | Google Search grounding |
| `mistral` | Mistral API free credits | `setup mistral` | no |
| `groq` | Groq free API (gpt-oss-120b) | `setup groq` | browser search |
| `agy-gptoss` | Antigravity CLI · GPT-OSS 120B | agy | yes |
| `openrouter` | OpenRouter `:free` models (50/day) | `setup openrouter` | no |

Providers without a key are skipped. When a provider returns a quota error (agy's "Resets in …", HTTP 429), it cools down for that long and the next one is used. Each answer ends with the provider used, a session id, and which providers it fell back past.

Reviews (`review`) try Sonnet first. Web lookups only use providers with real search.

## Tools

- `delegate(prompt, files?, cwd?, prefer?)`: drafting and analysis.
- `analyze_files(files, question, prefer?)`: read text, code, CSV, Markdown and PDF files and answer with file + line/page citations.
- `web_lookup(query, prefer?)`: current facts with source URLs.
- `review(content? | files?, focus?, prefer?)`: an adversarial second opinion.
- `follow_up(session_id, question, prefer?)`: revision rounds; any provider can continue a session.
- `status()`: which providers are set up, cooling down, and used today.

`prefer` puts named providers first, e.g. `["gemini"]` for a very long document.

## Safety

- Every prompt starts with the READ-ONLY line, and the server itself never writes outside `~/.free-worker`.
- It refuses to read `~/.claude` (except `CLAUDE.md`), `.env*`, keys, `.ssh`, `.aws`, gcloud config, credential stores, browser profiles, `~/.threads-mcp`, and its own key file.
- API keys live in `~/.free-worker/config.json`, entered through a hidden prompt, and are scrubbed from any error text.
- Sessions (which can contain file text) are deleted after 7 days.
- agy gets file paths rather than file text, and tasks longer than ~24,000 characters skip agy (Windows command-line limit).
- Copilot runs with no tools, in an empty folder, with its built-in MCP servers off. It can't read, write or run anything, so it only sees the text free-worker passes it, after the blocklist above. Copilot CLI has no read-only switch and ignores `--available-tools=""`, which is why the allowlist holds a tool name that doesn't exist. Copilot's own saved copy of each session is deleted right after the call. Prompts go to GitHub, so files passed to this provider leave the machine, as they do with the other API providers.

## Add a free key (optional, any time)

Run in your own PowerShell window. Never paste keys into the chat.

```
uv run --script C:/Users/courts/free-worker/server.py setup groq
uv run --script C:/Users/courts/free-worker/server.py setup gemini
uv run --script C:/Users/courts/free-worker/server.py setup mistral
uv run --script C:/Users/courts/free-worker/server.py setup openrouter
```

The script checks the key against the provider's model list and picks a model automatically.

Get keys at: console.groq.com/keys · aistudio.google.com/apikey · console.mistral.ai/api-keys · openrouter.ai/settings/keys

Check everything: `uv run --script C:/Users/courts/free-worker/server.py status`

## Registered in Claude Code

```
claude mcp add free-worker --scope user -- uv run --script C:\Users\courts\free-worker\server.py
```
Restart Claude Code to load the tools.
