"""Tests for autotmux.config — TOML daemon tunables with safe fallbacks."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import config


class LoadConfigTests(unittest.TestCase):
    def setUp(self):
        self._saved_path = config.CONFIG_PATH

    def tearDown(self):
        config.CONFIG_PATH = self._saved_path

    def _write(self, td, text):
        p = os.path.join(td, 'config.toml')
        with open(p, 'w') as f:
            f.write(text)
        config.CONFIG_PATH = p
        return p

    def test_missing_file_returns_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            config.CONFIG_PATH = os.path.join(td, 'nope.toml')
            self.assertEqual(config.load(), config.DEFAULTS)

    def test_daemon_table_overrides(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, '[daemon]\nsqueue_interval = 60\n')
            cfg = config.load()
            self.assertEqual(cfg['squeue_interval'], 60)
            self.assertEqual(cfg['health_interval'],
                             config.DEFAULTS['health_interval'])

    def test_flat_layout_overrides(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, 'connect_timeout = 15\n')
            self.assertEqual(config.load()['connect_timeout'], 15)

    def test_unknown_key_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, '[daemon]\nbogus = 5\n')
            cfg = config.load()
            self.assertNotIn('bogus', cfg)
            self.assertEqual(cfg, config.DEFAULTS)

    def test_bool_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, '[daemon]\nsqueue_interval = true\n')
            self.assertEqual(config.load()['squeue_interval'],
                             config.DEFAULTS['squeue_interval'])

    def test_fractional_integer_setting_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, '[daemon]\nconnect_timeout = 1.5\n')
            self.assertEqual(config.load()['connect_timeout'],
                             config.DEFAULTS['connect_timeout'])

    def test_malformed_file_returns_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, 'this is = not valid toml [[[')
            self.assertEqual(config.load(), config.DEFAULTS)

    def test_load_returns_a_copy(self):
        cfg = config.load()
        cfg['squeue_interval'] = 999
        self.assertNotEqual(config.DEFAULTS['squeue_interval'], 999)

    def test_nonpositive_and_nonfinite_values_fall_back(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, '[daemon]\nsqueue_interval = -1\n'
                            'connect_timeout = nan\nbackoff_base = inf\n')
            cfg = config.load()
            self.assertEqual(cfg['squeue_interval'],
                             config.DEFAULTS['squeue_interval'])
            self.assertEqual(cfg['connect_timeout'],
                             config.DEFAULTS['connect_timeout'])
            self.assertEqual(cfg['backoff_base'],
                             config.DEFAULTS['backoff_base'])

    def test_non_table_daemon_section_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, 'daemon = 3\n')
            self.assertEqual(config.load(), config.DEFAULTS)

    def test_backoff_cap_cannot_be_below_base(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, '[daemon]\nbackoff_base = 30\nbackoff_cap = 5\n')
            cfg = config.load()
            self.assertEqual(cfg['backoff_cap'], cfg['backoff_base'])


class LoadClientConfigTests(unittest.TestCase):
    def setUp(self):
        self._saved_path = config.CONFIG_PATH

    def tearDown(self):
        config.CONFIG_PATH = self._saved_path

    def _write(self, td, text):
        path = os.path.join(td, 'config.toml')
        with open(path, 'w') as handle:
            handle.write(text)
        config.CONFIG_PATH = path
        return path

    def test_client_only_file_does_not_change_daemon_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, '[client]\ngateways = ["login1"]\n')
            self.assertEqual(config.load(), config.DEFAULTS)

    def test_gateway_client_values_are_validated(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(
                td,
                '[client]\n'
                'mode = "gateway"\n'
                'gateways = ["me@login1", "login2", "login2", "-oProxy=x"]\n'
                'connect_timeout = 7\n'
                'state_timeout = 4.5\n'
                'agent_command = ["python3", "-m", "autotmux.agent"]\n')
            cfg = config.load_client()
            self.assertEqual(cfg['mode'], 'gateway')
            self.assertEqual(cfg['gateways'], ['me@login1', 'login2'])
            self.assertEqual(cfg['connect_timeout'], 7)
            self.assertEqual(cfg['state_timeout'], 4.5)
            self.assertEqual(
                cfg['agent_command'], ['python3', '-m', 'autotmux.agent'])

    def test_invalid_client_values_fall_back_safely(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(
                td,
                '[client]\nmode = "recursive"\n'
                'gateways = "login1"\nhedge_delay = -1\n'
                'agent_command = ["-bad"]\n')
            cfg = config.load_client()
            self.assertEqual(cfg['mode'], config.CLIENT_DEFAULTS['mode'])
            self.assertEqual(cfg['gateways'], [])
            self.assertEqual(
                cfg['hedge_delay'], config.CLIENT_DEFAULTS['hedge_delay'])
            self.assertEqual(
                cfg['agent_command'], config.CLIENT_DEFAULTS['agent_command'])

    def test_load_client_returns_deep_enough_copies(self):
        with tempfile.TemporaryDirectory() as td:
            config.CONFIG_PATH = os.path.join(td, 'missing.toml')
            cfg = config.load_client()
            cfg['gateways'].append('login1')
            cfg['agent_command'].append('rpc')
            self.assertEqual(config.CLIENT_DEFAULTS['gateways'], [])
            self.assertEqual(config.CLIENT_DEFAULTS['agent_command'], ['atmux-agent'])

    def test_gateway_validator_rejects_options_and_shell_fragments(self):
        self.assertTrue(config.valid_gateway('user@login.example.edu'))
        self.assertTrue(config.valid_gateway('ssh-alias'))
        for value in ('-v', 'host;id', 'host name', '', None):
            self.assertFalse(config.valid_gateway(value))


if __name__ == '__main__':
    unittest.main()
