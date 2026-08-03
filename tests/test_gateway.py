"""Tests for local-client multi-login routing and the SSH-stdio agent."""

import io
import json
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from autotmux import agent, cli, config, gateway


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


class GatewayPoolTests(unittest.TestCase):
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
            response = pool._rpc_gateway(
                "login1", {"action": "ping"}, timeout=2)
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
        standby_ping = threading.Event()

        def rpc(name, payload, _timeout=None):
            if payload["action"] == "ping":
                self.assertEqual(name, "login2")
                standby_ping.set()
                return {"ok": True}
            return {"ok": True, "state": remote_state(), "host": "login1"}

        with mock.patch.object(pool, "_rpc_gateway", side_effect=rpc):
            ok, _ = pool.fetch_state()
            self.assertTrue(standby_ping.wait(0.5))
        self.assertTrue(ok)

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
            ["cluster-user@gpu1", "exec tmux attach -t 'train session'"])
        self.assertNotIn("atmux-agent", " ".join(argv))

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
