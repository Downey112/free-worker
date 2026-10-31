# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2.3,<3", "httpx>=0.27", "pypdf>=5"]
# ///
"""free-worker: a read-only MCP worker for Claude Code that routes each task to a
free model provider and falls back to the next one when a quota runs out.

Run as an MCP server:   uv run --script server.py
Add a provider key:     uv run --script server.py setup <groq|gemini|mistral|openrouter>
Show providers/quotas:  uv run --script server.py status
"""
from __future__ import annotations

import fnmatch
import getpass
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

logging.getLogger("httpx").setLevel(logging.WARNING)

HOME = Path(os.environ.get("FREE_WORKER_HOME", Path.home() / ".free-worker"))
CONFIG_FILE = HOME / "config.json"  # API keys + chosen models
STATE_FILE = HOME / "state.json"  # cooldowns + daily usage
SESSIONS = HOME / "sessions"
AGY_PATH = os.environ.get("AGY_PATH", str(Path.home() / "AppData/Local/agy/bin/agy.exe"))
AGY_TIMEOUT = int(os.environ.get("FREE_WORKER_AGY_TIMEOUT", "1200"))
HTTP_TIMEOUT = 300
COPILOT_PATH = os.environ.get("COPILOT_PATH", str(Path.home() / "AppData/Local/GitHubCopilotCLI/copilot.exe"))
COPILOT_TIMEOUT = int(os.environ.get("FREE_WORKER_COPILOT_TIMEOUT", "600"))
COPILOT_CWD = HOME / "copilot-empty"  # stays empty, so no repo config or instructions load
# --available-tools="" is silently ignored and leaves every tool on, including shell and
# apply_patch. An allowlist holding only a name that doesn't exist leaves the model with none.
COPILOT_LOCKDOWN = [
    "--available-tools=fw_no_such_tool", "--deny-tool=shell", "--deny-tool=write",
    "--deny-tool=url", "--disable-builtin-mcps", "--no-custom-instructions",
    "--no-ask-user", "--no-auto-update", "--disallow-temp-dir", "-s",
]

READ_ONLY = (
    "READ-ONLY MODE: Do not create, modify, or delete any files, do not run "
    "builds/installs/migrations, and do not mutate git state."
)
RETURN_FORMAT = (
    "Return:\n1. The deliverable (full text / full code).\n"
    "2. Evidence: file + line/page/timestamp + short quote for every fact and number.\n"
    "3. Assumptions and risks.\n4. Blind spots: anything you couldn't read or verify."
)
REVIEW_PROMPT = (
    "You are an adversarial reviewer. Hunt for bugs, wrong facts, unsupported claims, "
    "security issues, edge cases and unstated assumptions. Return numbered findings, each "
    "with location, severity (critical/major/minor), what is wrong, and what correct looks "
    "like. Say plainly when something is fine; do not invent problems."
)


class WorkerError(ToolError):
    """Expected failure; its message reaches Claude as-is."""


# ---------------------------------------------------------------- file safety

# Mirrors the "never let the worker read" list in ~/.claude/CLAUDE.md section 6.
_BLOCK_GLOBS = [
    "*/.env", "*/.env.*", "*.pem", "*.key", "*/.credentials.json", "*credentials*.json",
    "*service-account*.json", "*/.ssh/*", "*/.aws/*", "*/.config/gcloud/*",
    "*/.threads-mcp/*", "*/.free-worker/*", "*/cookies*", "*/Login Data*",
    "*/User Data/*", "*/token.json", "*/appdata/roaming/microsoft/credentials/*",
    "*/appdata/roaming/microsoft/protect/*", "*/appdata/local/microsoft/credentials/*",
]
AGY_MAX_PROMPT_CHARS = 24_000  # Windows caps a command line at ~32k chars
SESSION_TTL = 7 * 24 * 3600  # sessions hold file text, so don't keep them forever


def _blocked(path: Path) -> str | None:
    p = str(path.resolve()).replace("\\", "/")
    low = p.lower()
    claude = str(Path.home() / ".claude").replace("\\", "/").lower()
    if low.startswith(claude + "/") and low != claude + "/claude.md":
        return "inside ~/.claude (memory, history and sessions are off limits)"
    for g in _BLOCK_GLOBS:
        if fnmatch.fnmatch(low, g.lower()):
            return f"matches the secrets blocklist ({g})"
    return None


