import os
import sys
import time
import tempfile
import unittest
from unittest.mock import MagicMock, patch


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
COMMON_DIR = os.path.join(os.path.dirname(os.path.dirname(CURRENT_DIR)), "common")
if COMMON_DIR not in sys.path:
    sys.path.insert(0, COMMON_DIR)

from llm_utils import LLMClient
from llm_utils import LLMProvider


class TestLLMFallbackConfig(unittest.TestCase):
    def test_custom_settings_override_defaults_and_parse_models(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = os.path.join(temp_dir, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as handle:
                handle.write(
                    '{"llm_fallback_mode":"cross_provider",'
                    '"llm_fallback_models":"openai::gpt-4o, glm-5.2"}'
                )

            env = {"AUTOSUB_SETTINGS_PATH": settings_path}
            with patch.dict(os.environ, env, clear=True):
                mode, models = LLMClient._load_llm_fallback_config()

        self.assertEqual(mode, "cross_provider")
        self.assertEqual(models, ["openai::gpt-4o", "glm-5.2"])

    def test_invalid_mode_falls_back_to_same_provider(self):
        self.assertEqual(
            LLMClient._normalize_fallback_mode("unexpected"),
            "same_provider",
        )


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

    def test_429_ignores_retired_gateway_configuration(self):
        client = self._make_client()
        primary_error = Exception("API Error (Status 429): Too Many Requests")
        client._call_openai_compatible = MagicMock(side_effect=primary_error)
        env = {
            "DASHSCOPE_FALLBACK_BASE_URL": (
                "http://ai-gateway.example.net.cn/aliyun-bailian/v1/chat/completions"
            ),
            "DASHSCOPE_FALLBACK_API_KEY": "fallback-key",
            "DASHSCOPE_FALLBACK_MODEL": "glm-5.3",
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
        first_call = client._call_openai_compatible.call_args_list[0]
        self.assertEqual(
            first_call.args[2],
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

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


class TestGenerateContentRetryLimit(unittest.TestCase):
    def test_explicit_retry_limit_is_respected_for_timeouts(self):
        client = LLMClient.__new__(LLMClient)
        client.api_keys = {LLMProvider.DASHSCOPE: "test-key"}
        client.limiter = MagicMock()
        client.ordered_configs = []
        client._execute_provider_call = MagicMock(
            side_effect=Exception("HTTPSConnectionPool read timed out")
        )

        with self.assertRaises(Exception):
            client.generate_content(
                "hello",
                model_name="glm-5.3",
                provider=LLMProvider.DASHSCOPE,
                max_retries=1,
            )

        self.assertEqual(client._execute_provider_call.call_count, 1)


class TestGenerateContentFallbackTargets(unittest.TestCase):
    def _make_client(self, *, mode="same_provider", models=None):
        client = LLMClient.__new__(LLMClient)
        client.api_keys = {
            LLMProvider.DASHSCOPE: "dashscope-key",
            LLMProvider.OPENAI: "openai-key",
        }
        client.limiter = MagicMock()
        client.ordered_configs = []
        client.fallback_mode = mode
        client.fallback_models = models
        client._execute_provider_call = MagicMock(
            side_effect=Exception("API Error (Status 500): failed")
        )
        return client

    def _called_models(self, client):
        return [call.args[1] for call in client._execute_provider_call.call_args_list]

    def test_default_dashscope_chain_is_preserved(self):
        client = self._make_client()
        client.ordered_configs = [
            (LLMProvider.OPENAI, "openai-key", "gpt-4o")
        ]

        with self.assertRaises(Exception):
            client.generate_content(
                "hello",
                model_name="glm-5.3",
                provider=LLMProvider.DASHSCOPE,
                fallback=True,
                max_retries=1,
            )

        self.assertEqual(
            self._called_models(client),
            ["glm-5.3", "qwen3.7-max", "kimi-k2.6", "deepseek-v4-pro"],
        )

    def test_explicit_fallback_models_replace_dashscope_chain(self):
        client = self._make_client(models=["glm-5.2"])
        client._execute_provider_call.side_effect = [
            Exception("API Error (Status 500): failed"),
            "ok",
        ]

        result = client.generate_content(
            "hello",
            model_name="glm-5.3",
            provider=LLMProvider.DASHSCOPE,
            fallback=True,
            max_retries=1,
        )

        self.assertEqual(result, "ok")
        self.assertEqual(self._called_models(client), ["glm-5.3", "glm-5.2"])

    def test_same_provider_mode_does_not_cross_providers(self):
        client = self._make_client()
        client.ordered_configs = [
            (LLMProvider.OPENAI, "openai-key", "gpt-4o")
        ]

        with self.assertRaises(Exception):
            client.generate_content(
                "hello",
                model_name="gpt-4o",
                provider=LLMProvider.OPENAI,
                fallback=True,
                max_retries=1,
            )

        self.assertEqual(self._called_models(client), ["gpt-4o"])

    def test_cross_provider_mode_uses_ordered_configs(self):
        client = self._make_client(mode="cross_provider")
        client.ordered_configs = [
            (LLMProvider.DASHSCOPE, "dashscope-key", "glm-5.3")
        ]
        client._execute_provider_call.side_effect = [
            Exception("API Error (Status 500): failed"),
            "ok",
        ]

        result = client.generate_content(
            "hello",
            model_name="gpt-4o",
            provider=LLMProvider.OPENAI,
            fallback=True,
            max_retries=1,
        )

        self.assertEqual(result, "ok")
        self.assertEqual(self._called_models(client), ["gpt-4o", "glm-5.3"])

    def test_cross_provider_models_can_be_qualified_with_provider(self):
        client = self._make_client(
            mode="cross_provider",
            models=["openai::gpt-4o"],
        )
        client._execute_provider_call.side_effect = [
            Exception("API Error (Status 500): failed"),
            "ok",
        ]

        result = client.generate_content(
            "hello",
            model_name="glm-5.3",
            provider=LLMProvider.DASHSCOPE,
            fallback=True,
            max_retries=1,
        )

        self.assertEqual(result, "ok")
        self.assertEqual(
            [call.args[0] for call in client._execute_provider_call.call_args_list],
            [LLMProvider.DASHSCOPE, LLMProvider.OPENAI],
        )

    def test_off_mode_only_uses_primary_model(self):
        client = self._make_client(
            mode="off",
            models=["qwen3.7-max", "kimi-k2.6"],
        )

        with self.assertRaises(Exception):
            client.generate_content(
                "hello",
                model_name="glm-5.3",
                provider=LLMProvider.DASHSCOPE,
                fallback=True,
                max_retries=1,
            )

        self.assertEqual(self._called_models(client), ["glm-5.3"])

    def test_fallback_false_is_strict_even_when_fallback_is_configured(self):
        client = self._make_client(
            mode="cross_provider",
            models=["qwen3.7-max"],
        )
        client.ordered_configs = [
            (LLMProvider.OPENAI, "openai-key", "gpt-4o")
        ]

        with self.assertRaises(Exception):
            client.generate_content(
                "hello",
                model_name="glm-5.3",
                provider=LLMProvider.DASHSCOPE,
                fallback=False,
                max_retries=1,
            )

        self.assertEqual(self._called_models(client), ["glm-5.3"])


class TestGenerateContentEnvironmentDefaults(unittest.TestCase):
    def test_request_timeout_and_retry_defaults_come_from_environment(self):
        client = LLMClient.__new__(LLMClient)
        client.api_keys = {LLMProvider.DASHSCOPE: "test-key"}
        client.limiter = MagicMock()
        client.ordered_configs = []
        client._execute_provider_call = MagicMock(
            side_effect=Exception("HTTPSConnectionPool read timed out")
        )

        env = {"LLM_REQUEST_TIMEOUT": "123", "LLM_MAX_RETRIES": "1"}
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(Exception):
                client.generate_content(
                    "hello",
                    model_name="glm-5.3",
                    provider=LLMProvider.DASHSCOPE,
                )

        self.assertEqual(client._execute_provider_call.call_count, 1)
        self.assertEqual(
            client._execute_provider_call.call_args.kwargs["request_timeout"],
            123,
        )


class TestGenerateBatchProgressCallback(unittest.TestCase):
    def test_progress_callback_reports_completed_batches(self):
        client = LLMClient.__new__(LLMClient)
        client.max_workers = 1
        client.limiter = MagicMock()
        client.generate_content = MagicMock(return_value="ok")
        tasks = [
            {"index": 0, "prompt": "one"},
            {"index": 1, "prompt": "two"},
        ]
        progress = []

        with patch.dict(os.environ, {"LLM_BATCH_TIMEOUT": "1"}, clear=False):
            results = client.generate_batch(
                tasks,
                progress_callback=lambda done, total: progress.append((done, total)),
            )

        self.assertEqual(len(results), 2)
        self.assertEqual(progress, [(1, 2), (2, 2)])

    def test_request_limits_are_forwarded_to_generate_content(self):
        client = LLMClient.__new__(LLMClient)
        client.max_workers = 1
        client.limiter = MagicMock()
        client.generate_content = MagicMock(return_value="ok")

        results = client.generate_batch(
            [{"index": 0, "prompt": "one"}],
            request_timeout=123,
            max_retries=1,
            batch_timeout=1,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(
            client.generate_content.call_args.kwargs["request_timeout"],
            123,
        )
        self.assertEqual(
            client.generate_content.call_args.kwargs["max_retries"],
            1,
        )

    def test_batch_defaults_to_one_attempt(self):
        client = LLMClient.__new__(LLMClient)
        client.max_workers = 1
        client.limiter = MagicMock()
        client.generate_content = MagicMock(return_value="ok")

        client.generate_batch(
            [{"index": 0, "prompt": "one"}],
            batch_timeout=1,
        )

        self.assertEqual(
            client.generate_content.call_args.kwargs["max_retries"],
            1,
        )

    def test_batch_timeout_marks_pending_chunks_failed(self):
        client = LLMClient.__new__(LLMClient)
        client.max_workers = 1
        client.limiter = MagicMock()

        def slow_call(*_args, **_kwargs):
            time.sleep(0.2)
            return "ok"

        client.generate_content = MagicMock(side_effect=slow_call)

        results = client.generate_batch(
            [
                {"chunk_idx": 0, "prompt": "one"},
                {"chunk_idx": 1, "prompt": "two"},
            ],
            batch_timeout=0.01,
        )

        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["result"] is None for result in results))
        self.assertTrue(all("batch_timeout" in result["error"] for result in results))


if __name__ == "__main__":
    unittest.main()
