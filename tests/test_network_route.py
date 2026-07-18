import unittest

from common import network_route


class FakeSocket:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class NetworkRouteTests(unittest.TestCase):
    def test_reachable_clash_endpoint_is_selected(self):
        calls = []
        sock = FakeSocket()

        def connector(address, timeout):
            calls.append((address, timeout))
            return sock

        env = {"CLASH_PROXY": "http://127.0.0.1:7897"}
        route = network_route.resolve_clash_route(
            env, connector=connector, timeout=0.25
        )

        self.assertEqual(route.mode, "clash")
        self.assertEqual(route.proxy_url, "http://127.0.0.1:7897")
        self.assertEqual(calls, [(("127.0.0.1", 7897), 0.25)])
        self.assertTrue(sock.closed)

    def test_unreachable_clash_endpoint_selects_direct(self):
        def connector(_address, _timeout):
            raise ConnectionRefusedError("refused")

        route = network_route.resolve_clash_route(
            {"CLASH_PROXY": "http://127.0.0.1:7897"},
            connector=connector,
        )

        self.assertEqual(route.mode, "direct")
        self.assertEqual(route.reason, "unreachable")

    def test_missing_and_invalid_endpoints_select_direct(self):
        for env in (
            {},
            {"CLASH_PROXY": ""},
            {"CLASH_PROXY": "not a proxy"},
            {"CLASH_PROXY": "http://127.0.0.1:0"},
        ):
            with self.subTest(env=env):
                route = network_route.resolve_clash_route(env)
                self.assertEqual(route.mode, "direct")

    def test_direct_route_removes_all_inherited_proxy_variables(self):
        env = {
            "CLASH_PROXY": "http://127.0.0.1:7897",
            "HTTP_PROXY": "http://stale",
            "HTTPS_PROXY": "http://stale",
            "http_proxy": "http://stale",
            "https_proxy": "http://stale",
        }

        network_route.apply_clash_route(
            env, network_route.NetworkRoute("direct", reason="unreachable")
        )

        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertNotIn(key, env)
        self.assertEqual(env["CLASH_PROXY"], "http://127.0.0.1:7897")

    def test_clash_route_exports_proxy_and_localhost_bypass(self):
        env = {"NO_PROXY": "example.test"}
        network_route.apply_clash_route(
            env,
            network_route.NetworkRoute(
                "clash", "socks5://127.0.0.1:7897", "reachable"
            ),
        )

        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertEqual(env[key], "socks5://127.0.0.1:7897")
        for host in ("127.0.0.1", "localhost", "::1"):
            self.assertIn(host, env["NO_PROXY"].split(","))
        self.assertEqual(env["NO_PROXY"], env["no_proxy"])


if __name__ == "__main__":
    unittest.main()
