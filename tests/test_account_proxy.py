import unittest

from common import account_proxy
from common.ipmart_proxy import IPMartProxyError, ProxyLease
from webui import scripts


def make_lease():
    return ProxyLease(
        "http", "gateway.example", 8080,
        "account-res-US-sid-00000042", "proxy-secret", "00000042", "203.0.113.8",
    )


class AccountProxyTests(unittest.TestCase):
    def test_runtime_lease_round_trip_includes_sid_credentials(self):
        lease = make_lease()
        env = account_proxy.lease_to_env(lease)
        self.assertEqual(env["ACCOUNT_PROXY_SID"], "00000042")
        self.assertEqual(env["ACCOUNT_PROXY_USERNAME"], lease.username)
        self.assertEqual(env["ACCOUNT_PROXY_PASSWORD"], "proxy-secret")
        self.assertEqual(account_proxy.lease_from_env(env), lease)

    def test_missing_runtime_lease_returns_none(self):
        self.assertIsNone(account_proxy.lease_from_env({}))
        self.assertIsNone(
            account_proxy.lease_from_env({"ACCOUNT_PROXY_SOURCE": "clash"})
        )

    def test_bitbrowser_fields_include_credentials(self):
        fields = account_proxy.bitbrowser_proxy_fields(make_lease())
        self.assertEqual(fields["proxyUserName"], "account-res-US-sid-00000042")
        self.assertEqual(fields["proxyPassword"], "proxy-secret")

    def test_invalid_inherited_lease_is_rejected(self):
        valid = account_proxy.lease_to_env(make_lease())
        invalid_cases = [
            dict(valid, ACCOUNT_PROXY_TYPE="socks5"),
            dict(valid, ACCOUNT_PROXY_HOST=""),
            dict(valid, ACCOUNT_PROXY_PORT="bad"),
            dict(valid, ACCOUNT_PROXY_PORT="70000"),
            dict(valid, ACCOUNT_PROXY_USERNAME=""),
            dict(valid, ACCOUNT_PROXY_PASSWORD=""),
            dict(valid, ACCOUNT_PROXY_SID="42"),
            dict(valid, ACCOUNT_PROXY_USERNAME="account-without-session"),
            dict(valid, ACCOUNT_PROXY_EXIT_IP="not-an-ip"),
        ]
        for env in invalid_cases:
            with self.subTest(keys=sorted(env)):
                with self.assertRaises(IPMartProxyError):
                    account_proxy.lease_from_env(env)

    def test_strip_http_proxy_env_removes_all_cases_but_keeps_clash_config(self):
        env = {
            "HTTP_PROXY": "http://127.0.0.1:7897",
            "HTTPS_PROXY": "http://127.0.0.1:7897",
            "http_proxy": "http://127.0.0.1:7897",
            "https_proxy": "http://127.0.0.1:7897",
            "CLASH_PROXY": "http://127.0.0.1:7897",
        }
        account_proxy.strip_http_proxy_env(env)
        self.assertEqual(env, {"CLASH_PROXY": "http://127.0.0.1:7897"})

    def test_strip_account_proxy_env_removes_the_complete_transient_lease(self):
        env = account_proxy.lease_to_env(make_lease())
        env["CLASH_PROXY"] = "http://127.0.0.1:7897"
        account_proxy.strip_account_proxy_env(env)
        self.assertEqual(env, {"CLASH_PROXY": "http://127.0.0.1:7897"})

    def test_ipmart_sid_configuration_is_exposed_in_webui(self):
        groups = scripts.ENV_SCHEMA
        items = {
            item["key"]: item
            for group in groups
            for item in group.get("items", [])
        }
        expected = {
            "IPMART_ENABLED",
            "IPMART_PROXY_HOST",
            "IPMART_PROXY_PORT",
            "IPMART_PROXY_USERNAME_TEMPLATE",
            "IPMART_PROXY_PASSWORD",
            "IPMART_MAX_ATTEMPTS",
            "IPMART_IP_CHECK_URL",
        }
        self.assertTrue(expected.issubset(items))
        self.assertTrue(items["IPMART_PROXY_USERNAME_TEMPLATE"]["secret"])
        self.assertTrue(items["IPMART_PROXY_PASSWORD"]["secret"])
        for obsolete_suffix in (
            "ACCESS_KEY", "API_BASE", "COUNTRY", "STICKY_MINUTES",
        ):
            self.assertNotIn(f"IPMART_{obsolete_suffix}", items)


if __name__ == "__main__":
    unittest.main()
