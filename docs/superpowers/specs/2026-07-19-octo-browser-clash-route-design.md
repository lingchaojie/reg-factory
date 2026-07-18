# Octo Browser And Clash Route Selection Design

## Goal

Add Octo Browser as a third fingerprint-browser provider and make the existing
non-IPMart network path choose Clash only when its configured local proxy port
is reachable. If IPMart is disabled and Clash is unavailable, the process must
start in direct mode instead of exporting a dead localhost proxy.

## Confirmed Requirements

- Add `octo` to the existing `FINGERPRINT_BROWSER` selector.
- Preserve the current BitBrowser and AdsPower behavior.
- Preserve the current IPMart coverage: Outlook, Graph token extraction, Graph
  mailbox access, and Claude share one IPMart account lease.
- Do not extend the IPMart lease to ChatGPT or Grok.
- Preserve the rule that an enabled IPMart flow never falls back to Clash,
  direct access, or an existing browser profile when lease acquisition,
  verification, or credentialed profile creation fails.
- When IPMart is disabled, use Clash only if `CLASH_PROXY` resolves to a local
  TCP endpoint that is reachable during startup or child-environment creation.
- When IPMart is disabled and Clash is not configured or is unreachable,
  remove inherited HTTP proxy variables and run the affected flow directly.
- Select the route before work begins. Do not change routes in the middle of an
  account attempt after a request or browser action fails.
- A missing Clash process is an expected direct-mode condition, not a fatal
  error.
- Never print the Octo API token or IPMart credentials.

## Non-Goals

- Expanding IPMart to ChatGPT or Grok.
- Replacing Clash node selection, rotation, or health scoring when Clash is
  available.
- Adding request-time fallback from Clash to direct access.
- Replacing the existing browser call sites with a new session framework.
- Migrating saved profiles between BitBrowser, AdsPower, and Octo Browser.
- Using Octo one-time profiles in the first implementation.

## Architecture

The implementation keeps the project's existing BitBrowser-compatible browser
contract. `BitBrowser()` remains the provider factory and returns an Octo
adapter when `FINGERPRINT_BROWSER=octo`. The adapter translates the current
profile arguments and response shape to Octo's Public and Local APIs.

A focused network-route helper owns Clash endpoint parsing, reachability
testing, and HTTP proxy environment cleanup. Existing orchestrators call this
helper before launching work or constructing child environments. This prevents
each entry point from implementing a slightly different fallback rule.

The two changes are independent at their boundaries: the route helper decides
whether the legacy non-IPMart path is `clash` or `direct`, while the browser
adapter decides how a browser profile represents `direct` or an explicit
IPMart proxy.

## Browser Provider Contract

Create `octobrowser.py` with an `OctoBrowser` class that implements the same
operations used by the current project:

- `create_browser(name, proxy_str=None, **kwargs) -> str`
- `open_browser(profile_id) -> dict`
- `close_browser(profile_id)`
- `delete_browser(profile_id)`
- `list_browsers(page=0, page_size=100) -> dict`
- `cleanup_browsers(keep=0) -> int`
- `_post(path, data=None, _retries=5)` for the limited legacy `/browser/*`
  compatibility calls still made by older scripts

The normalized `open_browser` result must contain a `ws` key so existing calls
to `playwright.chromium.connect_over_cdp(data["ws"])` remain unchanged.

### Octo API Mapping

| Project operation | Octo operation |
| --- | --- |
| Create profile | `POST {OCTO_PUBLIC_API}/api/v2/automation/profiles` |
| List profiles | `GET {OCTO_PUBLIC_API}/api/v2/automation/profiles` |
| Start profile | `POST {OCTO_LOCAL_API}/api/profiles/start` |
| Stop profile | `POST {OCTO_LOCAL_API}/api/profiles/stop` |
| Delete profile | `DELETE {OCTO_PUBLIC_API}/api/v2/automation/profiles` |

Public requests send `X-Octo-Api-Token`. Local start requests use
`headless=false`, `debug_port=true`, `only_local=true`, and a bounded timeout.
The returned `ws_endpoint` is normalized to `ws`.

Both HTTP sessions set `trust_env=False`. Octo management traffic must never
inherit a stale proxy from the parent shell. The selected route applies to the
launched browser profile and registration flows, not to the local/Public API
management requests used to create and control that profile.

### Profile Translation

The adapter maps existing BitBrowser-shaped fields without requiring business
flows to know Octo's schema:

- `name` becomes `title`.
- A profile without valid proxy fields omits the Octo `proxy` object and is
  direct.
- `proxyType`, `host`, `port`, `proxyUserName`, and `proxyPassword` become
  Octo's `proxy.type`, `proxy.host`, `proxy.port`, `proxy.login`, and
  `proxy.password`.
- Existing IP-derived language, timezone, geolocation, and WebRTC intent maps
  to Octo fingerprint fields using `type: ip`.
- The first implementation creates Windows profiles and lets Octo generate
  unspecified fingerprint values.

