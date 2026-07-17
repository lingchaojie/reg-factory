import unittest
import os
from unittest.mock import patch

import outlook_reg_loop
from common.ipmart_proxy import ProxyLease


class OutlookIPMartProxyTests(unittest.TestCase):
    def setUp(self):
        self.lease = ProxyLease(
            "http", "edge.example", 8080, "203.0.113.8"
        )

    def test_profile_creation_applies_ipmart_http_proxy(self):
        response = {"success": True, "data": {"id": "profile-1"}}
        with patch.object(
            outlook_reg_loop, "_fingerprint_provider", return_value="bitbrowser"
        ), patch.object(
            outlook_reg_loop, "_bb_call", return_value=response
        ) as call:
            profile_id = outlook_reg_loop.bb_create_for_outlook_reg(
                "outlook-1", self.lease
            )

        self.assertEqual(profile_id, "profile-1")
        body = call.call_args.args[1]
        self.assertEqual(body["proxyMethod"], 2)
        self.assertEqual(body["proxyType"], "http")
        self.assertEqual(body["host"], "edge.example")
        self.assertEqual(body["port"], "8080")
        self.assertNotIn("proxyUserName", body)
        self.assertNotIn("proxyPassword", body)

    def test_profile_creation_keeps_noproxy_when_no_lease_exists(self):
        response = {"success": True, "data": {"id": "profile-1"}}
        with patch.object(
            outlook_reg_loop, "_fingerprint_provider", return_value="bitbrowser"
        ), patch.object(
            outlook_reg_loop, "_bb_call", return_value=response
        ) as call:
            outlook_reg_loop.bb_create_for_outlook_reg("outlook-1", None)

        body = call.call_args.args[1]
        self.assertEqual(body["proxyType"], "noproxy")
        self.assertNotIn("host", body)

    def test_ipmart_runtime_lease_disables_clash_rotation(self):
        env = {
            "ACCOUNT_PROXY_SOURCE": "ipmart",
            "ACCOUNT_PROXY_TYPE": "http",
            "ACCOUNT_PROXY_HOST": "edge.example",
            "ACCOUNT_PROXY_PORT": "8080",
            "ACCOUNT_PROXY_EXIT_IP": "203.0.113.8",
        }

        self.assertTrue(outlook_reg_loop.should_skip_clash_rotation(env))
        self.assertFalse(outlook_reg_loop.should_skip_clash_rotation({}))

    def test_clash_env_helper_still_handles_an_empty_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(outlook_reg_loop.ensure_clash_proxy_env(), "")


if __name__ == "__main__":
    unittest.main()
