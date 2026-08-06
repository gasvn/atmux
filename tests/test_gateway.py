"""Tests for local-client multi-login routing and the SSH-stdio agent."""

import io
import json
import os
import tempfile
import threading
import time
import unittest
import warnings
from types import SimpleNamespace
from unittest import mock

from autotmux import agent, cli, config, gateway, notify


def client_settings(gateways=("login1", "login2"), **overrides):
    settings = dict(config.CLIENT_DEFAULTS)
    settings["gateways"] = list(gateways)
    settings["agent_command"] = ["atmux-agent"]
    settings.update({
        "state_timeout": 0.5,
        "hedge_delay": 0.01,
        "sticky_ttl": 60.0,
    })
    settings.update(overrides)
    return settings


def local_node():
    return {
        "alive": True,
        "socket": "",
        "network": {"state": "healthy", "retry_in": 0},
        "info": {"job_id": "-", "job_name": "local"},
        "sessions": [["laptop", "1"]],
        "last_error": "",
        "gateway_route": {
            "gateway": None, "target": "localhost", "fixed": True},
    }


def remote_state(session="train"):
    return {
        "updated": "2026-08-01 12:00:00",
        "nodes": {
            "localhost": {
                "alive": True, "socket": "/remote/ctl/local",
                "info": {}, "sessions": [["login-work", "1"]],
            },
            "gpu1": {
                "alive": True, "socket": "/remote/ctl/gpu1",
                "info": {"job_id": "123", "job_name": "train"},
                "sessions": [[session, "2"]],
            },
        },
        "keepalive": {},
        "keepalive_health": {},
    }


class TokenTests(unittest.TestCase):
    def test_interactive_token_round_trip_preserves_session_exactly(self):
        session = "train a'b;$(ignored)"
        token = gateway.encode_interactive_token("gpu1", "attach", session)
        self.assertRegex(token, r"^[A-Za-z0-9_-]+$")
        self.assertEqual(gateway.decode_interactive_token(token), {
            "v": 1, "node": "gpu1", "kind": "attach", "session": session})

    def test_interactive_token_rejects_invalid_payloads(self):
        for token in ("", "***", "bm90LWpzb24"):
            with self.assertRaises(ValueError):
                gateway.decode_interactive_token(token)
        with self.assertRaises(ValueError):
            gateway.encode_interactive_token("-oProxy=x", "shell")

    def test_transport_errors_cannot_inject_terminal_controls(self):
        self.assertEqual(gateway._safe_error("bad\x1b[2J\nnext"), "bad [2J next")


