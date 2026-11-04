#!/usr/bin/env python3
"""Stand-in for the Claude CLI: emits a canned stream-json run and echoes argv."""
import json, sys
args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
if prompt == "fail":
    print("boom: no such session", file=sys.stderr); sys.exit(1)
events = [
    {"type": "system", "subtype": "init", "session_id": "sess-123"},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Let me check."},
                                                  {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "denied"}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done: " + prompt}]}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "Done: " + prompt,
     "session_id": "sess-123", "num_turns": 2, "duration_ms": 1500, "total_cost_usd": 0.0123,
     "argv": args},
]
for e in events:
    print(json.dumps(e), flush=True)
