#!/usr/bin/env python3
"""Sir Hendrick: a Telegram bridge to Claude Code on this computer.

Modes:
  python3 sir_hendrick.py           run the bot (long-polls Telegram)
  python3 sir_hendrick.py notify    send a Claude Code hook event (JSON on stdin) to Telegram

Standard library only. Configuration comes from environment variables or a
.env file next to this script. See README.md.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("sir_hendrick")

TG_LIMIT = 4000  # Telegram hard limit is 4096; leave room for prefixes.
CHILD_ENV = "SIR_HENDRICK_CHILD"  # set on Claude runs started by the bot, so hooks don't double-report
PERMISSION_MODES = {"default", "acceptEdits", "plan", "bypassPermissions"}


# ---------------------------------------------------------------- config

def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ without overriding existing vars."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Config:
    token: str
    chat_id: Optional[int]
    claude_bin: str
    workdir: Path
    permission_mode: str
    allowed_tools: str
    model: str
    timeout: int
    state_file: Path

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit("TELEGRAM_BOT_TOKEN is not set (see .env.example)")
        raw_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        try:
            chat_id = int(raw_chat) if raw_chat else None
        except ValueError:
            raise SystemExit(f"TELEGRAM_CHAT_ID must be an integer, got {raw_chat!r}")
        mode = os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits").strip()
        if mode not in PERMISSION_MODES:
            raise SystemExit(f"CLAUDE_PERMISSION_MODE must be one of {sorted(PERMISSION_MODES)}")
        workdir = Path(os.environ.get("CLAUDE_WORKDIR", "~")).expanduser()
        if not workdir.is_dir():
            raise SystemExit(f"CLAUDE_WORKDIR does not exist: {workdir}")
        try:
            timeout = int(os.environ.get("CLAUDE_TIMEOUT", "1800"))
        except ValueError:
            raise SystemExit("CLAUDE_TIMEOUT must be an integer (seconds)")
        state = Path(os.environ.get("SIR_HENDRICK_STATE", "~/.sir-hendrick/state.json")).expanduser()
        return cls(
            token=token,
            chat_id=chat_id,
            # which() applies PATHEXT on Windows, so "claude" finds claude.exe / claude.cmd
            claude_bin=shutil.which(os.environ.get("CLAUDE_BIN", "claude")) or os.environ.get("CLAUDE_BIN", "claude"),
            workdir=workdir,
            permission_mode=mode,
            allowed_tools=os.environ.get("CLAUDE_ALLOWED_TOOLS", "").strip(),
            model=os.environ.get("CLAUDE_MODEL", "").strip(),
            timeout=max(timeout, 30),
            state_file=state,
        )


class State:
    """Tiny JSON store for the Claude session id and the Telegram update offset."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}

    def get(self, key: str, default=None):
        with self._lock:
            return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        with self._lock:
            self.data[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(self.path)


# ---------------------------------------------------------------- telegram

class Telegram:
    def __init__(self, token: str):
        self._base = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, http_timeout: float = 30, **params):
        req = urllib.request.Request(
            self._base + method,
            data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read() or b"{}")
            except json.JSONDecodeError:
                body = {}
            if e.code == 429:  # rate limited: wait as told, then retry once
                time.sleep(body.get("parameters", {}).get("retry_after", 3))
                return self.call(method, http_timeout, **params)
            raise RuntimeError(f"Telegram {method} failed: {e.code} {body.get('description')}") from None
        if not body.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {body.get('description')}")
        return body["result"]

    def send(self, chat_id: int, text: str) -> None:
        for chunk in split_text(text or "(empty)"):
            self.call("sendMessage", chat_id=chat_id, text=chunk, disable_web_page_preview=True)

    def typing(self, chat_id: int) -> None:
        try:
            self.call("sendChatAction", chat_id=chat_id, action="typing")
        except Exception:  # cosmetic only
            pass


