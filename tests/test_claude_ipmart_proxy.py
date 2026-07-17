import unittest
from unittest.mock import Mock, patch

import register
from common.ipmart_proxy import ProxyLease


class ClaudeIPMartProxyTests(unittest.TestCase):
    def setUp(self):
        self.lease = ProxyLease(
            "http", "edge.example", 8080, "203.0.113.8"
        )

    def test_create_profile_uses_inherited_lease(self):
        bb = Mock()
        bb.create_browser.return_value = "profile-1"

        profile_id = register.create_claude_profile(
            bb, "claude-1", self.lease
        )

        self.assertEqual(profile_id, "profile-1")
        self.assertEqual(
            bb.create_browser.call_args.kwargs,
            {
                "name": "claude-1",
                "proxyMethod": 2,
                "proxyType": "http",
                "host": "edge.example",
                "port": "8080",
            },
        )

    def test_create_profile_preserves_default_without_lease(self):
        bb = Mock()
        bb.create_browser.return_value = "profile-1"

        register.create_claude_profile(bb, "claude-1", None)

        bb.create_browser.assert_called_once_with(name="claude-1")

    def test_inherited_lease_suppresses_clash_selection(self):
        with patch.object(register, "_pick_claude_node") as pick, patch.object(
            register.proxy_switch, "set_node"
        ) as set_node:
            register.configure_claude_proxy("auto", self.lease)

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


if __name__ == "__main__":
    unittest.main()
