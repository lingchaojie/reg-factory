# IPMart Per-Account Shared Proxy Design

**Date:** 2026-07-17
**Status:** Approved in conversation

## Goal

For every account round, obtain one fresh US HTTP proxy from IPMart and use that same proxy for both the Outlook registration browser and the Claude registration browser. A later account round must acquire and verify a different exit IP. Temporary BitBrowser profiles continue to be deleted after use.

## Confirmed Requirements

- Use IPMart API mode with source-IP whitelist authentication.
- Use HTTP proxies.
- Request US proxies with `cntryCode=US`; leave state and city unset.
- Request the maximum supported sticky period, `time=30` minutes.
- Fetch one proxy per account round with `num=1` and TXT output with `format=1`.
- Share the same proxy endpoint across the Outlook and Claude stages of that round.
- Verify the real proxy exit IP before creating an account.
- Retry acquisition or validation at most three times.
- If all attempts fail, stop the round. Do not fall back to Clash or a direct connection.
- Do not log or persist the IPMart access key.
- Preserve existing behavior when the feature is disabled.

## External API Contract

The provider request is:

```text
GET https://api.ipmart.io/ipmart/common/getIps
    ?accessKey=<secret>
    &num=1
    &cntryCode=US
    &time=30
    &format=1
```

The request must use a `requests.Session` with `trust_env=False` and no inherited proxy. The project normally injects `HTTP_PROXY` and `HTTPS_PROXY` for Clash; inheriting those values would make IPMart see the Clash exit rather than the user's whitelisted source IP.

TXT output is parsed as one non-empty `host:port` line. The parser rejects missing hosts, non-numeric ports, ports outside `1..65535`, HTML/error bodies, and responses containing no usable endpoint.

## Architecture

### IPMart provider

Create `common/ipmart_proxy.py` as the single owner of provider-specific behavior. It exposes a small proxy lease value and acquisition operations:

```python
@dataclass(frozen=True)
class ProxyLease:
    proxy_type: str
    host: str
    port: int
    exit_ip: str

def acquire_proxy(used_exit_ips: set[str] | None = None) -> ProxyLease: ...
def verify_proxy(lease: ProxyLease, expected_exit_ip: str | None = None) -> str: ...
```

`acquire_proxy` performs up to the configured three attempts. Each attempt calls IPMart directly, parses one endpoint, and queries the configured IP-check URL through that HTTP proxy. A connection failure, malformed response, or duplicate exit IP consumes an attempt. Error messages are constructed without including the request URL or access key.

### Runtime lease transport

The selected lease is passed only through child-process environment variables:

```text
ACCOUNT_PROXY_SOURCE=ipmart
ACCOUNT_PROXY_TYPE=http
ACCOUNT_PROXY_HOST=<host>
ACCOUNT_PROXY_PORT=<port>
ACCOUNT_PROXY_EXIT_IP=<verified exit IP>
```

These values are runtime state, not `.env` configuration. A small provider-neutral helper in `common/account_proxy.py` reads and validates them and returns BitBrowser update fields:

```python
{
    "proxyMethod": 2,
    "proxyType": "http",
    "host": lease.host,
    "port": str(lease.port),
}
```

No username or password is supplied because IPMart API mode authenticates by source-IP whitelist.

### Full-flow orchestration

When IPMart is enabled, `run_full_flow.py` acquires one lease at the start of each `run_once` round, before Stage A. It places the runtime lease variables in a per-round copy of the child environment.

Stage A receives that environment and uses the lease for every Outlook browser attempt in the round. Clash node rotation is skipped while an IPMart runtime lease is present, so system proxy selection cannot contradict the explicit BitBrowser proxy.

After Stage A succeeds and before Stage B starts, the parent verifies the same endpoint again and requires the observed exit IP to equal the originally recorded IP. If it changed or cannot be reached, the round stops and Claude registration is not started. This is required because IPMart's maximum sticky lifetime is shorter than the current worst-case full-flow timeout.

Stage B inherits the same runtime lease. `register_three_platforms.py` passes its child environment unchanged to `register.py`. Claude profile creation consumes the runtime lease and skips the existing `--node auto` Clash selection/update path.

Both Outlook and Claude profiles remain ephemeral and are closed and deleted by their existing cleanup paths.

### Other entry points