def _read_file(path: Path, limit_chars: int = 2_000_000) -> str:
    reason = _blocked(path)
    if reason:
        raise WorkerError(f"Refused to read {path}: {reason}.")
    if not path.is_file():
        raise WorkerError(f"Not a file: {path}")
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        pages = PdfReader(str(path)).pages
        text = "\n".join(f"[page {i + 1}]\n{p.extract_text() or ''}" for i, p in enumerate(pages))
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > limit_chars:
        text = text[:limit_chars] + f"\n[truncated at {limit_chars} chars]"
    return text


def _files_block(files: list[str]) -> str:
    parts = []
    for f in files:
        path = Path(f)
        numbered = "\n".join(f"{i + 1:>5}| {line}" for i, line in enumerate(_read_file(path).splitlines()))
        parts.append(f"===== FILE: {path} =====\n{numbered}\n===== END FILE =====")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- state

_lock = threading.Lock()


def _load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def _save(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _cooldown(provider: str, seconds: float, reason: str) -> None:
    with _lock:
        st = _load(STATE_FILE, {})
        st.setdefault("cooldowns", {})[provider] = {"until": time.time() + seconds, "reason": reason}
        _save(STATE_FILE, st)


def _cooling(provider: str) -> dict | None:
    with _lock:
        cd = _load(STATE_FILE, {}).get("cooldowns", {}).get(provider)
    return cd if cd and cd["until"] > time.time() else None


def _scrub(text: str) -> str:
    """Remove any configured API key from text that goes back to Claude."""
    for entry in _load(CONFIG_FILE, {}).values():
        key = entry.get("key") if isinstance(entry, dict) else None
        if key and len(key) > 8:
            text = text.replace(key, "***")
    return text


def _count_use(provider: str) -> None:
    with _lock:
        st = _load(STATE_FILE, {})
        day = st.setdefault("usage", {}).setdefault(_today(), {})
        day[provider] = day.get(provider, 0) + 1
        st["usage"] = {d: v for d, v in st["usage"].items() if d >= _today()[:8] + "01"}  # keep this month
        _save(STATE_FILE, st)


# ---------------------------------------------------------------- providers


class QuotaHit(Exception):
    def __init__(self, seconds: float, reason: str):
        super().__init__(reason)
        self.seconds, self.reason = seconds, reason


class Skip(Exception):
    """Provider unusable for this call (no key, input too big, transient error)."""


@dataclass
class Provider:
    name: str
    label: str
    call: Callable[..., str]
    max_input_tokens: int
    web: bool = False
    reads_paths: bool = False  # agy reads files itself; APIs get the text inlined
    needs_key: str | None = None
    extra: dict = field(default_factory=dict)


def _parse_reset(text: str) -> float | None:
    m = re.search(r"[Rr]esets? in (?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", text)
    if m and any(m.groups()):
        h, mi, s = (int(x or 0) for x in m.groups())
        return h * 3600 + mi * 60 + s
    return None


def _agy(model: str) -> Callable[..., str]:
    def call(system: str, messages: list[dict], web: bool, cwd: str | None) -> str:
        transcript = "\n\n".join(f"[{m['role'].upper()}]\n{m['content']}" for m in messages)
        prompt = f"{READ_ONLY}\n\n{system}\n\n{transcript}".strip()
        if len(prompt) > AGY_MAX_PROMPT_CHARS:
            raise Skip(f"prompt {len(prompt):,} chars is over the Windows command-line limit for agy")
        args = [AGY_PATH, "--add-dir", cwd or str(Path.home()), "--model", model,
                "--print-timeout", f"{AGY_TIMEOUT}s", "-p", prompt]
        try:
            r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=AGY_TIMEOUT + 60, cwd=cwd or None)
        except FileNotFoundError:
            raise Skip(f"agy CLI not found at {AGY_PATH}")
        except subprocess.TimeoutExpired:
            raise Skip(f"agy timed out after {AGY_TIMEOUT}s")
        out = (r.stdout or "").strip()
        both = out + "\n" + (r.stderr or "")
        if re.search(r"RESOURCE_EXHAUSTED|quota (reached|exhausted)|Quota exhausted", both, re.I):
            raise QuotaHit(_parse_reset(both) or 3600, "quota: " + both.strip().splitlines()[-1][:200])
        if r.returncode != 0 or not out:
            raise Skip(f"agy exit {r.returncode}: {(r.stderr or out).strip()[-300:]}")
        return out
    return call


