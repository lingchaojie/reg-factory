import os
import tempfile
import unittest

from common import ipmart_proxy


class FakeResponse:
    def __init__(self, status_code=200, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.trust_env = True
        self.proxies = {"http": "http://inherited.invalid"}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


class IPMartProxyTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "IPMART_ENABLED": "1",
            "IPMART_ACCESS_KEY": "top-secret-key",
            "IPMART_API_BASE": "https://api.example/getIps",
            "IPMART_COUNTRY": "US",
            "IPMART_STICKY_MINUTES": "30",
            "IPMART_MAX_ATTEMPTS": "3",
            "IPMART_IP_CHECK_URL": "https://check.example/ip",
        }
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.usage_path = os.path.join(self.tmp.name, "usage.jsonl")

    def test_parse_proxy_text_accepts_one_http_endpoint(self):
        self.assertEqual(
            ipmart_proxy.parse_proxy_text("proxy.example.com:3128\n"),
            ("proxy.example.com", 3128),
        )

    def test_parse_proxy_text_rejects_malformed_values(self):
        for body in (
            "<html>error</html>",
            "host:not-a-port",
            "host:70000",
            "missing-port",
            "",
        ):
            with self.subTest(body=body):
                with self.assertRaises(ipmart_proxy.IPMartProxyError):
                    ipmart_proxy.parse_proxy_text(body)

    def test_settings_require_key_when_enabled(self):
        env = dict(self.env, IPMART_ACCESS_KEY="")
        with self.assertRaisesRegex(
            ipmart_proxy.IPMartProxyError, "IPMART_ACCESS_KEY"
        ):
            ipmart_proxy.settings_from_env(env)

    def test_settings_validate_sticky_minutes_and_attempts(self):
        with self.assertRaisesRegex(
            ipmart_proxy.IPMartProxyError, "IPMART_STICKY_MINUTES"
        ):
            ipmart_proxy.settings_from_env(
                dict(self.env, IPMART_STICKY_MINUTES="31")
            )
        with self.assertRaisesRegex(
            ipmart_proxy.IPMartProxyError, "IPMART_MAX_ATTEMPTS"
        ):
            ipmart_proxy.settings_from_env(
                dict(self.env, IPMART_MAX_ATTEMPTS="0")
            )

    def test_acquire_uses_direct_api_and_verifies_returned_proxy(self):
        api = FakeSession([FakeResponse(text="edge.example:8080\n")])
        probe = FakeSession([FakeResponse(payload={"ip": "203.0.113.8"})])

        lease = ipmart_proxy.acquire_proxy(
            env=self.env,
            used_exit_ips=set(),
            usage_path=self.usage_path,
            api_session_factory=lambda: api,
            probe_session_factory=lambda: probe,
            reserve=False,
            sleep=lambda _seconds: None,
        )

        self.assertFalse(api.trust_env)
        self.assertEqual(api.proxies, {})
        self.assertEqual(
            api.calls[0][1]["params"],
            {
                "accessKey": "top-secret-key",
                "num": 1,
                "cntryCode": "US",
                "time": 30,
                "format": 1,
            },
        )
        self.assertFalse(probe.trust_env)
        self.assertEqual(
            probe.proxies,
            {
                "http": "http://edge.example:8080",
                "https": "http://edge.example:8080",
            },
        )
        self.assertEqual(
            lease,
            ipmart_proxy.ProxyLease(
                proxy_type="http",
                host="edge.example",
                port=8080,
                exit_ip="203.0.113.8",
            ),
        )

    def test_duplicate_exit_retries_with_a_new_endpoint(self):
        api = FakeSession(
            [
                FakeResponse(text="edge1.example:8001"),
                FakeResponse(text="edge2.example:8002"),
            ]
        )
        probe = FakeSession(
            [
                FakeResponse(payload={"ip": "203.0.113.8"}),
                FakeResponse(payload={"ip": "203.0.113.9"}),
            ]
        )

        lease = ipmart_proxy.acquire_proxy(
            env=self.env,
            used_exit_ips={"203.0.113.8"},
            usage_path=self.usage_path,
            api_session_factory=lambda: api,
            probe_session_factory=lambda: probe,
            reserve=False,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(lease.host, "edge2.example")
        self.assertEqual(lease.exit_ip, "203.0.113.9")
        self.assertEqual(len(api.calls), 2)

    def test_three_failed_attempts_raise_sanitized_error(self):
        api = FakeSession([FakeResponse(status_code=500, text="failure")] * 3)

        with self.assertRaises(ipmart_proxy.IPMartProxyError) as caught:
            ipmart_proxy.acquire_proxy(
                env=self.env,
                usage_path=self.usage_path,
                api_session_factory=lambda: api,
                reserve=False,
                sleep=lambda _seconds: None,
            )

        self.assertEqual(len(api.calls), 3)
        self.assertNotIn("top-secret-key", str(caught.exception))
        self.assertNotIn("accessKey", str(caught.exception))

    def test_verify_proxy_detects_exit_ip_change(self):
        lease = ipmart_proxy.ProxyLease(
            "http", "edge.example", 8080, "203.0.113.8"
        )
        probe = FakeSession([FakeResponse(payload={"ip": "203.0.113.9"})])

        with self.assertRaisesRegex(ipmart_proxy.IPMartProxyError, "exit changed"):
            ipmart_proxy.verify_proxy(
                lease,
                expected_exit_ip="203.0.113.8",
                env=self.env,
                session_factory=lambda: probe,
            )

    def test_usage_ledger_round_trip(self):
        lease = ipmart_proxy.ProxyLease(
            "http", "edge.example", 8080, "203.0.113.8"
        )

        ipmart_proxy.reserve_lease(lease, self.usage_path)

        self.assertEqual(
            ipmart_proxy.load_used_exit_ips(self.usage_path),
            {"203.0.113.8"},
        )
        with open(self.usage_path, encoding="utf-8") as stream:
            contents = stream.read()
        self.assertNotIn("top-secret-key", contents)


if __name__ == "__main__":
    unittest.main()
