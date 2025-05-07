import unittest, os, json, tempfile
from jsonschema import exceptions as jsonschema_exceptions
from akita_ares.core.config_manager import ConfigManager
from akita_ares.core.logger import setup_logging 
setup_logging(level='CRITICAL', console_output=False, log_file=None) 
class TestConfigManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(); self.config_path = os.path.join(self.temp_dir.name, 'test_config.json'); self.schema_path = os.path.join(self.temp_dir.name, 'test_schema.json'); self.invalid_json_path = os.path.join(self.temp_dir.name, 'invalid.json')
        self.valid_config_data = {"logging": {"level": "DEBUG"},"ares_core": {"rns_config_path": "~/.rns_test"}}
        self.valid_schema_data = {"type": "object","properties": {"logging": {"type": "object", "properties": {"level": {"type": "string"}}},"ares_core": {"type": "object", "properties": {"rns_config_path": {"type": "string"}}, "required": ["rns_config_path"]}},"required": ["ares_core"]}
        self.invalid_config_data_for_schema = {"logging": {"level": 123}}
        with open(self.config_path, 'w') as f: json.dump(self.valid_config_data, f)
        with open(self.schema_path, 'w') as f: json.dump(self.valid_schema_data, f)
        with open(self.invalid_json_path, 'w') as f: f.write("{invalid json,")
    def tearDown(self): self.temp_dir.cleanup()
    def test_load_valid_config_no_schema(self): manager = ConfigManager(self.config_path, schema_fp=None); self.assertEqual(manager.get_config(), self.valid_config_data); self.assertIsNone(manager.schema)
    def test_load_valid_config_with_valid_schema(self): manager = ConfigManager(self.config_path, schema_fp=self.schema_path); self.assertEqual(manager.get_config(), self.valid_config_data); self.assertIsNotNone(manager.schema)
    def test_load_missing_config_file(self): missing_path = os.path.join(self.temp_dir.name, 'nonexistent.json'); manager = ConfigManager(missing_path); self.assertEqual(manager.get_config(), {})
    def test_load_invalid_json_config(self): manager = ConfigManager(self.invalid_json_path); self.assertEqual(manager.get_config(), {})
    def test_load_config_violating_schema(self): invalid_config_path = os.path.join(self.temp_dir.name, 'violating_config.json'); with open(invalid_config_path, 'w') as f: json.dump(self.invalid_config_data_for_schema, f); manager = ConfigManager(invalid_config_path, schema_fp=self.schema_path); self.assertEqual(manager.get_config(), {}) 
    def test_load_config_missing_schema_file(self): missing_schema_path = os.path.join(self.temp_dir.name, 'nonexistent_schema.json'); manager = ConfigManager(self.config_path, schema_fp=missing_schema_path); self.assertEqual(manager.get_config(), self.valid_config_data); self.assertIsNone(manager.schema)
    def test_get_section(self): manager = ConfigManager(self.config_path); self.assertEqual(manager.get_section("logging"), self.valid_config_data["logging"]); self.assertEqual(manager.get_section("ares_core"), self.valid_config_data["ares_core"]); self.assertEqual(manager.get_section("nonexistent"), {}); self.assertEqual(manager.get_section("nonexistent", default="default_val"), "default_val")
    def test_reload_config(self): manager = ConfigManager(self.config_path, schema_fp=self.schema_path); new_config_data = {"logging": {"level": "WARNING"}, "ares_core": {"rns_config_path": "/etc/rns"}}; with open(self.config_path, 'w') as f: json.dump(new_config_data, f); reloaded_config = manager.reload_config(); self.assertEqual(reloaded_config, new_config_data); self.assertEqual(manager.get_config(), new_config_data)
    def test_reload_config_becomes_invalid(self): manager = ConfigManager(self.config_path, schema_fp=self.schema_path); original_config = manager.get_config().copy(); with open(self.config_path, 'w') as f: json.dump(self.invalid_config_data_for_schema, f); reloaded_config = manager.reload_config(); self.assertEqual(manager.get_config(), original_config); self.assertEqual(reloaded_config, original_config)
    def test_reload_config_becomes_malformed(self): manager = ConfigManager(self.config_path, schema_fp=self.schema_path); original_config = manager.get_config().copy(); with open(self.config_path, 'w') as f: f.write("{malformed"); reloaded_config = manager.reload_config(); self.assertEqual(manager.get_config(), original_config); self.assertEqual(reloaded_config, original_config)
    def test_init_without_validate_on_load(self): invalid_config_path = os.path.join(self.temp_dir.name, 'violating_config2.json'); with open(invalid_config_path, 'w') as f: json.dump(self.invalid_config_data_for_schema, f); manager = ConfigManager(invalid_config_path, schema_fp=self.schema_path, validate_on_load=False); self.assertEqual(manager.get_config(), self.invalid_config_data_for_schema); self.assertRaises(jsonschema_exceptions.ValidationError, manager.validate_config_schema, manager.get_config())