def _copilot(system: str, messages: list[dict], web: bool, cwd: str | None) -> str:
    """GitHub Copilot CLI with no tools: it only sees text inlined here, so the secrets
    blocklist in _read_file still applies. The prompt goes over stdin (no command-line limit)."""
    if web:
        raise Skip("Copilot runs without tools, so it has no web search")
    transcript = "\n\n".join(f"[{m['role'].upper()}]\n{m['content']}" for m in messages)
    prompt = (f"{system}\nYou have no tools. Everything you may use is in this message; "
              f"if something is missing, say so.\n\n{transcript}")
    COPILOT_CWD.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "COPILOT_ALLOW_ALL"}
    sid = str(uuid.uuid4())
    try:
        r = subprocess.run([COPILOT_PATH, *COPILOT_LOCKDOWN, f"--session-id={sid}"], input=prompt,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=COPILOT_TIMEOUT, cwd=COPILOT_CWD, env=env)
    except FileNotFoundError:
        raise Skip(f"Copilot CLI not found at {COPILOT_PATH}")
    except subprocess.TimeoutExpired:
        raise Skip(f"Copilot timed out after {COPILOT_TIMEOUT}s")
    finally:
        # Copilot keeps every prompt on disk with no expiry, and prompts can hold file text.
        shutil.rmtree(Path.home() / ".copilot/session-state" / sid, ignore_errors=True)
    out = (r.stdout or "").strip()
    if r.returncode == 0 and out:
        return out  # don't scan a good answer: it may discuss quotas or rate limits
    detail = ((r.stderr or "") + "\n" + out).strip()
    if re.search(r"quota|rate.?limit|\b429\b|credits?|limit (reached|exceeded)", detail, re.I):
        raise QuotaHit(_parse_reset(detail) or 6 * 3600, "quota: " + detail.splitlines()[-1][:200])
    raise Skip(f"Copilot exit {r.returncode}: {detail[-300:]}")


