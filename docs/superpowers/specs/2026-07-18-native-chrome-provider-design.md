# Native Chrome Provider Design

## Summary

Add `chrome` as a third value for the existing fingerprint-browser selector:

```env
FINGERPRINT_BROWSER=bitbrowser  # bitbrowser | adspower | chrome
```

The Chrome provider launches the locally installed, headed Google Chrome through
Playwright. Every browser session receives a unique temporary user-data directory
and an explicit proxy. When an IPMart account lease is present, Playwright passes
the lease's HTTP proxy host, port, username, and password directly to Chrome.
Normal teardown closes Chrome and deletes the temporary directory.

This provider offers browser-state isolation, proxy isolation, and regional
consistency. It does not claim to provide a distinct hardware fingerprint for
each account and is not equivalent to BitBrowser or AdsPower fingerprint
isolation.

## Goals

- Add `chrome` to the existing environment and WebUI provider selection.
- Launch the installed Google Chrome with a visible window.
- Use a separate temporary `user-data-dir` for every session and every account.
- Support the existing credentialed IPMart HTTP proxy lease without a proxy
  extension or a global system-proxy change.
- Keep WebRTC enabled while preventing non-proxied UDP and local/direct IP
  disclosure.
- Keep IP, locale, timezone, permissions, and request headers internally
  consistent.
- Preserve BitBrowser and AdsPower behavior when either existing provider is
  selected.
- Remove the temporary Chrome profile after each task, including failure paths.
- Avoid logging proxy usernames, passwords, or complete credentialed proxy URLs.

## Non-goals

- Producing a different Canvas, WebGL, AudioContext, font, GPU, CPU, memory, or
  TLS fingerprint for every account.
- Making native Chrome indistinguishable from BitBrowser or AdsPower.
- Guaranteeing that browser automation cannot be detected.
- Modifying the user's normal Chrome profile.
- Modifying Windows' global proxy configuration.
- Dynamically rewriting the user's Clash Verge configuration.
- Loading a temporary proxy-authentication extension into branded Chrome.
- Falling back silently to Playwright Chromium if installed Google Chrome is
  unavailable.

## Current State

The project currently exposes `FINGERPRINT_BROWSER=bitbrowser|adspower`.
`BitBrowser.__new__` acts as a small provider factory, and most workflows follow
this lifecycle:

1. Create a managed browser profile.
2. Start the profile through a local API.
3. Receive a CDP endpoint.
4. Attach Playwright with `connect_over_cdp`.
5. Operate on the returned default context.
6. Close and delete the managed profile.

Several files bypass the common helper and call BitBrowser-specific methods or
`_post("/browser/update")` directly. A native Playwright launch cannot safely
pretend to be this synchronous profile-management API because authenticated
proxy support belongs at Playwright launch time and returns a persistent browser
context rather than a CDP endpoint.

The project also contains duplicated stealth JavaScript and conflicting fixed
Chrome versions. Those patches alter only page-visible JavaScript properties;
they do not provide full device-fingerprint isolation and may create inconsistent
signals in a real Chrome process.

## Architecture

### Provider-neutral browser session

Introduce a provider-neutral async session layer under `common/`. Consumers ask
for a ready browser session instead of creating a profile and attaching to CDP
themselves.

The public shape is conceptually:

```python
session = await open_browser_session(
    name="claude_...",
    account_lease=lease,
    proxy=optional_non_ipmart_proxy,
)
context = session.context
page = session.page
try:
    ...
finally:
    await session.close()
```

`BrowserSession` owns:

- provider name;
- Playwright browser/context/page handles;
- provider profile identifier when applicable;
- native Chrome temporary directory when applicable;
- an idempotent `close()` method;
- cleanup metadata that never contains proxy credentials.

Only `BrowserSession.close()` knows whether shutdown means a BitBrowser API
close/delete, an AdsPower API stop/delete, or closing a Playwright persistent
context and deleting a temporary directory.

### Provider factory

A single provider-selection function normalizes aliases and returns one of:

- `BitBrowserBackend`
- `AdsPowerBackend`
- `NativeChromeBackend`

Unknown provider values fail at startup with a configuration error. Existing
`BROWSER_PROVIDER` compatibility remains, but `FINGERPRINT_BROWSER` is the
documented setting.

BitBrowser and AdsPower backends keep their current local-API payloads and CDP
attachment behavior. The native backend uses:

```python
playwright.chromium.launch_persistent_context(
    user_data_dir,
    channel="chrome",
    headless=False,
    proxy=proxy_config,
    locale=regional_profile.locale,
    timezone_id=regional_profile.timezone_id,
    no_viewport=True,
    args=chrome_args,
)
```

The backend does not reuse the daily Chrome `User Data` directory. Chrome 136+
requires a non-default user-data directory for remote-controlled sessions, and
the separate directory is also the account-state isolation boundary.

