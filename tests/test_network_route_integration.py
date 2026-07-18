import argparse
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import outlook_reg_loop
import register
import register_three_platforms
import run_full_flow
from common.network_route import NetworkRoute


ROUTE_MODE_KEY = "NETWORK_ROUTE_MODE"


class NetworkRouteIntegrationTests(unittest.TestCase):
    def test_full_flow_strips_dead_clash_when_ipmart_is_disabled(self):
        args = argparse.Namespace(
            proxy="http://127.0.0.1:7897",
            clash_api="http://127.0.0.1:9097",
            clash_secret="",
            clash_group="GLOBAL",
        )
        base = {
            "IPMART_ENABLED": "0",
            "CLASH_PROXY": "http://127.0.0.1:7897",
        }
        with patch.object(run_full_flow.os, "environ", base), patch(
            "common.network_route.socket.create_connection",
            side_effect=ConnectionRefusedError,
        ):
            env = run_full_flow.build_child_env(args)
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertNotIn(key, env)

    def test_full_flow_explicit_proxy_overrides_clash_candidate(self):
        args = argparse.Namespace(
            proxy="http://explicit.example:8123",
            clash_api="http://127.0.0.1:9097",
            clash_secret="",
            clash_group="GLOBAL",
        )
        base = {
            "IPMART_ENABLED": "0",
            "CLASH_PROXY": "http://configured.example:7897",
        }
        connector = Mock(return_value=Mock())
        with patch.object(run_full_flow.os, "environ", base), patch(
            "common.network_route.socket.create_connection", connector
        ):
            env = run_full_flow.build_child_env(args)
        connector.assert_called_once_with(("explicit.example", 8123), 0.5)
        self.assertEqual(env["CLASH_PROXY"], args.proxy)
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertEqual(env[key], args.proxy)

    def test_full_flow_explicit_proxy_is_candidate_without_clash_env(self):
        args = argparse.Namespace(
            proxy="http://explicit.example:8123",
            clash_api="http://127.0.0.1:9097",
            clash_secret="",
            clash_group="GLOBAL",
        )
        base = {"IPMART_ENABLED": "0"}
        connector = Mock(return_value=Mock())
        with patch.object(run_full_flow.os, "environ", base), patch(
            "common.network_route.socket.create_connection", connector
        ):
            env = run_full_flow.build_child_env(args)
        connector.assert_called_once_with(("explicit.example", 8123), 0.5)
        self.assertEqual(env["CLASH_PROXY"], args.proxy)
        self.assertEqual(env["HTTPS_PROXY"], args.proxy)

    def test_full_flow_explicit_empty_proxy_forces_direct(self):
        args = argparse.Namespace(
            proxy="",
            proxy_explicit=True,
            clash_api="http://127.0.0.1:9097",
            clash_secret="",
            clash_group="GLOBAL",
        )
        base = {
            "IPMART_ENABLED": "0",
            "CLASH_PROXY": "http://inherited.example:7897",
            "HTTP_PROXY": "http://inherited.example:7897",
            "HTTPS_PROXY": "http://inherited.example:7897",
        }
        connector = Mock(return_value=Mock())
        with patch.object(run_full_flow.os, "environ", base), patch(
            "common.network_route.socket.create_connection", connector
        ):
            env = run_full_flow.build_child_env(args)
        connector.assert_not_called()
        self.assertNotIn("CLASH_PROXY", env)
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertNotIn(key, env)

    def test_full_flow_unprovided_proxy_uses_default_candidate(self):
        default_proxy = "http://default.example:7897"
        args = argparse.Namespace(
            proxy=default_proxy,
            proxy_explicit=False,
            clash_api="http://127.0.0.1:9097",
            clash_secret="",
            clash_group="GLOBAL",
        )
        base = {
            "IPMART_ENABLED": "0",
            "CLASH_PROXY": default_proxy,
        }
        connector = Mock(return_value=Mock())
        with patch.object(run_full_flow.os, "environ", base), patch(
            "common.network_route.socket.create_connection", connector
        ):
            env = run_full_flow.build_child_env(args)
        connector.assert_called_once_with(("default.example", 7897), 0.5)
        self.assertEqual(env["HTTPS_PROXY"], default_proxy)

    def test_resolved_direct_stays_direct_when_listener_becomes_reachable(self):
        proxy = "http://127.0.0.1:7897"
        args = argparse.Namespace(
            proxy=proxy,
            clash_api="http://127.0.0.1:9097",
            clash_secret="",
            clash_group="GLOBAL",
        )
        with patch.object(
            run_full_flow.os,
            "environ",
            {"IPMART_ENABLED": "0", "CLASH_PROXY": proxy},
        ), patch(
            "common.network_route.socket.create_connection",
            side_effect=ConnectionRefusedError,
        ):
            top_env = run_full_flow.build_child_env(args)

        downstream_connector = Mock(return_value=Mock())
        with patch(
            "common.network_route.socket.create_connection",
            downstream_connector,
        ):
            outlook_env = dict(top_env)
            selected = outlook_reg_loop.prepare_outlook_network(outlook_env)
            platform_env = register_three_platforms.platform_child_env(
                "grok", top_env, ["grok"]
            )

        downstream_connector.assert_not_called()
        self.assertEqual(top_env[ROUTE_MODE_KEY], "direct")
        self.assertNotIn("://", top_env[ROUTE_MODE_KEY])
        self.assertEqual(selected, "")
        for env in (outlook_env, platform_env):
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                self.assertNotIn(key, env)

    def test_resolved_clash_stays_clash_when_listener_becomes_unreachable(self):
        proxy = "http://127.0.0.1:7897"
        args = argparse.Namespace(
            proxy=proxy,
            clash_api="http://127.0.0.1:9097",
            clash_secret="",
            clash_group="GLOBAL",
        )
        with patch.object(
            run_full_flow.os,
            "environ",
            {"IPMART_ENABLED": "0", "CLASH_PROXY": proxy},
        ), patch(
            "common.network_route.socket.create_connection",
            return_value=Mock(),
        ):
            top_env = run_full_flow.build_child_env(args)

        downstream_connector = Mock(side_effect=ConnectionRefusedError)
        with patch(
            "common.network_route.socket.create_connection",
            downstream_connector,
        ):
            outlook_env = dict(top_env)
            selected = outlook_reg_loop.prepare_outlook_network(outlook_env)
            platform_env = register_three_platforms.platform_child_env(
                "grok", top_env, ["grok"]
            )

        downstream_connector.assert_not_called()
        self.assertEqual(top_env[ROUTE_MODE_KEY], "clash")
        self.assertNotIn("://", top_env[ROUTE_MODE_KEY])
        self.assertEqual(selected, proxy)
        for env in (outlook_env, platform_env):
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                self.assertEqual(env[key], proxy)

    def test_outlook_uses_direct_when_clash_is_unreachable(self):
        env = {
            "CLASH_PROXY": "http://127.0.0.1:7897",
            "HTTP_PROXY": "http://127.0.0.1:7897",
        }
        with patch(
            "common.network_route.socket.create_connection",
            side_effect=ConnectionRefusedError,
        ):
            selected = outlook_reg_loop.prepare_outlook_network(env)
        self.assertEqual(selected, "")
        self.assertNotIn("HTTP_PROXY", env)

    def test_claude_skips_node_selection_in_direct_mode(self):
        with patch.object(register, "_pick_claude_node") as pick:
            register.configure_claude_proxy(
                "auto",
                account_lease=None,
                ipmart_enabled=False,
                clash_available=False,
            )
        pick.assert_not_called()
        self.assertIsNone(register.CLAUDE_PROXY_NODE)

    def test_platform_child_env_does_not_rebuild_dead_clash(self):
        env = {
            "IPMART_ENABLED": "0",
            "CLASH_PROXY": "http://127.0.0.1:7897",
            "HTTP_PROXY": "http://127.0.0.1:7897",
        }
        with patch(
            "common.network_route.socket.create_connection",
            side_effect=ConnectionRefusedError,
        ):
            child = register_three_platforms.platform_child_env(
                "grok", env, ["grok"]
            )
        self.assertNotIn("HTTP_PROXY", child)

    def test_ipmart_branch_does_not_probe_or_change_coverage(self):
        env = {"IPMART_ENABLED": "1", "HTTP_PROXY": "http://legacy"}
        lease = SimpleNamespace(host="gateway", port=8000, exit_ip="203.0.113.8")
        with patch("common.network_route.resolve_clash_route") as resolve:
            register.prepare_claude_network(
                env, account_lease=lease, ipmart_enabled=True
            )
        resolve.assert_not_called()
        self.assertNotIn("HTTP_PROXY", env)


if __name__ == "__main__":
    unittest.main()