def _http_error(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    text = resp.text[:400]
    if resp.status_code == 429:
        retry = resp.headers.get("retry-after")
        secs = float(retry) if retry and retry.replace(".", "").isdigit() else None
        daily = re.search(r"per.?day|daily|RPD|TPD", text, re.I)
        raise QuotaHit(secs or (6 * 3600 if daily else 300), f"429: {text[:200]}")
    if resp.status_code in (401, 403):
        raise QuotaHit(24 * 3600, f"{resp.status_code} key rejected: {text[:200]}")
    raise Skip(f"HTTP {resp.status_code}: {text[:200]}")


def _openai_compat(base: str, key_name: str, web_tools: list | None = None) -> Callable[..., str]:
    def call(system: str, messages: list[dict], web: bool, cwd: str | None) -> str:
        cfg = _load(CONFIG_FILE, {}).get(key_name, {})
        body: dict[str, Any] = {
            "model": cfg["model"],
            "messages": [{"role": "system", "content": system}] + messages,
            "temperature": 0.3,
        }
        if web and web_tools:
            body["tools"] = web_tools
        try:
            resp = httpx.post(f"{base}/chat/completions", json=body, timeout=HTTP_TIMEOUT,
                              headers={"Authorization": f"Bearer {cfg['key']}"})
        except httpx.HTTPError as e:
            raise Skip(f"network: {type(e).__name__}")
        _http_error(resp)
        return resp.json()["choices"][0]["message"]["content"] or ""
    return call


def _gemini(system: str, messages: list[dict], web: bool, cwd: str | None) -> str:
    cfg = _load(CONFIG_FILE, {}).get("gemini", {})
    body: dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "model" if m["role"] == "assistant" else "user",
                      "parts": [{"text": m["content"]}]} for m in messages],
        "generationConfig": {"temperature": 0.3},
    }
    if web:
        body["tools"] = [{"google_search": {}}]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{cfg['model']}:generateContent"
    try:
        resp = httpx.post(url, json=body, timeout=HTTP_TIMEOUT, headers={"x-goog-api-key": cfg["key"]})
    except httpx.HTTPError as e:
        raise Skip(f"network: {type(e).__name__}")
    _http_error(resp)
    cand = (resp.json().get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
    chunks = cand.get("groundingMetadata", {}).get("groundingChunks", [])
    sources = [f"- {c['web'].get('title', '')}: {c['web'].get('uri', '')}" for c in chunks if "web" in c]
    if sources:
        text += "\n\nSources (Google Search grounding):\n" + "\n".join(dict.fromkeys(sources))
    if not text:
        raise Skip(f"empty Gemini answer (finishReason={cand.get('finishReason')})")
    return text


PROVIDERS: dict[str, Provider] = {p.name: p for p in [
    Provider("agy-flash", "agy · Gemini 3.8 Flash (High)", _agy("Gemini 3.8 Flash (High)"), 10**9, web=True, reads_paths=True),
    Provider("agy-sonnet", "agy · Claude Sonnet 4.6", _agy("Claude Sonnet 4.6 (Thinking)"), 10**9, web=True, reads_paths=True),
    Provider("agy-gptoss", "agy · GPT-OSS 120B", _agy("GPT-OSS 120B (Medium)"), 10**9, web=True, reads_paths=True),
    Provider("copilot", "GitHub Copilot CLI (Auto)", _copilot, 60_000),
    Provider("gemini", "Gemini API (free key)", _gemini, 900_000, web=True, needs_key="gemini"),
    Provider("mistral", "Mistral API", _openai_compat("https://api.mistral.ai/v1", "mistral"), 100_000, needs_key="mistral"),
    Provider("groq", "Groq API", _openai_compat("https://api.groq.com/openai/v1", "groq", [{"type": "browser_search"}]),
             6_000, web=True, needs_key="groq"),
    Provider("openrouter", "OpenRouter :free", _openai_compat("https://openrouter.ai/api/v1", "openrouter"), 30_000, needs_key="openrouter"),
]}

CHAINS = {
    "delegate": ["agy-flash", "agy-sonnet", "copilot", "gemini", "mistral", "groq", "agy-gptoss", "openrouter"],
    "analyze": ["agy-flash", "agy-sonnet", "copilot", "gemini", "mistral", "agy-gptoss", "groq", "openrouter"],
    "web": ["agy-flash", "agy-sonnet", "gemini", "groq", "agy-gptoss"],
    "review": ["agy-sonnet", "agy-flash", "copilot", "gemini", "mistral", "groq", "agy-gptoss", "openrouter"],
}


def _route(kind: str, system: str, messages: list[dict], *, web: bool = False, cwd: str | None = None,
           paths_prompt: list[dict] | None = None, prefer: list[str] | None = None) -> tuple[str, str, list[str]]:
    """Try providers in order. paths_prompt is an alternative message list for providers
    that read files themselves (agy) instead of receiving inlined text."""
    tried: list[str] = []
    chain = (prefer or []) + [p for p in CHAINS[kind] if p not in (prefer or [])]
    cfg = _load(CONFIG_FILE, {})
    for name in chain:
        p = PROVIDERS.get(name)
        if not p:
            continue
        if p.needs_key and not cfg.get(p.needs_key, {}).get("key"):
            continue  # not set up: skip silently
        cd = _cooling(name)
        if cd:
            hrs = (cd["until"] - time.time()) / 3600
            tried.append(f"{name} (cooling down {hrs:.1f}h: {cd['reason'][:60]})")
            continue
        msgs = paths_prompt if (p.reads_paths and paths_prompt) else messages
        est = sum(len(m["content"]) for m in msgs) // 4 + len(system) // 4
        if est > p.max_input_tokens:
            tried.append(f"{name} (input ~{est:,} tokens > {p.max_input_tokens:,})")
            continue
        try:
            out = p.call(system, msgs, web, cwd)
            _count_use(name)
            return out, name, tried
        except QuotaHit as q:
            _cooldown(name, q.seconds, _scrub(q.reason))
            tried.append(f"{name} (quota, cooling {q.seconds / 3600:.1f}h)")
        except Skip as s:
            tried.append(_scrub(f"{name} (skipped: {str(s)[:120]})"))
        except Exception as e:  # never let one provider's bug stop the chain
            tried.append(_scrub(f"{name} (error: {type(e).__name__}: {str(e)[:120]})"))
    raise WorkerError("No free provider could take this task. Tried: " + "; ".join(tried or ["none set up"]))


# ---------------------------------------------------------------- sessions

def _session_path(sid: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", sid or ""):
        raise WorkerError(f"Invalid session id {sid!r}.")
    return SESSIONS / f"{sid}.json"


def _prune_sessions() -> None:
    cutoff = time.time() - SESSION_TTL
    for f in SESSIONS.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def _finish(kind: str, system: str, messages: list[dict], cwd: str | None, web: bool,
            out: str, provider: str, tried: list[str], sid: str | None = None,
            path_messages: list[dict] | None = None) -> str:
    """Save the conversation for follow_up. path_messages is the same conversation with
    file paths instead of file text, used when agy picks up a follow-up."""
    reply = {"role": "assistant", "content": out}
    if sid:
        data = _load(_session_path(sid), {})
    else:
        sid = uuid.uuid4().hex[:12]
        data = {"kind": kind, "system": system, "cwd": cwd, "web": web, "created": time.time()}
    data["messages"] = messages + [reply]
    data["path_messages"] = (path_messages or messages) + [reply]
    _save(_session_path(sid), data)
    _prune_sessions()
    footer = f"\n\n---\n[free-worker] provider: {PROVIDERS[provider].label} | session: {sid}"
    if tried:
        footer += "\nfell back past: " + "; ".join(tried)
    return out + footer


# ---------------------------------------------------------------- MCP tools

mcp = MCPServer("free-worker")

WORKER_SYSTEM = (
    f"{READ_ONLY}\nYou are a careful research and drafting worker. Your output is audited "
    "line by line against the sources, so never invent facts, numbers, quotes or citations. "
    "If something is not in the provided material, say so."
)


@mcp.tool()
def delegate(prompt: str, files: list[str] | None = None, cwd: str | None = None,
             prefer: list[str] | None = None) -> str:
    """Hand a drafting/analysis task to the first free model with quota left.

    files: absolute paths whose text is given to the model (secrets and ~/.claude are refused).
    prefer: provider names to try first, e.g. ["gemini"]. Providers: agy-flash, agy-sonnet,
    agy-gptoss, copilot, gemini, mistral, groq, openrouter. The answer ends with the provider used and a
    session id for follow_up.
    """
    body = f"{prompt}\n\n{RETURN_FORMAT}"
    inline = [{"role": "user", "content": body + ("\n\n" + _files_block(files) if files else "")}]
    by_path = [{"role": "user", "content": body + ("\n\nFiles to read:\n" + "\n".join(files) if files else "")}]
    out, prov, tried = _route("delegate", WORKER_SYSTEM, inline, cwd=cwd, paths_prompt=by_path, prefer=prefer)
    return _finish("delegate", WORKER_SYSTEM, inline, cwd, False, out, prov, tried, path_messages=by_path)


@mcp.tool()
def analyze_files(files: list[str], question: str, prefer: list[str] | None = None) -> str:
    """Read files (text, code, CSV, Markdown, PDF) and answer a question about them, citing
    file + line or page for every claim."""
    if not files:
        raise WorkerError("Give at least one absolute file path.")
    q = f"{question}\n\nCite file + line/page + short quote for every claim.\n\n{RETURN_FORMAT}"
    inline = [{"role": "user", "content": f"{q}\n\n{_files_block(files)}"}]
    by_path = [{"role": "user", "content": f"{q}\n\nFiles to read:\n" + "\n".join(files)}]
    cwd = str(Path(files[0]).parent)
    out, prov, tried = _route("analyze", WORKER_SYSTEM, inline, cwd=cwd, paths_prompt=by_path, prefer=prefer)
    return _finish("analyze", WORKER_SYSTEM, inline, cwd, False, out, prov, tried, path_messages=by_path)


@mcp.tool()
def web_lookup(query: str, prefer: list[str] | None = None) -> str:
    """Look something up on the web (current versions, prices, docs, policies). Only providers
    with real web search are used: agy, Gemini (Google Search grounding), Groq (browser search)."""
    system = f"{READ_ONLY}\nAnswer from current web sources. Give the source URL and a short quote for each fact. Say when sources disagree or are old."
    msgs = [{"role": "user", "content": query}]
    out, prov, tried = _route("web", system, msgs, web=True, prefer=prefer)
    return _finish("web", system, msgs, None, True, out, prov, tried)


@mcp.tool()
def review(content: str | None = None, files: list[str] | None = None, focus: str | None = None,
           prefer: list[str] | None = None) -> str:
    """Adversarial second opinion on a draft, plan, diff or files. Defaults to a different
    model family from Claude's drafts where possible."""
    if not content and not files:
        raise WorkerError("Give content or files to review.")
    system = f"{READ_ONLY}\n{REVIEW_PROMPT}"
    ask = f"Focus: {focus}\n\n" if focus else ""
    inline = [{"role": "user", "content": ask + (content or "") + ("\n\n" + _files_block(files) if files else "")}]
    by_path = [{"role": "user", "content": ask + (content or "") + ("\n\nFiles to read:\n" + "\n".join(files) if files else "")}]
    cwd = str(Path(files[0]).parent) if files else None
    out, prov, tried = _route("review", system, inline, cwd=cwd, paths_prompt=by_path, prefer=prefer)
    return _finish("review", system, inline, cwd, False, out, prov, tried, path_messages=by_path)


@mcp.tool()
def follow_up(session_id: str, question: str, prefer: list[str] | None = None) -> str:
    """Continue an earlier session (revision rounds, follow-up questions). The whole earlier
    conversation is resent, so any provider can pick it up."""
    data = _load(_session_path(session_id), None)
    if not data:
        raise WorkerError(f"No session {session_id} (sessions expire after 7 days).")
    ask = {"role": "user", "content": question}
    msgs = data["messages"] + [ask]
    by_path = data.get("path_messages", data["messages"]) + [ask]
    out, prov, tried = _route(data["kind"], data["system"], msgs, web=data.get("web", False),
                              cwd=data.get("cwd"), paths_prompt=by_path, prefer=prefer)
    return _finish(data["kind"], data["system"], msgs, data.get("cwd"), data.get("web", False),
                   out, prov, tried, sid=session_id, path_messages=by_path)


@mcp.tool()
def status() -> dict[str, Any]:
    """Which providers are set up, which are cooling down after a quota hit, and today's usage."""
    cfg, st = _load(CONFIG_FILE, {}), _load(STATE_FILE, {})
    rows = {}
    for name, p in PROVIDERS.items():
        ready = not p.needs_key or bool(cfg.get(p.needs_key, {}).get("key"))
        cd = _cooling(name)
        rows[name] = {
            "provider": p.label,
            "set_up": ready,
            "model": cfg.get(p.needs_key, {}).get("model") if p.needs_key else None,
            "state": (f"cooling down until {datetime.fromtimestamp(cd['until']).strftime('%d %b %H:%M')} ({cd['reason'][:80]})"
                      if cd else ("ready" if ready else "no key — run: server.py setup " + (p.needs_key or ""))),
            "calls_today": st.get("usage", {}).get(_today(), {}).get(name, 0),
        }
    return rows


# ---------------------------------------------------------------- CLI setup

KEY_SETUP = {
    "groq": ("https://api.groq.com/openai/v1/models", "Bearer", ["openai/gpt-oss-120b", "llama-3.3-70b-versatile"],
             "https://console.groq.com/keys"),
    "mistral": ("https://api.mistral.ai/v1/models", "Bearer", ["mistral-medium-latest", "mistral-large-latest", "mistral-small-latest"],
                "https://console.mistral.ai/api-keys"),
    "openrouter": ("https://openrouter.ai/api/v1/models", "Bearer", [], "https://openrouter.ai/settings/keys"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/models", "goog",
               ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"], "https://aistudio.google.com/apikey"),
}


def _setup(name: str) -> None:
    if name not in KEY_SETUP:
        sys.exit(f"Unknown provider {name!r}. Choose one of: {', '.join(KEY_SETUP)}")
    url, auth, preferred, where = KEY_SETUP[name]
    print(f"Get a free {name} API key at {where}")
    key = getpass.getpass(f"{name} API key (hidden): ").strip()
    if not key:
        sys.exit("No key entered.")
    headers = {"x-goog-api-key": key} if auth == "goog" else {"Authorization": f"Bearer {key}"}
    resp = httpx.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        sys.exit(f"Key check failed: HTTP {resp.status_code} {resp.text[:200]}")
    data = resp.json()
    ids = [m.get("id") or m.get("name", "").removeprefix("models/") for m in data.get("data", data.get("models", []))]
    if name == "openrouter":
        free = sorted(i for i in ids if i.endswith(":free"))
        preferred = [i for i in free if "gpt-oss-120b" in i] + [i for i in free if "deepseek" in i] + free
    model = next((m for m in preferred if m in ids), None)
    if not model:
        sys.exit(f"None of the expected models were found. Available: {', '.join(ids[:30])}")
    with _lock:
        cfg = _load(CONFIG_FILE, {})
        cfg[name] = {"key": key, "model": model}
        _save(CONFIG_FILE, cfg)
    print(f"Saved. {name} will use model {model}. Key stored in {CONFIG_FILE} (never shown).")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    sys.stdout.reconfigure(encoding="utf-8")
    if cmd == "setup":
        _setup(sys.argv[2] if len(sys.argv) > 2 else "")
    elif cmd == "status":
        print(json.dumps(status(), indent=2, ensure_ascii=False))
    else:
        mcp.run()
