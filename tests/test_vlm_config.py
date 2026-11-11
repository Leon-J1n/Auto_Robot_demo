"""Offline checks for VLM connection settings."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from atomic_action.perception.vlm_estimate import VLMPointEstimator
from atomic_action.perception.vlm_estimate import config as vlm_config


class VLMConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "vlm.json"
        self.settings = {
            "api_key": "test-key",
            "base_url": "https://example.test/v1",
            "model": "test-model",
            "timeout_seconds": 12.5,
            "max_retries": 2,
        }
        self.path.write_text(json.dumps(self.settings), encoding="utf-8")
        self.path_patch = patch.object(vlm_config, "VLM_CONFIG_PATH", self.path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def test_estimator_uses_file_settings(self):
        factory = Mock(return_value=object())
        with patch.dict("sys.modules", {"openai": SimpleNamespace(OpenAI=factory)}), \
                patch.dict("os.environ", {"DASHSCOPE_API_KEY": "", "QWEN_MODEL": ""}):
            estimator = VLMPointEstimator()
            self.assertEqual(estimator.model, "test-model")
            estimator._get_client()
        factory.assert_called_once_with(
            api_key="test-key", base_url="https://example.test/v1",
            timeout=12.5, max_retries=2)

    def test_environment_overrides_key_and_model(self):
        factory = Mock(return_value=object())
        with patch.dict("sys.modules", {"openai": SimpleNamespace(OpenAI=factory)}), \
                patch.dict("os.environ", {"DASHSCOPE_API_KEY": "env-key", "QWEN_MODEL": "env-model"}):
            estimator = VLMPointEstimator()
            self.assertEqual(estimator.model, "env-model")
            estimator._get_client()
        self.assertEqual(factory.call_args.kwargs["api_key"], "env-key")

    def test_missing_key_fails_before_network_request(self):
        self.settings["api_key"] = ""
        self.path.write_text(json.dumps(self.settings), encoding="utf-8")
        with patch.dict("os.environ", {"DASHSCOPE_API_KEY": ""}):
            with self.assertRaisesRegex(ValueError, "api_key is missing"):
                VLMPointEstimator()._get_client()

    def test_invalid_config_is_rejected(self):
        self.settings["max_retries"] = -1
        self.path.write_text(json.dumps(self.settings), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "max_retries"):
            vlm_config.load_vlm_config()


if __name__ == "__main__":
    unittest.main()
