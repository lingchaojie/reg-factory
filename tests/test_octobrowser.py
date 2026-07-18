import unittest
from unittest.mock import patch

import requests

import octobrowser
from octobrowser import OctoBrowser


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.trust_env = True

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


class OctoBrowserTests(unittest.TestCase):
    def make_browser(self, responses, token="token-value"):
        session = FakeSession(responses)
        browser = OctoBrowser(
            public_api="https://app.octobrowser.net",
            local_api="http://127.0.0.1:58888",
            api_token=token,
            session=session,
        )
        return browser, session

    def test_create_direct_profile_omits_proxy(self):
        browser, session = self.make_browser([
            FakeResponse({
                "success": True,
                "data": {"uuid": "profile-1"},
                "msg": "",
            }, 201)
        ])
        profile_id = browser.create_browser(
            name="direct", proxyType="noproxy"
        )
        self.assertEqual(profile_id, "profile-1")
        body = session.calls[0][2]["json"]
        self.assertEqual(body["title"], "direct")
        self.assertEqual(body["fingerprint"]["os"], "win")
        self.assertNotIn("proxy", body)

    def test_canonical_default_uses_exact_automation_profiles_url(self):
        session = FakeSession([
            FakeResponse({
                "success": True,
                "data": {"uuid": "profile-1"},
            }, 201)
        ])
        canonical = "https://app.octobrowser.net/api/v2/automation"
        with patch.object(octobrowser, "OCTO_PUBLIC_API_BASE", canonical):
            browser = OctoBrowser(api_token="token-value", session=session)
        browser.create_browser(name="canonical")
        self.assertEqual(browser.public_api, canonical)
        self.assertEqual(
            session.calls[0][1], canonical + "/profiles"
        )

    def test_legacy_host_root_normalizes_to_automation_profiles_url(self):
        session = FakeSession([
            FakeResponse({
                "success": True,
                "data": {"uuid": "profile-1"},
            }, 201)
        ])
        browser = OctoBrowser(
            public_api="https://legacy.example.test/",
            api_token="token-value",
            session=session,
        )
        browser.create_browser(name="legacy")
        automation_base = (
            "https://legacy.example.test/api/v2/automation"
        )
        self.assertEqual(browser.public_api, automation_base)
        self.assertEqual(
            session.calls[0][1], automation_base + "/profiles"
        )

    def test_create_maps_ipmart_proxy(self):
        browser, session = self.make_browser([
            FakeResponse({
                "success": True,
                "data": {"uuid": "profile-2"},
                "msg": "",
            }, 201)
        ])
        browser.create_browser(
            name="leased",
            proxyType="http",
            host="gateway.example",
            port="8080",
            proxyUserName="account-sid-00000042",
            proxyPassword="secret",
        )
        self.assertEqual(session.calls[0][2]["json"]["proxy"], {
            "type": "http",
            "host": "gateway.example",
            "port": 8080,
            "login": "account-sid-00000042",
            "password": "secret",
        })

    def test_start_normalizes_ws_endpoint(self):
        browser, session = self.make_browser([
            FakeResponse({
                "uuid": "profile-1",
                "ws_endpoint": "ws://127.0.0.1:55000/devtools/browser/id",
                "debug_port": "55000",
            })
        ])
        result = browser.open_browser("profile-1")
        self.assertEqual(
            result["ws"], "ws://127.0.0.1:55000/devtools/browser/id"
        )
        self.assertEqual(
            session.calls[0][1],
            "http://127.0.0.1:58888/api/profiles/start",
        )

    def test_list_and_delete_use_public_api(self):
        browser, session = self.make_browser([
            FakeResponse({
                "success": True,
                "data": [{"uuid": "p1", "title": "one"}],
                "total_count": 1,
            }),
            FakeResponse({
                "success": True,
                "data": {"deleted_uuids": ["p1"]},
            }),
        ])
        listed = browser.list_browsers()
        self.assertEqual(listed["data"]["list"][0]["id"], "p1")
        browser.delete_browser("p1")
        self.assertEqual(session.calls[1][2]["json"]["uuids"], ["p1"])

    def test_stop_uses_local_api(self):
        browser, session = self.make_browser([
            FakeResponse({"msg": "Profile stopped"})
        ])
        browser.close_browser("p1")
        self.assertEqual(
            session.calls[0][1],
            "http://127.0.0.1:58888/api/profiles/stop",
        )
        self.assertEqual(session.calls[0][2]["json"], {"uuid": "p1"})

    def test_cleanup_honors_keep_count(self):
        browser, session = self.make_browser([
            FakeResponse({
                "success": True,
                "data": [
                    {"uuid": "old", "title": "old"},
                    {"uuid": "new", "title": "new"},
                ],
                "total_count": 2,
            }),
            FakeResponse({"msg": "Profile stopped"}),
            FakeResponse({
                "success": True,
                "data": {"deleted_uuids": ["old"]},
            }),
        ])
        deleted = browser.cleanup_browsers(keep=1)
        self.assertEqual(deleted, 1)
        self.assertEqual(session.calls[1][2]["json"], {"uuid": "old"})

    def test_legacy_update_patches_existing_profile(self):
        browser, session = self.make_browser([
            FakeResponse({
                "success": True,
                "data": {"uuid": "existing"},
            })
        ])
        result = browser._post(
            "/browser/update",
            {
                "id": "existing",
                "name": "legacy",
                "proxyType": "http",
                "host": "127.0.0.1",
                "port": "7897",
            },
        )
        self.assertEqual(result, {
            "success": True,
            "data": {"id": "existing", "browserId": "existing"},
        })
        self.assertEqual(session.calls[0][0], "PATCH")
        self.assertTrue(session.calls[0][1].endswith(
            "/api/v2/automation/profiles/existing"
        ))

    def test_legacy_post_honors_requested_retry_budget(self):
        browser, session = self.make_browser([
            requests.RequestException("unavailable"),
            requests.RequestException("unavailable"),
            requests.RequestException("unavailable"),
            requests.RequestException("unavailable"),
            requests.RequestException("unavailable"),
        ])
        with patch("octobrowser.time.sleep"):
            with self.assertRaises(RuntimeError):
                browser._post(
                    "/browser/delete", {"id": "p1"}, _retries=1
                )
        self.assertEqual(len(session.calls), 1)

    def test_missing_token_fails_before_public_request(self):
        browser, session = self.make_browser([], token="")
        with self.assertRaisesRegex(RuntimeError, "OCTO_API_TOKEN"):
            browser.create_browser("missing-token")
        self.assertEqual(session.calls, [])

    def test_errors_redact_token_and_proxy_credentials(self):
        browser, _session = self.make_browser([
            FakeResponse({
                "success": False,
                "msg": "token-value account-sid-00000042 secret",
            }, 400)
        ])
        with self.assertRaises(RuntimeError) as caught:
            browser.create_browser(
                "leased",
                proxyType="http",
                host="gateway.example",
                port="8080",
                proxyUserName="account-sid-00000042",
                proxyPassword="secret",
            )
        rendered = str(caught.exception)
        for secret in ("token-value", "account-sid-00000042", "secret"):
            self.assertNotIn(secret, rendered)

    def test_local_api_error_includes_configured_base_url(self):
        browser, _session = self.make_browser([
            FakeResponse({"error": "client unavailable"}, 503)
        ])
        with self.assertRaises(RuntimeError) as caught:
            browser.open_browser("p1")
        self.assertIn("http://127.0.0.1:58888", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