class _PoolFixture:
    """Cache paths redirected to a temp dir, plus a ready-made pool."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patchers = [
            mock.patch.object(
                gateway.paths, "GATEWAY_STATE_CACHE",
                os.path.join(self.temp.name, "state.json")),
            mock.patch.object(
                gateway.paths, "GATEWAY_SNAPSHOT_CACHE",
                os.path.join(self.temp.name, "snapshots.json")),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def pool(self, **overrides):
        return gateway.GatewayPool(
            client_settings(**overrides), local_state_loader=local_node)


class GatewayPoolTests(_PoolFixture, unittest.TestCase):
    def test_response_parser_ignores_login_banner_but_requires_marker(self):
        payload = {"protocol": 1, "ok": True, "host": "login1"}
        raw = ("banner\n" + gateway.PROTOCOL_PREFIX
               + json.dumps(payload) + "\n").encode()
        self.assertEqual(gateway.GatewayPool._parse_response(raw), payload)
        with self.assertRaises(gateway.GatewayError):
            gateway.GatewayPool._parse_response(b"only a banner\n")

    def test_real_rpc_framing_through_a_fake_ssh_transport(self):
        fake_ssh = os.path.join(self.temp.name, "ssh")
        with open(fake_ssh, "w") as handle:
            handle.write(
                f"#!{os.sys.executable}\n"
                "import os, sys\n"
                "os.execv(sys.executable, [sys.executable, '-m', "
                "'autotmux.agent', 'rpc'])\n")
        os.chmod(fake_ssh, 0o700)
        pool = self.pool(gateways=("login1",))
        env_path = self.temp.name + os.pathsep + os.environ.get("PATH", "")
        with mock.patch.dict(os.environ, {"PATH": env_path}):
            # The budget has to cover two Python interpreter starts (the fake
            # ssh, then the agent it execs). A tight bound here fails on a
            # loaded machine without telling us anything about framing.
            response = pool._rpc_gateway(
                "login1", {"action": "ping"}, timeout=30)
        self.assertTrue(response["ok"])
        self.assertEqual(response["protocol"], 1)
        self.assertIn("version", response)

    def test_ssh_argv_uses_private_master_and_direct_bypass(self):
        pool = self.pool()
        normal = pool._ssh_argv("login1")
        self.assertIn("ControlMaster=auto", normal)
        self.assertTrue(any(value.startswith("ControlPath=") for value in normal))
        self.assertEqual(normal[-1], "login1")
        direct = pool._ssh_argv("login1", direct=True)
        self.assertIn("ControlPath=none", direct)
        self.assertNotIn("ControlMaster=auto", direct)

    def test_initial_state_races_gateways_and_uses_fastest_success(self):
        pool = self.pool()

        def rpc(name, _payload, _timeout=None):
            if name == "login1":
                time.sleep(0.20)
            else:
                time.sleep(0.005)
            return {
                "ok": True, "state": remote_state(),
                "host": f"{name}.cluster", "keepalive_entries": []}

        with mock.patch.object(pool, "_rpc_gateway", side_effect=rpc):
            ok, state = pool.fetch_state()
        self.assertTrue(ok)
        self.assertEqual(pool.active_gateway, "login2")
        self.assertEqual(state["gateway"]["active"], "login2")
        self.assertIn("localhost", state["nodes"])
        self.assertEqual(state["nodes"]["localhost"]["sessions"], [["laptop", "1"]])
        login_rows = [name for name in state["nodes"] if name.startswith("login--")]
        self.assertEqual(len(login_rows), 1)
        self.assertEqual(pool._route_for(login_rows[0]).target, "localhost")
        self.assertTrue(pool._route_for(login_rows[0]).fixed)
        self.assertEqual(pool._route_for("gpu1").target, "gpu1")
        self.assertEqual(state["nodes"]["gpu1"]["socket"], "")

    def test_successful_active_route_keeps_standby_master_warm(self):
        pool = self.pool()
        standby_probe = threading.Event()
        seen = {}

        def rpc(name, payload, _timeout=None):
            if name == "login2":
                seen.update(payload)
                standby_probe.set()
                return {"ok": True, "state": remote_state(), "host": "login2"}
            return {"ok": True, "state": remote_state(), "host": "login1"}

        with mock.patch.object(pool, "_rpc_gateway", side_effect=rpc):
            ok, _ = pool.fetch_state()
            self.assertTrue(standby_probe.wait(0.5))
        self.assertTrue(ok)
        # The standby probe must still start a stopped login-node daemon; the
        # "state" action does that as well as carrying its tmux sessions.
        self.assertEqual(seen.get("action"), "state")
        self.assertIs(seen.get("ensure_daemon"), True)


    def test_all_gateways_down_returns_last_good_cache_and_local_sessions(self):
        pool = self.pool(state_timeout=0.1, hedge_delay=0.0)
        cached = pool._decorate_state(remote_state(), "login1", "login1")
        pool._last_state = cached
        with mock.patch.object(
                pool, "_rpc_gateway",
                side_effect=gateway.GatewayTransportError("network down", 255)):
            started = time.monotonic()
            ok, state = pool.fetch_state()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(ok)
        self.assertTrue(state["gateway"]["cached"])
        self.assertIn("network down", state["gateway"]["last_error"])
        self.assertIn("gpu1", state["nodes"])
        self.assertIn("localhost", state["nodes"])

    def test_cache_is_not_reused_for_a_different_gateway_pool(self):
        first = self.pool(gateways=("login1",))
        state = first._decorate_state(remote_state(), "login1", "login1")
        first._cache_state(state)
        second = self.pool(gateways=("other-login",))
        self.assertEqual(second._last_state, {})

    def test_circuit_breaker_temporarily_removes_failed_gateway(self):
        now = [100.0]
        pool = gateway.GatewayPool(
            client_settings(), clock=lambda: now[0],
            local_state_loader=local_node)
        pool._record_failure("login1", "timeout")
        self.assertEqual(pool._candidate_gateways(), ["login2"])
        now[0] += 100
        self.assertIn("login1", pool._candidate_gateways())

    def test_interactive_attach_fails_over_to_second_login(self):
        pool = self.pool()
        calls = []

        def interactive(name, token, direct=False):
            calls.append((name, direct, gateway.decode_interactive_token(token)))
            return ((255, "") if name == "login1" else (0, ""))

        with mock.patch.object(
                pool, "_fast_interactive_once", return_value=(255, "")), \
             mock.patch.object(pool, "_interactive_once", side_effect=interactive), \
             mock.patch("sys.stdout", new=io.StringIO()):
            rc, error, used_direct = pool.run_interactive(
                "gpu1", ["tmux", "attach", "-t", "'train session'"])
        self.assertEqual((rc, error, used_direct), (0, "", False))
        self.assertEqual([(item[0], item[1]) for item in calls], [
            ("login1", False), ("login1", True), ("login2", False)])
        self.assertEqual(calls[-1][2]["session"], "train session")
        self.assertEqual(pool.active_gateway, "login2")

    def test_long_interactive_lifetime_does_not_pollute_latency_score(self):
        pool = self.pool(gateways=("login1",))
        pool._health["login1"]["ewma_ms"] = 12.0
        with mock.patch.object(
                pool, "_fast_interactive_once", return_value=(0, "")) as fast, \
             mock.patch.object(
                pool, "_interactive_once",
                side_effect=AssertionError("fast path should avoid the agent")), \
             mock.patch("sys.stdout", new=io.StringIO()):
            rc, _, _ = pool.run_interactive(
                "gpu1", ["tmux", "attach", "-t", "train"])
        self.assertEqual(rc, 0)
        fast.assert_called_once()
        self.assertEqual(pool._health["login1"]["ewma_ms"], 12.0)

    def test_interactive_transport_is_isolated_from_rpc_master(self):
        pool = self.pool(gateways=("login1",))
        rpc = pool._ssh_argv("login1")
        interactive = pool._ssh_argv("login1", tty=True)
        rpc_control = next(value for value in rpc if value.startswith("ControlPath="))
        tty_control = next(
            value for value in interactive if value.startswith("ControlPath="))
        self.assertNotEqual(rpc_control, tty_control)
        self.assertIn("gateway-interactive", tty_control)

    def test_compute_fast_path_uses_proxy_and_one_target_pty(self):
        pool = self.pool(gateways=("login1",))
        pool._remote_users["login1"] = "cluster-user"
        argv = pool._fast_interactive_argv(
            "login1", "gpu1", "attach", "train session")
        proxy = next(value for value in argv if value.startswith("ProxyCommand="))
        self.assertIn("-W %h:%p login1", proxy)
        self.assertEqual(argv.count("-tt"), 1)
        self.assertEqual(
            argv[-2:],
            ["cluster-user@gpu1",
             "exec tmux attach-session -d -t 'train session'"])
        self.assertNotIn("atmux-agent", " ".join(argv))

    def test_interactive_path_disables_latency_amplifying_ssh_features(self):
        pool = self.pool(gateways=("login1",), control_persist=3600)
        argv = pool._fast_interactive_argv(
            "login1", "gpu1", "attach", "train")
        joined = " ".join(argv)
        self.assertIn("IPQoS=none", argv)
        self.assertIn("Compression=no", argv)
        self.assertIn("IgnoreUnknown=ObscureKeystrokeTiming", argv)
        self.assertIn("ObscureKeystrokeTiming=no", argv)
        self.assertIn("ControlPersist=300", argv)
        self.assertIn("attach-session -d", joined)

    def test_prewarm_establishes_master_without_allocating_a_pty(self):
        pool = self.pool(gateways=("login1",))
        pool._routes["gpu1"] = gateway.Route("login1", "gpu1")
        completed = SimpleNamespace(returncode=0)
        with mock.patch.object(
                pool, "_fast_interactive_master_present",
                return_value=False), \
             mock.patch.object(
                gateway.subprocess, "run", return_value=completed) as run:
            self.assertTrue(pool.prewarm_interactive("gpu1"))
        argv = run.call_args.args[0]
        self.assertIn("-T", argv)
        self.assertNotIn("-tt", argv)
        self.assertEqual(argv[-1], "true")

    def test_failed_compute_fast_path_falls_back_and_is_temporarily_cached(self):
        pool = self.pool(gateways=("login1",))
        with mock.patch.object(
                pool, "_fast_interactive_once", return_value=(255, "")) as fast, \
             mock.patch.object(
                pool, "_interactive_once", return_value=(0, "")) as agent_call, \
             mock.patch("sys.stdout", new=io.StringIO()):
            first = pool.run_interactive(
                "gpu1", ["tmux", "attach", "-t", "train"])
            second = pool.run_interactive(
                "gpu1", ["tmux", "attach", "-t", "train"])
        self.assertEqual(first[0], 0)
        self.assertEqual(second[0], 0)
        fast.assert_called_once()
        self.assertEqual(agent_call.call_count, 2)

    def test_stale_fast_master_retries_direct_before_agent_fallback(self):
        pool = self.pool(gateways=("login1",))
        with mock.patch.object(
                pool, "_fast_interactive_master_present", return_value=True), \
             mock.patch.object(
                pool, "_fast_interactive_once",
                side_effect=[(255, ""), (0, "")]) as fast, \
             mock.patch.object(
                pool, "_interactive_once",
                side_effect=AssertionError("direct fast retry should win")), \
             mock.patch("sys.stdout", new=io.StringIO()):
            rc, error, used_direct = pool.run_interactive(
                "gpu1", ["tmux", "attach", "-t", "train"])
        self.assertEqual((rc, error, used_direct), (0, "", True))
        self.assertFalse(fast.call_args_list[0].kwargs["direct"])
        self.assertTrue(fast.call_args_list[1].kwargs["direct"])

    def test_preview_is_cached_for_offline_display(self):
        pool = self.pool()
        response = {"ok": True, "content": "\x1b[31mhello\x1b[0m",
                    "captured_epoch": 10.0, "gateway": "login1"}
        with mock.patch.object(pool, "_rpc_failover", return_value=response):
            self.assertEqual(pool.preview("gpu1", "train"), response)
        ok, snapshots = pool.read_snapshots()
        self.assertTrue(ok)
        self.assertEqual(snapshots["gpu1:train"]["lines"], response["content"])
        self.assertTrue(os.path.exists(gateway.paths.GATEWAY_SNAPSHOT_CACHE))

    def test_localhost_preview_never_crosses_a_login_gateway(self):
        pool = self.pool()
        completed = SimpleNamespace(
            returncode=0, stdout="local pane", stderr="")
        with mock.patch.object(
                gateway.subprocess, "run", return_value=completed) as run, \
             mock.patch.object(pool, "_rpc_failover") as remote:
            response = pool.preview("localhost", "laptop")
        self.assertTrue(response["ok"])
        self.assertEqual(response["gateway"], "local")
        self.assertEqual(run.call_args.args[0], [
            "tmux", "capture-pane", "-p", "-e", "-t", "laptop"])
        remote.assert_not_called()

    def test_keepalive_uses_state_bundled_registry_without_extra_rpc(self):
        pool = self.pool()
        pool._keepalive_entries = [{"job_name": "train", "enabled": True}]
        pool._keepalive_known = True
        with mock.patch.object(pool, "_rpc_failover") as rpc:
            self.assertEqual(
                pool.keepalive_entries(),
                [{"job_name": "train", "enabled": True}])
        rpc.assert_not_called()

    def test_rpc_mux_retry_shares_one_total_timeout(self):
        now = [0.0]
        pool = gateway.GatewayPool(
            client_settings(), clock=lambda: now[0],
            local_state_loader=local_node)
        timeouts = []

        def once(_name, _payload, timeout, direct=False):
            timeouts.append((timeout, direct))
            if not direct:
                now[0] += 0.6
                raise gateway.GatewayTransportError("stale mux", 255)
            return {"protocol": 1, "ok": True}

        with mock.patch.object(pool, "_rpc_once", side_effect=once):
            response = pool._rpc_gateway("login1", {"action": "ping"}, 1.0)
        self.assertTrue(response["ok"])
        self.assertEqual(timeouts[0], (1.0, False))
        self.assertAlmostEqual(timeouts[1][0], 0.4)
        self.assertTrue(timeouts[1][1])

    def test_agent_ping_teaches_fast_path_the_cluster_username(self):
        pool = self.pool(gateways=("login1",))
        with mock.patch.object(
                pool, "_rpc_once",
                return_value={"protocol": 1, "ok": True,
                              "user": "cluster-user"}):
            pool._rpc_gateway("login1", {"action": "ping"}, 1.0)
        argv = pool._fast_interactive_argv(
            "login1", "gpu1", "attach", "train")
        self.assertIn("cluster-user@gpu1", argv)

    def test_explicit_authentication_is_only_non_batch_ssh_path(self):
        pool = self.pool(gateways=("login1",))
        with mock.patch.object(pool, "_master_alive",
                               side_effect=[False, True]), \
             mock.patch.object(gateway.subprocess, "call", return_value=0) as call, \
             mock.patch("sys.stdout", new=io.StringIO()):
            results = pool.authenticate()
        self.assertTrue(results[0]["ok"])
        argv = call.call_args.args[0]
        self.assertIn("BatchMode=no", argv)
        self.assertIn("ControlMaster=auto", argv)
        self.assertIn("-fN", argv)

    def test_gateway_check_probes_all_agents(self):
        pool = self.pool()

        def rpc(name, _payload, _timeout):
            if name == "login2":
                raise gateway.GatewayTransportError("down", 255)
            return {"ok": True, "host": "login1.real", "version": "0.5.0"}

        with mock.patch.object(pool, "_rpc_gateway", side_effect=rpc):
            results = pool.check_all()
        self.assertEqual([item["gateway"] for item in results],
                         ["login1", "login2"])
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[1]["ok"])


class LoginNodeRosterTests(_PoolFixture, unittest.TestCase):
    """Every configured login node runs its own daemon and its own tmux
    sessions.  Only one gateway wins the state race, so the others are
    harvested by the standby probe and merged into the table."""

    @staticmethod
    def _login_rows(state):
        return sorted(name for name in state["nodes"]
                      if name.startswith("login--"))

    def _pool_with_clock(self, **overrides):
        """A pool on a fake clock, seeded *after* the clock is installed so the
        recorded timestamp and the TTL comparison share one time source."""
        now = [1000.0]
        pool = self.pool(**overrides)
        pool._clock = lambda: now[0]
        self._seed(pool)

        def advance(seconds):
            now[0] += seconds

        return pool, advance

    @staticmethod
    def _seed(pool):
        pool._record_login_node("login2", {
            "ok": True, "host": "login2",
            "state": {"nodes": {"localhost": {
                "alive": True, "socket": "/remote/ctl/local", "info": {},
                "sessions": [["standby-work", "1"]]}}},
        })

    def _pool_with_roster(self, **overrides):
        pool = self.pool(**overrides)
        self._seed(pool)
        return pool

    def test_standby_login_node_sessions_are_listed(self):
        pool = self._pool_with_roster()
        state = pool._decorate_state(remote_state(), "login1", "login1")
        self.assertEqual(self._login_rows(state), ["login--login1",
                                                   "login--login2"])
        self.assertEqual(
            state["nodes"]["login--login2"]["sessions"], [["standby-work", "1"]])

    def test_standby_login_node_routes_through_its_own_gateway(self):
        pool = self._pool_with_roster()
        pool._decorate_state(remote_state(), "login1", "login1")
        route = pool._route_for("login--login2")
        self.assertEqual(route.gateway, "login2")
        self.assertEqual(route.target, "localhost")
        self.assertTrue(route.fixed)

    def test_active_gateway_is_not_duplicated_by_the_roster(self):
        pool = self.pool()
        pool._record_login_node("login1", {
            "ok": True, "host": "login1",
            "state": {"nodes": {"localhost": {
                "alive": True, "sessions": [["stale", "1"]]}}},
        })
        state = pool._decorate_state(remote_state(), "login1", "login1")
        self.assertEqual(self._login_rows(state), ["login--login1"])
        # The live race result wins over the harvested copy.
        self.assertEqual(
            state["nodes"]["login--login1"]["sessions"], [["login-work", "1"]])

    def test_two_aliases_for_one_login_host_collapse_to_one_row(self):
        """A round-robin alias can resolve to a host another alias already
        names; listing it twice would show the same sessions under two rows."""
        pool = self.pool(gateways=("login1", "login2", "rr"))
        pool._record_login_node("rr", {
            "ok": True, "host": "login1",
            "state": {"nodes": {"localhost": {
                "alive": True, "sessions": [["login-work", "1"]]}}},
        })
        state = pool._decorate_state(remote_state(), "login1", "login1")
        self.assertEqual(self._login_rows(state), ["login--login1"])

    def test_unreachable_gateway_is_dropped_from_the_roster(self):
        pool = self._pool_with_roster()
        pool._record_login_node("login2", {"ok": False, "reason": "timed out"})
        state = pool._decorate_state(remote_state(), "login1", "login1")
        self.assertEqual(self._login_rows(state), ["login--login1"])

    def test_fresh_roster_entries_are_kept(self):
        pool, advance = self._pool_with_clock()
        advance(gateway._LOGIN_NODE_TTL - 1.0)
        state = pool._decorate_state(remote_state(), "login1", "login1")
        self.assertEqual(self._login_rows(state),
                         ["login--login1", "login--login2"])

    def test_stale_roster_entries_age_out(self):
        pool, advance = self._pool_with_clock()
        advance(gateway._LOGIN_NODE_TTL + 1.0)
        state = pool._decorate_state(remote_state(), "login1", "login1")
        self.assertEqual(self._login_rows(state), ["login--login1"])

    def test_roster_survives_an_active_gateway_outage(self):
        """With every gateway failing the dashboard falls back to cache; the
        login nodes we can still hear from should stay listed."""
        pool = self._pool_with_roster(state_timeout=0.1, hedge_delay=0.0)
        pool._last_state = pool._decorate_state(
            remote_state(), "login1", "login1")
        state = pool._cached_or_empty_state("all gateways down")
        self.assertIn("login--login2", state["nodes"])
        self.assertEqual(
            pool._route_for("login--login2").gateway, "login2")

    def test_race_losers_are_harvested_without_extra_rpcs(self):
        """The hedge already paid for the losing gateways' replies, so their
        login sessions should be kept rather than thrown away and re-fetched a
        whole probe_interval later."""
        pool = self.pool(hedge_delay=0.0)
        actions = []

        def rpc(name, payload, _timeout=None):
            actions.append(payload["action"])
            if name == "login1":
                time.sleep(0.05)      # force login2 to win the race
            return {"ok": True, "state": remote_state(), "host": name}

        with mock.patch.object(pool, "_rpc_gateway", side_effect=rpc):
            ok, _ = pool.fetch_state()
            deadline = time.monotonic() + 2.0
            while (len(pool._login_nodes) < 2
                   and time.monotonic() < deadline):
                time.sleep(0.01)
        self.assertTrue(ok)
        self.assertEqual(sorted(pool._login_nodes), ["login1", "login2"])
        self.assertEqual(actions.count("state"), 2)

    def test_outgoing_winner_stays_listed_after_leadership_moves(self):
        pool = self.pool()
        pool._decorate_state(remote_state(), "login1", "login1")
        state = pool._decorate_state(remote_state(), "login2", "login2")
        self.assertEqual(self._login_rows(state),
                         ["login--login1", "login--login2"])
        self.assertEqual(pool._route_for("login--login1").gateway, "login1")

    def test_malformed_probe_replies_are_ignored(self):
        pool = self.pool()
        for reply in (None, {}, {"ok": True}, {"ok": True, "state": {}},
                      {"ok": True, "state": {"nodes": {}}},
                      {"ok": True, "state": {"nodes": {"localhost": "x"}}}):
            pool._record_login_node("login2", reply)
        state = pool._decorate_state(remote_state(), "login1", "login1")
        self.assertEqual(self._login_rows(state), ["login--login1"])


class HedgeAndStickyTests(_PoolFixture, unittest.TestCase):
    """Racing every gateway on every refresh is waste, and sitting on a slow
    one for a whole lease is the opposite waste. Both are decided from the
    latency the pool already measures."""

    def _pool_with_latency(self, latencies, **overrides):
        pool = self.pool(**overrides)
        for gateway, ms in latencies.items():
            pool._health[gateway]['ewma_ms'] = ms
        return pool

    def test_hedge_window_scales_with_the_measured_round_trip(self):
        pool = self._pool_with_latency({'login1': 500.0}, hedge_delay=0.25)
        # 500 ms * 2.5 = 1.25 s, not the 0.25 s floor that would fan out on
        # every single refresh.
        self.assertAlmostEqual(pool._hedge_delay_for('login1'), 1.25, places=2)

    def test_configured_delay_is_a_floor_not_a_target(self):
        pool = self._pool_with_latency({'login1': 10.0}, hedge_delay=0.25)
        self.assertAlmostEqual(pool._hedge_delay_for('login1'), 0.25, places=2)

    def test_hedge_window_is_capped(self):
        pool = self._pool_with_latency({'login1': 60_000.0})
        self.assertLessEqual(pool._hedge_delay_for('login1'),
                             gateway._HEDGE_CEILING)

    def test_an_unmeasured_gateway_uses_the_configured_delay(self):
        pool = self.pool(hedge_delay=0.4)
        self.assertAlmostEqual(pool._hedge_delay_for('login1'), 0.4, places=2)

    def test_lowest_latency_gateway_is_tried_first(self):
        pool = self._pool_with_latency({'login1': 900.0, 'login2': 200.0})
        self.assertEqual(pool._candidate_gateways()[0], 'login2')

    def test_sticky_holds_through_a_near_tie(self):
        pool = self._pool_with_latency({'login1': 567.0, 'login2': 396.0})
        pool._set_active('login1')
        self.assertEqual(pool._candidate_gateways()[0], 'login1')

    def test_sticky_yields_to_a_clearly_faster_route(self):
        pool = self._pool_with_latency({'login1': 900.0, 'login2': 200.0})
        pool._set_active('login1')
        self.assertEqual(pool._candidate_gateways()[0], 'login2')

    def test_a_small_absolute_win_is_treated_as_noise(self):
        """On a fast LAN a 60% ratio can still be a 30 ms difference."""
        health = {'a': {'ewma_ms': 20.0}, 'b': {'ewma_ms': 50.0}}
        self.assertFalse(gateway.GatewayPool._clearly_faster('a', 'b', health))

    def test_unmeasured_latency_never_forces_a_switch(self):
        health = {'a': {'ewma_ms': None}, 'b': {'ewma_ms': 900.0}}
        self.assertFalse(gateway.GatewayPool._clearly_faster('a', 'b', health))
        self.assertFalse(gateway.GatewayPool._clearly_faster('b', 'a', health))

    def test_a_healthy_primary_is_not_raced(self):
        """The waste this fixes: one RPC per refresh, not one per gateway.

        Standby probing is stubbed out so this measures the race alone; that
        runs on its own much slower interval.
        """
        pool = self._pool_with_latency(
            {'login1': 500.0, 'login2': 500.0}, hedge_delay=0.25)
        pool._set_active('login1')
        seen = []

        def rpc(name, _payload, _timeout=None):
            seen.append(name)
            return {'ok': True, 'state': remote_state(), 'host': name}

        with mock.patch.object(pool, '_rpc_gateway', side_effect=rpc), \
             mock.patch.object(pool, '_schedule_backup_probes'):
            ok, _ = pool.fetch_state()
        self.assertTrue(ok)
        self.assertEqual(seen, ['login1'])

    def test_a_silent_primary_still_gets_raced(self):
        """Hedging must not be traded away: a gateway that goes quiet has to
        fail over, it just should not be assumed slow at 0.25 s."""
        pool = self._pool_with_latency(
            {'login1': 10.0, 'login2': 10.0}, hedge_delay=0.05,
            state_timeout=2.0)
        pool._set_active('login1')
        seen = []

        def rpc(name, _payload, _timeout=None):
            seen.append(name)
            if name == 'login1':
                time.sleep(1.0)          # silent, not failed
            return {'ok': True, 'state': remote_state(), 'host': name}

        with mock.patch.object(pool, '_rpc_gateway', side_effect=rpc), \
             mock.patch.object(pool, '_schedule_backup_probes'):
            ok, _ = pool.fetch_state()
        self.assertTrue(ok)
        self.assertIn('login2', seen)


class JobReminderClaimTests(unittest.TestCase):
    """One daemon runs per login node against the same squeue, so without a
    shared record every one of them announces the same job."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, 'claims')

    def tearDown(self):
        self.temp.cleanup()

    def test_only_the_first_daemon_announces(self):
        self.assertTrue(notify.claim_job(self.path, '42'))
        self.assertFalse(notify.claim_job(self.path, '42'))
        self.assertFalse(notify.claim_job(self.path, '42'))

    def test_distinct_jobs_are_independent(self):
        self.assertTrue(notify.claim_job(self.path, '42'))
        self.assertTrue(notify.claim_job(self.path, '43'))

    def test_claims_expire(self):
        self.assertTrue(notify.claim_job(self.path, '42'))
        self.assertTrue(notify.claim_job(self.path, '42', ttl=0))

    def test_the_record_is_created_with_its_directory(self):
        nested = os.path.join(self.temp.name, 'cfg', 'claims')
        self.assertTrue(notify.claim_job(nested, '42'))
        self.assertTrue(os.path.isdir(nested))

    def test_an_unusable_record_fails_open(self):
        """A duplicate reminder is a far smaller harm than a silent one."""
        self.assertTrue(notify.claim_job('/nonexistent-dir/claims', '42'))

    def test_a_corrupt_claim_still_expires(self):
        """A write cut short by a crash leaves an empty file. Falling back to
        the server's mtime keeps it from becoming a claim that never ages."""
        self.assertTrue(notify.claim_job(self.path, '42'))
        target = os.path.join(self.path, notify._claim_name('42'))
        for junk in ('', 'not a number', '\x00'):
            with open(target, 'w') as handle:
                handle.write(junk)
            with self.subTest(junk=junk):
                self.assertFalse(notify.claim_job(self.path, '42'))
                self.assertTrue(notify.claim_job(self.path, '42', ttl=0))

    def test_a_released_claim_can_be_taken_again(self):
        """The claim is taken before sending, so a failed send has to hand it
        back or nothing retries until it expires."""
        self.assertTrue(notify.claim_job(self.path, '42'))
        self.assertFalse(notify.claim_job(self.path, '42'))
        notify.release_claim(self.path, '42')
        self.assertTrue(notify.claim_job(self.path, '42'))

    def test_releasing_leaves_other_claims_alone(self):
        notify.claim_job(self.path, '42')
        notify.claim_job(self.path, '43')
        notify.release_claim(self.path, '42')
        self.assertFalse(notify.claim_job(self.path, '43'))

    def test_releasing_something_unclaimed_is_harmless(self):
        notify.release_claim(self.path, 'never-claimed')
        notify.release_claim('/nonexistent-dir/claims', '42')

    def test_a_short_ttl_never_expires_another_kind_of_claim(self):
        """The single shared record was pruned with whichever TTL the caller
        happened to pass, so an idle notice (1 h) quietly evicted the job
        expiry claims that were supposed to last a week -- and the reminder
        went out twice."""
        now = 1_000_000.0
        self.assertTrue(notify.claim_job(self.path, '42', now=now))
        # An idle notice an hour later, with its own much shorter TTL.
        self.assertTrue(notify.claim_job(self.path, 'idle:n:s', ttl=3600,
                                         now=now + 7200))
        self.assertFalse(notify.claim_job(self.path, '42', now=now + 7200))

    def test_a_key_cannot_escape_the_claim_directory(self):
        """Session names reach this from tmux and are not path components."""
        for key in ('idle:../../etc/passwd:s', 'a/b/c', '..', '/abs'):
            with self.subTest(key=key):
                self.assertTrue(notify.claim_job(self.path, key))
                name = notify._claim_name(key)
                self.assertNotIn('/', name)
                self.assertEqual(
                    os.path.dirname(os.path.join(self.path, name)), self.path)

    def test_similar_keys_do_not_collide_onto_one_file(self):
        """The readable label is lossy, so identity has to come from the
        digest -- or two sessions would share one claim and one would go
        unannounced."""
        first, second = 'idle:node:a/b', 'idle:node:a-b'
        self.assertNotEqual(notify._claim_name(first),
                            notify._claim_name(second))
        self.assertTrue(notify.claim_job(self.path, first))
        self.assertTrue(notify.claim_job(self.path, second))

    def test_daemons_racing_from_separate_processes_yield_one_winner(self):
        """The property the previous implementation failed in production.

        It held an flock around one shared JSON record; on NFSv3 home that
        lock returned ENOLCK the moment four login nodes contended for it, so
        every daemon took the fail-open path and one quiet session produced
        four identical Slack messages in the same second. A same-process test
        could never have caught it -- the contention has to be real.
        """
        workers = 6
        start = time.time() + 0.5
        children = []
        # Created up front so the children only ever make bare syscalls and
        # os._exit -- they never take an interpreter lock (logging's included)
        # that a thread in this process could have been holding at fork time.
        os.makedirs(self.path, mode=0o700, exist_ok=True)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)
            for _ in range(workers):
                read_fd, write_fd = os.pipe()
                pid = os.fork()
                if pid == 0:                  # child
                    try:
                        os.close(read_fd)
                        while time.time() < start:
                            time.sleep(0.002)
                        won = notify.claim_job(self.path, 'contended')
                        os.write(write_fd, b'1' if won else b'0')
                    finally:
                        os._exit(0)
                os.close(write_fd)
                children.append((pid, read_fd))

        winners = 0
        for pid, read_fd in children:
            try:
                winners += os.read(read_fd, 1) == b'1'
            finally:
                os.close(read_fd)
                os.waitpid(pid, 0)
        self.assertEqual(winners, 1, f'{winners} of {workers} daemons posted')

    def test_the_record_stays_bounded(self):
        now = 1_000_000.0
        for i in range(200):
            notify.claim_job(self.path, str(i), now=now)
        self.assertEqual(len(os.listdir(self.path)), 200)
        # Long past every TTL: the next claim sweeps the stale ones away.
        notify.claim_job(self.path, 'later', now=now + notify.CLAIM_TTL + 1)
        self.assertEqual(os.listdir(self.path),
                         [notify._claim_name('later')])


