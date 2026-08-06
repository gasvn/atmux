"""Tests for autotmux.config — TOML daemon tunables with safe fallbacks."""
import os
import sys
import tempfile
import unittest
from unittest import mock

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
        self._saved_state_path = config.CLIENT_STATE_PATH
        self._state_dir = tempfile.TemporaryDirectory()
        config.CLIENT_STATE_PATH = os.path.join(
            self._state_dir.name, 'connections.json')

    def tearDown(self):
        config.CONFIG_PATH = self._saved_path
        config.CLIENT_STATE_PATH = self._saved_state_path
        self._state_dir.cleanup()

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

    def test_tui_selection_works_without_toml_and_overrides_legacy_client(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, '[client]\ngateways = ["old-login"]\n')
            config.save_client_state(
                'gateway', ['login2', 'login1'],
                ['/remote/bin/atmux-agent'])
            cfg = config.load_client()
        self.assertTrue(config.client_state_exists())
        self.assertEqual(cfg['mode'], 'gateway')
        self.assertEqual(cfg['gateways'], ['login2', 'login1'])
        self.assertEqual(
            cfg['agent_command'], ['/remote/bin/atmux-agent'])
        self.assertEqual(os.stat(config.CLIENT_STATE_PATH).st_mode & 0o777, 0o600)

    def test_tui_can_remember_native_mode_without_a_gateway(self):
        with tempfile.TemporaryDirectory() as td:
            config.CONFIG_PATH = os.path.join(td, 'missing.toml')
            config.save_client_state('login', [], ['atmux-agent'])
            cfg = config.load_client()
        self.assertEqual(cfg['mode'], 'login')
        self.assertEqual(cfg['gateways'], [])

    def test_ssh_alias_discovery_follows_include_and_skips_patterns(self):
        with tempfile.TemporaryDirectory() as td:
            included = os.path.join(td, 'cluster.conf')
            with open(included, 'w') as handle:
                handle.write('Host login2 login3\n  User me\n')
            root = os.path.join(td, 'config')
            with open(root, 'w') as handle:
                handle.write(
                    'Host * !blocked wildcard-*\n  ServerAliveInterval 10\n'
                    'Host login1 login2\n  HostName example\n'
                    'Include cluster.conf\n')
            aliases = config.discover_ssh_aliases(root)
        self.assertEqual(aliases, ['login1', 'login2', 'login3'])


