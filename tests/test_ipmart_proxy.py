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
        self.proxies = {"https": "http://inherited.invalid"}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


class IPMartProxyTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "IPMART_ENABLED": "1",
            "IPMART_PROXY_HOST": "gateway.example",
            "IPMART_PROXY_PORT": "8080",
            "IPMART_PROXY_USERNAME_TEMPLATE": "account-res-US-sid-{sid}",
            "IPMART_PROXY_PASSWORD": "p@ss/word",
            "IPMART_MAX_ATTEMPTS": "3",
            "IPMART_IP_CHECK_URL": "https://check.example/ip",
        }
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.usage_path = os.path.join(self.tmp.name, "usage.jsonl")

    def test_settings_require_gateway_credentials_and_one_sid_placeholder(self):
        bad = [
            dict(self.env, IPMART_PROXY_HOST=""),
            dict(self.env, IPMART_PROXY_PORT="bad"),
            dict(self.env, IPMART_PROXY_PORT="70000"),
            dict(self.env, IPMART_PROXY_USERNAME_TEMPLATE="account-res-US"),
            dict(self.env, IPMART_PROXY_USERNAME_TEMPLATE="{sid}-{sid}"),
            dict(self.env, IPMART_PROXY_PASSWORD=""),
            dict(self.env, IPMART_MAX_ATTEMPTS="0"),
        ]
        for env in bad:
            with self.subTest(env=env):
                with self.assertRaises(ipmart_proxy.IPMartProxyError):
                    ipmart_proxy.settings_from_env(env)

    def test_max_attempts_defaults_to_three_when_missing(self):
        env = dict(self.env)
        env.pop("IPMART_MAX_ATTEMPTS")
        self.assertEqual(ipmart_proxy.settings_from_env(env).max_attempts, 3)

    def test_generate_sid_is_eight_digits_and_preserves_leading_zeroes(self):
        self.assertEqual(ipmart_proxy.generate_sid(lambda _limit: 42), "00000042")

    def test_proxy_url_percent_encodes_credentials_without_repr_leak(self):
        lease = ipmart_proxy.ProxyLease(
            "http", "gateway.example", 8080,
            "account-res-US-sid-00000042", "p@ss/word", "00000042", "203.0.113.8",
        )
        self.assertEqual(
            ipmart_proxy.requests_proxy_url(lease),
            "http://account-res-US-sid-00000042:p%40ss%2Fword@gateway.example:8080",
        )
        self.assertNotIn("p@ss/word", repr(lease))
        self.assertNotIn("account-res-US", repr(lease))

    def test_acquire_renders_sid_and_verifies_through_credentialed_proxy(self):
        session = FakeSession([FakeResponse(payload={"ip": "203.0.113.8"})])
        lease = ipmart_proxy.acquire_proxy(
            env=self.env,
            usage_path=self.usage_path,
            session_factory=lambda: session,
            sid_factory=lambda: "00000042",
            reserve=False,
            sleep=lambda _seconds: None,
        )
        self.assertFalse(session.trust_env)
        self.assertEqual(lease.sid, "00000042")
        self.assertEqual(lease.username, "account-res-US-sid-00000042")
        self.assertEqual(lease.exit_ip, "203.0.113.8")
        self.assertEqual(
            session.proxies,
            {
                "http": "http://account-res-US-sid-00000042:p%40ss%2Fword@gateway.example:8080",
                "https": "http://account-res-US-sid-00000042:p%40ss%2Fword@gateway.example:8080",
            },
        )

    def test_duplicate_exit_retries_with_a_new_sid(self):
        session = FakeSession([
            FakeResponse(payload={"ip": "203.0.113.8"}),
            FakeResponse(payload={"ip": "203.0.113.9"}),
        ])
        sids = iter(["00000042", "00000043"])
        lease = ipmart_proxy.acquire_proxy(
            env=self.env,
            used_exit_ips={"203.0.113.8"},
            usage_path=self.usage_path,
            session_factory=lambda: session,
            sid_factory=lambda: next(sids),
            reserve=False,
            sleep=lambda _seconds: None,
        )
        self.assertEqual((lease.sid, lease.exit_ip), ("00000043", "203.0.113.9"))

    def test_ledger_contains_sid_and_exit_but_no_credentials(self):
        lease = ipmart_proxy.ProxyLease(
            "http", "gateway.example", 8080,
            "account-res-US-sid-00000042", "p@ss/word", "00000042", "203.0.113.8",
        )
        ipmart_proxy.reserve_lease(lease, self.usage_path)
        with open(self.usage_path, encoding="utf-8") as stream:
            contents = stream.read()
        self.assertIn('"sid": "00000042"', contents)
        self.assertIn('"exit_ip": "203.0.113.8"', contents)
        self.assertNotIn("account-res-US", contents)
        self.assertNotIn("p@ss/word", contents)

    def test_settings_and_errors_do_not_reveal_credentials(self):
        settings = ipmart_proxy.settings_from_env(self.env)
        self.assertNotIn("account-res-US", repr(settings))
        self.assertNotIn("p@ss/word", repr(settings))
        session = FakeSession([FakeResponse(status_code=407, text="denied")])
        with self.assertRaises(ipmart_proxy.IPMartProxyError) as caught:
            ipmart_proxy.acquire_proxy(
                env=dict(self.env, IPMART_MAX_ATTEMPTS="1"),
                usage_path=self.usage_path,
                session_factory=lambda: session,
                sid_factory=lambda: "00000042",
                reserve=False,
                sleep=lambda _seconds: None,
            )
        rendered = str(caught.exception)
        self.assertNotIn("account-res-US", rendered)
        self.assertNotIn("p@ss/word", rendered)

    def test_retry_uses_a_different_sid_and_stops_at_attempt_limit(self):
        session = FakeSession([
            FakeResponse(status_code=502),
            FakeResponse(status_code=502),
            FakeResponse(status_code=502),
        ])
        sids = iter(["00000042", "00000043", "00000044"])
        with self.assertRaises(ipmart_proxy.IPMartProxyError):
            ipmart_proxy.acquire_proxy(
                env=self.env,
                usage_path=self.usage_path,
                session_factory=lambda: session,
                sid_factory=lambda: next(sids),
                reserve=False,
                sleep=lambda _seconds: None,
            )
        self.assertEqual(len(session.calls), 3)

    def test_configured_attempt_limit_above_three_is_honored(self):
        session = FakeSession([FakeResponse(status_code=502) for _ in range(5)])
        sids = iter(f"{number:08d}" for number in range(42, 47))
        with self.assertRaises(ipmart_proxy.IPMartProxyError):
            ipmart_proxy.acquire_proxy(
                env=dict(self.env, IPMART_MAX_ATTEMPTS="5"),
                usage_path=self.usage_path,
                session_factory=lambda: session,
                sid_factory=lambda: next(sids),
                reserve=False,
                sleep=lambda _seconds: None,
            )
        self.assertEqual(len(session.calls), 5)

    def test_verify_rejects_changed_exit_through_the_same_proxy(self):
        lease = ipmart_proxy.ProxyLease(
            "http", "gateway.example", 8080,
            "account-res-US-sid-00000042", "p@ss/word", "00000042", "203.0.113.8",
        )
        session = FakeSession([FakeResponse(payload={"ip": "203.0.113.9"})])
        with self.assertRaisesRegex(ipmart_proxy.IPMartProxyError, "exit changed"):
            ipmart_proxy.verify_proxy(
                lease,
                expected_exit_ip=lease.exit_ip,
                env=self.env,
                session_factory=lambda: session,
            )
        self.assertFalse(session.trust_env)
        self.assertEqual(session.proxies["http"], ipmart_proxy.requests_proxy_url(lease))

    def test_old_ledger_records_still_reserve_exit_ips(self):
        with open(self.usage_path, "w", encoding="utf-8") as stream:
            stream.write('{"endpoint":"old.example:8000","exit_ip":"203.0.113.8"}\n')
        self.assertEqual(
            ipmart_proxy.load_used_exit_ips(self.usage_path),
            {"203.0.113.8"},
        )


if __name__ == "__main__":
    unittest.main()