class LocalSessionIdleTests(unittest.TestCase):
    """The laptop's own tmux sessions earn the same idle hint as remote ones,
    and parse with the same field order so a ':' in a name is safe."""

    def _sessions(self, stdout, now=1_000_000):
        result = SimpleNamespace(returncode=0, stdout=stdout)
        with mock.patch.object(gateway.subprocess, 'run', return_value=result), \
             mock.patch.object(gateway.time, 'time', return_value=now):
            return gateway.GatewayPool._local_node_state()['sessions']

    def test_idle_is_reported_for_local_sessions(self):
        self.assertEqual(
            self._sessions('999100:2:work\n999940:1:fresh\n'),
            [['work', '2', 900], ['fresh', '1', 60]])

    def test_local_session_name_may_contain_colons(self):
        self.assertEqual(
            self._sessions('999995:1:proj:sub\n'), [['proj:sub', '1', 5]])

    def test_clock_skew_never_yields_negative_idle(self):
        self.assertEqual(self._sessions('1000060:1:ahead\n'),
                         [['ahead', '1', 0]])

    def test_malformed_lines_are_skipped(self):
        self.assertEqual(self._sessions('garbage\n\n:: \n999940:1:ok\n'),
                         [['ok', '1', 60]])

    def test_unparseable_activity_still_lists_the_session(self):
        self.assertEqual(self._sessions('nope:1:work\n'), [['work', '1']])

    def test_missing_tmux_is_reported_not_raised(self):
        with mock.patch.object(gateway.subprocess, 'run',
                               side_effect=FileNotFoundError()):
            state = gateway.GatewayPool._local_node_state()
        self.assertEqual(state['sessions'], [])
        self.assertIn('tmux', state['last_error'])


