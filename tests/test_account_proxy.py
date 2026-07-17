import unittest

from common import account_proxy
from common.ipmart_proxy import IPMartProxyError, ProxyLease
from webui import scripts


class AccountProxyTests(unittest.TestCase):
    def test_runtime_lease_round_trip(self):
        lease = ProxyLease("http", "edge.example", 8080, "203.0.113.8")

        restored = account_proxy.lease_from_env(account_proxy.lease_to_env(lease))

        self.assertEqual(restored, lease)

    def test_missing_runtime_lease_returns_none(self):
        self.assertIsNone(account_proxy.lease_from_env({}))
        self.assertIsNone(
            account_proxy.lease_from_env({"ACCOUNT_PROXY_SOURCE": "clash"})
        )

    def test_bitbrowser_fields_have_no_credentials(self):
        lease = ProxyLease("http", "edge.example", 8080, "203.0.113.8")

        fields = account_proxy.bitbrowser_proxy_fields(lease)

        self.assertEqual(
            fields,
            {
                "proxyMethod": 2,
                "proxyType": "http",
                "host": "edge.example",
                "port": "8080",
            },
        )
        self.assertNotIn("proxyUserName", fields)
        self.assertNotIn("proxyPassword", fields)

    def test_invalid_inherited_lease_is_rejected(self):
        valid = account_proxy.lease_to_env(
            ProxyLease("http", "edge.example", 8080, "203.0.113.8")
        )
        invalid_cases = [
            dict(valid, ACCOUNT_PROXY_TYPE="socks5"),
            dict(valid, ACCOUNT_PROXY_HOST=""),
            dict(valid, ACCOUNT_PROXY_PORT="bad"),
            dict(valid, ACCOUNT_PROXY_PORT="70000"),
            dict(valid, ACCOUNT_PROXY_EXIT_IP="not-an-ip"),
        ]
        for env in invalid_cases:
            with self.subTest(env=env):
                with self.assertRaises(IPMartProxyError):
                    account_proxy.lease_from_env(env)

    def test_ipmart_configuration_keys_are_exposed_in_webui(self):
        keys = set(scripts.env_keys())
        self.assertTrue(
            {
                "IPMART_ENABLED",
                "IPMART_ACCESS_KEY",
                "IPMART_API_BASE",
                "IPMART_COUNTRY",
                "IPMART_STICKY_MINUTES",
                "IPMART_MAX_ATTEMPTS",
                "IPMART_IP_CHECK_URL",
            }.issubset(keys)
        )


if __name__ == "__main__":
    unittest.main()
