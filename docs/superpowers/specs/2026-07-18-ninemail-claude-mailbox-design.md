# NINEMALL Claude Mailbox Design

**Date:** 2026-07-18

**Status:** Approved for implementation planning

## Goal

Add a Claude-only mailbox channel selected by environment variable. The default
channel is `NINEMALL`: it reads purchased Outlook credentials from `mail.txt`
and retrieves Claude magic-link messages through the hosted AppleEmail API,
without opening or logging into Outlook in a browser.

The existing Outlook channel remains available for legacy Claude runs. The
mailbox behavior of ChatGPT, Grok, and GitHub is outside this change and must
remain unchanged.

## Configuration

```dotenv
EMAIL_PROVIDER=NINEMALL
NINEMALL_EMAIL_FILE=mail.txt
NINEMALL_API_BASE=https://www.appleemail.top
NINEMALL_API_PASSWORD=
NINEMALL_HTTP_TIMEOUT=30
NINEMALL_POLL_INTERVAL=5
```

`EMAIL_PROVIDER` accepts these values:

- `NINEMALL` is the default. It uses the new file format and AppleEmail API.
- `OUTLOOK` preserves the existing Claude mailbox behavior and legacy file
  format.

An unset or empty `EMAIL_PROVIDER` selects `NINEMALL`; any other unknown value
fails fast with a configuration error. The API base must be HTTPS.
`NINEMALL_API_PASSWORD` is the hosted service access
password and is unrelated to the Outlook mailbox password in the account file;
it is sent as an empty string when not configured.

## Account Formats And State

NINEMALL reads the file selected by `NINEMALL_EMAIL_FILE`, relative to the
repository root unless configured as an absolute path. Its required format is:

```text
email----password----client_id----refresh_token
```

All four fields must be present and non-empty. Blank lines and comment lines
beginning with `#` are ignored. Malformed rows are reported without printing
their credential contents.

The NINEMALL source file is read-only. In particular, the implementation must
ignore an AppleEmail `new_refresh_token` response field and must never update,
rewrite, reorder, delete, or consume rows from `mail.txt`.

Claude reservation state is stored separately:

- `mail_used_claude.txt` records reserved and successfully used addresses.
- `mail_error_claude.txt` records failed addresses and sanitized reason codes.

Reservation happens under the existing in-process mailbox lock before work is
started, so concurrent Claude workers cannot select the same address. State
files may include the email address and status but must not contain the
refresh token, client ID, API password, or full API response.

The legacy OUTLOOK format remains:

```text
email----password----refresh_token----client_id
```

Its existing source and state files are not migrated or rewritten.

## Architecture

Claude receives a small channel abstraction rather than adding provider checks
at every mailbox call site.

### Claude account store

The account-store component is responsible for selecting the configured
channel, parsing the channel's account format, reserving one account, and
recording success or failure. It returns a typed account object containing:

- `email`
- `password`
- `client_id`
- `refresh_token`
- provider and source metadata that never appear in logs

This object removes the current ambiguous tuple ordering and ensures that a
NINEMALL client ID is not mistaken for a refresh token.

### NINEMALL mailbox client

The NINEMALL client calls:

```text
POST {NINEMALL_API_BASE}/api/mail-all
```

with a JSON body containing:

```json
{
  "refresh_token": "<per-account refresh token>",
  "client_id": "<per-account client id>",
  "email": "<account email>",
  "mailbox": "INBOX or Junk",
  "response_type": "json",
  "password": "<NINEMALL_API_PASSWORD or empty string>"
}
```

POST is used even though the API also supports GET so credentials do not appear
in URLs, browser history, reverse-proxy access logs, or exception messages.

The response adapter reads `data` as the message array and normalizes these
fields:

- `send` to sender
- `subject` to subject
- `date` to received time
- `html` or `text` to body

The API documentation is at <https://www.appleemail.top/api.html>. The public
web client at <https://www.appleemail.top/mail.html> confirms the response
envelope and message field names.

### Claude mailbox facade

All Claude magic-link reads go through one facade. It dispatches NINEMALL
accounts to the hosted API client and OUTLOOK accounts to the existing Graph or
browser implementation. This facade is used for:

1. The first magic-link request.
2. The existing single resend attempt.
3. A new login attempt after onboarding loses its session.

NINEMALL dispatch is strict: it never invokes the Outlook Graph helper,
`mailbox_broker`, Outlook browser login, or browser fallback. OUTLOOK dispatch
keeps the current fallback behavior.

## NINEMALL Mail Polling