- Direct `register.py`: when IPMart is enabled and there is no inherited runtime lease, acquire one lease per `run_one` account. If that invocation also self-registers Outlook, the same BitBrowser profile already carries the lease for both sites.
- Direct `outlook_reg_loop.py`: when IPMart is enabled and there is no inherited runtime lease, acquire one lease per Outlook attempt.
- `register_three_platforms.py --from-pool`: Claude obtains a fresh IPMart lease because there is no Outlook stage in that command.
- Existing Clash behavior remains unchanged when `IPMART_ENABLED` is false.

## Configuration

Add these settings to `.env.example`, `config.py`, and the Web UI configuration schema:

```dotenv
IPMART_ENABLED=0
IPMART_ACCESS_KEY=
IPMART_API_BASE=https://api.ipmart.io/ipmart/common/getIps
IPMART_COUNTRY=US
IPMART_STICKY_MINUTES=30
IPMART_MAX_ATTEMPTS=3
IPMART_IP_CHECK_URL=https://api.ipify.org?format=json
```

`IPMART_ACCESS_KEY` is rendered as a secret field. Enabling the feature without a key is a startup error before any browser profile is created. Numeric values are validated at load/use time: sticky minutes must be `5..30`, and attempts must be positive.

The fixed defaults reflect the confirmed behavior. They remain configurable so the integration is not tied to a hard-coded provider host or IP-check service.

## Uniqueness And Usage Ledger

The real exit IP, not only the provider endpoint, is the uniqueness key. Maintain an ignored runtime JSONL file named `ipmart_proxy_usage.jsonl` containing timestamp, endpoint, and verified exit IP. Reserve the exit IP immediately after successful acquisition so a failed registration does not reuse it for a later account.

The orchestrator loads previously reserved IPs and rejects any repeat during acquisition. A process-local lock protects concurrent account tasks in one process. Running multiple independent registration processes concurrently is outside the supported scope; the README will state that strict uniqueness requires one orchestrator process.

Add `ipmart_proxy_usage.jsonl` to `.gitignore`. Never store the access key in this ledger.

## Failure Handling

- API timeout, non-2xx response, malformed TXT, unusable proxy, duplicate exit IP: retry acquisition within the three-attempt budget.
- Missing/invalid configuration: fail before creating a browser profile.
- Outlook succeeds but the pre-Claude recheck fails or the exit changes: mark the round failed and do not register Claude.
- BitBrowser rejects the proxy configuration: fail the account and use existing profile cleanup.
- Never silently fall back to Clash, `noproxy`, or an existing BitBrowser profile when IPMart is enabled.
- Logs may show the transient endpoint and verified exit IP for diagnosis, but must never contain `accessKey` or a URL containing it.

## Testing Strategy

Provider unit tests use a fake HTTP session and fake IP-check response; they do not consume real IPMart quota.

Tests cover:

- Exact request parameters and direct/no-environment-proxy behavior.
- Valid TXT parsing and rejection of malformed bodies and ports.
- Exit-IP verification through the returned proxy.
- Retry behavior for API, parsing, and connectivity failures.
- Duplicate exit-IP rejection and the three-attempt terminal failure.
- Access-key redaction from raised errors and captured output.
- Runtime lease environment serialization and BitBrowser proxy fields.
- Outlook profile creation uses the inherited HTTP proxy and skips Clash rotation.
- Full flow passes one lease to both stages and rechecks it before Claude.
- Full flow aborts when the proxy changes between stages.
- Claude uses the inherited lease and does not overwrite it with `--node auto`.
- Disabled mode preserves the current `noproxy`/Clash behavior.

An optional manual smoke test uses a real configured access key to acquire one proxy, verify its exit, create one BitBrowser profile, open an IP-check page, and delete the profile. It must be explicitly invoked and is not part of the normal automated test suite.

## Documentation And Operation

Update the README with:

- IPMart whitelist requirements.
- The `.env` settings and enablement sequence.
- The one-process limitation for strict uniqueness.
- A dry-run/configuration check that does not request a proxy.
- A real one-account command and the fact that it consumes an IPMart allocation.
- The 30-minute maximum stickiness caveat and pre-Claude identity recheck.

## Non-Goals

- Managing the IPMart subscription or whitelist.
- Deleting or releasing an IP through an undocumented provider API.
- Reusing or modifying the user's existing BitBrowser profiles.
- Falling back to Clash when IPMart is enabled.
- Coordinating uniqueness across multiple independently launched orchestrator processes.