class ExternalControlPathTests(_PoolFixture, unittest.TestCase):
    """`[client] control_path` reuses SSH masters owned by something else (an
    MFA helper, say).  AutoTmux must ride those masters for every gateway
    connection and never create one at a path it does not own."""

    EXTERNAL = "/tmp/cm-external-%n"

    def external_pool(self, **overrides):
        return self.pool(control_path=self.EXTERNAL, **overrides)

    @staticmethod
    def _opt(argv, name):
        return [v for v in argv if v.startswith(f"{name}=")]

    def test_rpc_and_interactive_share_the_external_master(self):
        pool = self.external_pool()
        self.assertEqual(pool._control_path("login1"), self.EXTERNAL)
        self.assertEqual(pool._interactive_control_path("login1"), self.EXTERNAL)

    def test_external_master_is_used_but_never_created(self):
        pool = self.external_pool()
        for argv in (pool._ssh_argv("login1"),
                     pool._ssh_argv("login1", tty=True)):
            self.assertEqual(self._opt(argv, "ControlPath"),
                             [f"ControlPath={self.EXTERNAL}"])
            self.assertIn("ControlMaster=no", argv)
            self.assertNotIn("ControlMaster=auto", argv)
            # Persist belongs to the owner; claiming it would take the socket.
            self.assertEqual(self._opt(argv, "ControlPersist"), [])

    def test_proxy_hop_to_a_compute_node_uses_the_external_master(self):
        """The attach failure this option exists for: the ProxyCommand hop is a
        separate socket from the RPC one, so it must be covered too."""
        pool = self.external_pool()
        proxy = pool._jump_proxy_command("login1", direct=False)
        self.assertIn("ControlPath=", proxy)
        self.assertIn("ControlMaster=no", proxy)

    def test_proxy_hop_tokens_survive_the_outer_expansion(self):
        """OpenSSH expands %-tokens in a ProxyCommand against the *outer*
        destination before running it.  An unescaped %n would resolve to the
        compute node, sending the inner ssh at a socket that cannot exist and
        making every attach fail.
        """
        pool = self.external_pool()
        proxy = pool._jump_proxy_command("login1", direct=False)
        self.assertIn("ControlPath=/tmp/cm-external-%%n", proxy)
        self.assertNotIn("ControlPath=/tmp/cm-external-%n", proxy)
        # The forwarding tokens must stay single so the outer ssh does expand
        # them -- that is how the hop learns which compute node to reach.
        self.assertIn("-W %h:%p", proxy)

    def test_plain_control_paths_are_unchanged_in_a_proxy_command(self):
        pool = self.pool()
        proxy = pool._jump_proxy_command("login1", direct=False)
        self.assertIn(
            f"ControlPath={pool._interactive_control_path('login1')}", proxy)

    def test_compute_node_master_is_still_our_own(self):
        """The jump master terminates on the compute node, not the login node,
        so it must not be pointed at the login-node socket."""
        pool = self.external_pool()
        argv = pool._fast_interactive_argv("login1", "gpu1", "shell", None)
        jump = pool._jump_control_path("login1", "gpu1")
        self.assertEqual(self._opt(argv, "ControlPath"),
                         [f"ControlPath={jump}"])
        self.assertNotEqual(jump, self.EXTERNAL)
        self.assertIn("ControlMaster=auto", argv)

    def test_direct_bypass_still_skips_multiplexing(self):
        pool = self.external_pool()
        argv = pool._ssh_argv("login1", direct=True)
        self.assertIn("ControlPath=none", argv)
        self.assertNotIn(f"ControlPath={self.EXTERNAL}", argv)

    def test_gateway_login_reports_instead_of_stealing_the_socket(self):
        pool = self.external_pool()
        with mock.patch.object(pool, "_master_alive", return_value=False), \
             mock.patch.object(gateway.subprocess, "call") as call:
            results = pool.authenticate()
        call.assert_not_called()
        self.assertTrue(all(not r["ok"] for r in results))
        self.assertIn(self.EXTERNAL, results[0]["error"])

    def test_gateway_login_still_creates_our_own_master_by_default(self):
        pool = self.pool()
        with mock.patch.object(pool, "_master_alive", side_effect=[False, True,
                                                                  False, True]), \
             mock.patch.object(gateway.subprocess, "call", return_value=0) as call:
            pool.authenticate()
        self.assertTrue(call.called)
        argv = call.call_args[0][0]
        self.assertIn("ControlMaster=auto", argv)

    def test_unset_option_keeps_private_per_purpose_masters(self):
        pool = self.pool()
        self.assertEqual(pool.external_control_path(), "")
        self.assertNotEqual(pool._control_path("login1"),
                            pool._interactive_control_path("login1"))
        self.assertIn("ControlMaster=auto", pool._ssh_argv("login1"))


