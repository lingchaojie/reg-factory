# Octo Headless WebUI Setting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a global `OCTO_HEADLESS` WebUI checkbox, defaulting to `false`, that controls the `headless` boolean sent whenever the shared Octo provider starts a profile.

**Architecture:** Keep the runtime decision inside `octobrowser.py` and read the environment at profile-start time so WebUI saves affect later launches without a restart. Extend the existing environment metadata and renderer with a real checkbox that serializes canonical `true`/`false` strings. Do not add per-script overrides or alter any non-Octo provider.

**Tech Stack:** Python 3, `unittest`, FastAPI environment endpoints, vanilla JavaScript, Octo Local API.

## Global Constraints

- The canonical key is exactly `OCTO_HEADLESS`.
- The default is exactly `false`.
- Runtime true values are `1`, `true`, `yes`, and `on`, matched case-insensitively after trimming.
- Every other value is false.
- The WebUI writes only `true` or `false`.
- The setting applies to every profile started through the shared Octo provider.
- BitBrowser, AdsPower, IPMart routing, Clash/direct routing, and one-time-profile behavior must not change.
- The Octo client remains required for Local API start and stop operations.

---

### Task 1: Dynamic Octo Start Payload

**Files:**
- Modify: `tests/test_octobrowser.py`
- Modify: `octobrowser.py`

**Interfaces:**
- Consumes: process environment key `OCTO_HEADLESS: str | None`.
- Produces: `_env_bool(name: str, default: bool = False) -> bool` and a Local API start payload whose `headless` member is a Python boolean.

- [ ] **Step 1: Add failing default and dynamic-read tests**

Add `import os` if it is not already present, then add these tests beside `test_start_normalizes_ws_endpoint` in `tests/test_octobrowser.py`:

```python
def test_start_defaults_to_visible_octo_window(self):
    browser, session = self.make_browser([
        FakeResponse({
            "uuid": "profile-1",
            "ws_endpoint": "ws://127.0.0.1:55000/devtools/browser/id",
            "debug_port": "55000",
        })
    ])
    with patch.dict(os.environ, {}, clear=True):
        browser.open_browser("profile-1")
    self.assertIs(session.calls[0][2]["json"]["headless"], False)

def test_start_reads_enabled_headless_value_at_call_time(self):
    browser, session = self.make_browser([
        FakeResponse({
            "uuid": "profile-1",
            "ws_endpoint": "ws://127.0.0.1:55000/devtools/browser/id",
            "debug_port": "55000",
        })
    ])
    with patch.dict(os.environ, {"OCTO_HEADLESS": "  YeS  "}, clear=True):
        browser.open_browser("profile-1")
    self.assertIs(session.calls[0][2]["json"]["headless"], True)
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
python -m unittest tests.test_octobrowser.OctoBrowserTests.test_start_defaults_to_visible_octo_window tests.test_octobrowser.OctoBrowserTests.test_start_reads_enabled_headless_value_at_call_time -v
```

Expected: the default test passes because the existing payload is false, while the enabled test fails with `False is not True`. This is an acceptable RED because the pair locks both compatibility and the missing behavior.

- [ ] **Step 3: Implement the minimal parser and dynamic payload value**

Add near the top of `octobrowser.py`:

```python
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_ENV_VALUES
```

Change only the existing start payload member in `OctoBrowser.open_browser()`:

```python
"headless": _env_bool("OCTO_HEADLESS"),
```

- [ ] **Step 4: Verify GREEN and parser boundaries**

Run:

```powershell
python -m unittest tests.test_octobrowser -v
```

Expected: all Octo adapter tests pass. Confirm the new enabled test passes and the existing start URL/CDP normalization test remains green.

- [ ] **Step 5: Commit the runtime behavior**

```powershell
git add tests/test_octobrowser.py octobrowser.py
git commit -m "feat: support Octo headless profile starts"
```

---

### Task 2: WebUI Boolean Metadata, Rendering, and Saving

**Files:**
- Modify: `tests/test_octo_provider_integration.py`
- Modify: `webui/scripts.py`
- Modify: `webui/static/app.js`

**Interfaces:**
- Consumes: environment-schema items with `type: "bool"`, `value: str`, and `default: bool`.
- Produces: an `OCTO_HEADLESS` checkbox whose DOM value is serialized as the strings `true` or `false` by both connection tests and environment saves.

- [ ] **Step 1: Add failing WebUI contract tests**

Extend `test_webui_metadata_exposes_canonical_octo_settings` in `tests/test_octo_provider_integration.py`:

```python
self.assertIn("OCTO_HEADLESS", items)
self.assertEqual(items["OCTO_HEADLESS"]["type"], "bool")
self.assertIs(items["OCTO_HEADLESS"]["default"], False)
```

Add a focused frontend-source contract test beside `test_frontend_maps_octo_status_label`:

```python
def test_frontend_renders_and_serializes_boolean_env_items(self):
    source = Path(server.WEBUI, "static", "app.js").read_text(
        encoding="utf-8"
    )
    self.assertIn("it.type === 'bool'", source)
    self.assertIn('type="checkbox"', source)
    self.assertIn("i.checked ? 'true' : 'false'", source)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_octo_provider_integration.OctoProviderIntegrationTests.test_webui_metadata_exposes_canonical_octo_settings tests.test_octo_provider_integration.OctoProviderIntegrationTests.test_frontend_renders_and_serializes_boolean_env_items -v
```

Expected: metadata fails because `OCTO_HEADLESS` is absent; frontend contract fails because environment items do not support checkboxes.

- [ ] **Step 3: Add the schema item**

In the fingerprint-browser group in `webui/scripts.py`, immediately after `OCTO_API_TOKEN`, add:

```python
{"key": "OCTO_HEADLESS", "type": "bool", "default": False,
 "help": "Octo 无头模式；开启后所有 Octo profile 均不显示浏览器窗口"},
```

- [ ] **Step 4: Add checkbox rendering and canonical serialization**

In `webui/static/app.js`, add this helper immediately before `loadEnv()`:

```javascript
function envControlValue(i){
  return i.type === 'checkbox' ? (i.checked ? 'true' : 'false') : i.value;
}
```

Inside `loadEnv()`, replace the environment-control construction with a three-way choice:

```javascript
const type = it.secret ? 'password':'text';
const value = it.value || it.default || '';
const boolChecked = ['1','true','yes','on'].includes(
  String(value).trim().toLowerCase()
);
const control = it.type === 'choice'
  ? `<select data-env="${it.key}">${(it.choices||[]).map(c=>`<option value="${c}" ${c===value?'selected':''}>${c}</option>`).join('')}</select>`
  : it.type === 'bool'
    ? `<input type="checkbox" data-env="${it.key}" ${boolChecked?'checked':''}>`
    : `<input type="${type}" data-env="${it.key}" value="${(it.value||'').replace(/"/g,'&quot;')}"
             placeholder="${it.default? '默认 '+it.default : ''}">`;
```

Update both environment collection loops:

```javascript
$$('input[data-env],select[data-env]').forEach(i=>{
  const value = envControlValue(i);
  if(value!=='') env[i.dataset.env]=value;
});
```

for `runTest()`, and:

```javascript
$$('input[data-env],select[data-env]').forEach(i=>{
  env[i.dataset.env] = envControlValue(i);
});
```

for the save handler.

- [ ] **Step 5: Verify WebUI GREEN and JavaScript syntax**

Run:

```powershell
python -m unittest tests.test_octo_provider_integration tests.test_webui_env_reload -v
node --check webui/static/app.js
```

Expected: all selected Python tests pass and Node exits 0.

- [ ] **Step 6: Commit the WebUI behavior**

```powershell
git add tests/test_octo_provider_integration.py webui/scripts.py webui/static/app.js
git commit -m "feat: configure Octo headless mode in WebUI"
```

---

### Task 3: Operator Configuration and Full Regression

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: the implemented `OCTO_HEADLESS` environment contract.
- Produces: operator-facing defaults and guidance matching runtime and WebUI behavior.

- [ ] **Step 1: Add the example configuration**

In `.env.example`, add immediately after `OCTO_API_TOKEN=`:

```dotenv
# true 时所有 Octo profile 均以无窗口模式启动；默认 false
OCTO_HEADLESS=false
```

- [ ] **Step 2: Update README guidance and configuration table**

In the Octo Browser option, state that `OCTO_HEADLESS=true` globally hides all Octo profile windows, defaults to false, still requires the Octo client, and prevents manual CAPTCHA takeover.

Add this configuration-table row:

```markdown
| `OCTO_HEADLESS` | 全局 Octo 无头模式；WebUI 复选框配置，默认 `false`；开启后所有 Octo profile 不显示窗口，Octo 客户端仍须运行 | 否 |
```

- [ ] **Step 3: Add a CHANGELOG entry**

Under the current release section, add:

```markdown
- WebUI 新增 `OCTO_HEADLESS` 全局复选框，默认关闭；开启后所有共享 Octo provider 启动均传递 `headless: true`。
```

- [ ] **Step 4: Run complete verification**

Run:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q .
node --check webui/static/app.js
git diff --check
```

Expected: all 254 baseline tests plus the new tests pass, compilation and JavaScript syntax exit 0, and `git diff --check` prints no errors.

- [ ] **Step 5: Verify scope and secrets**

Run:

```powershell
git diff --stat main...HEAD
rg -n "OCTO_HEADLESS|headless" octobrowser.py webui .env.example README.md CHANGELOG.md tests/test_octobrowser.py tests/test_octo_provider_integration.py
git status --short
```

Expected: only the planned runtime, WebUI, test, and documentation files changed; no token or proxy credential values are introduced; the worktree is clean after the final commit.

- [ ] **Step 6: Commit documentation**

```powershell
git add .env.example README.md CHANGELOG.md
git commit -m "docs: explain Octo headless configuration"
```
