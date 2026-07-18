import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import outlook_reg_loop
from common import account_proxy
from common.ipmart_proxy import ProxyLease


class OutlookIPMartProxyTests(unittest.TestCase):
    def setUp(self):
        self.lease = ProxyLease(
            "http", "gateway.example", 8080,
            "account-res-US-sid-00000042", "proxy-secret",
            "00000042", "203.0.113.8",
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
        self.assertEqual(body["host"], "gateway.example")
        self.assertEqual(body["port"], "8080")
        self.assertEqual(body["proxyUserName"], "account-res-US-sid-00000042")
        self.assertEqual(body["proxyPassword"], "proxy-secret")

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
        env = account_proxy.lease_to_env(self.lease)

        self.assertTrue(outlook_reg_loop.should_skip_clash_rotation(env))
        self.assertFalse(outlook_reg_loop.should_skip_clash_rotation({}))

    def test_clash_env_helper_still_handles_an_empty_environment(self):
        env = {}

        self.assertEqual(outlook_reg_loop.ensure_clash_proxy_env(env), "")
        self.assertEqual(env, {})

    def test_graph_extraction_forwards_the_lease_proxy(self):
        with patch(
            "extract_graph_tokens.get_graph_token",
            return_value={"refresh_token": "rt", "client_id": "cid"},
        ) as get_token:
            result = outlook_reg_loop.extract_graph_for_account(
                "a@outlook.com",
                "Pass1!",
                attempts=1,
                lease=self.lease,
            )

        self.assertEqual(result["refresh_token"], "rt")
        proxy_url = get_token.call_args.kwargs["proxy_url"]
        self.assertIn("account-res-US-sid-00000042", proxy_url)
        self.assertIn("gateway.example:8080", proxy_url)

    def test_ipmart_network_setup_removes_inherited_clash_proxy(self):
        env = account_proxy.lease_to_env(self.lease)
        env.update(
            {
                "HTTP_PROXY": "http://127.0.0.1:7897",
                "HTTPS_PROXY": "http://127.0.0.1:7897",
                "CLASH_PROXY": "http://127.0.0.1:7897",
            }
        )

        with patch.object(outlook_reg_loop, "ensure_clash_proxy_env") as ensure:
            result = outlook_reg_loop.prepare_outlook_network(
                env,
                lease=self.lease,
                ipmart_enabled=True,
            )

        self.assertEqual(result, "")
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertEqual(env["CLASH_PROXY"], "http://127.0.0.1:7897")
        ensure.assert_not_called()

    def test_graph_retry_does_not_rotate_clash_with_an_account_lease(self):
        responses = [None, {"refresh_token": "rt", "client_id": "cid"}]
        with patch(
            "extract_graph_tokens.get_graph_token",
            side_effect=responses,
        ), patch("common.proxy_switch.set_node") as set_node, patch.object(
            outlook_reg_loop.time,
            "sleep",
        ):
            result = outlook_reg_loop.extract_graph_for_account(
                "a@outlook.com",
                "Pass1!",
                attempts=2,
                lease=self.lease,
            )

        self.assertEqual(result["refresh_token"], "rt")
        set_node.assert_not_called()

    def test_inherited_round_stops_after_graph_recovery_is_saved(self):
        attempt = AsyncMock(
            return_value=("a@outlook.com", "Pass1!", [])
        )
        with patch.object(
            sys, "argv", ["outlook_reg_loop.py", "--count", "2", "--sleep", "0"]
        ), patch.object(
            outlook_reg_loop, "lease_from_env", return_value=self.lease
        ), patch.object(
            outlook_reg_loop,
            "settings_from_env",
            return_value=SimpleNamespace(enabled=True),
        ), patch.object(
            outlook_reg_loop, "load_standalone", return_value=object()
        ), patch.object(
            outlook_reg_loop, "prepare_outlook_network", return_value=""
        ), patch.object(
            outlook_reg_loop, "clash_proxy_from_env", return_value=None
        ), patch.object(
            outlook_reg_loop, "count_pool", return_value=0
        ), patch.object(
            outlook_reg_loop, "one_attempt", attempt
        ), patch.object(
            outlook_reg_loop, "extract_graph_for_account", return_value=None
        ), patch.object(
            outlook_reg_loop, "append_no_graph_account"
        ) as append_recovery, patch.object(
            outlook_reg_loop, "write_record"
        ) as write_record, patch.object(
            outlook_reg_loop.time, "sleep"
        ), patch.object(
            outlook_reg_loop, "log"
        ):
            outlook_reg_loop.main()

        self.assertEqual(attempt.await_count, 1)
        append_recovery.assert_called_once_with("a@outlook.com", "Pass1!")
        write_record.assert_not_called()

    def test_ipmart_profile_failure_hides_credentialed_api_text(self):
        leaked = (
            "http://account-res-US-sid-00000042:proxy-secret@"
            "gateway.example:8080 account-res-US-sid-{sid}"
        )
        with patch.object(
            outlook_reg_loop, "_fingerprint_provider", return_value="bitbrowser"
        ), patch.object(
            outlook_reg_loop,
            "_bb_call",
            return_value={"success": False, "msg": leaked},
        ), self.assertRaises(RuntimeError) as caught:
            outlook_reg_loop.bb_create_for_outlook_reg("outlook-1", self.lease)

        rendered = str(caught.exception)
        self.assertEqual(
            rendered,
            "BitBrowser profile creation failed with IPMart account proxy",
        )
        for secret in (
            "account-res-US-sid-00000042",
            "proxy-secret",
            "account-res-US-sid-{sid}",
            leaked,
        ):
            self.assertNotIn(secret, rendered)

    def test_legacy_profile_failure_keeps_useful_api_text(self):
        with patch.object(
            outlook_reg_loop, "_fingerprint_provider", return_value="bitbrowser"
        ), patch.object(
            outlook_reg_loop,
            "_bb_call",
            return_value={"success": False, "msg": "window quota exceeded"},
        ), self.assertRaisesRegex(RuntimeError, "window quota exceeded"):
            outlook_reg_loop.bb_create_for_outlook_reg("outlook-1", None)


if __name__ == "__main__":
    unittest.main()