class SharedMasterKeepaliveTests(_PoolFixture, unittest.TestCase):
    """Keepalive options on a multiplexed session are ignored -- the master
    owns the TCP stream.  Riding someone else's master therefore silently
    replaces our hang budget with theirs, which is how an unstable network
    turns into a dashboard that never comes back."""

    @staticmethod
    def _ssh_g(interval, count):
        lines = []
        if interval is not None:
            lines.append(f"serveraliveinterval {interval}")
        if count is not None:
            lines.append(f"serveralivecountmax {count}")
        lines.append("controlmaster auto")
        return SimpleNamespace(
            returncode=0, stdout="\n".join(lines).encode())

    def _pool(self, external=True):
        return self.pool(control_path="/tmp/cm-%n") if external else self.pool()

    def test_budget_is_interval_times_count(self):
        pool = self._pool()
        with mock.patch.object(gateway.subprocess, "run",
                               return_value=self._ssh_g(60, 30)):
            self.assertEqual(pool.master_keepalive_seconds("login1"), 1800.0)

    def test_a_slow_shared_master_is_reported(self):
        pool = self._pool()
        with mock.patch.object(gateway.subprocess, "run",
                               return_value=self._ssh_g(60, 30)):
            warning = pool.keepalive_warning("login1")
        self.assertIn("1800", warning)
        self.assertIn("45", warning)   # our own 15 x 3 budget

    def test_a_comparable_shared_master_is_not_reported(self):
        pool = self._pool()
        with mock.patch.object(gateway.subprocess, "run",
                               return_value=self._ssh_g(15, 4)):
            self.assertEqual(pool.keepalive_warning("login1"), "")

    def test_nothing_is_reported_when_we_own_the_master(self):
        """Our own masters carry our own settings, so there is nothing to say
        even if the user's ssh_config is lax."""
        pool = self._pool(external=False)
        with mock.patch.object(gateway.subprocess, "run",
                               return_value=self._ssh_g(60, 30)) as run:
            self.assertEqual(pool.keepalive_warning("login1"), "")
        run.assert_not_called()

    def test_unreadable_or_malformed_output_is_not_guessed_at(self):
        pool = self._pool()
        cases = [
            SimpleNamespace(returncode=1, stdout=b""),
            self._ssh_g(None, None),
            self._ssh_g("abc", 3),
            self._ssh_g(0, 3),
            self._ssh_g(60, None),
        ]
        for case in cases:
            with self.subTest(case=case.stdout):
                with mock.patch.object(gateway.subprocess, "run",
                                       return_value=case):
                    self.assertIsNone(pool.master_keepalive_seconds("login1"))
                    self.assertEqual(pool.keepalive_warning("login1"), "")

    def test_a_stuck_ssh_g_cannot_wedge_the_caller(self):
        pool = self._pool()
        with mock.patch.object(
                gateway.subprocess, "run",
                side_effect=gateway.subprocess.TimeoutExpired("ssh", 5)):
            self.assertIsNone(pool.master_keepalive_seconds("login1"))
        with mock.patch.object(gateway.subprocess, "run",
                               side_effect=OSError("no ssh")):
            self.assertIsNone(pool.master_keepalive_seconds("login1"))


