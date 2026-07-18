import argparse
import os
import tempfile
import unittest
from unittest.mock import patch

import run_full_flow
from common import ipmart_proxy
from common.ipmart_proxy import IPMartProxyError, ProxyLease


def args_for_test(dry_run=False, **overrides):
    values = dict(
        dry_run=dry_run,
        skip_email=False,
        email="",
        password="",
        platforms=["claude"],
        node="auto",
        platform_timeout=600,
        broker="",
        keep_on_fail=False,
        import_c2a=False,
        codex=False,
        codex_group=None,
        codex_manual_phone=False,
        grok_sub2api=False,
        grok_sub2api_group=None,
        email_attempts=1,
        email_timeout=180,
        email_total_timeout=300,
        max_press="3",
        email_confirm_before_register=False,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


class FakeResponse:
    status_code = 200

    def __init__(self, exit_ip):
        self.exit_ip = exit_ip
        self.text = ""

    def json(self):
        return {"ip": self.exit_ip}


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.trust_env = True
        self.proxies = {}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


class FullFlowIPMartProxyTests(unittest.TestCase):
    def setUp(self):
        self.lease = ProxyLease(
            "http", "gateway.example", 8080,
            "account-res-US-sid-00000042", "proxy-secret", "00000042", "203.0.113.8",
        )
        self.base_env = {
            "EMAIL_PROVIDER": "OUTLOOK",
            "IPMART_ENABLED": "1",
            "IPMART_PROXY_HOST": "gateway.example",
            "IPMART_PROXY_PORT": "8080",
            "IPMART_PROXY_USERNAME_TEMPLATE": "account-res-US-sid-{sid}",
            "IPMART_PROXY_PASSWORD": "proxy-secret",
            "HTTP_PROXY": "http://127.0.0.1:7897",
            "HTTPS_PROXY": "http://127.0.0.1:7897",
            "http_proxy": "http://127.0.0.1:7897",
            "https_proxy": "http://127.0.0.1:7897",
            "CLASH_PROXY": "http://127.0.0.1:7897",
        }

    def test_one_lease_reaches_both_stages_and_is_rechecked(self):
        captured = []

        def fake_email(_args, env):
            captured.append(dict(env))
            return ("a@outlook.com", "Pass1!", "rt", "cid")

        def fake_platforms(_args, env, *_account):
            captured.append(dict(env))
            return 0

        verify_calls = []

        def fake_verify(lease, expected_exit_ip=None, **_kwargs):
            verify_calls.append((lease, expected_exit_ip))
            return expected_exit_ip

        with patch.object(
            run_full_flow, "stage_email", side_effect=fake_email
        ), patch.object(
            run_full_flow, "stage_platforms", side_effect=fake_platforms
        ):
            rc, email = run_full_flow.run_once(
                args_for_test(),
                self.base_env,
                acquire=lambda **_kwargs: self.lease,
                verify=fake_verify,
            )

        self.assertEqual((rc, email), (0, "a@outlook.com"))
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0], captured[1])
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertNotIn(key, captured[0])
        expected_lease_env = {
            "ACCOUNT_PROXY_SOURCE": "ipmart",
            "ACCOUNT_PROXY_TYPE": "http",
            "ACCOUNT_PROXY_HOST": "gateway.example",
            "ACCOUNT_PROXY_PORT": "8080",
            "ACCOUNT_PROXY_USERNAME": "account-res-US-sid-00000042",
            "ACCOUNT_PROXY_PASSWORD": "proxy-secret",
            "ACCOUNT_PROXY_SID": "00000042",
            "ACCOUNT_PROXY_EXIT_IP": "203.0.113.8",
        }
        for key, value in expected_lease_env.items():
            self.assertEqual(captured[0][key], value)
        self.assertEqual(verify_calls, [(self.lease, "203.0.113.8")])

    def test_changed_exit_aborts_before_platform_stage(self):
        with patch.object(
            run_full_flow,
            "stage_email",
            return_value=("a@outlook.com", "Pass1!", "rt", "cid"),
        ), patch.object(run_full_flow, "stage_platforms") as platforms:
            rc, email = run_full_flow.run_once(
                args_for_test(),
                self.base_env,
                acquire=lambda **_kwargs: self.lease,
                verify=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    IPMartProxyError("proxy exit changed")
                ),
            )

        self.assertEqual((rc, email), (1, "a@outlook.com"))
        platforms.assert_not_called()

    def test_acquisition_failure_aborts_before_email_stage(self):
        with patch.object(run_full_flow, "stage_email") as email_stage:
            rc, email = run_full_flow.run_once(
                args_for_test(),
                self.base_env,
                acquire=lambda **_kwargs: (_ for _ in ()).throw(
                    IPMartProxyError("provider unavailable")
                ),
            )

        self.assertEqual((rc, email), (1, ""))
        email_stage.assert_not_called()

    def test_dry_run_does_not_generate_a_sid_or_probe(self):
        with patch.object(
            run_full_flow,
            "stage_email",
            return_value=("dry-run@outlook.com", "Pass1!", "", ""),
        ), patch.object(run_full_flow, "stage_platforms", return_value=0):
            rc, _email = run_full_flow.run_once(
                args_for_test(dry_run=True),
                self.base_env,
                acquire=lambda **_kwargs: self.fail(
                    "dry-run consumed IPMart acquisition"
                ),
                verify=lambda **_kwargs: self.fail(
                    "dry-run consumed IPMart verification"
                ),
            )
        self.assertEqual(rc, 0)

    def test_no_graph_ready_mailbox_prevents_platform_launch(self):
        with patch.object(
            run_full_flow, "stage_email", return_value=None
        ), patch.object(run_full_flow, "stage_platforms") as platforms:
            rc, email = run_full_flow.run_once(
                args_for_test(),
                self.base_env,
                acquire=lambda **_kwargs: self.lease,
            )

        self.assertEqual((rc, email), (1, ""))
        platforms.assert_not_called()

    def test_skip_email_non_claude_does_not_acquire_or_verify(self):
        with patch.object(
            run_full_flow, "stage_platforms", return_value=0
        ):
            rc, email = run_full_flow.run_once(
                args_for_test(
                    skip_email=True,
                    email="existing@outlook.com",
                    password="Pass1!",
                    platforms=["chatgpt", "grok"],
                ),
                self.base_env,
                acquire=lambda **_kwargs: self.fail("unexpected acquisition"),
                verify=lambda *_args, **_kwargs: self.fail(
                    "unexpected verification"
                ),
            )

        self.assertEqual((rc, email), (0, "existing@outlook.com"))

    def test_outlook_only_lease_is_not_rechecked_without_claude(self):
        with patch.object(
            run_full_flow,
            "stage_email",
            return_value=("a@outlook.com", "Pass1!", "rt", "cid"),
        ), patch.object(run_full_flow, "stage_platforms", return_value=0):
            rc, email = run_full_flow.run_once(
                args_for_test(platforms=["chatgpt"]),
                self.base_env,
                acquire=lambda **_kwargs: self.lease,
                verify=lambda *_args, **_kwargs: self.fail(
                    "non-Claude flow was rechecked"
                ),
            )

        self.assertEqual((rc, email), (0, "a@outlook.com"))

    def test_non_claude_stage_recovers_exact_original_proxy_route(self):
        env = dict(self.base_env)
        original = {
            "HTTP_PROXY": "http://explicit-upper-http.example:8001",
            "HTTPS_PROXY": "http://explicit-upper-https.example:8002",
            "http_proxy": "http://explicit-lower-http.example:8003",
            "https_proxy": "http://explicit-lower-https.example:8004",
        }
        env.update(original)
        captured = {}

        def fake_email(_args, child_env):
            captured["outlook"] = dict(child_env)
            return ("a@outlook.com", "Pass1!", "rt", "cid")

        def fake_platforms(_args, child_env, *_account):
            captured["platforms"] = dict(child_env)
            return 0

        with patch.object(
            run_full_flow, "stage_email", side_effect=fake_email
        ), patch.object(
            run_full_flow, "stage_platforms", side_effect=fake_platforms
        ):
            rc, _email = run_full_flow.run_once(
                args_for_test(platforms=["claude", "chatgpt"]),
                env,
                acquire=lambda **_kwargs: self.lease,
                verify=lambda *_args, **_kwargs: self.lease.exit_ip,
            )

        self.assertEqual(rc, 0)
        for key in original:
            self.assertNotIn(key, captured["outlook"])
        for key, value in original.items():
            self.assertEqual(captured["platforms"][key], value)
        self.assertNotEqual(
            captured["platforms"]["HTTPS_PROXY"], env["CLASH_PROXY"]
        )

    def test_normal_first_candidate_flow_makes_two_dedicated_checks(self):
        fake_session = FakeSession(
            [FakeResponse("203.0.113.8"), FakeResponse("203.0.113.8")]
        )
        with tempfile.TemporaryDirectory() as tmp:
            usage_path = os.path.join(tmp, "usage.jsonl")

            def acquire(**kwargs):
                return ipmart_proxy.acquire_proxy(
                    **kwargs,
                    usage_path=usage_path,
                    session_factory=lambda: fake_session,
                    sid_factory=lambda: "00000042",
                    reserve=False,
                    sleep=lambda _seconds: None,
                )

            def verify(lease, **kwargs):
                return ipmart_proxy.verify_proxy(
                    lease, **kwargs, session_factory=lambda: fake_session
                )

            with patch.object(
                run_full_flow,
                "stage_email",
                return_value=("a@outlook.com", "Pass1!", "rt", "cid"),
            ), patch.object(run_full_flow, "stage_platforms", return_value=0):
                rc, _email = run_full_flow.run_once(
                    args_for_test(), self.base_env, acquire=acquire, verify=verify
                )

        self.assertEqual(rc, 0)
        self.assertEqual(len(fake_session.calls), 2)


if __name__ == "__main__":
    unittest.main()
