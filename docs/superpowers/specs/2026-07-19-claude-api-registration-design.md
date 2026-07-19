# Claude API Personal Registration Design

**Date:** 2026-07-19

**Status:** Implemented; final lifecycle and durability review incorporated

## Goal

Add `claude_api` as a new, independent platform registration flow. The flow
opens `https://platform.claude.com/`, authenticates with an email account,
selects the personal-account path, reaches the Claude Platform console, and
saves the authenticated browser session for a later recharge workflow.

The existing `claude` flow for Claude.ai remains unchanged. A mailbox may be
used once for `claude` and once for `claude_api`; the two flows keep independent
reservation, success, and error state.

## Scope

This change includes:

- A standalone `register_claude_api.py` entry point.
- A `claude_api` platform option in the existing CLI orchestrators and WebUI.
- NINEMALL support for both Claude Platform magic links and numeric email
  verification codes.
- Legacy OUTLOOK support through the existing Graph, mailbox broker, and
  Outlook-browser channels.
- Personal-account selection and authenticated-session export.
- Safe error classification, cancellation, cleanup, and automated tests.

This change does not include:

- Creating a non-personal organization or team.
- Creating an API key.
- Adding credit, binding a payment method, or performing any recharge action.
- Changing the behavior of the existing Claude.ai registration flow.
- Changing ChatGPT, Grok, GitHub, Gmail, or temporary-mail behavior.

Recharge will be designed as a later stage after its page flow and required
inputs are supplied. The registration output is deliberately reusable by that
stage.

## Confirmed Platform Behavior

The current Claude Platform login page is `https://platform.claude.com/login`.
It exposes an email field and `Continue with email`. After submission, the
authentication UI supports two alternative email artifacts:

- A Claude Platform magic link.
- A numeric verification code entered through the `Enter verification code`
  path (`data-testid="code"` in the current UI).

These are alternatives, not a primary and fallback channel. Mail polling must
look for both on every pass and immediately use whichever valid artifact the
received message contains.

The current NINEMALL implementation only exposes Claude.ai magic links through
`extract_claude_magic_link()` and `poll_magic_link()`. Although the AppleEmail
response already contains message subject, HTML, and text, numeric-code
extraction is new work in this feature.

## Architecture

### Standalone registration flow

Create `register_claude_api.py` rather than adding a target mode to the large
Claude.ai registrar. It owns only Claude Platform page states, authentication,
personal-account onboarding, session export, and its CLI contract.

The script reuses existing provider-neutral infrastructure for:

- Browser/profile creation and teardown.
- IPMart account leases and proxy propagation.
- Claude mailbox account selection.
- Process cancellation and worker cleanup.
- Secret-safe logging.

Claude.ai-specific birthday, phone, chat onboarding, `sessionKey` validation,
and message-sending logic are not imported into the new flow.

### Account store and independent state

Extend `ClaudeEmailAccountStore` with an explicit `purpose` state namespace.
The source-account parsing remains provider-specific and unchanged:

```text
NINEMALL: email----password----client_id----refresh_token
OUTLOOK:  email----password----refresh_token----client_id
```

NINEMALL continues reading the configured `NINEMALL_EMAIL_FILE` without
rewriting it. State files are selected by purpose:

```text
Claude.ai:       mail_used_claude.txt / mail_error_claude.txt
Claude Platform: mail_used_claude_api.txt / mail_error_claude_api.txt
```

The two namespaces allow the same address to be reserved independently by the
two platform flows. Every source read, reservation, release, and terminal-state
write runs under a per-root interprocess advisory lock as well as an in-process
thread lock. Ledger updates are written to a private temporary file, fsynced,
and atomically replaced. A shared `claude` + `claude_api` reservation is one
transaction: a credential-free journal records only each ledger filename and
its pre-transaction size, and recovery truncates or removes every partially
updated ledger before another process may select an address. Sticky terminal
states still prevent duplicate use. Lock, journal, and state files must be
ignored by Git.

OUTLOOK reads the existing `emails.txt` source and uses
`emails_used_claude_api.txt` / `emails_error_claude_api.txt` for the new
purpose. These files and the two new NINEMALL state files must be ignored by
Git. Legacy Claude.ai state semantics do not change.

### Mail verification result

Introduce a typed Claude Platform verification result with optional,
mutually-usable fields rather than returning an ambiguous string:

```text
magic_link: validated URL or absent
code: validated numeric code or absent
received_at: normalized message time
```

The extractor may return either artifact. If an unusual message contains both,
the registration page state selects the artifact: use the code when the code
input is already visible; otherwise use the magic link. There is no global
magic-link-first rule.

Keep the existing Claude.ai `poll_magic_link()` API and behavior intact. New
Claude Platform extraction and polling APIs are additive.

## Mailbox Channels

### NINEMALL

For a NINEMALL account, the flow uses the existing strict AppleEmail channel:

```text
POST https://www.appleemail.top/api/mail-all
```