class ClientControlPathConfigTests(unittest.TestCase):
    def test_valid_values_are_expanded(self):
        self.assertEqual(
            config._client_control_path("~/.ssh/cm-%n"),
            os.path.join(os.path.expanduser("~"), ".ssh/cm-%n"))
        self.assertEqual(config._client_control_path("  /tmp/cm-%n  "),
                         "/tmp/cm-%n")

    def test_empty_means_manage_our_own(self):
        for value in ("", "   "):
            self.assertEqual(config._client_control_path(value), "")

    def test_dangerous_values_are_rejected(self):
        for value in ("-oProxyCommand=x", "/tmp/a\nb", "/tmp/a\x00b",
                      "/tmp/\x1b[2Jb", "x" * 260, 5, None, ["/tmp/cm"]):
            with self.subTest(value=value):
                self.assertIsNone(config._client_control_path(value))

    def test_default_is_unset(self):
        self.assertEqual(config.CLIENT_DEFAULTS["control_path"], "")


class AgentTests(unittest.TestCase):
    def test_ping_and_invalid_node_are_bounded_protocol_operations(self):
        response = agent.handle_rpc({"action": "ping"})
        self.assertTrue(response["ok"])
        self.assertIn("version", response)
        self.assertIn("user", response)
        invalid = agent.handle_rpc({
            "action": "preview", "node": "-oProxy=x", "session": "s"})
        self.assertEqual(invalid["kind"], "invalid")

    def test_standby_ping_can_warm_login_daemon(self):
        with mock.patch.object(agent, "_daemon_running", return_value=False), \
             mock.patch.object(agent, "_request_daemon_start",
                               return_value=True) as start:
            response = agent.handle_rpc(
                {"action": "ping", "ensure_daemon": True})
        self.assertTrue(response["ok"])
        self.assertTrue(response["daemon_starting"])
        start.assert_called_once_with()

    def test_daemon_start_request_reaches_the_spawn_path(self):
        with mock.patch.object(agent, "_daemon_running", return_value=False), \
             mock.patch.object(agent.subprocess, "Popen") as popen:
            self.assertTrue(agent._request_daemon_start())
        popen.assert_called_once()
        self.assertIn("autotmux.daemon", popen.call_args.args[0])

    def test_state_response_bundles_registry_and_starts_no_second_daemon(self):
        state = remote_state()
        entries = [{"job_name": "train", "enabled": True}]
        with mock.patch.object(agent, "_read_state", return_value=state), \
             mock.patch.object(agent, "_daemon_running", return_value=True), \
             mock.patch.object(
                 agent.keepalive, "_load_registry_checked",
                 return_value=(True, entries)), \
             mock.patch.object(agent, "_request_daemon_start") as start:
            response = agent.handle_rpc({"action": "state"})
        self.assertTrue(response["ok"])
        self.assertEqual(response["keepalive_entries"], entries)
        start.assert_not_called()

    def test_state_registry_read_has_a_hard_bound(self):
        def blocked(_path):
            time.sleep(0.2)
            return True, []

        with mock.patch.object(
                agent.keepalive, "_load_registry_checked", side_effect=blocked):
            started = time.monotonic()
            self.assertEqual(agent._bounded_registry_read(0.01), (False, []))
        self.assertLess(time.monotonic() - started, 0.1)

    def test_scontrol_rejects_option_without_spawning(self):
        with mock.patch.object(agent.subprocess, "run") as run:
            self.assertIsNone(agent._scontrol_job("-Q"))
        run.assert_not_called()

    def test_keepalive_set_is_explicit_and_idempotent(self):
        with mock.patch.object(
                agent.keepalive, "set_entry_enabled", return_value=True) as setter:
            response = agent.handle_rpc({
                "action": "keepalive-set", "job_name": "train",
                "enabled": True, "command": "/work/train.sh",
                "workdir": "/work", "job_id": "123", "entry_id": "id1"})
        self.assertTrue(response["ok"])
        setter.assert_called_once_with(
            config.KEEPALIVE_PATH, "train", True, "/work/train.sh", "/work",
            job_id="123", entry_id="id1")

    def test_interactive_compute_attach_retries_bad_remote_master(self):
        token = gateway.encode_interactive_token("gpu1", "attach", "train")
        with mock.patch.object(agent, "_control_path", return_value="/tmp/cm"), \
             mock.patch.object(agent, "_compute_ssh_argv",
                               side_effect=[["ssh", "mux"], ["ssh", "direct"]]) as build, \
             mock.patch.object(agent.subprocess, "call", side_effect=[255, 0]), \
             mock.patch.object(agent, "_report_interactive") as report:
            rc = agent.interactive_main(token)
        self.assertEqual(rc, 0)
        self.assertEqual(build.call_count, 2)
        self.assertTrue(build.call_args_list[1].kwargs["direct"])
        report.assert_called_once_with("gpu1", 0, "gateway-attach")

    def test_agent_fallback_also_uses_isolated_low_latency_master(self):
        with mock.patch.object(agent, "_read_state", return_value={}):
            argv = agent._compute_ssh_argv("gpu1", "train")
        joined = " ".join(argv)
        self.assertIn("ControlMaster=auto", argv)
        self.assertIn("ControlPersist=300", argv)
        self.assertIn("IPQoS=none", argv)
        self.assertIn("Compression=no", argv)
        self.assertIn("interactive-ctl", joined)
        self.assertIn("attach-session -d", argv[-1])


