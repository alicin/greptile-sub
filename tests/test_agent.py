"""Offline checks for the repo tools and the OpenAI agent loop. No model or GitHub calls."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openai.types.chat import ChatCompletion

from greptile_sub import llm
from greptile_sub.tools import RepoTools


def completion(content=None, tool_calls=None) -> ChatCompletion:
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {"id": f"call_{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
            for i, (n, a) in enumerate(tool_calls)
        ]
    return ChatCompletion.model_validate({
        "id": "x", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "tool_calls" if tool_calls else "stop", "message": message}],
    })


class FakeClient:
    def __init__(self, *responses: ChatCompletion):
        self.responses = list(responses)
        self.requests = []
        self.chat = mock.Mock()
        self.chat.completions.create = self._create

    def _create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs, default=str)))
        return self.responses.pop(0)


class RepoToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "repo"
        (root / "src").mkdir(parents=True)
        (root / ".git").mkdir()
        (root / "src/app.py").write_text("def div(a, b):\n    return a / b\n")
        (root / ".git/config").write_text("secret")
        (root / "blob.bin").write_bytes(b"\0\1\2div")
        self.outside = Path(self.tmp.name) / "outside.txt"
        self.outside.write_text("div TOKEN=hunter2")
        os.symlink(self.outside, root / "src/link.py")
        self.tools = RepoTools(root)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, name, **args):
        return self.tools.call(name, json.dumps(args))

    def test_list_read_grep(self):
        self.assertEqual(self.call("list_files"), "blob.bin\nsrc/app.py")
        self.assertIn("    2|     return a / b", self.call("read_file", path="src/app.py"))
        self.assertEqual(self.call("grep", pattern=r"a / b"), "src/app.py:2: return a / b")
        self.assertEqual(self.call("list_files", pattern="*.py"), "src/app.py")

    def test_cannot_escape_the_checkout(self):
        for args in ({"path": "../outside.txt"}, {"path": "src/link.py"}, {"path": "/etc/hosts"}):
            self.assertIn("outside the repository", self.call("read_file", **args))
        self.assertNotIn("hunter2", self.call("grep", pattern="TOKEN"))
        self.assertNotIn("secret", self.call("grep", pattern="secret"))
        self.assertNotIn("link.py", self.call("list_files"))

    def test_bad_calls_return_errors_not_exceptions(self):
        self.assertTrue(self.call("rm_rf").startswith("error: unknown tool"))
        self.assertTrue(self.call("grep", pattern="(").startswith("error:"))
        self.assertTrue(self.tools.call("read_file", "not json").startswith("error:"))
        self.assertTrue(self.call("read_file", path="src/app.py", bogus=1).startswith("error:"))


class AgentLoopTest(unittest.TestCase):
    SCHEMA = {"type": "object", "properties": {"bug": {"type": "string"}}, "required": ["bug"]}

    def run_agent(self, fake: FakeClient, tools=None, max_steps=5):
        with mock.patch.object(llm, "_client", fake):
            return llm.agent_json(
                instructions="Find the bug.", prompt="PR #1", schema=self.SCHEMA, name="t",
                model="m", tools=tools or mock.Mock(calls=0, call=mock.Mock(return_value="ok")),
                max_steps=max_steps,
            )

    def test_tool_round_trip_then_json(self):
        tools = mock.Mock(calls=2, call=mock.Mock(side_effect=["1| def div", "src/app.py:2: a / b"]))
        fake = FakeClient(
            completion(tool_calls=[("read_file", {"path": "src/app.py"}), ("grep", {"pattern": "a / b"})]),
            completion("Division by zero."),
            completion('{"bug": "div by zero"}'),
        )
        self.assertEqual(self.run_agent(fake, tools), {"bug": "div by zero"})
        second, final = fake.requests[1], fake.requests[2]
        self.assertEqual([m["role"] for m in second["messages"]], ["system", "user", "assistant", "tool", "tool"])
        self.assertEqual(second["messages"][3], {"role": "tool", "tool_call_id": "call_0", "content": "1| def div"})
        self.assertIn("tools", second)
        self.assertNotIn("tools", final)
        self.assertEqual(final["response_format"]["type"], "json_schema")
        self.assertEqual(final["messages"][-2], {"role": "assistant", "content": "Division by zero."})

    def test_step_limit_forces_an_answer(self):
        looping = [completion(tool_calls=[("list_files", {})]) for _ in range(2)]
        fake = FakeClient(*looping, completion('{"bug": "none"}'))
        self.assertEqual(self.run_agent(fake, max_steps=2), {"bug": "none"})
        self.assertIn("Step limit reached", json.dumps(fake.requests[-1]["messages"]))

    def test_bad_final_json_raises(self):
        for bad in ("not json", '{"other": 1}'):
            with self.assertRaises(llm.LLMError):
                self.run_agent(FakeClient(completion("done"), completion(bad)))


if __name__ == "__main__":
    unittest.main()
