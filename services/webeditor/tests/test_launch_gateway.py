import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import launch_gateway


class LaunchGatewayTests(unittest.TestCase):
    def test_route_address(self):
        with patch("launch_gateway.socket.socket") as factory:
            probe = factory.return_value.__enter__.return_value
            probe.getsockname.return_value = ("192.168.10.5", 12345)
            self.assertEqual(launch_gateway.current_ipv4(), "192.168.10.5")
            probe.connect.assert_called_once_with(("8.8.8.8", 80))
            probe.getsockname.return_value = ("127.0.0.1", 12345)
            with self.assertRaises(OSError):
                launch_gateway.current_ipv4()

    def test_temporary_override_preserves_source_and_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "custom config.json"
            original = {"library_server_url": "http://old:8000", "gateway": {"port": 9000},
                        "camera_preview": {"max_hz": 12}, "label": "\u6d4b\u8bd5"}
            source.write_text(json.dumps(original), encoding="utf-8")
            before = source.read_bytes()
            observed = []

            def run(command, **kwargs):
                config_file = Path(command[-1])
                observed.append(config_file)
                actual = json.loads(config_file.read_text(encoding="utf-8"))
                self.assertEqual(actual, {**original, "library_server_url": "http://192.168.10.5:8000"})
                self.assertEqual(command[2:-2], ["--port", "9001", "--keep-state"])
                self.assertEqual(command[-2], "--config")
                return 7

            with patch("launch_gateway.current_ipv4", return_value="192.168.10.5"), \
                    patch("launch_gateway.subprocess.call", side_effect=run):
                self.assertEqual(launch_gateway.main(["--config", str(source), "--port", "9001", "--keep-state"]), 7)
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse(observed[0].exists())

    def test_cleanup_when_gateway_fails_to_start(self):
        observed = []

        def fail(command, **kwargs):
            observed.append(Path(command[-1]))
            raise OSError("failed to spawn")

        with patch("launch_gateway.current_ipv4", return_value="192.168.10.5"), \
                patch("launch_gateway.subprocess.call", side_effect=fail):
            with self.assertRaises(OSError):
                launch_gateway.main([])
        self.assertFalse(observed[0].exists())


if __name__ == "__main__":
    unittest.main()