## Configuration

Add these environment-backed settings:

```dotenv
FINGERPRINT_BROWSER=octo
OCTO_API_TOKEN=
OCTO_PUBLIC_API=https://app.octobrowser.net
OCTO_LOCAL_API=http://127.0.0.1:58888
```

`FINGERPRINT_BROWSER` continues to default to `bitbrowser`. The WebUI selector
offers `bitbrowser`, `adspower`, and `octo`. `OCTO_API_TOKEN` is secret. The
existing `IPMART_ENABLED` switch remains the only switch controlling whether
the current account-lease flow is enabled; no second routing-mode option is
added.

## Network Route Resolution

Introduce a small value describing the selected legacy route:

- `clash`: a configured Clash proxy endpoint accepted a TCP connection.
- `direct`: no configured endpoint exists or the endpoint was unreachable.

The helper parses HTTP, HTTPS, and SOCKS proxy URLs with `urllib.parse`, uses a
short bounded TCP connection to the parsed host and port, and returns `direct`
for invalid URLs, non-positive ports, resolution failures, refused
connections, and timeouts. It does not send traffic through the proxy as part
of the probe.

When the result is `direct`, the helper removes all four proxy variables:

- `HTTP_PROXY`
- `HTTPS_PROXY`
- `http_proxy`
- `https_proxy`

When the result is `clash`, the existing environment injection and node logic
remain available. `NO_PROXY/no_proxy` must continue to include localhost so
browser and controller APIs bypass the proxy.

### Decision Flow

1. Read and validate the existing IPMart settings.
2. If IPMart is enabled, preserve the existing IPMart orchestration and its
   platform-specific environment behavior.
3. If IPMart is disabled, probe the configured Clash proxy endpoint.
4. If reachable, retain the current Clash path.
5. If unavailable, strip inherited HTTP proxy variables and run direct.
6. Pass the already-resolved environment to child processes; do not re-enable
   Clash merely because `CLASH_PROXY` is still present as configuration.

The helper is applied to the full-flow orchestrator, Outlook loop/standalone
entry points, Claude setup, and platform child-environment construction. This
closes the current gaps where a dead `127.0.0.1:7897` is exported even though
the Clash process is not running.

## Error Handling And Security

- Octo Public API operations that require account access fail with a clear
  configuration error when `OCTO_API_TOKEN` is empty.
- An unreachable Octo Local API fails profile start with an actionable message
  that includes the configured local base URL but no secrets.
- Octo business errors preserve the provider's code/message unless the request
  contained IPMart credentials. Credentialed errors use the existing sanitized
  account-proxy error boundary.
- Transient Octo transport failures use the adapter's bounded retry policy.
  Authentication, validation, quota, and other business errors are not retried.
- Clash probe failures select direct mode and emit one concise route log.
- IPMart errors retain their existing fail-closed behavior and secret
  redaction.

## Testing

### Route Selection

- A reachable configured Clash endpoint selects `clash` and retains the proxy
  environment.
- A refused connection, timeout, invalid URL, or absent `CLASH_PROXY` selects
  `direct` and removes all inherited HTTP proxy variables.
- Localhost remains in `NO_PROXY` when Clash is selected.
- Full-flow, Outlook, Claude, and platform child environments use the resolved
  direct route instead of reconstructing `CLASH_PROXY`.
- Existing IPMart-enabled tests continue to prove the Outlook-to-Claude lease
  is unchanged and does not fall back.

### Octo Adapter

- Provider selection returns `OctoBrowser` for `octo` and preserves existing
  providers for their current aliases.
- Profile creation maps direct and authenticated IPMart proxy payloads.
- Public requests include the API token without exposing it in exceptions.
- Start maps Octo's `ws_endpoint` to `ws`.
- Stop, delete, list, cleanup, and legacy compatibility calls use the correct
  API and normalized response shapes.
- Missing token and unavailable Local API errors are deterministic and
  actionable.

### Integration And Configuration

- WebUI metadata exposes the Octo provider and settings with the token marked
  secret.
- WebUI status tests the selected provider's Local API.
- Documentation and `.env.example` describe Octo and the automatic
  Clash-or-direct behavior.
- Existing BitBrowser, AdsPower, IPMart, proxy environment, and orchestration
  suites remain green.

## Acceptance Criteria

- Selecting `FINGERPRINT_BROWSER=octo` can create a temporary profile, start it,
  return a CDP endpoint consumable by Playwright, stop it, and delete it.
- The same adapter can create an Octo profile containing the current IPMart
  credentialed proxy fields without leaking credentials.
- With `IPMART_ENABLED=0` and no listener at the configured Clash proxy
  endpoint, the orchestrated child environment contains no HTTP proxy variables
  and the browser profile is direct.
- With `IPMART_ENABLED=0` and a reachable Clash proxy endpoint, existing Clash
  routing remains active.
- With `IPMART_ENABLED=1`, the established Outlook, Graph, mailbox, and Claude
  lease behavior is unchanged.