class SessionNoteTests(unittest.TestCase):
    """Notes answer "which run is this?" for session names chosen for typing."""

    def _path(self, td):
        return os.path.join(td, 'notes.json')

    def test_a_note_round_trips(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._path(td)
            self.assertTrue(config.save_note('tu_debug', 'hb047 sweep', path))
            self.assertEqual(config.load_notes(path), {'tu_debug': 'hb047 sweep'})

    def test_an_empty_note_clears_it(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._path(td)
            config.save_note('tu_debug', 'sweep', path)
            config.save_note('tu_debug', '   ', path)
            self.assertEqual(config.load_notes(path), {})

    def test_notes_are_keyed_by_session_not_node(self):
        """A renewed batch job comes back on whatever node Slurm had free; a
        note tied to the old node would vanish while the run continues."""
        with tempfile.TemporaryDirectory() as td:
            path = self._path(td)
            config.save_note('train', 'sweep A', path)
            self.assertIn('train', config.load_notes(path))

    def test_control_characters_and_newlines_cannot_reach_a_cell(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._path(td)
            config.save_note('s', 'a\nb\tc\x1b[31md\x00', path)
            self.assertEqual(config.load_notes(path)['s'], 'a b c [31md')

    def test_a_long_note_is_truncated(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._path(td)
            config.save_note('s', 'x' * 500, path)
            self.assertEqual(len(config.load_notes(path)['s']),
                             config.NOTE_LIMIT)

    def test_a_hand_edited_file_cannot_break_the_dashboard(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._path(td)
            for content in ('not json', '[]', '{"s": 42}', '{"": "x"}',
                            '{"s": null}'):
                with open(path, 'w', encoding='utf-8') as handle:
                    handle.write(content)
                notes = config.load_notes(path)
                self.assertIsInstance(notes, dict)
                self.assertNotIn(42, notes.values())

    def test_a_missing_file_is_simply_no_notes(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(config.load_notes(self._path(td)), {})

    def test_an_unwritable_location_reports_failure_without_raising(self):
        # A note is a convenience; failing to store one must never propagate
        # into the refresh path that draws the table.
        self.assertFalse(config.save_note('s', 'x', '/proc/nope/notes.json'))

    def test_a_blank_session_name_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(config.save_note('', 'x', self._path(td)))


class ClusterDefinitionTests(unittest.TestCase):
    """`gateways` is one cluster's interchangeable entry points; `clusters`
    adds independent places. Collapsing them into one list is the bug this
    shape exists to prevent."""

    def test_no_clusters_means_just_the_primary(self):
        self.assertEqual(
            config.client_clusters({'gateways': ['k6', 'k7']}),
            [(config.PRIMARY_CLUSTER, ('k6', 'k7'))])

    def test_extra_clusters_follow_the_primary_in_order(self):
        groups = config.client_clusters({
            'gateways': ['k6'],
            'clusters': {'lab': ['ws'], 'other': ['ol1', 'ol2']},
        })
        self.assertEqual(groups, [
            (config.PRIMARY_CLUSTER, ('k6',)),
            ('lab', ('ws',)),
            ('other', ('ol1', 'ol2')),
        ])

    def test_a_standalone_machine_is_just_a_cluster_of_one(self):
        groups = config.client_clusters(
            {'gateways': ['k6'], 'clusters': {'vps': ['my-vps']}})
        self.assertEqual(groups[1], ('vps', ('my-vps',)))

    def test_no_gateways_at_all_means_no_groups(self):
        self.assertEqual(config.client_clusters({'gateways': []}), [])
        self.assertEqual(config.client_clusters({}), [])
        self.assertEqual(config.client_clusters(None), [])

    def test_one_bad_cluster_never_costs_the_good_ones(self):
        cleaned = config.clean_clusters({
            'lab': ['ws'],
            'bad name': ['x'],          # invalid cluster name
            'empty': [],                # nothing usable
            'wrong': 'not-a-list',
            'hosts': ['-oProxyCommand=evil', 'good'],
        })
        self.assertEqual(cleaned['lab'], ['ws'])
        self.assertEqual(cleaned['hosts'], ['good'])
        self.assertNotIn('bad name', cleaned)
        self.assertNotIn('empty', cleaned)
        self.assertNotIn('wrong', cleaned)

    def test_a_cluster_cannot_take_the_primary_name(self):
        """It would shadow `gateways` and silently drop that whole cluster."""
        cleaned = config.clean_clusters(
            {config.PRIMARY_CLUSTER: ['x'], 'lab': ['ws']},
            exclude=(config.PRIMARY_CLUSTER,))
        self.assertEqual(list(cleaned), ['lab'])

    def test_cluster_definitions_are_bounded(self):
        many = {f'c{i}': [f'h{i}'] for i in range(200)}
        self.assertLessEqual(len(config.clean_clusters(many)),
                             config.CLUSTERS_MAX)
        wide = {'c': [f'h{i}' for i in range(200)]}
        self.assertLessEqual(len(config.clean_clusters(wide)['c']),
                             config.CLUSTER_GATEWAYS_MAX)

    def test_junk_is_ignored_rather_than_raising(self):
        for value in (None, [], 'text', 42):
            with self.subTest(value=value):
                self.assertEqual(config.clean_clusters(value), {})

    def test_the_connection_dialog_cannot_delete_configured_clusters(self):
        """It only edits the primary group. Rewriting the file without the
        others would silently destroy them."""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'connections.json')
            with mock.patch.object(config, 'CLIENT_STATE_PATH', path):
                config.save_client_state(
                    'gateway', ['k6'], ['atmux-agent'],
                    clusters={'lab': ['ws']})
                config.save_client_state('gateway', ['k6', 'k7'],
                                         ['atmux-agent'])
                state = config._read_client_state()
        self.assertEqual(state['gateways'], ['k6', 'k7'])
        self.assertEqual(state['clusters'], {'lab': ['ws']})


class LayoutPreferenceTests(unittest.TestCase):
    """Which panes are on screen, remembered between runs."""

    def _path(self, td):
        return os.path.join(td, 'layout.json')

    def test_the_cycle_visits_every_mode_once_and_wraps(self):
        mode = config.LAYOUT_DEFAULT
        seen = [mode]
        for _ in config.LAYOUT_MODES:
            mode = config.next_layout(mode)
            seen.append(mode)
        self.assertEqual(seen[:-1], list(config.LAYOUT_MODES))
        self.assertEqual(seen[-1], config.LAYOUT_DEFAULT)

    def test_an_unknown_mode_resolves_to_the_default_not_the_end(self):
        """A name from a hand-edited file, or from a newer release, must not
        make the key look dead on its first press."""
        for value in ('nonsense', '', None, 42, []):
            with self.subTest(value=value):
                self.assertEqual(config.next_layout(value),
                                 config.LAYOUT_DEFAULT)

    def test_a_mode_round_trips(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._path(td)
            self.assertTrue(config.save_layout('jobs', path))
            self.assertEqual(config.load_layout(path), 'jobs')

    def test_a_missing_file_is_the_default(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(config.load_layout(self._path(td)),
                             config.LAYOUT_DEFAULT)

    def test_a_hand_edited_file_cannot_blank_the_screen(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._path(td)
            for content in ('not json', '[]', '{}', '{"mode": "nope"}',
                            '{"mode": null}', '{"mode": 3}'):
                with open(path, 'w', encoding='utf-8') as handle:
                    handle.write(content)
                with self.subTest(content=content):
                    self.assertEqual(config.load_layout(path),
                                     config.LAYOUT_DEFAULT)

    def test_an_oversized_file_is_ignored_rather_than_read(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._path(td)
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write(' ' * (config._LAYOUT_FILE_LIMIT + 10))
            self.assertEqual(config.load_layout(path), config.LAYOUT_DEFAULT)

    def test_an_unknown_mode_is_never_written(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._path(td)
            self.assertFalse(config.save_layout('nonsense', path))
            self.assertFalse(os.path.exists(path))

    def test_an_unwritable_location_reports_failure_without_raising(self):
        self.assertFalse(config.save_layout('wide', '/proc/nope/layout.json'))

    def test_a_failed_write_leaves_the_previous_choice_intact(self):
        """The temp file is renamed into place, so a crash mid-write cannot
        leave a half-written preference behind."""
        with tempfile.TemporaryDirectory() as td:
            path = self._path(td)
            config.save_layout('wide', path)
            with mock.patch('os.replace', side_effect=OSError('boom')):
                self.assertFalse(config.save_layout('jobs', path))
            self.assertEqual(config.load_layout(path), 'wide')
            leftovers = [n for n in os.listdir(td) if n.startswith('.layout')]
            self.assertEqual(leftovers, [])


if __name__ == '__main__':
    unittest.main()