Each polling round queries `INBOX` and then `Junk`, using the existing request
contract, time budget, bounded retry, cancellation, and no-redirect behavior.
It normalizes `send`, `subject`, `date`, and `html` or `text` exactly as the
current client does.

For each round, the Claude Platform extractor:

1. Filters to messages whose sender or subject identifies Anthropic or Claude.
2. Rejects messages older than the current send or resend timestamp, with the
   existing small clock-skew allowance.
3. Sorts usable messages newest first.
4. Scans subject, normalized plain text, and HTML-derived text for both a
   Platform magic link and a numeric verification code.
5. Returns immediately when the newest usable message contains either valid
   artifact.

Magic-link acceptance is host- and path-based. A direct link must use HTTPS,
host `platform.claude.com`, and the expected `/magic-link` route. Microsoft
SafeLinks may be decoded, but the decoded target must pass the same validation.
The raw wrapped or decoded link is never logged.

Code extraction operates on human-visible message text, not raw HTML. It
accepts a 4-10 digit token only when the token is directly associated with an
explicit login-code or verification-code phrase in the subject or body. The
extractor does not impose an unverified six-digit-only rule because the current
Platform input declares numeric input but no client-side length. This
contextual rule rejects dates, CSS colors, style values, URLs, and unrelated
numeric identifiers while allowing the live Anthropic email template to
determine the actual token length.

NINEMALL remains strict. It must never fall back to Microsoft Graph,
`mailbox_broker`, Outlook browser login, or an IMAP channel. AppleEmail
`new_refresh_token` values remain ignored, and `mail.txt` remains read-only.

### OUTLOOK

For an OUTLOOK account, reuse the current mailbox channels in this order when
their required credentials or configuration are available:

1. Microsoft Graph with refresh token and client ID.
2. Shared mailbox broker.
3. Outlook browser login and inbox/junk scanning.

Each channel must extract both Claude Platform artifact types. A configured
IPMart account lease is propagated through the registration browser and Graph
mailbox calls exactly as it is for the current Claude flow.

## Registration State Machine

1. Reserve one account in the `claude_api` state namespace.
2. Start one monotonic account deadline, then acquire and verify any configured
   account-scoped proxy lease within it.
3. Create a temporary browser profile and open
   `https://platform.claude.com/`.
4. Confirm the email login page is present.
5. Fill the account email and click the uniquely identified
   `Continue with email` button.
6. Record the send timestamp before submission completes.
7. Start mailbox polling for both valid artifact types.
8. When a magic link is returned, navigate the same registration page to it
   through the existing safe-navigation pattern.
9. When a code is returned, open or retain the `Enter verification code` UI,
   fill the unique code input, and submit it.
10. If neither artifact arrives in the first window, request one resend, record
    a new timestamp, and start one fresh dual-artifact polling window.
11. Reject an expired link, rejected code, stale message, or artifact for a
    different host without exposing the artifact in logs.
12. On first-account setup, select only an explicitly identified personal
    account option.
13. Do not populate or submit organization or team creation forms.
14. Declare success only after page URL and a stable authenticated console
    element jointly confirm entry to Claude Platform.
15. Export the authenticated session and mark the `claude_api` reservation
    successful.
16. On any failure or cancellation, stop the current owner and clean up browser
    resources before changing the reservation ledger. Synchronous profile
    create/open/close/delete calls are serialized by one daemon owner so a
    timed-out call cannot race cleanup. Profile deletion is submitted only
    after profile close has completed successfully. A cancellation-resistant
    Playwright operation retains ownership until it confirms completion; if
    either async ownership or cleanup remains unconfirmed, keep the reservation
    reserved and publish neither success nor error.

The single account deadline covers proxy acquisition, profile creation,
profile open, CDP connection, mail verification, onboarding, and session
export. Each phase receives only the remaining time. The inner page state
machine leaves a small settle margin inside that same deadline so its progress
marker can be converted to a contextual terminal code rather than being
overwritten by a generic outer timeout. Mail, code-confirmation, and console
phases therefore preserve `mail_timeout`, `verification_rejected`, and
`console_not_reached` respectively. Cleanup has a separately bounded safety
budget; it never authorizes a concurrent delete or a false ledger terminal.

If no personal option is available, or the console cannot be reached without
creating a non-personal organization, the flow fails with
`personal_account_not_available`; it must not report a false success.

## Session Output

Save all authenticated cookies needed by Claude Platform under:

```text
cookies/claude_api/
```

Use one unique, email-key-associated full-cookie JSON artifact per successful
save, following the repository's existing safe filename conventions. Write a
private temporary file, fsync it, atomically rename it, and only then publish
its index record under an interprocess lock. On index failure, remove the new
cookie artifact so the index cannot publish a missing reference. Store only a
one-way email key and non-secret filename metadata; do not copy the source
email, mailbox password, client ID, refresh token, NINEMALL API password,
verification code, magic link, or cookie value into the session index or logs.
On POSIX, newly written cookie and index files are mode `0600`.

