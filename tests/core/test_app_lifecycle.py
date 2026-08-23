import json
import os
import tempfile
import unittest

from types import SimpleNamespace
from unittest.mock import patch

from akita_ares.main import ARESApp


class _FakeReticulum:
    instances = []
    exited = False

    def __init__(self, configdir, loglevel):
        self.configdir = configdir
        self.loglevel = loglevel
        self.__class__.instances.append(self)

    @staticmethod
    def transport_enabled():
        return True

    @staticmethod
    def exit_handler():
        _FakeReticulum.exited = True


class _FakeIdentity:
    hexhash = "ab" * 16

    @staticmethod
    def from_file(path):
        return _FakeIdentity() if os.path.exists(path) else None

    def to_file(self, path):
        with open(path, "wb") as identity_file:
            identity_file.write(b"test-private-key")
        return True


class _FakeProxyManager:
    def __init__(self, config, rns_instance, metrics_monitor, identity, path_selector):
        self.config = config
        self.identity = identity
        self.proxy_routes = [{"alias": "default"}]
        self.service_destination = None
        self.shutdown_called = False

    def shutdown(self):
        self.shutdown_called = True

    def periodic_check(self):
        return None


class TestARESAppLifecycle(unittest.TestCase):
    def test_initializes_persistent_identity_and_shuts_down_rns(self):
        with tempfile.TemporaryDirectory() as temp_directory:
            schema_path = os.path.join(temp_directory, "schema.json")
            config_path = os.path.join(temp_directory, "config.json")
            rns_path = os.path.join(temp_directory, "rns")
            identity_path = os.path.join(temp_directory, "identity")
            with open(schema_path, "w", encoding="utf-8") as schema_file:
                json.dump({"type": "object"}, schema_file)
            with open(config_path, "w", encoding="utf-8") as config_file:
                json.dump(
                    {
                        "logging": {"level": "CRITICAL", "file": None, "console_output": False},
                        "ares_core": {
                            "rns_config_path": rns_path,
                            "identity_path": identity_path,
                            "main_loop_sleep_interval": 1,
                        },
                        "monitoring": {"enabled": False},
                        "destination_proxying": {
                            "enabled": True,
                            "is_proxy_node": False,
                            "proxy_routes": [],
                        },
                    },
                    config_file,
                )
            args = SimpleNamespace(config=config_path, schema=schema_path, loglevel=None)
            fake_rns = SimpleNamespace(
                Reticulum=_FakeReticulum,
                Identity=_FakeIdentity,
                LOG_WARNING=2,
            )
            _FakeReticulum.instances.clear()
            _FakeReticulum.exited = False
            with patch("akita_ares.main.RNS", fake_rns), \
                 patch("akita_ares.main.RNS_AVAILABLE", True), \
                 patch("akita_ares.main.proxying.ProxyManager", _FakeProxyManager), \
                 patch("akita_ares.main.signal.signal"):
                app = ARESApp(args)
                self.assertTrue(os.path.exists(identity_path))
                self.assertEqual(os.stat(identity_path).st_mode & 0o777, 0o600)
                self.assertIs(app.proxy_manager.identity, app.rns_identity)
                app.shutdown()
                app.shutdown()
            self.assertTrue(_FakeReticulum.exited)
            self.assertEqual(len(_FakeReticulum.instances), 1)