def split_text(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Split on line boundaries where possible so no chunk exceeds the limit."""
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks or [""]


# ---------------------------------------------------------------- activity formatting

def _short(value, n: int = 300) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def format_tool(name: str, inp: dict) -> str:
    inp = inp or {}
    if name == "Bash":
        return f"🖥 $ {_short(inp.get('command', ''), 500)}"
    if name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        icon = "📖" if name == "Read" else "✏️"
        return f"{icon} {name} {inp.get('file_path') or inp.get('notebook_path', '')}"
    if name in ("Grep", "Glob"):
        where = f" in {inp['path']}" if inp.get("path") else ""
        return f"🔎 {name} {_short(inp.get('pattern', ''), 120)}{where}"
    if name == "WebFetch":
        return f"🌐 Fetch {inp.get('url', '')}"
    if name == "WebSearch":
        return f"🌐 Search {_short(inp.get('query', ''), 200)}"
    if name in ("Task", "Agent"):
        return f"🤖 Subagent: {_short(inp.get('description') or inp.get('prompt', ''), 200)}"
    if name == "TodoWrite":
        todos = inp.get("todos") or []
        lines = [f"  {'✓' if t.get('status') == 'completed' else '•'} {t.get('content', '')}" for t in todos]
        return "📋 Plan\n" + "\n".join(lines)
    return f"🔧 {name} {_short(inp, 200)}"


def _tool_result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return _short(content or "", 300)


class ActivityTracker:
    """Turns Claude stream-json events into human-readable activity lines.

    Assistant text is held back until the next tool call, so the final answer
    is not reported twice (it arrives separately in the `result` event).
    """

    def __init__(self, emit: Callable[[str], None]):
        self.emit = emit
        self.pending_text = ""
        self.session_id: Optional[str] = None
        self.result: Optional[dict] = None

    def handle(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "system" and event.get("subtype") == "init":
            self.session_id = event.get("session_id") or self.session_id
        elif etype == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    self._flush_text()
                    self.pending_text = block["text"].strip()
                elif block.get("type") == "tool_use":
                    self._flush_text()
                    self.emit(format_tool(block.get("name", "?"), block.get("input")))
        elif etype == "user":
            content = event.get("message", {}).get("content", [])
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result" and block.get("is_error"):
                    self.emit(f"⚠️ Tool error: {_tool_result_text(block)}")
        elif etype == "result":
            self.result = event
            self.session_id = event.get("session_id") or self.session_id

    def _flush_text(self) -> None:
        if self.pending_text:
            self.emit(f"💭 {_short(self.pending_text, 600)}")
            self.pending_text = ""


class Reporter:
    """Batches activity lines into few Telegram messages (rate-limit friendly)."""

    def __init__(self, send: Callable[[str], None], interval: float = 2.0):
        self.send, self.interval = send, interval
        self.lines: list[str] = []
        self.last = 0.0

    def add(self, line: str) -> None:
        self.lines.append(line)
        if time.monotonic() - self.last >= self.interval or sum(map(len, self.lines)) > TG_LIMIT:
            self.flush()

    def flush(self) -> None:
        if self.lines:
            text, self.lines = "\n".join(self.lines), []
            self.last = time.monotonic()
            try:
                self.send(text)
            except Exception as e:  # never let reporting kill a Claude run
                log.warning("activity send failed: %s", e)


# ---------------------------------------------------------------- claude runner

class ClaudeRunner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def build_cmd(self, prompt: str, session_id: Optional[str]) -> list[str]:
        cmd = [self.cfg.claude_bin, "-p", prompt, "--output-format", "stream-json", "--verbose",
               "--permission-mode", self.cfg.permission_mode]
        if session_id:
            cmd += ["--resume", session_id]
        if self.cfg.allowed_tools:
            cmd += ["--allowedTools", self.cfg.allowed_tools]
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        return cmd

    def run(self, prompt: str, session_id: Optional[str], on_event: Callable[[dict], None]) -> tuple[int, str]:
        """Run Claude to completion, streaming parsed events. Returns (exit code, stderr tail)."""
        env = {**os.environ, CHILD_ENV: "1"}
        env.pop("TELEGRAM_BOT_TOKEN", None)  # Claude never needs the bot token
        with self._lock:
            self._proc = subprocess.Popen(
                self.build_cmd(prompt, session_id), cwd=self.cfg.workdir, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
        proc = self._proc
        timer = threading.Timer(self.cfg.timeout, proc.kill)
        timer.start()
        stderr_lines: list[str] = []
        err_reader = threading.Thread(target=lambda: stderr_lines.extend(proc.stderr), daemon=True)
        err_reader.start()
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    on_event(json.loads(line))
                except json.JSONDecodeError:
                    log.debug("non-JSON output: %s", line)
            code = proc.wait()
        finally:
            timer.cancel()
            err_reader.join(timeout=2)
            proc.stdout.close()
            proc.stderr.close()
            with self._lock:
                self._proc = None
        return code, "".join(stderr_lines[-20:]).strip()

    def stop(self) -> bool:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
                return True
        return False


# ---------------------------------------------------------------- bot

HELP = (
    "🎩 Sir Hendrick at your service.\n\n"
    "Send any message and I'll pass it to Claude on your computer, report what Claude does, "
    "and reply with the result.\n\n"
    "/new – start a fresh Claude conversation\n"
    "/stop – stop the running task\n"
    "/status – show what I'm doing\n"
    "/id – show this chat's ID"
)


class Bot:
    def __init__(self, cfg: Config, tg: Telegram, runner: ClaudeRunner, state: State):
        self.cfg, self.tg, self.runner, self.state = cfg, tg, runner, state
        self.current_prompt: Optional[str] = None

    # -- polling
    def serve(self) -> None:
        if self.cfg.chat_id is None:
            log.warning("TELEGRAM_CHAT_ID not set: setup mode. Message the bot to learn your chat ID.")
        else:
            self._safe_send(f"🎩 Sir Hendrick online on {platform.node()}. Workdir: {self.cfg.workdir}")
        offset = self.state.get("offset", 0)
        while True:
            try:
                # Long-poll `timeout` (50s) must stay below the HTTP timeout (60s).
                updates = self.tg.call("getUpdates", http_timeout=60, offset=offset,
                                       timeout=50, allowed_updates=["message"])
            except Exception as e:
                log.warning("getUpdates failed: %s; retrying in 5s", e)
                time.sleep(5)
                continue
            for upd in updates:
                offset = upd["update_id"] + 1
                self.state.set("offset", offset)  # persist first: never replay a command after a crash
                try:
                    self.handle(upd)
                except Exception:
                    log.exception("failed to handle update %s", upd.get("update_id"))

    # -- dispatch
    def handle(self, upd: dict) -> None:
        msg = upd.get("message") or {}
        chat_id = msg.get("chat", {}).get("id")
        text = (msg.get("text") or "").strip()
        if chat_id is None or not text:
            return
        if self.cfg.chat_id is None:  # setup mode: only reveal the chat id
            self.tg.send(chat_id, f"Your chat ID is {chat_id}. Set TELEGRAM_CHAT_ID={chat_id} and restart me.")
            return
        if chat_id != self.cfg.chat_id:
            log.warning("ignored message from unauthorized chat %s", chat_id)
            return

        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if cmd in ("/start", "/help"):
            self.tg.send(chat_id, HELP)
        elif cmd == "/id":
            self.tg.send(chat_id, f"Chat ID: {chat_id}")
        elif cmd == "/new":
            if self.runner.busy:
                self.tg.send(chat_id, "Busy. /stop first, then /new.")
            else:
                self.state.set("session_id", None)
                self.tg.send(chat_id, "🆕 Fresh conversation. Next message starts a new Claude session.")
        elif cmd == "/stop":
            self.tg.send(chat_id, "🛑 Stopping." if self.runner.stop() else "Nothing is running.")
        elif cmd == "/status":
            sid = self.state.get("session_id")
            doing = f"Working on: {_short(self.current_prompt, 200)}" if self.runner.busy else "Idle."
            self.tg.send(chat_id, f"{doing}\nSession: {sid or 'none'}\nMode: {self.cfg.permission_mode}")
        elif cmd:
            self.tg.send(chat_id, "Unknown command. /help")
        elif self.runner.busy:
            self.tg.send(chat_id, "⏳ Still working on the last task. Wait, or /stop it.")
        else:
            self.current_prompt = text
            threading.Thread(target=self._run_claude, args=(chat_id, text), daemon=True).start()

    # -- claude task
    def _run_claude(self, chat_id: int, prompt: str) -> None:
        self.tg.typing(chat_id)
        reporter = Reporter(lambda t: self.tg.send(chat_id, t))
        tracker = ActivityTracker(reporter.add)
        reporter.add("▶️ Claude is on it.")
        session_id = self.state.get("session_id")
        try:
            code, stderr = self.runner.run(prompt, session_id, tracker.handle)
        except FileNotFoundError:
            self._safe_send(f"❌ Claude CLI not found ({self.cfg.claude_bin}). Install Claude Code or set CLAUDE_BIN.")
            return
        except Exception as e:
            log.exception("claude run failed")
            self._safe_send(f"❌ Could not run Claude: {e}")
            return
        finally:
            self.current_prompt = None
        reporter.flush()

        if tracker.session_id:
            self.state.set("session_id", tracker.session_id)
        res = tracker.result
        if res is None:
            hint = " (timed out or stopped)" if code < 0 else ""
            self._safe_send(f"❌ Claude exited with code {code}{hint}.\n{stderr[-1500:]}".strip())
            if session_id and "session" in stderr.lower():
                self.state.set("session_id", None)
                self._safe_send("Session couldn't be resumed, so I cleared it. Send your message again.")
            return
        footer = f"\n\n— {res.get('num_turns', '?')} turns, {res.get('duration_ms', 0) / 1000:.0f}s"
        if res.get("total_cost_usd") is not None:
            footer += f", ${res['total_cost_usd']:.4f}"
        if res.get("is_error"):
            self._safe_send(f"⚠️ Claude finished with an error ({res.get('subtype')}):\n{res.get('result', '')}{footer}")
        else:
            self._safe_send(f"✅ {res.get('result') or '(no text reply)'}{footer}")

    def _safe_send(self, text: str) -> None:
        try:
            self.tg.send(self.cfg.chat_id, text)
        except Exception as e:
            log.error("send failed: %s", e)


# ---------------------------------------------------------------- hook notifier

def format_hook(event: dict) -> Optional[str]:
    """Summarize a Claude Code hook payload for Telegram, or None to skip."""
    name = event.get("hook_event_name", "")
    where = Path(event.get("cwd") or ".").name
    if name in ("PreToolUse", "PostToolUse"):
        return f"[{where}] {format_tool(event.get('tool_name', '?'), event.get('tool_input'))}"
    if name == "UserPromptSubmit":
        return f"[{where}] 🗣 Prompt: {_short(event.get('prompt', ''), 400)}"
    if name == "Notification":
        return f"[{where}] 🔔 {event.get('message', '')}"
    if name == "Stop":
        return f"[{where}] ✅ Claude finished its turn"
    if name == "SessionStart":
        return f"[{where}] ▶️ Claude session started ({event.get('source', '')})"
    if name == "SessionEnd":
        return f"[{where}] ⏹ Claude session ended"
    return None


def notify_main(cfg: Config) -> int:
    """Hook entry point. Always exits 0 so a Telegram outage never blocks Claude."""
    if os.environ.get(CHILD_ENV) or cfg.chat_id is None:
        return 0  # bot-run sessions are already reported live
    try:
        text = format_hook(json.load(sys.stdin))
        if text:
            Telegram(cfg.token).send(cfg.chat_id, text)
    except Exception as e:
        print(f"sir-hendrick notify: {e}", file=sys.stderr)
    return 0


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env_file(Path(__file__).with_name(".env"))
    cfg = Config.from_env()
    if argv[1:2] == ["notify"]:
        return notify_main(cfg)
    Bot(cfg, Telegram(cfg.token), ClaudeRunner(cfg), State(cfg.state_file)).serve()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        sys.exit(130)