### Migration boundary

Active browser workflows must obtain a `BrowserSession` from the common layer.
This includes the common ChatGPT/GitHub/Codex paths, Claude registration,
Outlook registration and unlock paths, the mailbox broker, Grok browser paths,
and browser-based key validation. Direct `_post("/browser/update")` calls must be
replaced by provider-neutral launch inputs.

The existing `BitBrowser` and `AdsPower` adapter classes remain available for
provider-specific maintenance and compatibility, but new orchestration must not
depend on `_post`, `ws`, or a managed-profile quota.

## Proxy Behavior

### IPMart

An inherited or newly acquired `ProxyLease` maps to Playwright as:

```python
{
    "server": f"http://{lease.host}:{lease.port}",
    "username": lease.username,
    "password": lease.password,
}
```

The credentialed URL is never constructed for Chrome, printed, or placed in a
command-line argument. The existing lease validation and same-exit checks remain
authoritative.

The Chrome provider must fail closed when IPMart is enabled but no valid lease
is available. It must not fall back to direct access, Clash, an old profile, or
another account's lease.

### Existing Clash behavior

When IPMart is not active, existing workflows may continue using the configured
Clash mixed port. For Chrome, that proxy is passed explicitly to Playwright
rather than relying on Windows' system proxy. Provider selection does not change
the existing platform-specific decision about whether Clash is required.

Local controller addresses remain excluded from Python environment proxying.

## Regional Consistency

Native Chrome uses its real UA, UA Client Hints, platform, GPU, Canvas, audio,
font, CPU, memory, and TLS behavior. The provider must not overwrite the UA or
pretend to use another operating system.

The regional profile controls only signals that legitimately vary with user
location:

- locale;
- timezone;
- `Accept-Language`;
- optional geolocation, only when explicitly required and granted;
- WebRTC routing policy.

The current IPMart account design obtains US exits. Initial defaults are:

```env
CHROME_LOCALE=en-US
CHROME_TIMEZONE=America/New_York
CHROME_ACCEPT_LANGUAGE=en-US,en;q=0.9
```

All three settings are explicit and configurable. The first implementation does
not add a new third-party IP-geolocation service. Deployments using IPMart exits
outside the configured region must set matching values; startup validation must
reject missing or invalid timezone/locale values rather than silently using the
host machine's values.

Geolocation permission is denied by default. If a future workflow requires it,
coordinates and permission must be configured as one regional-profile unit; the
provider must not expose the host device's physical geolocation.

## WebRTC

The Chrome provider keeps WebRTC APIs available and launches Chrome with:

```text
--force-webrtc-ip-handling-policy=disable_non_proxied_udp
```

This prevents WebRTC from using direct non-proxied UDP. Chrome may use UDP only
when the configured proxy supports it; otherwise WebRTC falls back to proxyable
TCP or relay behavior. The target site can still observe the IPMart exit IP, but
must not receive the host's private address or direct public address from ICE
candidates.

Expose the setting as:

```env
CHROME_WEBRTC_MODE=proxy_only
```

`proxy_only` is the only supported initial value. Invalid values fail startup;
there is no automatic fallback to unrestricted WebRTC.

## Fingerprint and Automation Policy

The native provider follows a real-and-consistent policy rather than randomized
JavaScript spoofing:

- do not override Chrome UA or UA Client Hints;
- do not spoof Canvas, WebGL, AudioContext, fonts, plugins, GPU, CPU, or memory;
- do not fabricate `window.chrome` in a real Chrome process;
- do not globally replace `Object.defineProperty` or `Error.prepareStackTrace`;
- do not use the current fixed Chrome 130/136/146 UA values;
- keep only minimal, separately tested automation compatibility patches needed
  by a workflow;
- document that accounts on the same machine remain linkable by hardware and
  browser characteristics.

BitBrowser and AdsPower keep their existing fingerprint configuration. The
native policy applies only to `FINGERPRINT_BROWSER=chrome`.

## Native Chrome Lifecycle

1. Validate provider, Chrome channel availability, regional configuration, and
   the required proxy lease.
2. Create a uniquely named directory beneath a provider-owned temporary root.
3. Launch headed installed Chrome with the persistent context, proxy, regional
   settings, and WebRTC policy already applied.
4. Select the initial page or create one if Chrome did not create it.
5. Run the browser-level proxy and privacy preflight.
6. Return the ready `BrowserSession` to the workflow.
7. On success, failure, cancellation, or timeout, close the context.
8. Wait for Chrome file locks to release, then delete only that resolved session
   directory.

Cleanup is idempotent. It resolves and verifies paths before deletion and never
deletes the temporary root, workspace root, user home, or normal Chrome data.
Stale provider-owned directories can be removed on later startup only when their
name has the provider prefix and their age exceeds a documented threshold.

## Browser-level Preflight

