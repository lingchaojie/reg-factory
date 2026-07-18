import argparse
import unittest
from unittest.mock import patch

import run_full_flow
from common.ipmart_proxy import IPMartProxyError, ProxyLease


def args_for_test(dry_run=False):
    return argparse.Namespace(
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


class FullFlowIPMartProxyTests(unittest.TestCase):
    def setUp(self):
        self.lease = ProxyLease(
            "http", "gateway.example", 8080,
            "account-res-US-sid-00000042", "proxy-secret", "00000042", "203.0.113.8",
        )
        self.base_env = {
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


if __name__ == "__main__":
    unittest.main()
