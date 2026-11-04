import json, os, sys, tempfile, time, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import sir_hendrick as sh  # noqa: E402


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))

    def typing(self, chat_id):
        pass


def make_cfg(tmp, chat_id=42, **kw):
    base = dict(token="t", chat_id=chat_id, claude_bin=str(HERE / "fake_claude.py"), workdir=Path(tmp),
                permission_mode="acceptEdits", allowed_tools="", model="", timeout=60,
                state_file=Path(tmp) / "state.json")
    base.update(kw)
    return sh.Config(**base)


def upd(chat_id, text, uid=1):
    return {"update_id": uid, "message": {"chat": {"id": chat_id}, "text": text}}


class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_cfg(self.tmp)
        self.tg = FakeTelegram()
        self.state = sh.State(self.cfg.state_file)
        self.bot = sh.Bot(self.cfg, self.tg, sh.ClaudeRunner(self.cfg), self.state)

    def run_prompt(self, text):
        self.bot._run_claude(42, text)  # synchronous for tests
        return "\n".join(t for _, t in self.tg.sent)

    def test_unauthorized_chat_ignored(self):
        self.bot.handle(upd(999, "rm -rf /"))
        self.assertEqual(self.tg.sent, [])

    def test_setup_mode_reveals_only_chat_id(self):
        bot = sh.Bot(make_cfg(self.tmp, chat_id=None), self.tg, sh.ClaudeRunner(self.cfg), self.state)
        bot.handle(upd(777, "hello"))
        self.assertIn("777", self.tg.sent[0][1])
        self.assertEqual(len(self.tg.sent), 1)

    def test_full_run_reports_activity_and_result(self):
        out = self.run_prompt("list files")
        self.assertIn("💭 Let me check.", out)
        self.assertIn("🖥 $ ls -la", out)
        self.assertIn("⚠️ Tool error: denied", out)
        self.assertIn("✅ Done: list files", out)
        self.assertIn("$0.0123", out)
        self.assertEqual(out.count("Done: list files"), 1)  # final text not duplicated
        self.assertEqual(self.state.get("session_id"), "sess-123")

    def test_resume_passes_session(self):
        self.state.set("session_id", "abc")
        cmd = sh.ClaudeRunner(self.cfg).build_cmd("hi", "abc")
        self.assertEqual(cmd[cmd.index("--resume") + 1], "abc")
        self.assertIn("acceptEdits", cmd)

    def test_failed_resume_clears_session(self):
        self.state.set("session_id", "old")
        out = self.run_prompt("fail")
        self.assertIn("exited with code 1", out)
        self.assertIsNone(self.state.get("session_id"))

    def test_new_and_status_commands(self):
        self.state.set("session_id", "abc")
        self.bot.handle(upd(42, "/new"))
        self.assertIsNone(self.state.get("session_id"))
        self.bot.handle(upd(42, "/status@SirHendrickBot"))
        self.assertIn("Idle.", self.tg.sent[-1][1])

    def test_child_env_strips_token(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = "secret"
        try:
            out = self.run_prompt("x")
        finally:
            del os.environ["TELEGRAM_BOT_TOKEN"]
        self.assertNotIn("secret", out)


class HelperTests(unittest.TestCase):
    def test_split_text_respects_limit(self):
        text = ("a" * 50 + "\n") * 200 + "b" * 9000
        chunks = sh.split_text(text, 1000)
        self.assertTrue(all(len(c) <= 1000 for c in chunks))
        self.assertEqual("".join(chunks), text)

    def test_reporter_batches(self):
        sent = []
        r = sh.Reporter(sent.append, interval=60)
        r.add("one"); r.add("two"); r.add("three")
        r.flush()
        self.assertEqual(sent, ["one", "two\nthree"])

    def test_format_hook(self):
        self.assertEqual(sh.format_hook({"hook_event_name": "PostToolUse", "cwd": "/x/proj",
                                         "tool_name": "Bash", "tool_input": {"command": "pwd"}}),
                         "[proj] 🖥 $ pwd")
        self.assertIsNone(sh.format_hook({"hook_event_name": "PreCompact"}))

    def test_env_file_does_not_override(self):
        p = Path(tempfile.mkdtemp()) / ".env"
        p.write_text("SH_TEST_A='x'\nexport SH_TEST_B=y\n# c\n")
        os.environ["SH_TEST_A"] = "keep"
        sh.load_env_file(p)
        self.assertEqual(os.environ["SH_TEST_A"], "keep")
        self.assertEqual(os.environ["SH_TEST_B"], "y")


if __name__ == "__main__":
    unittest.main()
