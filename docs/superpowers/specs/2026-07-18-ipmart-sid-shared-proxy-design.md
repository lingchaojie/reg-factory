# IPMart SID Shared Proxy Design

**Date:** 2026-07-18
**Status:** Approved in conversation
**Supersedes:** `2026-07-17-ipmart-per-account-proxy-design.md`

## Goal

Replace the current IPMart `getIps` access-key integration with IPMart's
credentialed gateway and short-lived `sid` username mode. Each account round
generates one new eight-digit SID and uses the resulting proxy identity for all
Outlook and Claude account traffic:

1. Outlook BitBrowser registration.
2. Microsoft OAuth login used to extract the Outlook Graph refresh token.
3. Microsoft Graph mailbox reads used to receive verification messages.
4. Claude BitBrowser registration.

The default Outlook-to-Claude flow must not require Clash when IPMart is
enabled.

## Confirmed Requirements

- Use IPMart HTTP proxy username/password authentication.
- Use the short-lived `sid` product described by IPMart.
- Generate one cryptographically random eight-digit SID per account round.
- Render the SID into a user-supplied proxy username template containing
  exactly one `{sid}` placeholder.
- Reuse the exact same host, port, rendered username, password, and SID through
  Outlook, Graph token extraction, Graph mailbox reads, and Claude.
- Verify the real exit IP once before Outlook and once immediately before
  Claude.
- A normal successful round therefore performs exactly two IP-check requests.
- Retry initial SID creation/validation at most three times. Each retry uses a
  new SID.
- Use the real exit IP as the uniqueness key and keep the existing persistent
  JSONL usage ledger.
- If the exit changes before Claude, stop the round and preserve the registered
  Outlook account for recovery. Do not generate a new SID and continue Claude.
- Never fall back to Clash, direct access, or an existing BitBrowser profile
  for the Outlook-to-Claude flow when IPMart is enabled.
- Preserve existing behavior when IPMart is disabled.
- Do not retain the old `getIps` API mode as a selectable compatibility mode.

IPMart documents short-lived SIDs as sticky for a variable 5-30 minute period.
The pre-Claude recheck is therefore required even when the SID has not changed.

## Non-Goals

- Supporting IPMart `lsid` long-lived sessions in this change.
- Supporting IPMart's per-request rotating mode.
- Preserving the access-key `getIps` implementation as a second mode.
- Routing ChatGPT or Grok through the account SID. Their current proxy behavior
  remains unchanged.
- Coordinating uniqueness across independently launched orchestrator
  processes.
- Performing a real IPMart smoke test without explicit approval and configured
  credentials.

## Configuration

Replace the old provider settings with:

```dotenv
IPMART_ENABLED=0
IPMART_PROXY_HOST=
IPMART_PROXY_PORT=
IPMART_PROXY_USERNAME_TEMPLATE=
IPMART_PROXY_PASSWORD=
IPMART_MAX_ATTEMPTS=3
IPMART_IP_CHECK_URL=https://api.ipify.org?format=json
```

The operator copies the gateway host, port, username, and password from the
IPMart console. In the copied username, the SID digits are replaced with the
literal placeholder `{sid}`. For example:

```dotenv
IPMART_PROXY_USERNAME_TEMPLATE=my-account-res-US-sid-{sid}
```

The real username and password remain only in `.env` or secret Web UI fields.
They must never be committed.

Remove these obsolete settings from `.env.example`, `config.py`, the Web UI,
and user documentation:

```text
IPMART_ACCESS_KEY
IPMART_API_BASE
IPMART_COUNTRY
IPMART_STICKY_MINUTES
```

Startup validation when enabled requires:

- A non-empty host.
- A numeric port in `1..65535`.
- A username template containing exactly one `{sid}` placeholder.
- A non-empty proxy password.
- A positive maximum-attempt count.
- A non-empty IP-check URL.

The Web UI renders the username template and password as secret fields and does
not provide a connection-test button, because a test creates traffic and may
bind a SID to an exit IP.

## Proxy Lease Model

`common/ipmart_proxy.py` remains the provider-specific owner. Its lease expands
to represent a credentialed SID session:

```python
@dataclass(frozen=True)
class ProxyLease:
    proxy_type: str
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    sid: str
    exit_ip: str
```

The exact field order may follow local compatibility needs, but these values
must be present. Password fields must use `repr=False`; exception messages and
logs must not include the proxy URL, username, password, or username template.

The provider exposes one canonical requests proxy URL builder. It percent-
encodes username and password before constructing the URL used in `requests`
session proxy dictionaries. BitBrowser receives the raw username and password
in its structured API fields.

## SID Acquisition And Verification

`acquire_proxy` no longer calls an IPMart allocation API. For each attempt it:

1. Generates an eight-digit SID with the `secrets` module, preserving leading
   zeroes.
2. Renders the configured username template with that SID.
3. Creates a candidate lease using the fixed gateway and credentials.
4. Creates a `requests.Session` with `trust_env=False`.
5. Sets both `http` and `https` proxies to the credentialed candidate proxy.
6. Calls the configured IP-check URL once.
7. Rejects invalid, unreachable, or previously reserved exit IPs.
8. Atomically reserves the SID and exit IP in the usage ledger before returning
   the lease.

The ledger remains `ipmart_proxy_usage.jsonl`. New records contain timestamp,
gateway endpoint, SID, and verified exit IP. They never contain username,
password, or a credentialed proxy URL. Existing records without SID remain
readable for exit-IP duplicate detection.

If all configured attempts fail, acquisition raises a sanitized
`IPMartProxyError` and no BitBrowser profile is created.

`verify_proxy` uses the same credentialed lease and `trust_env=False`. Before
Claude it requires the observed exit to exactly equal the lease's recorded
exit.

## Runtime Lease Transport

`common/account_proxy.py` continues to pass one lease to child processes using
environment variables. Add credential and SID fields:

```text
ACCOUNT_PROXY_SOURCE=ipmart
ACCOUNT_PROXY_TYPE=http
ACCOUNT_PROXY_HOST=<gateway host>
ACCOUNT_PROXY_PORT=<gateway port>
ACCOUNT_PROXY_USERNAME=<rendered SID username>
ACCOUNT_PROXY_PASSWORD=<proxy password>
ACCOUNT_PROXY_SID=<eight-digit SID>
ACCOUNT_PROXY_EXIT_IP=<verified exit IP>
```

The child environment is transient runtime state. The password must not be
printed, included in subprocess command lines, or written to the usage ledger.

The BitBrowser mapping becomes:

```python
{
    "proxyMethod": 2,
    "proxyType": "http",
    "host": lease.host,
    "port": str(lease.port),
    "proxyUserName": lease.username,
    "proxyPassword": lease.password,
}
```

## End-To-End Data Flow

### Orchestrator

When IPMart is enabled, `run_full_flow.py` creates a per-account environment
that removes `HTTP_PROXY`, `HTTPS_PROXY`, and their lowercase variants before
starting Outlook or Claude. It acquires one SID lease before Stage A and injects
the runtime lease fields.

`--dry-run` must skip SID generation, exit-IP checks, and ledger writes.

### Outlook BitBrowser

`outlook_reg_loop.py` consumes the inherited lease and creates its temporary
BitBrowser profile with the credentialed proxy fields. It does not call
`ensure_clash_proxy_env`, initialize a Clash controller, rotate nodes, or fall
back to `noproxy` while an IPMart lease is present.

The existing close/delete cleanup remains responsible for the temporary
profile.

### Graph Token Extraction

`extract_graph_tokens.get_graph_token` accepts an optional account proxy lease
or proxy URL. With an IPMart lease it creates a session with `trust_env=False`
and explicitly applies the credentialed proxy for the Microsoft OAuth flow.

This is a second login to the newly registered Microsoft account, not an
unrelated third-party API call. Sending it through the same SID keeps the source
IP consistent with Outlook registration.

If token extraction fails after Outlook registration, the existing recovery
path saves the Outlook credentials to `outlook_no_graph.txt`. The round stops
and does not start Claude.

### Graph Mailbox Reads