Do not assume Claude Platform uses the Claude.ai `sessionKey` cookie. Success
is based on the authenticated console state and the exported cookie set. The
future recharge stage will consume this exported session rather than repeat
email authentication.

## Orchestrator and WebUI Integration

Add `claude_api` to platform choices in:

- `register_three_platforms.py`
- `run_full_flow.py`
- WebUI task configuration and script metadata

The generated command forwards email, password, refresh token, client ID,
timeout, proxy context, and mailbox-provider configuration without printing
secrets.

For `register_three_platforms.py --from-pool`, a `claude_api` run uses the
currently selected provider, whether NINEMALL or OUTLOOK, and reserves its
purpose-specific ledger. In a run containing both `claude` and `claude_api`,
the orchestrator atomically reserves one source account for both purposes.
Pure OUTLOOK `claude` retains its legacy `tri` pool contract so the existing
Claude.ai flow remains unchanged.
Each child marks only its own ledger, so one child may succeed while the other
fails without corrupting or consuming the other's state. `run_full_flow.py`
bypasses its mailbox-registration Stage A only for a NINEMALL Claude-family-only
run; explicit OUTLOOK keeps the existing Stage A behavior. Mixed runs containing
ChatGPT or Grok preserve the current legacy `tri` mailbox routing and must not
route those platforms through NINEMALL.

The existing `claude` option, default behavior, and result format remain
unchanged. `claude_api` emits the same high-level success marker expected by the
orchestrators while retaining its own log and state namespace.

## Error Handling

Use sanitized reason codes. The initial set is:

- `mail_timeout`
- `verification_artifact_not_found`
- `magic_link_invalid`
- `verification_rejected`
- `personal_account_not_available`
- `console_not_reached`
- `registration_error`

Existing NINEMALL transport codes such as `http_400`, `http_401`, `http_403`,
`invalid_json`, `invalid_response`, `network_error`, `transient_http`, and
`unexpected_http` remain valid.

Logs may include provider, folder, retry number, page-state name, and masked
email address. Logs and exceptions must not include mailbox passwords, client
IDs, refresh tokens, API passwords, request bodies, full API responses,
verification codes, magic links, session cookies, or credential-bearing proxy
URLs. Orchestrators also mask complete email addresses found in relayed child
output. A run containing `claude_api` returns a nonzero status for child launch
failure, nonzero child exit, or a missing semantic success marker; legacy runs
without `claude_api` retain their prior exit convention.

## Testing

All automated tests use temporary files, mocked mailbox responses, and mocked
browser/page objects. They must not perform live AppleEmail, Microsoft,
Anthropic, proxy, or account-creation requests.

Coverage includes:

- Independent Claude.ai and Claude Platform reservation state for one address.
- NINEMALL and OUTLOOK column ordering remaining correct.
- NINEMALL extraction of a Platform code from subject, text, and HTML-derived
  visible text.
- Rejection of stale codes, wrong-length values, dates, CSS colors, URLs, and
  unrelated numeric identifiers.
- Direct Platform magic-link validation and SafeLinks decoding.
- Rejection of wrong hosts, non-HTTPS links, malformed links, and unexpected
  paths.
- A magic-link-only message completing the link path.
- A code-only message completing the code path.
- The exceptional both-artifacts case selecting according to current page
  state rather than a global priority.
- INBOX/Junk order, newest-first selection, timestamp filtering, and a single
  resend with a fresh baseline.
- Strict absence of Outlook/Graph/broker fallback for NINEMALL.
- Graph, broker, and Outlook-browser paths for OUTLOOK.
- Personal-account selection using a unique, explicit locator.
- Refusal to submit organization or team forms.
- Console-state success detection and false-success rejection.
- Session export under `cookies/claude_api/` without mailbox credentials.
- CLI command construction, NINEMALL credential ordering, proxy propagation,
  task cancellation, cleanup, and redacted logging.
- `claude_api` integration in direct CLI, both orchestrators, and WebUI.
- Claude-family-only NINEMALL routing and unchanged mixed-platform routing.
- All existing Claude.ai, NINEMALL magic-link, account-store, proxy, and
  lifecycle tests continuing to pass.

## Acceptance Criteria

The feature is complete when:

1. `claude_api` is selectable independently from `claude` in CLI and WebUI.
2. One NINEMALL mailbox can be independently processed by both platform flows.
3. Claude Platform authentication succeeds with either the valid magic-link
   email form or the valid numeric-code email form, with no artificial
   preference between them.
4. NINEMALL never falls back to Outlook infrastructure.
5. Only the personal-account route is selected; no organization is created.
6. Success requires a verified authenticated console state.
7. A reusable, secret-safe Claude Platform session is exported for the later
   recharge workflow.
8. Existing Claude.ai and non-Claude workflows remain behaviorally unchanged.
9. Focused and regression tests pass without consuming real external accounts
   or credentials.
