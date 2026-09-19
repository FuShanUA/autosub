import os
import sys
import unittest
from unittest.mock import MagicMock, patch


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
COMMON_DIR = os.path.join(os.path.dirname(os.path.dirname(CURRENT_DIR)), "common")
if COMMON_DIR not in sys.path:
    sys.path.insert(0, COMMON_DIR)

from llm_utils import LLMClient


class TestDashscopeThinkingParameter(unittest.TestCase):
    def _make_client(self):
        client = LLMClient.__new__(LLMClient)
        client.limiter = MagicMock()
        return client

    def test_disable_thinking_is_omitted_for_glm_5_3(self):
        client = self._make_client()
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{"message": {"content": "ok"}}]
        }
        session = MagicMock()
        session.post.return_value = response

        with patch("llm_utils.requests.Session", return_value=session):
            result = client._call_openai_compatible(
                "glm-5.3",
                "hello",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "test-key",
                disable_thinking=True,
            )

        self.assertEqual(result, "ok")
        payload = session.post.call_args.kwargs["json"]
        self.assertNotIn("enable_thinking", payload)

    def test_disable_thinking_is_sent_for_glm_5_2(self):
        client = self._make_client()
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{"message": {"content": "ok"}}]
        }
        session = MagicMock()
        session.post.return_value = response

        with patch("llm_utils.requests.Session", return_value=session):
            result = client._call_openai_compatible(
                "glm-5.2",
                "hello",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "test-key",
                disable_thinking=True,
            )

        self.assertEqual(result, "ok")
        payload = session.post.call_args.kwargs["json"]
        self.assertIs(payload.get("enable_thinking"), False)


if __name__ == "__main__":
    unittest.main()
