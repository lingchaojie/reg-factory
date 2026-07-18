import unittest
from unittest.mock import Mock, patch

import register
from common.ipmart_proxy import ProxyLease


def make_lease():
    return ProxyLease(
        "http", "gateway.example", 8080,
        "account-res-US-sid-00000042", "proxy-secret", "00000042", "203.0.113.8",
    )


class ClaudeIPMartProxyTests(unittest.TestCase):
    def test_create_profile_uses_inherited_credentialed_lease(self):
        bb = Mock()
        bb.create_browser.return_value = "profile-1"
        profile_id = register.create_claude_profile(bb, "claude-1", make_lease())
        self.assertEqual(profile_id, "profile-1")
        self.assertEqual(bb.create_browser.call_args.kwargs, {
            "name": "claude-1",
            "proxyMethod": 2,
            "proxyType": "http",
            "host": "gateway.example",
            "port": "8080",
            "proxyUserName": "account-res-US-sid-00000042",
            "proxyPassword": "proxy-secret",
        })

    def test_create_profile_preserves_default_without_lease(self):
        bb = Mock()
        register.create_claude_profile(bb, "claude-1", None)
        bb.create_browser.assert_called_once_with(name="claude-1")

    def test_inherited_lease_suppresses_clash_selection(self):
        with patch.object(register, "_pick_claude_node") as pick, patch.object(
            register.proxy_switch, "set_node"
        ) as set_node:
            register.configure_claude_proxy("auto", make_lease())
        pick.assert_not_called()
        set_node.assert_not_called()
        self.assertIsNone(register.CLAUDE_PROXY_NODE)

    def test_enabled_ipmart_suppresses_clash_before_direct_acquisition(self):
        with patch.object(register, "_pick_claude_node") as pick:
            register.configure_claude_proxy(
                "auto", account_lease=None, ipmart_enabled=True
            )
        pick.assert_not_called()
        self.assertIsNone(register.CLAUDE_PROXY_NODE)

    def test_ipmart_strips_process_http_proxy_before_acquisition(self):
        env = {
            "HTTP_PROXY": "http://127.0.0.1:7897",
            "HTTPS_PROXY": "http://127.0.0.1:7897",
            "CLASH_PROXY": "http://127.0.0.1:7897",
        }
        register.prepare_claude_network(
            env, account_lease=make_lease(), ipmart_enabled=False
        )
        self.assertEqual(env, {"CLASH_PROXY": "http://127.0.0.1:7897"})

    def test_enabled_ipmart_strips_http_proxy_for_direct_acquisition(self):
        env = {"HTTP_PROXY": "http://127.0.0.1:7897"}
        register.prepare_claude_network(
            env, account_lease=None, ipmart_enabled=True
        )
        self.assertEqual(env, {})

    def test_disabled_ipmart_without_lease_preserves_http_proxy(self):
        env = {"HTTP_PROXY": "http://127.0.0.1:7897"}
        result = register.prepare_claude_network(
            env, account_lease=None, ipmart_enabled=False
        )
        self.assertIs(result, env)
        self.assertEqual(env, {"HTTP_PROXY": "http://127.0.0.1:7897"})


if __name__ == "__main__":
    unittest.main()
