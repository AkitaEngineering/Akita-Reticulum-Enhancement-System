import unittest

from akita_ares.cli.main_cli import parse_args


class TestMainCli(unittest.TestCase):
    def test_parse_args_defaults_to_start_with_global_options(self):
        args = parse_args(['--loglevel', 'CRITICAL'])
        self.assertEqual(args.command, 'start')
        self.assertEqual(args.loglevel, 'CRITICAL')
        self.assertEqual(args.func.__name__, 'handle_start_command')

    def test_parse_args_preserves_explicit_subcommand(self):
        args = parse_args(['--config', 'config.json', 'status'])
        self.assertEqual(args.command, 'status')
        self.assertEqual(args.config, 'config.json')
        self.assertEqual(args.func.__name__, 'handle_status_command')