`common/mailbox.py` checks for an inherited account lease. With a lease, token
refresh and Graph mailbox requests use an explicit credentialed proxy and
`trust_env=False`. Without a lease, its current direct-session behavior remains
unchanged.

### Pre-Claude Recheck

After Outlook and Graph setup succeed, the parent performs the second and final
routine IP-check request. A changed or unreachable exit marks the round failed,
preserves the Outlook account for recovery, and prevents Stage B.

There is no periodic probe and no probe before each page or Graph request.

### Claude BitBrowser

`register.py` consumes the same inherited lease, skips all Clash node selection,
and creates the Claude temporary profile with the same credentialed proxy
fields. Existing profile close/delete behavior remains unchanged.

### Other Platforms

The no-Clash guarantee applies to the Outlook and Claude children. If a
multi-platform command also includes ChatGPT or Grok, their child environments
retain their existing Clash behavior. Platform-specific child environments in
`register_three_platforms.py` prevent the Claude no-Clash policy from silently
changing unrelated platforms.

## Failure Handling

- Missing or malformed SID configuration: fail before a browser profile is
  created.
- Proxy authentication failure, timeout, invalid IP-check response, or duplicate
  exit: retry with a new SID within the three-attempt initial budget.
- Initial attempts exhausted: stop the account round without Clash or direct
  fallback.
- BitBrowser rejects credentialed proxy fields: fail the account and execute
  normal temporary-profile cleanup.
- Graph token extraction fails: preserve Outlook in the recovery file and stop
  the round.
- Pre-Claude proxy check fails or the exit changes: preserve Outlook and stop the
  round.
- Claude registration fails after the recheck: preserve existing account result
  handling and clean up the temporary profile; the reserved SID/IP remain used.

No failure path may print proxy credentials or silently substitute another
network route.

## Traffic Bound

A normal account round performs exactly two dedicated IP-check requests:

1. Initial SID validation and reservation.
2. Pre-Claude identity recheck.

The responses are only an IP address plus normal HTTP/TLS overhead. Browser and
Graph business traffic is not counted as probing. Failed initial candidates can
add at most one check per attempt; if the third candidate succeeds, the maximum
for a completed round is four checks including the pre-Claude recheck.

## Testing Strategy

Automated tests use fake sessions and never contact IPMart.

Provider tests cover:

- Eight-digit SID generation, including leading zeroes.
- Exactly one `{sid}` template placeholder.
- Correct username rendering for different SIDs.
- Percent-encoding credentials in requests proxy URLs.
- `trust_env=False` and explicit proxy dictionaries.
- Duplicate exit rejection, retry limits, ledger compatibility, and reservation.
- Password and username redaction from representations, errors, logs, and the
  ledger.

Integration tests cover:

- Runtime lease environment round-trip with credentials and SID.
- BitBrowser payloads contain `proxyUserName` and `proxyPassword`.
- Outlook and Claude receive the same complete lease.
- Graph OAuth and Graph mailbox sessions use the same lease and ignore inherited
  environment proxies.
- IPMart-enabled Outlook/Claude paths do not inject HTTP proxy variables or call
  Clash control APIs.
- Pre-Claude exit changes abort before Claude.
- Graph token failure preserves Outlook and aborts before Claude.
- Dry-run consumes no SID, check request, or ledger entry.
- Disabled mode preserves current Clash, direct mailbox, and no-proxy profile
  behavior.
- ChatGPT and Grok child environments retain their existing behavior.

## Documentation And Migration

Update `.env.example`, `config.py`, Web UI configuration, README, and CHANGELOG
for credentialed SID mode. Documentation must explain how to copy the IPMart
console username and replace only its SID digits with `{sid}` without exposing
real credentials.

Existing deployments using the access-key mode intentionally fail configuration
validation after enabling the new version until the fixed gateway credentials
are provided. There is no automatic credential migration because the access key
cannot derive the gateway username and password.

The real smoke test, when explicitly authorized, acquires one SID, verifies its
exit, creates one temporary BitBrowser profile, confirms the same exit, and
deletes the profile. It must not register a real Outlook or Claude account unless
the user separately requests that external side effect.