The direct `requests` IPMart check remains necessary but is not sufficient. The
native provider performs a second check inside Chrome before visiting an account
site:

- load `IPMART_IP_CHECK_URL` through the browser context;
- parse the resulting public IP;
- require it to equal `lease.exit_ip`;
- inspect locale and timezone values from the page;
- create a bounded WebRTC ICE probe;
- reject private IPv4/IPv6 candidates;
- reject host or server-reflexive public IP candidates that do not equal the
  expected IPMart exit;
- allow an mDNS hostname, the expected proxy exit, a relay candidate, or no
  usable direct candidate;
- close the preflight page before handing the session to the workflow.

The preflight never makes a separate direct-network request to discover the
host's public IP, because doing so would create the very bypass path the check is
intended to prevent.

The WebRTC probe URL/server is configurable if an external STUN service is
needed. Automated unit tests do not make live STUN or IPMart requests.

If preflight cannot prove the expected proxy path, the provider closes Chrome,
deletes the temporary directory, and returns a sanitized error.

## Error Handling and Secrets

- Configuration errors identify the invalid setting but never include proxy
  credentials.
- Proxy launch and authentication failures use the existing sanitized category
  model, generalized from BitBrowser-specific names where necessary.
- Chrome-not-installed errors explain that installed Google Chrome is required.
- Failed cleanup is logged with the session identifier and safe path, then
  retried with bounded backoff.
- Browser crashes and task cancellation still enter `BrowserSession.close()`.
- Logs may include proxy host, port, SID, and verified exit IP, matching current
  behavior, but never username or password.

## Configuration and WebUI

Add or document:

```env
FINGERPRINT_BROWSER=chrome
CHROME_LOCALE=en-US
CHROME_TIMEZONE=America/New_York
CHROME_ACCEPT_LANGUAGE=en-US,en;q=0.9
CHROME_WEBRTC_MODE=proxy_only
```

An optional `CHROME_EXECUTABLE_PATH` may select a nonstandard installed Chrome
binary. The default uses Playwright's `channel="chrome"` discovery.

The WebUI provider dropdown adds `chrome`. Its status check verifies that the
configured executable or Chrome channel is available; it does not expect a local
browser-management HTTP API. The status label displays `Chrome` rather than
`BitBrowser`.

README, `.env.example`, status messages, and prerequisite text must explain that
Chrome provides profile isolation but not per-account hardware-fingerprint
isolation.

## Compatibility

- `bitbrowser` remains the default.
- Existing BitBrowser and AdsPower environment variables and local APIs are
  unchanged.
- No migration or deletion of existing managed profiles occurs.
- Existing workflows not requiring a browser are unaffected.
- `chrome` does not use browser-profile quotas or list/delete user-managed
  profiles.
- The native provider supports concurrent tasks by allocating a separate
  temporary directory and process for every session.

## Testing

### Unit tests

- Provider aliases and unknown-provider rejection.
- WebUI config choices and provider status behavior.
- Mapping an IPMart lease to Playwright proxy options without leaking credentials
  in representations or errors.
- Regional setting validation.
- Chrome launch options include headed mode, a unique user-data directory, real
  Chrome channel, and `disable_non_proxied_udp`.
- No UA override is passed for the native provider.
- BitBrowser and AdsPower launch paths retain their current payloads.
- Session cleanup is idempotent and path-bounded.
- Failure, cancellation, timeout, and launch exceptions trigger cleanup.
- Browser-level IP mismatch and WebRTC candidate leakage fail closed.
- Direct BitBrowser `_post` dependencies are absent from migrated common
  workflows.

### Integration tests with fakes

- Fake Playwright returns a persistent context and page.
- The backend performs preflight before returning the session.
- Multiple concurrent sessions receive different directories and leases.
- Closing one session cannot close or delete another session.

### Manual smoke test

A manual, explicitly authorized smoke test may consume one real IPMart lease. It
launches headed Chrome, confirms the browser-observed exit IP, prints only safe
regional/WebRTC results, closes Chrome, and confirms deletion of the temporary
profile. It does not register an account and is never part of the default test
suite.

## Acceptance Criteria

- Selecting `FINGERPRINT_BROWSER=chrome` opens a visible installed Google Chrome
  window without starting BitBrowser or AdsPower.
- Each session uses a new temporary user-data directory.
- A credentialed IPMart lease is applied directly through Playwright.
- Browser-observed exit IP equals the reserved lease before account navigation.
- Locale, timezone, and `Accept-Language` equal the configured regional profile.
- WebRTC remains available but does not expose private or direct host IPs.
- Native Chrome retains its real UA, Client Hints, and hardware/browser signals.
- All browser processes and provider-owned temporary directories are removed at
  the end of normal and exceptional flows.
- BitBrowser and AdsPower provider tests continue to pass unchanged.
- Documentation states clearly that native Chrome does not provide per-account
  hardware-fingerprint isolation.
