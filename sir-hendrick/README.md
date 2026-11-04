# Sir Hendrick 🎩

Telegram bot that lets you talk to Claude Code running on your computer. You message the bot, Claude does the work on your machine, and Sir Hendrick reports every step (commands, file edits, searches, errors) and then the answer.

- Two-way: replies continue the same Claude conversation (`/new` starts fresh).
- Locked to one Telegram chat ID; everyone else is ignored.
- Optional hook reports activity from Claude sessions you start yourself in a terminal.
- Python 3.9+ standard library only. No `pip install`.

## 1. Create the bot (BotFather)

1. In Telegram, open **@BotFather** → `/newbot`.
2. Name: `Sir Hendrick`. Username: anything ending in `bot`, e.g. `SirHendrickBot`.
3. Copy the token it gives you. Treat it like a password.
4. Optional: `/setdescription`, `/setuserpic`, and `/setcommands` with:
   ```
   new - Start a fresh Claude conversation
   stop - Stop the running task
   status - Show what I'm doing
   id - Show this chat's ID
   ```

## 2. Install on your computer

Prereqs: Python 3.9+, and [Claude Code](https://docs.claude.com/en/docs/claude-code) installed and signed in (`claude` runs in your terminal).

```bash
cd sir-hendrick
cp .env.example .env          # put TELEGRAM_BOT_TOKEN in it
python3 sir_hendrick.py       # setup mode: message your bot, it replies with your chat ID
# put TELEGRAM_CHAT_ID in .env, then restart:
python3 sir_hendrick.py
```

You'll get "🎩 Sir Hendrick online" in Telegram. Send it a task.

## 3. Permissions: how much control Claude gets

`CLAUDE_PERMISSION_MODE` in `.env`:

| Mode | What Claude can do unattended |
|---|---|
| `plan` | Read-only, proposes a plan |
| `acceptEdits` (default) | Read and edit files. Shell commands only if listed in `CLAUDE_ALLOWED_TOOLS` (e.g. `Bash`) |
| `bypassPermissions` | Everything, no prompts. Full computer control |

There is no one at the keyboard to approve prompts, so a tool that isn't allowed gets denied and you'll see "⚠️ Tool error" in the report. For full control use `CLAUDE_ALLOWED_TOOLS="Bash Read Edit Write WebFetch WebSearch"` or `bypassPermissions`. Only do this with the chat ID lock set, and keep the token private.

## 4. Keep it running

**Linux (systemd user service)** – `~/.config/systemd/user/sir-hendrick.service`:
```ini
[Unit]
Description=Sir Hendrick Telegram bot
After=network-online.target

[Service]
WorkingDirectory=%h/Main/sir-hendrick
ExecStart=/usr/bin/python3 %h/Main/sir-hendrick/sir_hendrick.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```
`systemctl --user enable --now sir-hendrick && loginctl enable-linger $USER`

**macOS**: run it in a `tmux` session, or a launchd agent pointing at the same command.
**Windows**: Task Scheduler → "At log on" → `pythonw sir_hendrick.py` with "Start in" set to this folder.

Make sure `claude` is on the PATH the service sees, or set `CLAUDE_BIN` to its full path (`which claude`).

## 5. Optional: report Claude sessions you start yourself

Add to `~/.claude/settings.json` (adjust the path):
```json
{
  "hooks": {
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "python3 ~/Main/sir-hendrick/sir_hendrick.py notify" }] }],
    "PostToolUse":      [{ "matcher": "*", "hooks": [{ "type": "command", "command": "python3 ~/Main/sir-hendrick/sir_hendrick.py notify" }] }],
    "Notification":     [{ "hooks": [{ "type": "command", "command": "python3 ~/Main/sir-hendrick/sir_hendrick.py notify" }] }],
    "Stop":             [{ "hooks": [{ "type": "command", "command": "python3 ~/Main/sir-hendrick/sir_hendrick.py notify" }] }]
  }
}
```
Tasks started from Telegram are skipped here (already reported live). The hook always exits 0, so a Telegram outage never blocks Claude.

## Commands

| | |
|---|---|
| any text | Send to Claude (continues the conversation) |
| `/new` | Fresh conversation |
| `/stop` | Kill the running task |
| `/status` | Idle/working, session, mode |
| `/id` | Show chat ID |

One task runs at a time. Telegram updates are acknowledged before handling, so a crash never replays a command.

## Tests

```bash
python3 -m unittest discover -s tests
```