class CliDeploymentModeTests(unittest.TestCase):
    def setUp(self):
        self.saved_pool = cli._GATEWAY_POOL

    def tearDown(self):
        cli._GATEWAY_POOL = self.saved_pool

    @staticmethod
    def args(**values):
        defaults = {"gateway": [], "gateway_mode": False, "login_mode": False}
        defaults.update(values)
        return SimpleNamespace(**defaults)

    def test_auto_mode_stays_native_on_ssh_login(self):
        with mock.patch.object(config, "load_client",
                               return_value=client_settings()) as load, \
             mock.patch.object(cli, "_is_remote_session", return_value=True), \
             mock.patch.object(cli.gateway_client, "GatewayPool") as pool:
            error = cli._configure_gateway_mode(self.args())
        self.assertEqual(error, "")
        self.assertIsNone(cli._GATEWAY_POOL)
        load.assert_not_called()
        pool.assert_not_called()

    def test_auto_mode_enables_local_pool_outside_ssh(self):
        sentinel = object()
        with mock.patch.object(config, "load_client",
                               return_value=client_settings()), \
             mock.patch.object(cli, "_is_remote_session", return_value=False), \
             mock.patch.object(cli.gateway_client, "GatewayPool",
                               return_value=sentinel):
            error = cli._configure_gateway_mode(self.args())
        self.assertEqual(error, "")
        self.assertIs(cli._GATEWAY_POOL, sentinel)

    def test_login_mode_always_preserves_native_deployment(self):
        settings = client_settings()
        settings["mode"] = "gateway"
        with mock.patch.object(config, "load_client", return_value=settings):
            error = cli._configure_gateway_mode(self.args(login_mode=True))
        self.assertEqual(error, "")
        self.assertIsNone(cli._GATEWAY_POOL)

    def test_cli_gateway_override_forces_local_mode(self):
        sentinel = object()
        settings = client_settings(gateways=())
        settings["mode"] = "login"
        with mock.patch.object(config, "load_client", return_value=settings), \
             mock.patch.object(cli.gateway_client, "GatewayPool",
                               return_value=sentinel) as pool:
            error = cli._configure_gateway_mode(
                self.args(gateway=["login9", "login10"]))
        self.assertEqual(error, "")
        self.assertIs(cli._GATEWAY_POOL, sentinel)
        self.assertEqual(
            pool.call_args.args[0]["gateways"], ["login9", "login10"])

    def test_local_first_run_offers_tui_setup_without_a_config(self):
        settings = dict(config.CLIENT_DEFAULTS)
        with mock.patch.object(cli, "_is_remote_session", return_value=False), \
             mock.patch.object(
                cli, "_load_client_config_bounded",
                return_value=(True, settings)):
            self.assertTrue(cli._should_offer_connection_setup(self.args()))
        settings["mode"] = "login"
        with mock.patch.object(cli, "_is_remote_session", return_value=False), \
             mock.patch.object(
                cli, "_load_client_config_bounded",
                return_value=(True, settings)):
            self.assertFalse(cli._should_offer_connection_setup(self.args()))

    def test_gateway_sequence_orders_states_across_remote_host_clocks(self):
        current = {
            "gateway_sequence": 8, "monotonic_clock_id": "login1:boot",
            "updated_monotonic": 500, "updated": "2026-08-01 12:00:10"}
        incoming = {
            "gateway_sequence": 9, "monotonic_clock_id": "login2:boot",
            "updated_monotonic": 1, "updated": "2026-08-01 12:00:00"}
        self.assertFalse(cli.AutotmuxApp._state_is_older(incoming, current))
        incoming["gateway_sequence"] = 7
        self.assertTrue(cli.AutotmuxApp._state_is_older(incoming, current))


class GatewayFrontendPilotTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_gateway_state_renders_without_starting_local_daemon(self):
        class FakePool:
            settings = {"state_timeout": 0.2}
            active_gateway = "login1"

            def fetch_state(self):
                state = remote_state()
                state.update({
                    "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "updated_monotonic": time.monotonic(),
                    "monotonic_clock_id": cli._CLOCK_ID,
                    "gateway": {
                        "mode": "gateway", "active": "login1",
                        "healthy": 2, "total": 2, "cached": False,
                        "items": [{"name": "login1", "latency_ms": 12.0}],
                    },
                })
                return True, state

            @staticmethod
            def read_snapshots():
                return True, {}

            @staticmethod
            def keepalive_entries(_fresh=False):
                return []

            @staticmethod
            def preview(_node, _session):
                return {"ok": True, "content": "preview"}

        saved = cli._GATEWAY_POOL
        cli._GATEWAY_POOL = FakePool()
        try:
            app = cli.AutotmuxApp()
            with mock.patch.object(
                    cli, "_launch_daemon",
                    side_effect=AssertionError("must not start local daemon")):
                async with app.run_test() as pilot:
                    await pilot.pause()
                    self.assertIn("gpu1", {row[0] for row in app.all_sessions})
                    self.assertIn("gateway login1", app.sub_title)
        finally:
            cli._GATEWAY_POOL = saved


if __name__ == "__main__":
    unittest.main()
