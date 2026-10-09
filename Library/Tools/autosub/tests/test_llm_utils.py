import os
import sys
import unittest
from unittest.mock import MagicMock, patch


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
COMMON_DIR = os.path.join(os.path.dirname(os.path.dirname(CURRENT_DIR)), "common")
if COMMON_DIR not in sys.path:
    sys.path.insert(0, COMMON_DIR)

from llm_utils import LLMClient
from llm_utils import LLMProvider


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


class TestDashscopeGatewayFallback(unittest.TestCase):
    def _make_client(self):
        client = LLMClient.__new__(LLMClient)
        client.limiter = MagicMock()
        return client

    def test_429_uses_configured_gateway_and_key(self):
        client = self._make_client()
        client._call_openai_compatible = MagicMock(
            side_effect=[
                Exception("API Error (Status 429): Too Many Requests"),
                "fallback-ok",
            ]
        )
        env = {
            "DASHSCOPE_FALLBACK_BASE_URL": (
                "http://ai-gateway.example.net.cn/aliyun-bailian/v1/chat/completions"
            ),
            "DASHSCOPE_FALLBACK_API_KEY": "fallback-key",
            "DASHSCOPE_FALLBACK_MODEL": "glm-5.3",
        }

        with patch.dict(os.environ, env, clear=False):
            result = client._execute_provider_call(
                LLMProvider.DASHSCOPE,
                "glm-5.3",
                "hello",
                "primary-key",
            )

        self.assertEqual(result, "fallback-ok")
        self.assertEqual(client._call_openai_compatible.call_count, 2)
        first_call = client._call_openai_compatible.call_args_list[0]
        second_call = client._call_openai_compatible.call_args_list[1]
        self.assertEqual(
            first_call.args[2],
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        self.assertEqual(
            second_call.args[2],
            "http://ai-gateway.example.net.cn/aliyun-bailian/v1",
        )
        self.assertEqual(second_call.args[0], "glm-5.3")
        self.assertEqual(second_call.args[3], "fallback-key")

    def test_gateway_is_not_used_without_configuration(self):
        client = self._make_client()
        primary_error = Exception("API Error (Status 429): Too Many Requests")
        client._call_openai_compatible = MagicMock(side_effect=primary_error)
        env = {
            "DASHSCOPE_FALLBACK_BASE_URL": "",
            "DASHSCOPE_FALLBACK_API_KEY": "",
            "DASHSCOPE_FALLBACK_MODEL": "",
        }

        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(Exception) as ctx:
                client._execute_provider_call(
                    LLMProvider.DASHSCOPE,
                    "glm-5.3",
                    "hello",
                    "primary-key",
                )

        self.assertIn("Status 429", str(ctx.exception))
        self.assertEqual(client._call_openai_compatible.call_count, 1)

    def test_non_transient_error_stays_on_primary_gateway(self):
        client = self._make_client()
        primary_error = Exception("API Error (Status 400): invalid request")
        client._call_openai_compatible = MagicMock(side_effect=primary_error)
        env = {
            "DASHSCOPE_FALLBACK_BASE_URL": (
                "http://ai-gateway.example.net.cn/aliyun-bailian/v1"
            ),
            "DASHSCOPE_FALLBACK_API_KEY": "fallback-key",
        }

        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(Exception) as ctx:
                client._execute_provider_call(
                    LLMProvider.DASHSCOPE,
                    "glm-5.3",
                    "hello",
                    "primary-key",
                )

        self.assertIn("Status 400", str(ctx.exception))
        self.assertEqual(client._call_openai_compatible.call_count, 1)


if __name__ == "__main__":
    unittest.main()
