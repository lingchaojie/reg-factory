# Octo Headless WebUI Configuration Design

## Goal

Add a single global WebUI setting that controls whether every Octo Browser
profile is started in headless mode. The setting is disabled by default and
does not affect BitBrowser or AdsPower.

## Configuration Contract

- Canonical environment key: `OCTO_HEADLESS`.
- Default value: `false`.
- Accepted true values at runtime: `1`, `true`, `yes`, and `on`, matched
  case-insensitively after trimming whitespace.
- Every other value, including an empty or missing value, means `false`.
- The WebUI writes the canonical strings `true` and `false`.

## WebUI Behavior

The existing fingerprint-browser settings group gains an `OCTO_HEADLESS`
boolean item labeled as Octo headless mode. It is rendered as a checkbox:

- unchecked by default;
- checked when the saved value parses as true;
- saved as `true` when checked and `false` when unchecked.

The setting remains visible even when another fingerprint-browser provider is
selected, consistent with the other provider-specific fields in this group.

## Runtime Behavior

`OctoBrowser.open_browser()` reads the current `OCTO_HEADLESS` value when it
builds the Local API start payload and sends the parsed boolean in the
`headless` field of `POST /api/profiles/start`.

Reading at profile-start time ensures a setting saved through the long-lived
WebUI process is applied to subsequently launched profiles without requiring a
server restart. All flows that use the shared Octo provider inherit the same
behavior, including Outlook, Claude, ChatGPT, and Grok.

The Octo client must still be running because profile start and stop continue
to use its Local API. This feature changes only window visibility; it does not
introduce one-time profiles or change proxy/IPMart routing.

## Compatibility

- Existing installations behave exactly as before because the default is
  `false`.
- BitBrowser and AdsPower payloads and startup behavior are unchanged.
- Existing Octo API token and Public/Local base configuration are unchanged.
- No per-script or per-profile override is added in this scope.

## Testing

Tests will be added before production changes and will verify:

1. Octo sends `headless: false` when `OCTO_HEADLESS` is missing.
2. Octo sends `headless: true` for an enabled environment value.
3. The value is read at start time rather than frozen at module import.
4. WebUI metadata exposes a boolean `OCTO_HEADLESS` item with default `false`.
5. WebUI rendering and save collection use a checkbox and canonical
   `true`/`false` strings.
6. Existing Octo provider, BitBrowser, AdsPower, IPMart, and route-selection
   regression suites remain green.

## Documentation

`.env.example`, README configuration guidance, and CHANGELOG will document the
new setting, its default, its global Octo scope, and the requirement to keep
the Octo client running.