After Claude submits an email address, the facade records the request time and
polls until the caller's existing timeout expires:

1. POST for `INBOX`.
2. POST for `Junk`.
3. Normalize and combine the returned messages.
4. Prefer messages whose sender or subject contains `anthropic` or `claude`.
5. Sort usable matches by received time, newest first.
6. Ignore messages older than the current request or resend time, allowing a
   small clock-skew tolerance.
7. Extract a direct `https://claude.ai/magic-link#...` URL from the subject,
   text, or HTML body.
8. If the URL is wrapped by Microsoft SafeLinks, decode the wrapped target and
   accept it only when the target host is `claude.ai` and the path is
   `/magic-link`.
9. Sleep for `NINEMALL_POLL_INTERVAL` and repeat when no valid link is found.

HTML entity decoding and URL parsing use standard-library functions. A link is
never accepted solely because an arbitrary body contains the word `claude`.

The source refresh token is reused unchanged for every request. A
`new_refresh_token` field in either a success or error response is ignored.

## Errors And Retries

Each HTTP request uses `NINEMALL_HTTP_TIMEOUT`. Network errors, HTTP 429, and
HTTP 5xx responses are retried up to three times with bounded backoff. An empty
mailbox or an API response equivalent to `Nothing to fetch` is a normal polling
result, not an account failure.

HTTP 400 for malformed credentials, HTTP 401, invalid JSON, invalid response
shape, a non-HTTPS base URL, and malformed local account rows are classified
with sanitized reason codes. Credential errors fail the account immediately;
transient errors continue only within their retry and overall polling limits.

If the first polling window expires, Claude performs its existing single resend
and starts a fresh polling window using the resend timestamp, preventing an old
link from being selected. If that also expires, the account is written to
`mail_error_claude.txt` and the registration fails. No Outlook page is created
at any point in the NINEMALL path.

Logs may contain the provider name, mailbox folder, retry number, sanitized
error category, and masked email address. Logs must not contain Outlook
passwords, client IDs, refresh tokens, API passwords, request bodies, complete
API responses, or complete magic links.

## Entry Points And Scope

The following Claude entry points honor `EMAIL_PROVIDER`:

- Direct `register.py` execution.
- Claude registration started from the WebUI.
- `register_three_platforms.py --platforms claude`.
- Claude-only `run_full_flow.py` execution.

For NINEMALL, direct Claude execution without `--email` reserves from
`NINEMALL_EMAIL_FILE` instead of attempting Outlook self-registration.
Explicit `--email` runs must also accept and propagate a matching
`--client-id` and `--token`.

The orchestrators may consume the NINEMALL store only when the requested
platform set is exactly `claude`. Mixed-platform and non-Claude runs retain the
legacy `emails.txt` path and behavior. This prevents the new default from
changing ChatGPT, Grok, or GitHub.

## Testing

Tests use temporary credential files and mocked HTTP responses. No live mailbox,
AppleEmail request, Microsoft request, proxy allocation, or external account is
used during automated verification.

Coverage includes:

- Default provider selection and rejection of unknown values.
- NINEMALL and OUTLOOK column ordering.
- Required-field validation and secret-safe errors.
- Concurrent reservation of distinct NINEMALL accounts.
- Correct POST URL and JSON field construction.
- Rejection of non-HTTPS API bases.
- INBOX then Junk polling.
- Response normalization for `send`, `subject`, `date`, `html`, and `text`.
- Direct magic-link and SafeLinks extraction.
- Sender/subject filtering and stale-message rejection after resend.
- Empty mailboxes and `Nothing to fetch` behavior.
- HTTP 400/401 handling and retry behavior for network errors, 429, and 5xx.
- Strict immutability of `mail.txt` when `new_refresh_token` is returned.
- Strict absence of Outlook browser fallback for NINEMALL.
- First login, resend, and onboarding re-login using the same facade.
- Propagation of `client_id` through direct, WebUI, and Claude-only orchestrated
  entry points.
- Legacy OUTLOOK behavior and non-Claude platform behavior remaining unchanged.

## Security Note

A full refresh token was pasted into the design conversation. It is treated as
compromised and is not copied into this document, code, tests, logs, commits, or
API calls. It should be revoked or replaced before real execution.

## Out Of Scope

- Changing ChatGPT, Grok, GitHub, Gmail, or temporary-mail providers.
- Importing NINEMALL rows into `emails.txt`.
- Outlook self-registration for NINEMALL.
- Browser or IMAP fallback for NINEMALL.
- Live API validation with real account credentials.
- Updating refresh tokens returned by AppleEmail.
