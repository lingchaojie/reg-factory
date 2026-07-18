# -*- coding: utf-8 -*-
"""
Claude.ai auto registration script
BitBrowser + Playwright + Mail API + SMS API
Full auto: create email -> register -> magic link -> form -> phone verify -> extract cookie
Usage: python register.py [--count N]
"""

import argparse
import asyncio
import functools
import json
import os
import random
import re
import string
import sys
import time
from datetime import datetime
from urllib.parse import urlsplit

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")

import requests
from playwright.async_api import async_playwright

from bitbrowser import BitBrowser
try:
    from common import proxy_switch
except Exception:
    proxy_switch = None
from common import human_mouse as _hm
from common.account_proxy import (
    IPMartBitBrowserError,
    bitbrowser_proxy_fields,
    lease_from_env,
    sanitized_bitbrowser_error,
    strip_http_proxy_env,
)
from common.ipmart_proxy import (
    IPMartProxyError,
    acquire_proxy,
    settings_from_env,
)
from common.claude_email_accounts import (
    ClaudeEmailAccount,
    ClaudeEmailAccountStore,
    normalize_email_provider,
)
from common.ninemail_mailbox import NineMallMailboxClient, NineMallMailboxError
try:
    from check_outlook_status import check_account_api
except Exception:
    check_account_api = None
from config import (
    CLAUDE_LOGIN_URL,
    COOKIE_OUTPUT_DIR,
    SMS_API_BASE,
    SMS_COUNTRY_BLACKLIST,
    SMS_COUNTRY_PREFER,
    SMS_PROJECT_ID,
    SMS_TOKEN,
    HERO_SMS_API_BASE,
    HERO_SMS_API_KEY,
    HERO_SMS_SERVICE,
    HERO_SMS_COUNTRY_PREFER,
    CAPSOLVER_API_KEY,
    EZCAPTCHA_API_KEY,
    EZCAPTCHA_API_BASE,
    NINEMALL_API_BASE,
    NINEMALL_API_PASSWORD,
    NINEMALL_EMAIL_FILE,
    NINEMALL_HTTP_TIMEOUT,
    NINEMALL_POLL_INTERVAL,
    EMAIL_PROVIDER,
)

# single registration timeout (seconds)
REGISTER_TIMEOUT = 600

# Clash 代理：claude.com 对本机 IP 区域封锁(app-unavailable-in-region)，走干净节点绕过。
# None/"none" = 不走代理(默认，向后兼容)；"auto" = 自动探测能进 claude 的节点；其它 = 指定节点名。
CLAUDE_PROXY_NODE = None
CLAUDE_PROXY_HOST = "127.0.0.1"
CLAUDE_PROXY_PORT = "7897"
# 节点轮换：同一节点 IP 连续注册 1-2 个新 Claude 号就会被风控打 /restricted，
# 故记录最近用过的节点、auto 选号时避开，雨露均沾分散 IP 信誉。
CLAUDE_NODE_USAGE_FILE = "claude_node_usage.txt"
CLAUDE_NODE_AVOID_RECENT = 3  # 避开最近 N 次用过的节点


def _recent_claude_nodes(limit=CLAUDE_NODE_AVOID_RECENT):
    try:
        with open(CLAUDE_NODE_USAGE_FILE, encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        return lines[-limit:]
    except Exception:
        return []


def _record_claude_node(node):
    try:
        with open(CLAUDE_NODE_USAGE_FILE, "a", encoding="utf-8") as f:
            f.write(node + "\n")
    except Exception:
        pass


def _pick_claude_node():
    """auto 选节点：避开最近用过的节点找一个能过 claude CF 的；找不到再放开全量。返回节点名或 None。"""
    markers = ("app-unavailable-in-region", "unavailable in your",
               "just a moment", "performing security")
    try:
        alln = proxy_switch.concrete_nodes()
    except Exception as e:
        print(f"  [proxy] 取节点列表失败: {e}")
        return None
    recent = set(_recent_claude_nodes())
    fresh = [n for n in alln if n not in recent] or alln
    print(f"  [proxy] 避开最近节点 {sorted(recent)}; 在 {len(fresh)}/{len(alln)} 个候选里探测...")
    node = proxy_switch.find_working_node(
        test_url="https://claude.ai/login", challenge_markers=markers, candidates=fresh)
    if not node and fresh is not alln:
        print("  [proxy] 新鲜节点都不通，放开全量重探...")
        node = proxy_switch.find_working_node(
            test_url="https://claude.ai/login", challenge_markers=markers, candidates=alln)
    return node


def prepare_claude_network(
    env=None, *, account_lease=None, ipmart_enabled=False
):
    env = os.environ if env is None else env
    if account_lease is not None or ipmart_enabled:
        strip_http_proxy_env(env)
    return env


def configure_claude_proxy(
    node_arg, account_lease=None, *, ipmart_enabled=False
):
    global CLAUDE_PROXY_NODE
    CLAUDE_PROXY_NODE = None
    if account_lease is not None:
        print(
            "  [proxy] IPMart account proxy "
            f"{account_lease.host}:{account_lease.port} "
            f"exit={account_lease.exit_ip}"
        )
        return
    if ipmart_enabled:
        print("  [proxy] IPMart enabled; skipping Clash node selection")
        return
    if not node_arg or node_arg.lower() == "none":
        return
    if proxy_switch is None:
        print(
            "  [proxy] proxy_switch unavailable; "
            "Claude may be region-blocked"
        )
        return
    try:
        if node_arg.lower() == "auto":
            print("  [proxy] probing Clash nodes for Claude...")
            node = _pick_claude_node()
            if not node:
                print(
                    "  [proxy] no working Claude node found; "
                    "continuing without a browser proxy"
                )
                return
            CLAUDE_PROXY_NODE = node
            _record_claude_node(node)
            print(f"  [proxy] selected node: {node}")
            return
        proxy_switch.set_node(node_arg)
        time.sleep(2)
        CLAUDE_PROXY_NODE = node_arg
        print(f"  [proxy] selected node: {proxy_switch.current_node()}")
    except Exception as exc:
        print(f"  [proxy] Clash node selection failed: {exc}")


def create_claude_profile(bb, name, account_lease=None):
    proxy_fields = (
        bitbrowser_proxy_fields(account_lease) if account_lease else {}
    )
    safe_error = None
    try:
        return bb.create_browser(name=name, **proxy_fields)
    except Exception as exc:
        if account_lease is None:
            raise
        safe_error = sanitized_bitbrowser_error(exc)
    raise safe_error from None

# web2api 验证服务地址
WEB2API_BASE = "http://127.0.0.1:9000"

# 邮箱文件路径
EMAILS_FILE = "emails.txt"
EMAILS_USED_FILE = "emails_used.txt"
EMAILS_ERROR_FILE = "emails_error.txt"


def verify_registered_outlook(email, password, tag="[outlook]"):
    """Verify the newly registered Outlook account is usable before downstream use."""
    if check_account_api is None:
        print(f"  {tag} verify skipped: check_outlook_status unavailable")
        return True
    result = check_account_api(email, password)
    status = result.get("status")
    code = result.get("code") or ""
    msg = result.get("message") or ""
    print(f"  {tag} post-register verify: {status} {code} {msg[:80]}")
    return status == "ok"


def _load_used_emails():
    """加载已使用和异常邮箱集合"""
    used = set()
    for fpath in [EMAILS_USED_FILE, EMAILS_ERROR_FILE]:
        if os.path.exists(fpath):
            with open(fpath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        used.add(line.split("----")[0].strip().lower())
    return used


import threading
_email_lock = threading.Lock()


def validate_session_key(session_key: str) -> bool:
    """Placeholder: 验证由 validate_session_key_with_page 异步完成"""
    return True  # 同步版本不可用，跳过


async def validate_session_key_with_page(page, session_key: str) -> bool:
    """用全新的 BitBrowser 窗口验证 sessionKey 是否独立可用。
    开新窗口 → 设 sessionKey cookie → 打开 claude.ai → 检查是否登录成功 → 发消息收到回复。
    """
    bb = BitBrowser()
    profile_id = None
    try:
        name = f"validate_{datetime.now().strftime('%H%M%S')}"
        profile_id = bb.create_browser(name=name)
        info = bb.open_browser(profile_id)
        ws = info.get("ws", "")

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(ws)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            vpage = await context.new_page()

            # 设 sessionKey cookie
            await context.add_cookies([{
                "name": "sessionKey",
                "value": session_key,
                "domain": ".claude.ai",
                "path": "/",
            }])

            # 打开 claude.ai，看是否能正常加载（不被重定向到 login）
            await vpage.goto("https://claude.ai", timeout=30000)
            await asyncio.sleep(5)

            current_url = vpage.url.lower()
            if '/login' in current_url:
                print(f"  [validate] redirected to login — sessionKey invalid")
                return False

            # 在页面内发消息验证
            result = await vpage.evaluate("""
                async () => {
                    try {
                        // 1) 拿 org_uuid
                        const accResp = await fetch('https://claude.ai/api/account', { credentials: 'include' });
                        if (!accResp.ok) return { step: 'account', status: accResp.status, ok: false };
                        const accData = await accResp.json();
                        const memberships = accData.memberships || [];
                        if (!memberships.length) return { step: 'account', status: accResp.status, ok: false, error: 'no memberships' };
                        const orgUuid = (memberships[0].organization || {}).uuid;
                        if (!orgUuid) return { step: 'account', status: accResp.status, ok: false, error: 'no org_uuid' };

                        // 2) 创建会话
                        const convResp = await fetch(`https://claude.ai/api/organizations/${orgUuid}/chat_conversations`, {
                            method: 'POST', credentials: 'include',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ name: '', model: 'claude-sonnet-4-5-20250929' })
                        });
                        if (!convResp.ok) {
                            const t = await convResp.text();
                            return { step: 'create_conv', status: convResp.status, ok: false, error: t.substring(0, 200) };
                        }
                        const convData = await convResp.json();
                        const convUuid = convData.uuid;
                        if (!convUuid) return { step: 'create_conv', status: convResp.status, ok: false, error: 'no conv uuid' };

                        // 3) 发测试消息
                        const completionUrl = `https://claude.ai/api/organizations/${orgUuid}/chat_conversations/${convUuid}/completion`;
                        const msgResp = await fetch(completionUrl, {
                            method: 'POST', credentials: 'include',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                prompt: 'hi',
                                timezone: 'UTC',
                                personalized_styles: [{ type: 'default', key: 'Default', name: 'Normal', nameKey: 'normal_style_name', prompt: 'Normal\\n', summary: 'Default responses from Claude', summaryKey: 'normal_style_summary', isDefault: true }],
                                tools: [],
                                attachments: [],
                                files: [],
                                sync_sources: [],
                                rendering_mode: 'messages',
                                create_conversation_params: { name: '', include_conversation_preferences: true, is_temporary: false }
                            })
                        });
                        if (!msgResp.ok) {
                            const t = await msgResp.text();
                            return { step: 'completion', status: msgResp.status, ok: false, error: t.substring(0, 300) };
                        }

                        // 4) 读 SSE 流，确认收到文本回复
                        const reader = msgResp.body.getReader();
                        const decoder = new TextDecoder();
                        let gotText = false;
                        let chunks = '';
                        const timeout = Date.now() + 30000;
                        while (Date.now() < timeout) {
                            const { done, value } = await reader.read();
                            if (done) break;
                            chunks += decoder.decode(value, { stream: true });
                            if (chunks.includes('"text"') || chunks.includes('content_block_delta')) {
                                gotText = true;
                                reader.cancel();
                                break;
                            }
                        }

                        return { step: 'done', status: msgResp.status, ok: gotText, preview: chunks.substring(0, 200) };
                    } catch(e) {
                        return { step: 'error', status: 0, ok: false, error: e.message };
                    }
                }
            """)

            step = result.get('step', '?')
            status = result.get('status', 0)
            ok = result.get('ok', False)

            if ok:
                print(f"  [validate] new browser chat OK — sessionKey valid!")
                return True

            error = result.get('error', result.get('preview', ''))[:200]
            print(f"  [validate] failed at step={step} HTTP {status}: {error}")
            return False
    except Exception as e:
        print(f"  [validate] error: {e}")
        return False
    finally:
        try:
            if profile_id:
                bb.close_browser(profile_id)
                await asyncio.sleep(2)
                bb.delete_browser(profile_id)
        except Exception:
            pass


def read_next_email_from_file():
    """从 emails.txt 读取下一个未使用的邮箱，返回 (email, password, token) 或 None
    线程安全：读取后立即标记为已使用，防止并发取到同一个"""
    with _email_lock:
        if not os.path.exists(EMAILS_FILE):
            print(f"  [email-file] {EMAILS_FILE} not found")
            return None
        used = _load_used_emails()
        with open(EMAILS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("----")
                email_addr = parts[0].strip().lower()
                if email_addr in used:
                    continue
                password = parts[1].strip() if len(parts) >= 2 else ""
                token = parts[2].strip() if len(parts) >= 3 else ""
                # 立即标记为已使用，防止其他线程取到同一个
                with open(EMAILS_USED_FILE, "a", encoding="utf-8") as uf:
                    uf.write(f"{email_addr}----{password}----reserved\n")
                print(f"  [email-file] picked: {email_addr}")
                return email_addr, password, token
        print(f"  [email-file] no unused emails left in {EMAILS_FILE}")
        return None


def mark_email_used(email, password=""):
    """记录已成功使用的邮箱"""
    with open(EMAILS_USED_FILE, "a", encoding="utf-8") as f:
        f.write(f"{email}----{password}\n")


def mark_email_error(email, password="", reason=""):
    """记录异常邮箱"""
    with open(EMAILS_ERROR_FILE, "a", encoding="utf-8") as f:
        f.write(f"{email}----{password}----{reason}\n")


def prepare_email_accounts(
    args, provider=None, store_factory=ClaudeEmailAccountStore
):
    provider = normalize_email_provider(provider or EMAIL_PROVIDER)
    source = args.emails or (
        NINEMALL_EMAIL_FILE if provider == "NINEMALL" else EMAILS_FILE
    )
    store = store_factory(provider=provider, source_file=source)
    if args.email:
        if provider == "NINEMALL" and (
            not args.token or not args.client_id
        ):
            raise SystemExit(
                "NINEMALL --email requires --token and --client-id"
            )
        account = ClaudeEmailAccount(
            provider=provider,
            email=args.email.strip(),
            password=(args.password or "").strip(),
            client_id=(args.client_id or "").strip(),
            refresh_token=(args.token or "").strip(),
        )
        return [account], store
    if args.emails:
        return store.reserve_many(limit=None), store
    if provider == "NINEMALL":
        return store.reserve_many(limit=args.count), store
    return [None] * args.count, store


def mark_claude_account_used(account, account_store):
    if (
        account is not None
        and account.provider == "NINEMALL"
        and account_store is not None
    ):
        account_store.mark_used(account)
        return
    if account is not None:
        mark_email_used(account.email, account.password)


def mark_claude_account_error(account, account_store, reason):
    if (
        account is not None
        and account.provider == "NINEMALL"
        and account_store is not None
    ):
        account_store.mark_error(account, reason)
        return
    if account is not None:
        mark_email_error(account.email, account.password, reason)


# Arkose Labs public key for Microsoft signup
MS_SIGNUP_ARKOSE_KEY = "B7D8911C-5CC8-A9A3-35B0-554ACEE604DA"


def solve_arkose_capsolver(public_key=MS_SIGNUP_ARKOSE_KEY, page_url="https://signup.live.com/", max_wait=120):
    """Use CapSolver to solve Arkose Labs (FunCaptcha) challenge.
    Returns token string or None."""
    if not CAPSOLVER_API_KEY:
        print("  [capsolver] no API key configured, skipping")
        return None
    try:
        # Step 1: create task
        payload = {
            "clientKey": CAPSOLVER_API_KEY,
            "task": {
                "type": "FunCaptchaTaskProxyLess",
                "websiteURL": page_url,
                "websitePublicKey": public_key,
            }
        }
        resp = requests.post("https://api.capsolver.com/createTask", json=payload, timeout=30)
        data = resp.json()
        if data.get("errorId", 1) != 0:
            print(f"  [capsolver] create error: {data.get('errorDescription', data)}")
            return None
        task_id = data["taskId"]
        print(f"  [capsolver] task created: {task_id}")

        # Step 2: poll result
        start = time.time()
        while time.time() - start < max_wait:
            time.sleep(5)
            resp = requests.post("https://api.capsolver.com/getTaskResult", json={
                "clientKey": CAPSOLVER_API_KEY,
                "taskId": task_id,
            }, timeout=30)
            result = resp.json()
            status = result.get("status")
            if status == "ready":
                token = result.get("solution", {}).get("token")
                print(f"  [capsolver] solved! token: {token[:60]}...")
                return token
            elif status == "failed":
                print(f"  [capsolver] failed: {result.get('errorDescription', '')}")
                return None
            elapsed = int(time.time() - start)
            print(f"  [capsolver] waiting... ({elapsed}s)")
        print("  [capsolver] timeout")
        return None
    except Exception as e:
        print(f"  [capsolver] error: {e}")
        return None


def solve_funcaptcha_ezcaptcha(public_key=MS_SIGNUP_ARKOSE_KEY, page_url="https://signup.live.com/", max_wait=60):
    """Use EZ-Captcha to solve FunCaptcha/Arkose Labs challenge.
    Returns token string or None."""
    if not EZCAPTCHA_API_KEY:
        print("  [ezcaptcha] no API key configured, skipping")
        return None
    try:
        # Step 1: create task
        payload = {
            "clientKey": EZCAPTCHA_API_KEY,
            "task": {
                "type": "FunCaptchaTaskProxyLess",
                "websiteURL": page_url,
                "websitePublicKey": public_key,
            }
        }
        resp = requests.post(f"{EZCAPTCHA_API_BASE}/createTask", json=payload, timeout=30)
        data = resp.json()
        if data.get("errorId", 1) != 0:
            print(f"  [ezcaptcha] create error: {data.get('errorDescription', data)}")
            return None
        task_id = data["taskId"]
        print(f"  [ezcaptcha] task created: {task_id}")

        # Step 2: poll result
        start = time.time()
        while time.time() - start < max_wait:
            time.sleep(5)
            resp = requests.post(f"{EZCAPTCHA_API_BASE}/getTaskResult", json={
                "clientKey": EZCAPTCHA_API_KEY,
                "taskId": task_id,
            }, timeout=30)
            result = resp.json()
            status = result.get("status")
            if status == "ready":
                token = result.get("solution", {}).get("token")
                print(f"  [ezcaptcha] solved! token: {token[:60]}...")
                return token
            elif status == "failed":
                print(f"  [ezcaptcha] failed: {result.get('errorDescription', '')}")
                return None
            elapsed = int(time.time() - start)
            print(f"  [ezcaptcha] waiting... ({elapsed}s)")
        print("  [ezcaptcha] timeout")
        return None
    except Exception as e:
        print(f"  [ezcaptcha] error: {e}")
        return None


def solve_perimeterx_ezcaptcha(page_url="https://signup.live.com/", app_id="PXzC5j78di", max_wait=60):
    """Use EZ-Captcha to solve PerimeterX press-and-hold challenge."""
    if not EZCAPTCHA_API_KEY:
        print("  [ezcaptcha-px] no API key configured, skipping")
        return None

    try:
        payload = {
            "clientKey": EZCAPTCHA_API_KEY,
            "task": {
                "type": "PerimeterX",
                "websiteURL": page_url,
                "websiteKey": app_id,
            }
        }
        print(f"  [ezcaptcha-px] creating task: PerimeterX, key={app_id}")
        resp = requests.post(f"{EZCAPTCHA_API_BASE}/createTask", json=payload, timeout=30)
        data = resp.json()
        if data.get("errorId", 1) != 0:
            err = data.get("errorDescription", data.get("errorCode", str(data)))
            print(f"  [ezcaptcha-px] create error: {err}")
            return None

        task_id = data["taskId"]
        print(f"  [ezcaptcha-px] task created: {task_id}")

        start = time.time()
        while time.time() - start < max_wait:
            time.sleep(5)
            resp = requests.post(f"{EZCAPTCHA_API_BASE}/getTaskResult", json={
                "clientKey": EZCAPTCHA_API_KEY,
                "taskId": task_id,
            }, timeout=30)
            result = resp.json()
            status = result.get("status")
            if status == "ready":
                solution = result.get("solution", {})
                print(f"  [ezcaptcha-px] solved! solution keys: {list(solution.keys())}")
                print(f"  [ezcaptcha-px] solution: {json.dumps(solution, ensure_ascii=False)[:200]}")
                return solution
            elif status == "failed":
                print(f"  [ezcaptcha-px] failed: {result.get('errorDescription', '')}")
                return None
            elapsed = int(time.time() - start)
            print(f"  [ezcaptcha-px] waiting... ({elapsed}s)")

        print(f"  [ezcaptcha-px] timeout")
        return None
    except Exception as e:
        print(f"  [ezcaptcha-px] error: {e}")
        return None


async def inject_arkose_token(page, token):
    """Inject solved Arkose token into the page to bypass enforcement frame."""
    try:
        # 方法1: 通过 enforcement callback 注入 token
        injected = await page.evaluate(f"""
            () => {{
                // 尝试找到 enforcement iframe 并提交 token
                const frames = document.querySelectorAll('iframe[id*="enforcement"], iframe[data-e2e="enforcement-frame"]');
                if (frames.length > 0) {{
                    // 通过 parent window 的回调提交
                    if (window.CE_READY) {{
                        window.CE_READY("{token}");
                        return "ce_ready";
                    }}
                }}
                // 方法2: 直接设置隐藏字段
                const hidden = document.querySelector('input[name="fc-token"], input[name="FunCaptcha"]');
                if (hidden) {{
                    hidden.value = "{token}";
                    return "hidden_field";
                }}
                // 方法3: 触发全局验证回调
                if (typeof window.fcCallback === 'function') {{
                    window.fcCallback("{token}");
                    return "fc_callback";
                }}
                if (typeof window.ArkoseEnforcement !== 'undefined') {{
                    try {{ window.ArkoseEnforcement.setConfig({{data: {{token: "{token}"}}}}) }} catch(e) {{}}
                    return "arkose_enforcement";
                }}
                return "no_method";
            }}
        """)
        print(f"  [arkose] inject result: {injected}")
        return injected != "no_method"
    except Exception as e:
        print(f"  [arkose] inject error: {e}")
        return False


# ========== Outlook Registration ==========

async def register_outlook(page):
    """Register a new outlook email account, returns (email, password) or (None, None)"""
    os.makedirs("screenshots", exist_ok=True)
    try:
        await page.goto("https://signup.live.com/signup?lic=1", timeout=30000)
        await asyncio.sleep(3)
        await page.screenshot(path="screenshots/outlook_start.png")

        # 生成邮箱和密码（前缀必须以字母开头）
        prefix = random.choice(string.ascii_lowercase) + "".join(random.choices(string.ascii_lowercase + string.digits, k=11))
        email = f"{prefix}@outlook.com"
        password = "Aa1!" + "".join(random.choices(string.ascii_letters + string.digits, k=12))

        print(f"  [outlook] registering: {email}")

        # Step 1: 输入邮箱
        email_ok = False
        for retry in range(5):
            email_input = page.locator('input[type="email"], input[name="MemberName"], input[id="MemberName"], input[id="usernameInput"], input[name="Username"]').first
            if await email_input.count() == 0:
                print("  [outlook] email input not found")
                await page.screenshot(path="screenshots/outlook_no_email.png")
                return None, None

            # 检测页面是否有域名下拉框（旧版只填前缀，新版填完整邮箱）
            domain_dropdown = page.locator('select[id="LiveDomainBoxList"], select[name="LiveDomainBoxList"], #LiveDomainBoxList').first
            has_domain_dropdown = await domain_dropdown.count() > 0

            await email_input.fill("")
            await asyncio.sleep(0.3)
            if has_domain_dropdown:
                # 旧版：有域名下拉框，只填前缀
                await email_input.fill(prefix)
                try:
                    await domain_dropdown.select_option("outlook.com")
                except Exception:
                    pass
                print(f"  [outlook] filled prefix: {prefix} (dropdown mode)")
            else:
                # 新版：直接填完整邮箱
                await email_input.fill(email)
                print(f"  [outlook] filled email: {email}")

            await asyncio.sleep(0.5)
            for sel in ['input[type="submit"]', 'button[type="submit"]', '#iSignupAction', 'button[id="iSignupAction"]']:
                btn = page.locator(sel).first
                if await btn.count() > 0:
                    await btn.click(timeout=3000)
                    break
            await asyncio.sleep(3)

            # 检查页面反馈
            page_text = await page.evaluate("() => document.body.innerText")
            page_lower = page_text.lower()
            print(f"  [outlook] page: {page_text[:200].replace(chr(10), ' ')}")

            # 邮箱被占用
            if ("already" in page_lower and "email" in page_lower) or "taken" in page_lower or "已被" in page_text:
                prefix = random.choice(string.ascii_lowercase) + "".join(random.choices(string.ascii_lowercase + string.digits, k=11))
                email = f"{prefix}@outlook.com"
                print(f"  [outlook] email taken, retry: {email}")
                continue

            # 格式错误
            if "needs to start" in page_lower or "in the format" in page_lower or "enter a valid" in page_lower or "use letters" in page_lower:
                prefix = random.choice(string.ascii_lowercase) + "".join(random.choices(string.ascii_lowercase + string.digits, k=9))
                email = f"{prefix}@outlook.com"
                print(f"  [outlook] format error, retry: {email}")
                continue
                continue

            email_ok = True
            break

        if not email_ok:
            print("  [outlook] all email attempts failed")
            await page.screenshot(path="screenshots/outlook_email_fail.png")
            return None, None

        await page.screenshot(path="screenshots/outlook_after_email.png")

        # Step 2: 输入密码
        await asyncio.sleep(2)
        # 等待密码输入框出现
        for _ in range(10):
            pwd_input = page.locator('input[type="password"], input[name="Password"], input[id="PasswordInput"], input[name="passwd"]').first
            if await pwd_input.count() > 0:
                break
            await asyncio.sleep(1)
        if await pwd_input.count() > 0:
            await pwd_input.fill(password)
            print(f"  [outlook] password filled")
            await asyncio.sleep(0.5)
            # 点 Next 按钮
            clicked_next = False
            for sel in ['#iSignupAction', 'input[type="submit"]', 'button[type="submit"]', 'button:has-text("Next")', 'button:has-text("next")']:
                btn = page.locator(sel).first
                if await btn.count() > 0:
                    try:
                        await btn.click(timeout=3000)
                        clicked_next = True
                        print(f"  [outlook] clicked next: {sel}")
                        break
                    except Exception:
                        pass
            if not clicked_next:
                # 试 Enter
                await page.keyboard.press("Enter")
                print("  [outlook] pressed Enter for password")
            await asyncio.sleep(3)
            # 打印密码提交后的页面状态
            pwd_page_text = await page.evaluate("() => document.body.innerText")
            print(f"  [outlook] after pwd: {pwd_page_text[:150].replace(chr(10), ' ')}")
            await page.screenshot(path="screenshots/outlook_after_pwd.png")
        else:
            print("  [outlook] password input not found")
            await page.screenshot(path="screenshots/outlook_no_pwd.png")
            return None, None

        # Step 3: 姓名在新版流程中移到了用户名之后，这里跳过

        # Step 4: 地区 + 生日
        year, month, day = generate_birthday()
        await asyncio.sleep(2)

        # 先等页面加载完
        for _ in range(10):
            page_text = await page.evaluate("() => document.body.innerText")
            if "birth" in page_text.lower() or "country" in page_text.lower() or "region" in page_text.lower():
                break
            await asyncio.sleep(1)

        await page.screenshot(path="screenshots/outlook_bday_page.png")

        # 尝试方式1: 传统 <select> 下拉框
        all_selects = page.locator('select')
        select_count = await all_selects.count()
        print(f"  [outlook] found {select_count} select elements")

        if select_count >= 2:
            if select_count >= 3:
                # Country, Month, Day
                try:
                    await all_selects.nth(0).select_option("US")
                    print("  [outlook] country: US")
                except Exception:
                    try:
                        await all_selects.nth(0).select_option(index=1)
                    except Exception:
                        pass
                await all_selects.nth(1).select_option(str(month))
                await all_selects.nth(2).select_option(str(day))
            else:
                # Month, Day only
                await all_selects.nth(0).select_option(str(month))
                await all_selects.nth(1).select_option(str(day))
            year_input = page.locator('input[id*="Year"], input[id*="year"], input[name*="Year"], input[name*="year"], input[type="text"]').first
            if await year_input.count() > 0:
                await year_input.fill(str(year))
        else:
            # 方式2: 新版页面 — 用 input/combobox/dropdown 组件
            print("  [outlook] no <select>, trying new UI components...")

            # 打印页面所有 input 和 button 帮助调试
            form_info = await page.evaluate("""() => {
                const inputs = [...document.querySelectorAll('input, select, button[role="combobox"], [role="listbox"], [role="combobox"], [aria-haspopup]')];
                return inputs.map(el => ({
                    tag: el.tagName, id: el.id, name: el.name, type: el.type,
                    role: el.getAttribute('role'), placeholder: el.placeholder,
                    ariaLabel: el.getAttribute('aria-label'), className: el.className.substring(0, 60)
                }));
            }""")
            print(f"  [outlook] form elements: {json.dumps(form_info, ensure_ascii=False)[:500]}")

            # 尝试找 Country 下拉
            country_input = page.locator('#countryDropdownId, [aria-label*="country" i], [aria-label*="region" i], [aria-label*="Country" i]').first
            if await country_input.count() > 0:
                await country_input.click(force=True)
                await asyncio.sleep(1)
                # 选 United States
                us_option = page.locator('[role="option"]:has-text("United States")').first
                if await us_option.count() > 0:
                    await us_option.click()
                    print("  [outlook] country: United States")
                else:
                    await page.keyboard.type("United States")
                    await asyncio.sleep(0.5)
                    await page.keyboard.press("Enter")
                    print("  [outlook] country: typed United States")
                await asyncio.sleep(1)

            # 根据 ariaLabel 智能匹配日期字段（兼容 Day/Month/Year 不同顺序）
            month_names = ["", "January", "February", "March", "April", "May", "June",
                          "July", "August", "September", "October", "November", "December"]

            date_fields = await page.evaluate("""() => {
                const combos = document.querySelectorAll('button[role="combobox"], [role="combobox"]');
                return Array.from(combos).filter(c => c.offsetParent !== null).map(c => ({
                    id: c.id || '',
                    ariaLabel: (c.getAttribute('aria-label') || '').toLowerCase(),
                    name: (c.name || '').toLowerCase(),
                }));
            }""")

            for field in date_fields:
                label = field['ariaLabel'] + ' ' + field['id'] + ' ' + field['name']
                if 'month' in label:
                    month_el = page.locator(f'#{field["id"]}') if field['id'] else page.locator(f'[aria-label*="month" i]').first
                    await month_el.click(force=True)
                    await asyncio.sleep(1)
                    month_opt = page.locator(f'[role="option"]:has-text("{month_names[month]}")').first
                    if await month_opt.count() > 0:
                        await month_opt.click()
                        print(f"  [outlook] month: {month_names[month]}")
                    else:
                        await page.keyboard.type(month_names[month])
                        await asyncio.sleep(0.3)
                        await page.keyboard.press("Enter")
                    await asyncio.sleep(1)
                elif 'day' in label:
                    day_el = page.locator(f'#{field["id"]}') if field['id'] else page.locator(f'[aria-label*="day" i]').first
                    await day_el.click(force=True)
                    await asyncio.sleep(1)
                    day_opt = page.locator(f'[role="option"]:has-text("{day}")').first
                    if await day_opt.count() > 0:
                        await day_opt.click()
                        print(f"  [outlook] day: {day}")
                    else:
                        await page.keyboard.type(str(day))
                        await asyncio.sleep(0.3)
                        await page.keyboard.press("Enter")
                    await asyncio.sleep(1)

            # Year（输入框）
            year_input = page.locator('#BirthYearInput, [aria-label*="year" i], [id*="Year" i], input[type="text"]').first
            if await year_input.count() > 0:
                await year_input.fill(str(year))
                print(f"  [outlook] year: {year}")

        await asyncio.sleep(0.5)
        for sel in ['input[type="submit"]', 'button[type="submit"]', '#iSignupAction', 'button[id="iSignupAction"]']:
            btn = page.locator(sel).first
            if await btn.count() > 0:
                await btn.click(timeout=3000)
                break
        await asyncio.sleep(3)
        await page.screenshot(path="screenshots/outlook_after_bday.png")

        # Step 4.5: 用户名/Gamertag（新版可能有这步）
        await asyncio.sleep(2)
        username_input = page.locator('input[id*="displayName"], input[id*="gamertag"], input[name*="displayName"], input[placeholder*="name"], input[type="text"]').first
        if await username_input.count() > 0:
            page_text = await page.evaluate("() => document.body.innerText")
            if "name" in page_text.lower() or "gamertag" in page_text.lower():
                username = prefix[:8] + str(random.randint(100, 999))
                await username_input.fill(username)
                print(f"  [outlook] username: {username}")
                await asyncio.sleep(0.5)
                for sel in ['input[type="submit"]', 'button[type="submit"]', '#iSignupAction', 'button[id="iSignupAction"]']:
                    btn = page.locator(sel).first
                    if await btn.count() > 0:
                        await btn.click(timeout=3000)
                        break
                await asyncio.sleep(3)
                await page.screenshot(path="screenshots/outlook_after_username.png")

        # Step 4.6: 输入姓名 + 勾选 + 确认
        first_name, last_name = generate_name()
        await asyncio.sleep(2)

        # 等待姓名输入框出现
        for _ in range(10):
            fname_input = page.locator('input[name="FirstName"], input[id="FirstName"], input[name="firstNameInput"], input[id="firstNameInput"], input[aria-label*="first" i], input[placeholder*="first" i]').first
            lname_input = page.locator('input[name="LastName"], input[id="LastName"], input[name="lastNameInput"], input[id="lastNameInput"], input[aria-label*="last" i], input[aria-label*="surname" i], input[placeholder*="last" i]').first
            if await fname_input.count() > 0 or await lname_input.count() > 0:
                break
            # 通用 text input
            all_text_inputs = page.locator('input[type="text"]')
            if await all_text_inputs.count() >= 2:
                break
            await asyncio.sleep(1)

        if await fname_input.count() > 0:
            # 先填姓再填名
            if await lname_input.count() > 0:
                await lname_input.fill(last_name)
                print(f"  [outlook] last name: {last_name}")
            await fname_input.fill(first_name)
            print(f"  [outlook] first name: {first_name}")
        else:
            all_text_inputs = page.locator('input[type="text"]')
            count = await all_text_inputs.count()
            if count >= 2:
                await all_text_inputs.nth(0).fill(last_name)
                await all_text_inputs.nth(1).fill(first_name)
                print(f"  [outlook] name (generic): {last_name} {first_name}")
            else:
                print("  [outlook] name inputs not found, skipping")

        # 打钩
        checkbox = page.locator('input[type="checkbox"], [role="checkbox"]').first
        if await checkbox.count() > 0:
            try:
                checked = await checkbox.is_checked()
            except Exception:
                checked = False
            if not checked:
                await checkbox.click(force=True)
                print("  [outlook] checkbox checked")
            await asyncio.sleep(0.5)

        # 点确认
        await asyncio.sleep(0.5)
        for sel in ['input[type="submit"]', 'button[type="submit"]', '#iSignupAction', 'button[id="iSignupAction"]', 'button:has-text("Next")', 'button:has-text("next")']:
            btn = page.locator(sel).first
            if await btn.count() > 0:
                await btn.click(timeout=3000)
                break
        await asyncio.sleep(3)
        await page.screenshot(path="screenshots/outlook_after_name.png")

        # Step 5: 人机验证 — 按压 / 点击 / CapSolver
        print("  [outlook] checking for captcha/challenge...")

        # 调试：检查页面验证状态
        await asyncio.sleep(3)
        try:
            debug_info = await page.evaluate("""() => {
                const info = {
                    url: location.href,
                    bodyText: document.body.innerText.substring(0, 300),
                    iframes: [],
                    hiddenInputs: [],
                    visibleBtns: [],
                };
                document.querySelectorAll('iframe').forEach(f => {
                    info.iframes.push({src: (f.src||'').substring(0,120), w: f.offsetWidth, h: f.offsetHeight, display: getComputedStyle(f).display});
                });
                document.querySelectorAll('input[type="hidden"]').forEach(el => {
                    if (el.name) info.hiddenInputs.push({name: el.name, val: (el.value||'').substring(0,60)});
                });
                document.querySelectorAll('button, input[type="submit"]').forEach(el => {
                    if (el.offsetParent !== null) info.visibleBtns.push({text: el.textContent.trim().substring(0,40), id: el.id});
                });
                return info;
            }""")
            print(f"  [debug] main page: {json.dumps(debug_info, ensure_ascii=False)[:800]}")

            # 检查每个 hsprotect iframe 的内容
            for fi, f in enumerate(page.frames):
                if 'hsprotect.net' in (f.url or ''):
                    try:
                        hs_info = await f.evaluate("""() => {
                            const pxc = document.getElementById('px-captcha');
                            const pxcRect = pxc ? pxc.getBoundingClientRect() : null;
                            const pxcStyle = pxc ? window.getComputedStyle(pxc) : null;
                            const innerIframes = document.querySelectorAll('iframe');
                            const iframeInfo = [];
                            innerIframes.forEach(f => {
                                iframeInfo.push({
                                    src: (f.src||'').substring(0,80),
                                    w: f.offsetWidth, h: f.offsetHeight,
                                    display: f.style ? f.style.display : 'n/a',
                                    token: (f.getAttribute('token')||'').substring(0,40),
                                });
                            });
                            return {
                                bodyHTML: document.body ? document.body.innerHTML.substring(0, 800) : 'no body',
                                allElements: document.querySelectorAll('*').length,
                                pxCaptcha: pxcRect ? {w: pxcRect.width, h: pxcRect.height, x: pxcRect.x, y: pxcRect.y} : null,
                                pxcDisplay: pxcStyle ? pxcStyle.display : 'no style',
                                pxcChildren: pxc ? pxc.children.length : 0,
                                pxcInnerHTML: pxc ? pxc.innerHTML.substring(0, 500) : '',
                                innerIframes: iframeInfo,
                            };
                        }""")
                        print(f"  [debug] hsprotect[{fi}]: px-captcha={hs_info['pxCaptcha']}, children={hs_info['pxcChildren']}, display={hs_info['pxcDisplay']}")
                        print(f"  [debug] hsprotect[{fi}] innerHTML: {hs_info['pxcInnerHTML'][:300]}")
                        print(f"  [debug] hsprotect[{fi}] inner iframes: {json.dumps(hs_info['innerIframes'], ensure_ascii=False)[:300]}")
                    except Exception as e:
                        print(f"  [debug] hsprotect[{fi}] eval error: {e}")
        except Exception as e:
            print(f"  [debug] error: {e}")

        arkose_solved = False
        press_count = 0
        max_press = 3  # 最多按压3次，过不了就放弃
        no_btn_rounds = 0  # 连续没找到按钮的轮数
        for wait_round in range(30):  # 最多等 90 秒
            try:
                page_text = (await page.evaluate("() => document.body.innerText")).lower()
                current_url = page.url.lower()
            except Exception:
                # 页面正在导航，等一下再检查
                await asyncio.sleep(3)
                try:
                    page_text = (await page.evaluate("() => document.body.innerText")).lower()
                    current_url = page.url.lower()
                except Exception:
                    # 可能验证通过了，页面跳转中
                    current_url = page.url.lower()
                    if "signup" not in current_url:
                        print(f"  [outlook] navigated away: {current_url[:80]}")
                        break
                    continue

            # 注册成功
            if "outlook" in current_url and "signup" not in current_url and "login" not in current_url:
                print("  [outlook] registration complete!")
                break
            if "welcome" in page_text or "inbox" in page_text or "account has been created" in page_text:
                print("  [outlook] registration complete!")
                break

            # 检查主页面是否还在 signup 流程中（可能已经跳过验证）
            if "signup" not in current_url and "live.com" in current_url:
                print(f"  [outlook] left signup page: {current_url}")
                break

            # FIDO/passkey 页面 — 跳过
            if "fido" in current_url or "passkey" in current_url:
                print(f"  [outlook] FIDO/passkey page, skipping...")
                try:
                    # 点 Skip / No thanks / Cancel
                    for sel in ['a:has-text("Skip")', 'button:has-text("Skip")', 'a:has-text("No thanks")',
                                'button:has-text("No thanks")', 'button:has-text("Cancel")', 'a[id*="skip"]',
                                'a[id*="cancel"]', '#cancelBtn', '#skipBtn']:
                        btn = page.locator(sel).first
                        if await btn.count() > 0:
                            await btn.click(timeout=3000)
                            print(f"  [outlook] clicked skip on FIDO page: {sel}")
                            break
                    else:
                        # 没找到按钮，试 JS
                        await page.evaluate("""() => {
                            const links = document.querySelectorAll('a, button');
                            for (const l of links) {
                                const t = l.textContent.toLowerCase();
                                if (t.includes('skip') || t.includes('no thanks') || t.includes('cancel') || t.includes('not now')) {
                                    l.click();
                                    return;
                                }
                            }
                        }""")
                        print(f"  [outlook] JS-clicked skip on FIDO page")
                except Exception as e:
                    print(f"  [outlook] FIDO skip error: {e}")
                await asyncio.sleep(3)
                continue

            # privacynotice 页面 — 点 OK/Accept 按钮
            if "privacynotice" in current_url:
                print(f"  [outlook] privacy notice page, clicking OK...")
                await asyncio.sleep(2)
                for label in ['OK', 'Accept', 'Continue', 'Next', 'I agree', 'Got it']:
                    btn = page.locator(f'button:has-text("{label}"), input[value="{label}"], a:has-text("{label}")').first
                    if await btn.count() > 0:
                        try:
                            await btn.click(timeout=3000)
                            print(f"  [outlook] clicked: {label}")
                            break
                        except Exception:
                            pass
                await asyncio.sleep(3)
                continue

            # 方法1: PerimeterX px-captcha — 模拟真人 press and hold
            if press_count < max_press:
                pressed = False

                # 打印 frame 信息（首次 + 每10轮）
                if wait_round == 0 or wait_round % 10 == 0:
                    frame_info = []
                    for i, f in enumerate(page.frames):
                        frame_info.append({"idx": i, "name": f.name, "url": f.url[:100]})
                    print(f"  [outlook] frames({wait_round}): {json.dumps(frame_info, ensure_ascii=False)[:600]}")

                # 找到可见的 hsprotect iframe
                target_box = None
                try:
                    hs_iframes = page.locator('iframe[src*="hsprotect.net"]')
                    hs_count = await hs_iframes.count()
                    for hi in range(hs_count):
                        iframe_el = hs_iframes.nth(hi)
                        box = await iframe_el.bounding_box()
                        if box and box['width'] > 50 and box['height'] > 30:
                            target_box = box
                            break
                except Exception:
                    pass

                # 也尝试通过 frame 内 #px-captcha 获取精确位置
                if not target_box:
                    try:
                        for f in page.frames:
                            if 'hsprotect.net' in (f.url or '') and 'ch_ctx' in (f.url or ''):
                                px = f.locator('#px-captcha')
                                if await px.count() > 0:
                                    target_box = await px.bounding_box()
                                    break
                    except Exception:
                        pass

                if target_box and target_box['width'] > 30 and target_box['height'] > 10:
                    press_count += 1
                    pressed = True

                    bx = target_box['x']
                    by = target_box['y']
                    bw = target_box['width']
                    bh = target_box['height']

                    # 按钮中心（加随机偏移）
                    cx = bx + bw * random.uniform(0.35, 0.65)
                    cy = by + bh * random.uniform(0.4, 0.7)

                    print(f"  [outlook] press #{press_count}: target=({bx:.0f},{by:.0f}) {bw:.0f}x{bh:.0f}, click=({cx:.0f},{cy:.0f})")

                    # 拟人按住(WindMouse 逼近 + OU 生理震颤)，取代旧的贝塞尔逼近 +
                    # ±2px 均匀抖动。旧抖动是白噪声(无动量)，PerimeterX 行为模型可判；
                    # 这里轨迹变速 + 自相关抖动更像真人手。is_done 用「hsprotect iframe
                    # 消失」判定：进度条走满即松手，未满按住到 max_hold 兜底。
                    async def _hold_done():
                        try:
                            b = await hs_iframes.first.bounding_box()
                            return (not b) or b['height'] < 10
                        except Exception:
                            return True   # iframe 取不到多半是已导航/消失

                    try:
                        held, _passed = await _hm.human_press_and_hold(
                            page, cx, cy, is_done=_hold_done,
                            max_hold=random.uniform(16.0, 22.0), min_hold=2.0,
                        )
                    except Exception as _he:
                        print(f"  [outlook] human_press_and_hold err, fallback: {_he}")
                        await page.mouse.down()
                        await asyncio.sleep(random.uniform(16, 22))
                        await page.mouse.up()
                        held = 18.0
                    print(f"  [outlook] mouse up, held {held:.1f}s")

                    # 等待验证结果
                    await asyncio.sleep(random.uniform(3, 6))

                    # 检查是否通过（页面可能已导航，需要 try/except）
                    try:
                        await page.screenshot(path=f"screenshots/outlook_after_hold_{press_count}.png")
                        new_text = (await page.evaluate("() => document.body.innerText")).lower()
                        new_url = page.url.lower()
                        if "press and hold" not in new_text or "signup" not in new_url:
                            print(f"  [outlook] captcha passed! url={new_url[:60]}")
                            no_btn_rounds = 0
                        else:
                            # 检查 iframe 是否变化
                            try:
                                new_box = await hs_iframes.first.bounding_box()
                                if not new_box or new_box['height'] < 10:
                                    print(f"  [outlook] iframe gone, captcha may have passed!")
                            except Exception:
                                pass
                            # 按压之间等待（递增，让验证重置）
                            wait = random.uniform(2, 5) + press_count * 2
                            await asyncio.sleep(wait)
                    except Exception as e:
                        # 页面导航导致 context destroyed — 验证可能已通过
                        print(f"  [outlook] post-press check error (likely navigation): {e}")
                        await asyncio.sleep(3)
                else:
                    no_btn_rounds += 1
                    try:
                        for f in page.frames:
                            if f == page.main_frame:
                                continue
                            frame_url = f.url.lower()
                            if frame_url == "about:blank" or "cfp.microsoft.com" in frame_url:
                                continue
                            try:
                                btns = f.locator('button, [role="button"], input[type="button"], input[type="submit"], div[class*="btn"], div[class*="button"]')
                                btn_count = await btns.count()
                                if btn_count > 0 and wait_round % 5 == 0:
                                    print(f"  [outlook] frame {f.url[:60]}: {btn_count} buttons")
                                for bi in range(btn_count):
                                    btn = btns.nth(bi)
                                    try:
                                        box = await btn.bounding_box()
                                    except Exception:
                                        continue
                                    if box and box['width'] > 30 and box['height'] > 20:
                                        x = box['x'] + box['width'] / 2
                                        y = box['y'] + box['height'] / 2
                                        press_count += 1
                                        print(f"  [outlook] btn press #{press_count} at ({x:.0f}, {y:.0f}), size={box['width']:.0f}x{box['height']:.0f}, frame={f.url[:80]}...")
                                        await page.mouse.move(x, y)
                                        await asyncio.sleep(0.3)
                                        await page.mouse.down()
                                        await asyncio.sleep(18)
                                        await page.mouse.up()
                                        pressed = True
                                        await asyncio.sleep(5)
                                        await page.screenshot(path=f"screenshots/outlook_after_hold_{press_count}.png")
                                        break
                                if pressed:
                                    break
                            except Exception:
                                continue
                    except Exception as e:
                        print(f"  [outlook] frame scan error: {e}")

                if pressed:
                    no_btn_rounds = 0
                else:
                    no_btn_rounds += 1

            # 方法2: 检查主页面上的验证元素（有时不在 iframe 里）
            if press_count < max_press and no_btn_rounds >= 3:
                try:
                    # 检查主页面是否有隐藏的验证按钮或 checkbox
                    main_btns = page.locator('#hipTemplateContainer button, #HipPaneForm button, [id*="hip"] button, [id*="captcha"] button')
                    main_count = await main_btns.count()
                    if main_count > 0:
                        print(f"  [outlook] found {main_count} buttons in main page captcha area")
                        for bi in range(main_count):
                            btn = main_btns.nth(bi)
                            box = await btn.bounding_box()
                            if box and box['width'] > 20 and box['height'] > 15:
                                press_count += 1
                                await btn.click(timeout=3000)
                                print(f"  [outlook] clicked main page captcha button #{press_count}")
                                await asyncio.sleep(5)
                                no_btn_rounds = 0
                                break
                except Exception as e:
                    if wait_round % 10 == 0:
                        print(f"  [outlook] main page captcha check error: {e}")

            # 方法3: 尝试直接提交（有时验证已通过但页面没跳转）
            if no_btn_rounds >= 8 and no_btn_rounds % 8 == 0:
                print(f"  [outlook] no buttons for {no_btn_rounds} rounds, trying submit...")
                try:
                    for sel in ['#iSignupAction', 'input[type="submit"]', 'button[type="submit"]']:
                        submit = page.locator(sel).first
                        if await submit.count() > 0 and await submit.is_visible():
                            await submit.click(timeout=3000)
                            print(f"  [outlook] clicked submit: {sel}")
                            await asyncio.sleep(5)
                            break
                except Exception:
                    pass

            # 按压用完了，尝试打码平台
            if press_count >= max_press and not arkose_solved:
                print("  [outlook] press attempts exhausted, trying captcha solvers...")
                await page.screenshot(path="screenshots/outlook_captcha_detected.png")

                # 尝试 EZ-Captcha (PerimeterX)
                px_solution = solve_perimeterx_ezcaptcha(page_url=page.url)
                if px_solution:
                    # PerimeterX solution 可能包含 cookie、token、uuid 等
                    # 尝试通过设置 cookie 或注入 token 来绕过
                    try:
                        # 方法1: 如果返回了 cookie，设置到浏览器
                        if isinstance(px_solution, dict):
                            for key in ['_pxCaptcha', '_px3', '_px2', '_pxhd', '_pxvid', '_pxde']:
                                if key in px_solution:
                                    await context.add_cookies([{
                                        "name": key,
                                        "value": str(px_solution[key]),
                                        "domain": ".live.com",
                                        "path": "/",
                                    }])
                                    print(f"  [px] set cookie: {key}={str(px_solution[key])[:40]}...")

                            # 方法2: 如果有 token，注入到页面
                            token_val = px_solution.get("token") or px_solution.get("uuid") or px_solution.get("captchaToken")
                            if token_val:
                                await inject_arkose_token(page, str(token_val))

                            # 方法3: 设置所有返回的 cookie
                            if "cookie" in px_solution:
                                cookie_str = px_solution["cookie"]
                                if isinstance(cookie_str, str):
                                    for part in cookie_str.split(";"):
                                        part = part.strip()
                                        if "=" in part:
                                            cname, cval = part.split("=", 1)
                                            await context.add_cookies([{
                                                "name": cname.strip(),
                                                "value": cval.strip(),
                                                "domain": ".live.com",
                                                "path": "/",
                                            }])
                                            print(f"  [px] set cookie from string: {cname.strip()}")

                        # 刷新页面让 cookie 生效
                        await page.reload(timeout=15000)
                        await asyncio.sleep(5)
                        arkose_solved = True
                        await page.screenshot(path="screenshots/outlook_captcha_solved.png")
                        print(f"  [px] solution applied, page reloaded")
                        continue
                    except Exception as e:
                        print(f"  [px] inject error: {e}")

                # 尝试 EZ-Captcha (FunCaptcha)
                if not arkose_solved:
                    token = solve_funcaptcha_ezcaptcha(page_url=page.url)
                    if token:
                        injected = await inject_arkose_token(page, token)
                        if injected:
                            arkose_solved = True
                            await asyncio.sleep(5)
                            await page.screenshot(path="screenshots/outlook_captcha_solved.png")
                            continue

                # 尝试 CapSolver
                if not arkose_solved:
                    token = solve_arkose_capsolver(page_url=page.url)
                    if token:
                        injected = await inject_arkose_token(page, token)
                        if injected:
                            arkose_solved = True
                            await asyncio.sleep(5)
                            await page.screenshot(path="screenshots/outlook_captcha_solved.png")
                            continue

                arkose_solved = True  # 避免重复调用

            if wait_round % 5 == 0:
                await page.screenshot(path=f"screenshots/outlook_wait_{wait_round}.png")
                print(f"  [outlook] waiting... ({wait_round*3}s) no_btn={no_btn_rounds}")

            await asyncio.sleep(3)
        else:
            print("  [outlook] captcha/challenge timeout")
            await page.screenshot(path="screenshots/outlook_timeout.png")
            return None, None

        # 验证通过后，处理可能出现的 privacy notice / 确认页面
        for retry in range(10):
            current_url = page.url.lower()
            if "privacynotice" not in current_url and "signup" not in current_url:
                break
            print(f"  [outlook] post-captcha page ({retry+1}): {page.url[:80]}")
            await page.screenshot(path=f"screenshots/outlook_postcaptcha_{retry}.png")
            # 尝试点各种按钮
            clicked_any = False
            for label in ['OK', 'Accept', 'Continue', 'Next', 'I agree', 'Got it', 'Agree']:
                btn = page.locator(f'button:has-text("{label}"), input[value="{label}"], a:has-text("{label}")').first
                if await btn.count() > 0:
                    try:
                        await btn.click(timeout=3000)
                        print(f"  [outlook] clicked: {label}")
                        clicked_any = True
                        break
                    except Exception:
                        pass
            if not clicked_any:
                # 没找到按钮，试 JS 点击页面上所有可见按钮
                try:
                    await page.evaluate("""
                        () => {
                            const btns = document.querySelectorAll('button, input[type="submit"], a.btn, a.button');
                            for (const b of btns) {
                                if (b.offsetParent !== null) { b.click(); return; }
                            }
                        }
                    """)
                    print("  [outlook] JS-clicked first visible button")
                except Exception:
                    pass
            await asyncio.sleep(3)
            # 等页面跳转
            try:
                await page.wait_for_load_state("load", timeout=5000)
            except Exception:
                pass

        if not verify_registered_outlook(email, password):
            print("  [outlook] verification failed, discarding self-registered email")
            return None, None

        print(f"  [outlook] ok: {email} / {password}")
        return email, password

    except Exception as e:
        print(f"  [outlook] failed: {e}")
        try:
            await page.screenshot(path="screenshots/outlook_error.png")
        except Exception:
            pass
        return None, None

async def _replit_click_email_submit(page, tag):
    """Find and click the email-form submit button, skipping OAuth buttons."""
    btns_info = await page.evaluate("""
        () => Array.from(document.querySelectorAll('button, [role="button"]'))
            .filter(b => b.offsetParent !== null)
            .map(b => ({text: b.textContent.trim().substring(0, 60), type: b.type || '', id: b.id || ''}))
    """)
    print(f"  {tag} [replit] buttons: {btns_info}")

    OAUTH_KEYWORDS = ['google', 'github', 'apple', 'facebook', 'twitter', 'discord', 'with x', 'sign-on', 'sso', 'log in']

    for sel in [
        'button:has-text("Create account")',
        'button:has-text("Create Account")',
        'button:has-text("Sign up")',
        'button:has-text("Sign Up")',
        'button:has-text("Register")',
        'button:has-text("Get started")',
        'button:has-text("Continue with email")',
    ]:
        btn = page.locator(sel).first
        if await btn.count() > 0:
            txt = (await btn.text_content() or "").lower()
            if not any(kw in txt for kw in OAUTH_KEYWORDS):
                await btn.click()
                print(f"  {tag} [replit] clicked: {sel}")
                return True

    btns = page.locator('button[type="submit"], button')
    for i in range(await btns.count()):
        btn = btns.nth(i)
        if not await btn.is_visible():
            continue
        txt = (await btn.text_content() or "").lower()
        if any(kw in txt for kw in OAUTH_KEYWORDS):
            continue
        if txt.strip() == '':
            continue
        await btn.click()
        print(f"  {tag} [replit] clicked submit (fallback): '{txt[:40]}'")
        return True

    print(f"  {tag} [replit] no suitable submit button found")
    return False


async def _get_replit_verify_link_pw(context, email, password, max_wait=90):
    """Open Outlook inbox in a new page and find Replit verification email link."""
    page = await context.new_page()
    try:
        await page.goto("https://login.live.com/", timeout=60000)
        await asyncio.sleep(3)
        email_input = page.locator('input[type="email"], input[name="loginfmt"]').first
        if await email_input.count() > 0:
            await email_input.fill(email)
            await page.keyboard.press("Enter")
            await asyncio.sleep(3)
        pwd_input = page.locator('input[type="password"], input[name="passwd"]').first
        if await pwd_input.count() > 0:
            await pwd_input.fill(password)
            await page.keyboard.press("Enter")
            await asyncio.sleep(5)
        for label in ["Yes", "是"]:
            btn = page.locator(f'button:has-text("{label}"), input[value="{label}"]').first
            if await btn.count() > 0:
                try:
                    await btn.click(timeout=3000)
                    await asyncio.sleep(2)
                except Exception:
                    pass
        await page.goto("https://outlook.live.com/mail/0/inbox", timeout=60000)
        await asyncio.sleep(5)
        start = time.time()
        while time.time() - start < max_wait:
            found = await page.evaluate("""
                () => {
                    const items = document.querySelectorAll('[role="listitem"], [role="option"]');
                    for (const item of items) {
                        const text = (item.textContent || '').toLowerCase();
                        if (text.includes('replit') || text.includes('verify') || text.includes('confirm your')) {
                            item.click();
                            return 'found';
                        }
                    }
                    return null;
                }
            """)
            if found == 'found':
                await asyncio.sleep(3)
                link = await page.evaluate("""
                    () => {
                        for (const a of document.querySelectorAll('a')) {
                            const href = a.href || '';
                            if (href.includes('replit.com') && (
                                href.includes('verify') || href.includes('confirm') ||
                                href.includes('token') || href.includes('code') || href.includes('activate')
                            )) return href;
                            if (href.includes('safelinks') && href.toLowerCase().includes('replit')) {
                                try {
                                    const u = new URL(href);
                                    const orig = u.searchParams.get('url');
                                    if (orig && orig.includes('replit')) return orig;
                                } catch(e) {}
                            }
                        }
                        return null;
                    }
                """)
                if link:
                    return link
            elapsed = int(time.time() - start)
            print(f"  [replit-mail] waiting... ({elapsed}s/{max_wait}s)")
            await asyncio.sleep(5)
            try:
                refresh_btn = page.locator('[aria-label*="Refresh"], [title*="Refresh"]').first
                if await refresh_btn.count() > 0:
                    await refresh_btn.click()
                    await asyncio.sleep(2)
                else:
                    await page.goto("https://outlook.live.com/mail/0/inbox", timeout=30000)
                    await asyncio.sleep(3)
            except Exception:
                pass
    except Exception as e:
        print(f"  [replit-mail] error: {e}")
    finally:
        try:
            await page.close()
        except Exception:
            pass
    return None


async def register_replit(context, email, email_password, email_token="", tag=""):
    """Register a Replit account with the given email.
    Returns (username, replit_password) on success, None on failure.
    """
    email_prefix = email.split("@")[0]
    base = re.sub(r'[^a-zA-Z0-9]', '', email_prefix)[:12] or "user"
    username = (base + ''.join(random.choices(string.digits, k=4)))[:20]
    replit_pwd = "Rp1!" + ''.join(random.choices(string.ascii_letters + string.digits, k=14))

    print(f"\n  {tag} [replit] registering @{username} ({email})")
    page = await context.new_page()
    try:
        await page.goto("https://replit.com/signup", timeout=60000)
        await asyncio.sleep(3)
        print(f"  {tag} [replit] url: {page.url}")

        uname_sel = 'input[name="username"], input[placeholder*="username" i], input[id*="username" i]'
        email_sel = 'input[type="email"], input[name="email"], input[placeholder*="email" i]'
        pwd_sel   = 'input[type="password"], input[name="password"]'

        # Click "Email & password" to reveal email form
        for sel in [
            'button:has-text("Email & password")',
            'button:has-text("Email")',
            'button:has-text("Continue with email")',
            'a:has-text("Continue with email")',
        ]:
            btn = page.locator(sel).first
            if await btn.count() > 0:
                await btn.click()
                print(f"  {tag} [replit] clicked '{sel}'")
                await asyncio.sleep(2)
                break

        # Fill fields
        if await page.locator(uname_sel).count() > 0:
            await page.locator(uname_sel).first.fill(username)
            print(f"  {tag} [replit] username: {username}")
            await asyncio.sleep(0.5)
        if await page.locator(email_sel).count() > 0:
            await page.locator(email_sel).first.fill(email)
            print(f"  {tag} [replit] email: {email}")
            await asyncio.sleep(0.5)
        if await page.locator(pwd_sel).count() > 0:
            await page.locator(pwd_sel).first.fill(replit_pwd)
            print(f"  {tag} [replit] password filled")
            await asyncio.sleep(0.3)

        await _replit_click_email_submit(page, tag)
        await asyncio.sleep(5)
        print(f"  {tag} [replit] after submit url: {page.url}")

        # Handle multi-step form
        for _step in range(3):
            if '__/auth' in page.url:
                print(f"  {tag} [replit] unexpected OAuth redirect, aborting")
                return None
            filled = False
            for sel, val in [(uname_sel, username), (email_sel, email), (pwd_sel, replit_pwd)]:
                if await page.locator(sel).count() > 0:
                    cur = await page.locator(sel).first.input_value()
                    if not cur:
                        await page.locator(sel).first.fill(val)
                        filled = True
                        await asyncio.sleep(0.3)
            if not filled:
                break
            await _replit_click_email_submit(page, tag)
            await asyncio.sleep(4)

        # Email verification
        page_text = (await page.evaluate("() => document.body.innerText")).lower()
        if any(kw in page_text for kw in ['verify your email', 'check your email', 'confirm your email', 'sent you an email']):
            print(f"  {tag} [replit] email verification required...")
            verify_link = None
            if email_token:
                verify_link = get_magic_link_by_token(email, email_token, max_wait=60)
            if not verify_link:
                verify_link = await _get_replit_verify_link_pw(context, email, email_password, max_wait=90)
            if verify_link:
                print(f"  {tag} [replit] verify link: {verify_link[:80]}...")
                await page.goto(verify_link, timeout=30000)
                await asyncio.sleep(3)

        # Save to replit_accounts.txt
        os.makedirs(COOKIE_OUTPUT_DIR, exist_ok=True)
        replit_file = os.path.join(COOKIE_OUTPUT_DIR, "replit_accounts.txt")
        with open(replit_file, "a", encoding="utf-8") as f:
            f.write(f"{email}----{username}----{replit_pwd}\n")
        print(f"  {tag} [replit] saved: @{username}")
        return username, replit_pwd

    except Exception as e:
        print(f"  {tag} [replit] error: {e}")
        return None
    finally:
        try:
            await page.close()
        except Exception:
            pass


def get_magic_link_by_token(
    email,
    refresh_token,
    client_id="9e5f94bc-e8a4-4e73-b8be-63364c29d753",
    max_wait=90,
    account_lease=None,
):
    from common.mailbox import get_link_by_token

    return get_link_by_token(
        email,
        refresh_token,
        client_id=client_id,
        link_regex=r"https://claude\.ai/magic-link#[A-Za-z0-9_\-:=+/]+",
        sender_contains=("anthropic", "claude"),
        subject_contains=("magic", "verify", "sign in", "login"),
        must_contain="claude.ai/magic-link#",
        max_wait=max_wait,
        poll=5,
        account_lease=account_lease,
    )


def build_ninemail_client():
    return NineMallMailboxClient(
        base_url=NINEMALL_API_BASE,
        api_password=NINEMALL_API_PASSWORD,
        http_timeout=NINEMALL_HTTP_TIMEOUT,
        poll_interval=NINEMALL_POLL_INTERVAL,
    )


async def fetch_claude_magic_link(
    context,
    account,
    max_wait,
    received_after=None,
    account_lease=None,
    ninemail_client=None,
):
    if account.provider == "NINEMALL":
        client = ninemail_client or build_ninemail_client()
        return await asyncio.to_thread(
            client.poll_magic_link,
            account,
            max_wait,
            received_after,
        )
    link = None
    if account.refresh_token:
        fetch_token_link = functools.partial(
            get_magic_link_by_token,
            account.email,
            account.refresh_token,
            client_id=account.client_id or "9e5f94bc-e8a4-4e73-b8be-63364c29d753",
            max_wait=max_wait,
            account_lease=account_lease,
        )
        link = await asyncio.to_thread(fetch_token_link)
    if link:
        return link
    outlook_page = await context.new_page()
    try:
        return await get_magic_link_outlook_pw(
            outlook_page, account.email, account.password, max_wait=max_wait
        )
    finally:
        await outlook_page.close()


async def navigate_to_claude_magic_link(page, magic_link, timeout):
    try:
        await page.goto(magic_link, timeout=timeout)
    except Exception:
        try:
            await page.evaluate(
                "(url) => { window.location.href = url; }",
                magic_link,
            )
            await page.wait_for_load_state("domcontentloaded", timeout=timeout)
        except Exception:
            raise RuntimeError("magic_link_navigation_failed") from None


def log_safe_page_origin(label, page_url):
    try:
        parsed = urlsplit(str(page_url or ""))
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        if scheme not in {"http", "https"} or not hostname:
            raise ValueError
        if ":" in hostname:
            hostname = f"[{hostname}]"
        origin = f"{scheme}://{hostname}"
    except (AttributeError, TypeError, ValueError):
        origin = "[redacted]"
    print(f"{label}{origin}")


# ---- 多语言按钮匹配（BitBrowser 节点地区不同，Claude 登录界面语言可能是 英/日/中/繁/韩/西/法/德）----
CONTINUE_EMAIL_LABELS = [
    # 具体"用邮箱继续"优先，避免误点 Continue with Google/Apple
    "Continue with email", "Continue with Email",
    "メールで続行", "メールアドレスで続行", "メールで登録", "電子メールで続行",
    "使用邮箱继续", "用邮箱继续", "通过电子邮件继续", "使用电子邮件继续", "邮箱继续",
    "使用電子郵件繼續", "用電子郵件繼續",
    "이메일로 계속", "이메일로 계속하기",
    "Continuar con correo electrónico", "Continuer avec l'adresse e-mail", "Mit E-Mail fortfahren",
    # 泛化"继续/下一步"兜底
    "Continue", "続行", "次へ", "继续", "下一步", "繼續", "계속", "Continuar", "Continuer", "Weiter",
]


async def click_continue_email(page, timeout=8000):
    """点击 Claude 的'用邮箱继续/Continue'按钮，多语言精确匹配；命中即 True。"""
    for label in CONTINUE_EMAIL_LABELS:
        try:
            btn = page.get_by_role("button", name=label, exact=True)
            if await btn.count() > 0:
                await btn.last.click(timeout=timeout)
                print(f"  clicked continue: {label}")
                return True
        except Exception:
            pass
    # 退化：含 email/メール/邮箱 字样的按钮，或 submit
    try:
        cand = page.locator(
            'button:has-text("email"), button:has-text("Email"), '
            'button:has-text("メール"), button:has-text("邮箱"), button:has-text("電子郵件"), '
            'button[type="submit"]'
        ).last
        if await cand.count() > 0:
            await cand.click(timeout=timeout)
            print("  clicked continue (fallback submit)")
            return True
    except Exception:
        pass
    return False


async def get_magic_link_outlook_pw(page, email, password, max_wait=90):
    """Get magic link from outlook inbox using Playwright browser login"""
    if os.environ.get("MAILBOX_BROKER"):
        # broker 模式：委托共享取码服务取 magic link，不在本浏览器开 Outlook 标签
        from common.mailbox import fetch_from_broker
        return await fetch_from_broker(
            email, password,
            ("anthropic", "claude", "noreply", "no-reply"),
            ("magic", "verify", "sign in", "login", "验证"),
            r"", "link", max_wait,
        )
    try:
        await page.goto("https://login.live.com/", timeout=60000)
        await asyncio.sleep(3)

        # 输入邮箱
        email_input = page.locator('input[type="email"], input[name="loginfmt"]').first
        if await email_input.count() > 0:
            await email_input.fill(email)
            await asyncio.sleep(0.5)
            await page.keyboard.press("Enter")
            await asyncio.sleep(3)

        # 输入密码
        pwd_input = page.locator('input[type="password"], input[name="passwd"]').first
        if await pwd_input.count() > 0:
            await pwd_input.fill(password)
            await asyncio.sleep(0.5)
            await page.keyboard.press("Enter")
            await asyncio.sleep(5)

        # 可能有 "Stay signed in?" 提示，点 Yes 保持登录
        for label in ["Yes", "是"]:
            btn = page.locator(f'button:has-text("{label}"), input[value="{label}"]').first
            if await btn.count() > 0:
                try:
                    await btn.click(timeout=3000)
                    await asyncio.sleep(2)
                except Exception:
                    pass

        # 跳转到 outlook 收件箱
        await page.goto("https://outlook.live.com/mail/0/inbox", timeout=60000)
        await asyncio.sleep(5)

        # 在当前打开的文件夹里查找并提取 magic link
        async def _find_in_current_folder():
            # 查找 Anthropic/Claude 邮件并点开
            found = await page.evaluate("""
                () => {
                    const items = document.querySelectorAll('[role="listitem"], [role="option"], [aria-label*="Anthropic"], [aria-label*="Claude"], [aria-label*="anthropic"]');
                    for (const item of items) {
                        const text = (item.textContent || '').toLowerCase();
                        if (text.includes('anthropic') || text.includes('claude') || text.includes('magic') || text.includes('verification')) {
                            item.click();
                            return 'found';
                        }
                    }
                    return null;
                }
            """)
            if found != 'found':
                return None
            await asyncio.sleep(3)
            # 提取 magic link（兼容 Outlook SafeLinks 包装）
            return await page.evaluate("""
                () => {
                    const allLinks = document.querySelectorAll('a');
                    for (const a of allLinks) {
                        const href = (a.href || '');
                        const hrefLower = href.toLowerCase();
                        if (hrefLower.includes('claude.ai/magic-link')) return href;
                        if (hrefLower.includes('safelinks') && hrefLower.includes('claude')) {
                            try {
                                const url = new URL(href);
                                const original = url.searchParams.get('url');
                                if (original) return original;
                            } catch(e) {}
                            return href;
                        }
                    }
                    const body = document.body.innerHTML;
                    const m = body.match(/https:\\/\\/claude\\.ai\\/magic-link[^"'<\\s]+/);
                    return m ? m[0] : null;
                }
            """)

        # magic link 邮件经常被判进垃圾箱(Junk Email)，inbox + junk 两个文件夹都扫
        folders = [
            ("inbox", "https://outlook.live.com/mail/0/inbox"),
            ("junk", "https://outlook.live.com/mail/0/junkemail"),
        ]
        start = time.time()
        while time.time() - start < max_wait:
            for fname, furl in folders:
                # 切到目标文件夹(首轮 inbox 已打开，跳过重复跳转)
                if not (fname == "inbox" and time.time() - start < 6):
                    try:
                        await page.goto(furl, timeout=30000)
                        await asyncio.sleep(3)
                    except Exception:
                        continue
                link = await _find_in_current_folder()
                if link:
                    print(f"  magic link found ({fname})")
                    return link

            elapsed = int(time.time() - start)
            print(f"  waiting for email (inbox+junk)... ({elapsed}s/{max_wait}s)")
            await asyncio.sleep(5)

        return None
    except Exception as e:
        print(f"  [outlook] login/read failed: {e}")
        return None


# ========== SMS API ==========

def get_phone_number(max_retries=5):
    """Get phone number, try preferred countries first, skip blacklisted.
    Falls back to hero-sms if firefox.fun has no numbers."""
    # 先试 firefox.fun
    for country in SMS_COUNTRY_PREFER:
        attempts = max_retries if country == "" else 1
        for attempt in range(attempts):
            params = {
                "act": "getPhone", "token": SMS_TOKEN, "iid": SMS_PROJECT_ID,
                "did": "", "country": country, "dock": "", "otpmode": "",
                "maxPrice": "0", "mobile": "", "pushUrl": "",
            }
            resp = requests.get(SMS_API_BASE, params=params, timeout=30)
            text = resp.text.strip()
            print(f"  [sms] api(country={country or 'any'}, try={attempt+1}): {text}")
            parts = text.split("|")
            if parts[0] == "1" and len(parts) >= 8:
                pkey = parts[1]
                country_code = parts[4]
                phone = parts[7]
                if country_code in SMS_COUNTRY_BLACKLIST:
                    print(f"  [sms] +{country_code} blacklisted, releasing and retrying...")
                    release_phone(pkey)
                    time.sleep(1)
                    continue
                print(f"  [sms] phone: +{country_code}{phone} (pkey: {pkey})")
                return phone, country_code, pkey
            else:
                print(f"  [sms] country={country or 'any'} no number")
                break

    # firefox.fun 没号，试 hero-sms
    print("  [sms] firefox.fun exhausted, trying hero-sms...")
    result = hero_get_phone_number()
    if result:
        full_phone, pkey = result
        # hero-sms 返回完整号码含国家码，需要拆分
        # 返回 full_phone 作为 phone，空字符串作为 country_code（号码已含国家码）
        return full_phone, "", pkey

    raise Exception("get phone failed: all platforms exhausted")


def get_sms_code(pkey, max_wait=120, interval=5):
    """Get SMS code - handles both platforms"""
    if str(pkey).startswith("hero_"):
        return hero_get_sms_code(pkey, max_wait, interval)

    params = {"act": "getPhoneCode", "token": SMS_TOKEN, "pkey": pkey}
    start = time.time()
    while time.time() - start < max_wait:
        resp = requests.get(SMS_API_BASE, params=params, timeout=30)
        text = resp.text.strip()
        parts = text.split("|")
        if parts[0] == "1" and len(parts) >= 2:
            code = parts[1]
            sms_content = parts[2] if len(parts) >= 3 else ""
            print(f"  [sms] code: {code}")
            if sms_content:
                print(f"  [sms] msg: {sms_content[:80]}")
            return code
        elapsed = int(time.time() - start)
        print(f"  waiting sms... ({elapsed}s/{max_wait}s)")
        time.sleep(interval)
    return None


def release_phone(pkey):
    """Release phone - handles both platforms by pkey prefix"""
    if str(pkey).startswith("hero_"):
        hero_release_phone(pkey)
        return
    params = {"act": "cancelPhone", "token": SMS_TOKEN, "pkey": pkey}
    try:
        resp = requests.get(SMS_API_BASE, params=params, timeout=10)
        print(f"  [sms] released: {resp.text.strip()}")
    except Exception:
        pass


# ========== Hero-SMS (backup) ==========

def hero_get_phone_number():
    """Get phone from hero-sms.com, sorted by cheapest price first"""
    # 先查价格，按便宜+库存多排序
    try:
        r = requests.get(HERO_SMS_API_BASE, params={
            "api_key": HERO_SMS_API_KEY,
            "action": "getPrices",
            "service": HERO_SMS_SERVICE,
        }, timeout=15)
        prices = r.json()
        # (cost, -count, country_id)
        ranked = []
        for cid, svc in prices.items():
            info = svc.get(HERO_SMS_SERVICE, {})
            cost = info.get("cost", 999)
            count = info.get("count", 0)
            if count > 0 and cost < 1.0:  # 跳过太贵的
                ranked.append((cost, -count, int(cid)))
        ranked.sort()
        countries = [c for _, _, c in ranked]
        print(f"  [hero-sms] {len(countries)} countries sorted by price (cheapest: ${ranked[0][0]} id={ranked[0][2]})")
    except Exception as e:
        print(f"  [hero-sms] getPrices failed: {e}, using default order")
        countries = HERO_SMS_COUNTRY_PREFER

    for country in countries:
        try:
            r = requests.get(HERO_SMS_API_BASE, params={
                "api_key": HERO_SMS_API_KEY,
                "action": "getNumber",
                "service": HERO_SMS_SERVICE,
                "country": country,
            }, timeout=30)
            text = r.text.strip()
            if text.startswith("ACCESS_NUMBER:"):
                # ACCESS_NUMBER:id:phone
                parts = text.split(":")
                act_id = parts[1]
                full_phone = parts[2]
                # hero-sms返回的号码已包含国家码
                pkey = f"hero_{act_id}"
                print(f"  [hero-sms] country={country}: +{full_phone} (id: {act_id})")
                return full_phone, pkey
            # NO_NUMBERS 等，继续试下一个国家
        except Exception as e:
            print(f"  [hero-sms] error country={country}: {e}")
    return None


def hero_get_sms_code(pkey, max_wait=120, interval=5):
    """Get SMS code from hero-sms"""
    act_id = str(pkey).replace("hero_", "")
    # 先通知平台我们已发送短信
    try:
        requests.get(HERO_SMS_API_BASE, params={
            "api_key": HERO_SMS_API_KEY,
            "action": "setStatus",
            "id": act_id,
            "status": 1,  # 通知已发送短信
        }, timeout=10)
    except Exception:
        pass

    start = time.time()
    while time.time() - start < max_wait:
        try:
            r = requests.get(HERO_SMS_API_BASE, params={
                "api_key": HERO_SMS_API_KEY,
                "action": "getStatus",
                "id": act_id,
            }, timeout=30)
            text = r.text.strip()
            if text.startswith("STATUS_OK:"):
                code = text.split(":")[1]
                print(f"  [hero-sms] code: {code}")
                # 提取纯数字验证码
                digits = re.search(r'\d{4,8}', code)
                if digits:
                    return digits.group(0)
                return code
            elif text == "STATUS_WAIT_CODE":
                elapsed = int(time.time() - start)
                print(f"  [hero-sms] waiting sms... ({elapsed}s/{max_wait}s)")
            elif text == "STATUS_CANCEL":
                print(f"  [hero-sms] cancelled")
                return None
            else:
                print(f"  [hero-sms] status: {text}")
        except Exception as e:
            print(f"  [hero-sms] error: {e}")
        time.sleep(interval)
    return None


def hero_release_phone(pkey):
    """Cancel/release phone on hero-sms"""
    act_id = str(pkey).replace("hero_", "")
    try:
        r = requests.get(HERO_SMS_API_BASE, params={
            "api_key": HERO_SMS_API_KEY,
            "action": "setStatus",
            "id": act_id,
            "status": 8,  # cancel
        }, timeout=10)
        print(f"  [hero-sms] release: {r.text.strip()}")
    except Exception:
        pass


# ========== helpers ==========

def generate_prefix(length=10):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))


def generate_birthday():
    current_year = datetime.now().year
    year = random.randint(current_year - 40, current_year - 25)
    month = random.randint(1, 12)
    if month in (1, 3, 5, 7, 8, 10, 12):
        max_day = 31
    elif month in (4, 6, 9, 11):
        max_day = 30
    else:
        max_day = 28
    day = random.randint(1, max_day)
    return year, month, day


def generate_name():
    first_names = [
        "James", "John", "Robert", "Michael", "David", "William", "Richard", "Joseph",
        "Thomas", "Charles", "Mary", "Patricia", "Jennifer", "Linda", "Barbara",
        "Elizabeth", "Susan", "Jessica", "Sarah", "Karen", "Emily", "Emma", "Olivia",
        "Daniel", "Matthew", "Anthony", "Mark", "Steven", "Andrew", "Brian",
    ]
    last_names = [
        "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
        "Davis", "Rodriguez", "Martinez", "Anderson", "Taylor", "Thomas", "Moore",
        "Jackson", "Martin", "Lee", "Thompson", "White", "Harris", "Clark",
    ]
    return random.choice(first_names), random.choice(last_names)


async def human_type(page, selector, text, delay=50):
    el = page.locator(selector).first
    await el.click()
    await asyncio.sleep(0.3)
    await page.keyboard.press("Control+a")
    await page.keyboard.press("Backspace")
    for char in text:
        await page.keyboard.type(char, delay=delay)
    await asyncio.sleep(0.3)


async def solve_turnstile(page, max_wait=60):
    """
    Detect and click Cloudflare Turnstile on the current page.
    Turnstile is embedded as an iframe from challenges.cloudflare.com.
    We wait up to 15s for it to appear, then try multiple click methods.
    """
    print("  [cf] scanning for Turnstile...")

    # phase 1: wait for Turnstile to appear (up to 15 seconds)
    DETECT_WAIT = 15
    cf_found = False
    for wait_i in range(DETECT_WAIT):
        for frame in page.frames:
            furl = frame.url or ''
            if 'challenges.cloudflare.com' in furl or 'cloudflare' in furl.lower():
                cf_found = True
                break
        if cf_found:
            break
        dom_check = await page.evaluate("""
            () => {
                if (document.querySelector('.cf-turnstile, [data-sitekey], [id*="turnstile"], [class*="turnstile"]'))
                    return true;
                const iframes = document.querySelectorAll('iframe');
                for (const f of iframes) {
                    const src = (f.src || '').toLowerCase();
                    if (src.includes('cloudflare') || src.includes('turnstile') || src.includes('challenges'))
                        return true;
                }
                return false;
            }
        """)
        if dom_check:
            cf_found = True
            break
        if wait_i % 3 == 0:
            print(f"  [cf] waiting for Turnstile... ({wait_i}s)")
        await asyncio.sleep(1)

    if not cf_found:
        print("  [cf] no Turnstile detected after 15s, skipping")
        return True

    print("  [cf] Turnstile found! attempting to solve...")
    await asyncio.sleep(2)

    iframe_info = await page.evaluate("""
        () => {
            const result = [];
            document.querySelectorAll('iframe').forEach(f => {
                result.push({src: f.src || '', width: f.offsetWidth, height: f.offsetHeight,
                             id: f.id || '', cls: (f.className||'').substring(0,80)});
            });
            return result;
        }
    """)
    print(f"  [cf] iframes on page: {len(iframe_info)}")
    for fi in iframe_info:
        print(f"    src={fi['src'][:80]} size={fi['width']}x{fi['height']} id={fi['id']}")

    # phase 2: try clicking (multiple rounds)
    clicked = False
    for round_i in range(max_wait // 3):
        if not clicked:
            cf_frame = None
            for frame in page.frames:
                furl = frame.url or ''
                if 'challenges.cloudflare.com' in furl or 'cloudflare' in furl.lower():
                    cf_frame = frame
                    break

            # method 1: click checkbox/label inside CF iframe
            if cf_frame:
                try:
                    cb = cf_frame.locator('input[type="checkbox"], label, .ctp-checkbox-label, .mark, #challenge-stage')
                    if await cb.count() > 0:
                        await cb.first.click()
                        print("  [cf] method1: clicked checkbox in iframe")
                        clicked = True
                except Exception as e:
                    print(f"  [cf] method1 failed: {e}")

            # method 2: click CF iframe body center
            if not clicked and cf_frame:
                try:
                    body = cf_frame.locator('body')
                    box = await body.bounding_box()
                    if box and box['width'] > 0:
                        cx = box['width'] / 2
                        cy = box['height'] / 2
                        await cf_frame.click('body', position={"x": cx, "y": cy})
                        print(f"  [cf] method2: clicked iframe body ({cx:.0f},{cy:.0f})")
                        clicked = True
                except Exception as e:
                    print(f"  [cf] method2 failed: {e}")

            # method 3: click iframe element from parent page coords
            if not clicked:
                try:
                    cf_iframe_el = page.locator(
                        'iframe[src*="challenges.cloudflare.com"], '
                        'iframe[src*="turnstile"], '
                        'iframe[src*="cloudflare"]'
                    )
                    if await cf_iframe_el.count() > 0:
                        box = await cf_iframe_el.first.bounding_box()
                        if box and box['width'] > 0:
                            await page.mouse.click(box["x"] + 30, box["y"] + box["height"] / 2)
                            print(f"  [cf] method3: clicked iframe coords ({box['x']+30:.0f},{box['y']+box['height']/2:.0f})")
                            clicked = True
                except Exception as e:
                    print(f"  [cf] method3 failed: {e}")

            # method 4: click .cf-turnstile or [data-sitekey] container
            if not clicked:
                try:
                    cf_div = page.locator('.cf-turnstile, [data-sitekey], [id*="turnstile"], [class*="turnstile"]')
                    if await cf_div.count() > 0:
                        box = await cf_div.first.bounding_box()
                        if box and box['width'] > 0:
                            await page.mouse.click(box["x"] + 30, box["y"] + box["height"] / 2)
                            print(f"  [cf] method4: clicked turnstile container")
                            clicked = True
                except Exception as e:
                    print(f"  [cf] method4 failed: {e}")

            # method 5: find ANY iframe with cloudflare src
            if not clicked:
                try:
                    all_iframes = page.locator('iframe')
                    count = await all_iframes.count()
                    for idx in range(count):
                        iframe = all_iframes.nth(idx)
                        src = (await iframe.get_attribute('src') or '').lower()
                        if 'cloudflare' in src or 'turnstile' in src or 'challenge' in src:
                            box = await iframe.bounding_box()
                            if box and box['width'] > 0:
                                await page.mouse.click(box["x"] + 30, box["y"] + box["height"] / 2)
                                print(f"  [cf] method5: clicked iframe#{idx} by src match")
                                clicked = True
                                break
                except Exception as e:
                    print(f"  [cf] method5 failed: {e}")

        if clicked:
            await asyncio.sleep(3)
            cf_done = await page.evaluate("""
                () => {
                    const input = document.querySelector('[name="cf-turnstile-response"]');
                    if (input && input.value) return 'token';
                    const hidden = document.querySelector('input[name="turnstile-token"], input[data-turnstile]');
                    if (hidden && hidden.value) return 'hidden';
                    return '';
                }
            """)
            if cf_done:
                print(f"  [cf] Turnstile passed! (verified: {cf_done})")
                return True

            still_has_cf = False
            for frame in page.frames:
                if 'cloudflare' in (frame.url or '').lower():
                    still_has_cf = True
                    break
            if not still_has_cf:
                dom_still = await page.evaluate("""
                    () => {
                        const iframes = document.querySelectorAll('iframe');
                        for (const f of iframes) {
                            if ((f.src||'').toLowerCase().includes('cloudflare')) return true;
                        }
                        return false;
                    }
                """)
                if not dom_still:
                    print("  [cf] Turnstile iframe gone, assuming passed")
                    return True

            print(f"  [cf] click didn't resolve, retrying... (round {round_i+1})")
            clicked = False
        else:
            await asyncio.sleep(2)

        if round_i % 5 == 0 and round_i > 0:
            print(f"  [cf] still working... ({round_i * 3}s)")

    print("  [cf] auto-solve timeout, continuing anyway...")
    return False


async def handle_birthday_page(page, birth_year, birth_month, birth_day):
    """Detect and fill birthday page. Returns True if birthday page was found."""
    page_text = await page.evaluate("() => document.body.innerText.toLowerCase()")
    has_birthday = any(k in page_text for k in ['birthday', 'date of birth', 'birth date'])
    if not has_birthday:
        return False

    print("  [birthday] detected!")
    month_names = ['January', 'February', 'March', 'April', 'May', 'June',
                   'July', 'August', 'September', 'October', 'November', 'December']

    all_elements = await page.evaluate("""
        () => {
            const result = [];
            document.querySelectorAll('button, select, input, [role="combobox"], [role="listbox"], [aria-haspopup]').forEach(el => {
                if (el.offsetParent !== null) {
                    result.push({
                        tag: el.tagName.toLowerCase(),
                        text: el.textContent.trim().substring(0, 80),
                        role: el.getAttribute('role') || '',
                        ariaLabel: el.getAttribute('aria-label') || '',
                        ariaHaspopup: el.getAttribute('aria-haspopup') || '',
                        id: el.id || '', name: el.getAttribute('name') || '',
                        type: el.getAttribute('type') || '',
                        className: (el.className || '').toString().substring(0, 100),
                        dataState: el.getAttribute('data-state') || '',
                    });
                }
            });
            return result;
        }
    """)
    print(f"  [birthday] {len(all_elements)} elements on page:")
    for el in all_elements:
        print(f"    <{el['tag']}> text=\"{el['text'][:50]}\" role=\"{el['role']}\" "
              f"aria=\"{el['ariaLabel']}\" haspopup=\"{el['ariaHaspopup']}\" "
              f"id=\"{el['id']}\" name=\"{el['name']}\" type=\"{el['type']}\" "
              f"state=\"{el['dataState']}\" class=\"{el['className'][:60]}\"")

    # method 1: standard <select>
    selects = page.locator('select')
    select_count = await selects.count()
    if select_count >= 2:
        print(f"  [birthday] {select_count} <select> found")
        for i in range(select_count):
            sel = selects.nth(i)
            if not await sel.is_visible():
                continue
            name = (await sel.get_attribute('name') or '').lower()
            sel_id = (await sel.get_attribute('id') or '').lower()
            aria = (await sel.get_attribute('aria-label') or '').lower()
            combined = name + ' ' + sel_id + ' ' + aria
            try:
                if 'month' in combined:
                    await sel.select_option(value=str(birth_month))
                elif 'day' in combined:
                    await sel.select_option(value=str(birth_day))
                elif 'year' in combined:
                    await sel.select_option(value=str(birth_year))
            except Exception:
                pass
        return True

    # method 2: custom combobox
    combos = page.locator('button[role="combobox"], [role="combobox"], button[aria-haspopup="listbox"], button[aria-haspopup="menu"], button[aria-haspopup="true"]')
    combo_count = await combos.count()
    if combo_count >= 2:
        print(f"  [birthday] {combo_count} combobox found")
        filled = 0
        for i in range(combo_count):
            combo = combos.nth(i)
            if not await combo.is_visible():
                continue
            aria_label = (await combo.get_attribute('aria-label') or '').lower()
            text = (await combo.text_content() or '').strip().lower()
            combo_id = (await combo.get_attribute('id') or '').lower()
            combo_name = (await combo.get_attribute('name') or '').lower()
            combined = aria_label + ' ' + text + ' ' + combo_id + ' ' + combo_name
            target_text = None
            if 'month' in combined:
                target_text = month_names[birth_month - 1]
            elif 'year' in combined:
                target_text = str(birth_year)
            elif 'day' in combined:
                target_text = str(birth_day)
            if not target_text:
                order_map = {0: month_names[birth_month - 1], 1: str(birth_day), 2: str(birth_year)}
                target_text = order_map.get(filled)
            if not target_text:
                continue
            try:
                await combo.click()
                await asyncio.sleep(0.5)
                option = page.locator(f'[role="option"]:has-text("{target_text}")').first
                await option.click(timeout=3000)
                print(f"    selected: {target_text}")
                filled += 1
                await asyncio.sleep(0.3)
            except Exception as e:
                print(f"    select failed ({target_text}): {e}")
        if filled > 0:
            return True

    # method 3: plain buttons with Month/Day/Year text
    buttons = page.locator('button')
    btn_count = await buttons.count()
    birthday_btns = []
    for i in range(btn_count):
        btn = buttons.nth(i)
        if not await btn.is_visible():
            continue
        text = (await btn.text_content() or '').strip()
        if text.lower() in ['month', 'day', 'year', 'mm', 'dd', 'yyyy',
                            'select month', 'select day', 'select year']:
            birthday_btns.append((i, text.lower(), btn))
    if len(birthday_btns) >= 2:
        print(f"  [birthday] {len(birthday_btns)} buttons: {[b[1] for b in birthday_btns]}")
        for _, btn_text, btn in birthday_btns:
            target_text = None
            if 'month' in btn_text:
                target_text = month_names[birth_month - 1]
            elif 'year' in btn_text:
                target_text = str(birth_year)
            elif 'day' in btn_text:
                target_text = str(birth_day)
            if not target_text:
                continue
            try:
                await btn.click()
                await asyncio.sleep(0.5)
                for opt_sel in [
                    f'[role="option"]:has-text("{target_text}")',
                    f'li:has-text("{target_text}")',
                    f'div[role="menuitem"]:has-text("{target_text}")',
                ]:
                    opt = page.locator(opt_sel).first
                    if await opt.count() > 0:
                        await opt.click(timeout=3000)
                        print(f"    selected: {target_text}")
                        break
                await asyncio.sleep(0.3)
            except Exception as e:
                print(f"    select failed ({target_text}): {e}")
        return True

    # method 4: date input
    date_input = page.locator('input[type="date"]')
    if await date_input.count() > 0 and await date_input.first.is_visible():
        date_str = f"{birth_year}-{birth_month:02d}-{birth_day:02d}"
        await date_input.first.fill(date_str)
        print(f"  [birthday] date input: {date_str}")
        return True

    print("  [birthday] cannot auto-fill, skipping")
    return False


async def handle_onboarding(page, first_name, last_name, max_rounds=10):
    """Click through post-registration onboarding pages:
    personal use, display name, don't improve, etc.
    Keeps clicking until we land on /chat or /new or no more buttons."""
    print("\n  [onboarding] checking for onboarding pages...")

    for round_i in range(max_rounds):
        await asyncio.sleep(2)

        # 检测是否被 logout（排除 returnTo=onboarding 的情况）
        current_url = page.url.lower()
        if '/logout' in current_url:
            log_safe_page_origin("  [onboarding] detected logout at origin: ", page.url)
            # 等一下看是否自动跳转回来
            await asyncio.sleep(5)
            if '/logout' in page.url.lower() or ('/login' in page.url.lower() and 'returnto' not in page.url.lower()):
                print(f"  [onboarding] confirmed logout")
                return "session_lost"
            continue

        # 检测是否被重定向到营销/登录页（session 丢失）
        if '/login' in current_url:
            await asyncio.sleep(3)
            try:
                body_text = await page.evaluate("() => (document.body.innerText || '').substring(0, 500)")
            except Exception:
                body_text = ""
            body_lower = body_text.lower()
            if any(kw in body_lower for kw in ['contact sales', 'think fast', 'platform solutions', 'continue with']) or len(body_text.strip()) > 30:
                log_safe_page_origin(
                    "  [onboarding] session lost on login/marketing origin: ",
                    page.url,
                )
                return "session_lost"
            # 空白页可能还在加载，继续等
            if len(body_text.strip()) < 30:
                print(f"  [onboarding] login page still loading, waiting...")
                continue

        # 每轮开始先检测并关闭可能存在的 modal 弹窗（如 Claude Code 设置）
        try:
            modal_closed = await page.evaluate("""
                () => {
                    const overlays = document.querySelectorAll('[data-state="open"].fixed, [role="dialog"], [data-radix-dialog-content]');
                    for (const dlg of overlays) {
                        const btns = dlg.querySelectorAll('button');
                        if (btns.length >= 2) {
                            // 点第二个按钮（Later/稍后）
                            btns[btns.length - 1].click();
                            return 'closed modal: ' + (btns[btns.length - 1].textContent || '').trim().substring(0, 40);
                        }
                        if (btns.length === 1) {
                            btns[0].click();
                            return 'closed modal: ' + (btns[0].textContent || '').trim().substring(0, 40);
                        }
                    }
                    return null;
                }
            """)
            if modal_closed:
                print(f"  [onboarding] {modal_closed}")
                await asyncio.sleep(2)
        except Exception:
            pass

        current_url = page.url
        # 精确检查 path，避免 returnTo=%2Fnew 误匹配
        from urllib.parse import urlparse
        url_path = urlparse(current_url).path
        if any(k in url_path for k in ['/chat', '/new']):
            print("  [onboarding] reached chat page, done!")
            return True

        try:
            page_text = await page.evaluate("() => document.body.innerText")
        except Exception:
            # 页面正在导航（可能已经到聊天页了）
            await asyncio.sleep(3)
            url_path = urlparse(page.url).path
            if any(k in url_path for k in ['/chat', '/new']):
                print("  [onboarding] reached chat page during navigation!")
                return True
            log_safe_page_origin(
                "  [onboarding] page navigating at origin: ", page.url
            )
            continue
        page_lower = page_text.lower()
        log_safe_page_origin(
            f"  [onboarding] round {round_i+1}, origin: ", current_url
        )
        print(f"  [onboarding] page text preview: {page_text[:150].replace(chr(10), ' ')}")

        clicked = False
        need_continue = False  # whether we need to also click Continue after a selection

        # "Let's create your account" — check terms checkbox only, don't touch toggles
        if not clicked and ("let's create your account" in page_lower or ('consumer terms' in page_lower and 'acceptable use' in page_lower)):
            print("  [onboarding] terms page, checking agreement checkbox...")
            cb_count = await page.evaluate("""
                () => {
                    let clicked = 0;
                    document.querySelectorAll('input[type="checkbox"]').forEach(el => {
                        if (!el.checked && el.offsetParent !== null) { el.click(); clicked++; }
                    });
                    return clicked;
                }
            """)
            if cb_count:
                print(f"  [onboarding] checked {cb_count} checkbox(es)")
            await asyncio.sleep(1)
            for label in ['Continue', 'Create account', 'Next', 'Get started']:
                btn = page.locator(f'button:has-text("{label}")').first
                if await btn.count() > 0:
                    try:
                        await btn.click(timeout=3000)
                        print(f"  [onboarding] clicked: {label}")
                        clicked = True
                        break
                    except Exception:
                        pass

        # "Personal" use button / card
        if any(k in page_lower for k in ['personal', 'how will you', 'using claude', 'what brings you']):
            # method 1: try button first
            for label in ['For personal use', 'Personal', 'personal',
                          "I'm using Claude for personal", 'For personal']:
                btn = page.locator(f'button:has-text("{label}")').first
                if await btn.count() > 0:
                    try:
                        await btn.click(timeout=3000)
                        print(f"  [onboarding] clicked button: {label}")
                        clicked = True
                        need_continue = True
                        break
                    except Exception:
                        pass

            # method 2: use JS to find the smallest (most specific) element containing "personal"
            if not clicked:
                try:
                    clicked_js = await page.evaluate("""
                        () => {
                            const candidates = [];
                            const all = document.querySelectorAll('*');
                            for (const el of all) {
                                if (el.offsetParent === null) continue;
                                const text = (el.textContent || '').trim();
                                if (!/personal/i.test(text)) continue;
                                // skip very large elements (likely page/container)
                                const rect = el.getBoundingClientRect();
                                if (rect.width > 600 || rect.height > 400) continue;
                                if (rect.width < 20 || rect.height < 20) continue;
                                candidates.push({el, area: rect.width * rect.height, rect});
                            }
                            if (candidates.length === 0) return null;
                            // sort by area ascending = smallest first = most specific
                            candidates.sort((a, b) => a.area - b.area);
                            // prefer clickable-looking elements
                            for (const c of candidates) {
                                const tag = c.el.tagName.toLowerCase();
                                const role = c.el.getAttribute('role') || '';
                                const cursor = getComputedStyle(c.el).cursor;
                                if (tag === 'button' || tag === 'a' || role === 'button' ||
                                    role === 'radio' || role === 'option' || cursor === 'pointer') {
                                    c.el.click();
                                    return {tag, text: c.el.textContent.trim().substring(0, 80),
                                            w: c.rect.width, h: c.rect.height};
                                }
                            }
                            // fallback: click the smallest matching element
                            const best = candidates[0];
                            best.el.click();
                            return {tag: best.el.tagName.toLowerCase(),
                                    text: best.el.textContent.trim().substring(0, 80),
                                    w: best.rect.width, h: best.rect.height};
                        }
                    """)
                    if clicked_js:
                        print(f"  [onboarding] JS-clicked 'personal' <{clicked_js['tag']}> "
                              f"({clicked_js['w']:.0f}x{clicked_js['h']:.0f}): {clicked_js['text'][:50]}")
                        clicked = True
                        need_continue = True
                except Exception as e:
                    print(f"  [onboarding] personal JS-click failed: {e}")

            # after selecting personal use card, click Continue/Next to proceed
            if need_continue:
                await asyncio.sleep(1)
                for label in ['Continue', 'Next', 'Get started', 'Start', 'Submit']:
                    btn = page.locator(f'button:has-text("{label}")').first
                    if await btn.count() > 0:
                        try:
                            await btn.click(timeout=3000)
                            print(f"  [onboarding] clicked: {label}")
                            break
                        except Exception:
                            pass

        # Plans / pricing page — select Free plan (优先于 Continue)
        # 排除营销首页（含 "contact sales"、"think fast" 等）
        is_marketing = any(kw in page_lower for kw in ['contact sales', 'think fast', 'platform solutions pricing'])
        if not is_marketing and any(k in page_lower for k in ['pick a plan', 'plans that grow', 'meet claude', 'pricing']):
            clicked = False  # 强制重新选择 plan
            # 截图看当前页面
            try:
                await page.screenshot(path="screenshots/plan_page.png")
            except Exception:
                pass
            # 优先用 JS 精确点击 Free plan 卡片（避免匹配到导航链接）
            try:
                clicked_js = await page.evaluate("""
                    () => {
                        // 找包含 "$0" 或 "Meet Claude" 的卡片/按钮
                        const allEls = document.querySelectorAll('button, [role="button"], [role="radio"], div[class*="card"], div[class*="plan"]');
                        for (const el of allEls) {
                            if (el.offsetParent === null) continue;
                            const text = el.textContent || '';
                            if ((text.includes('$0') || text.includes('S$0') || text.includes('Meet Claude')) && text.includes('Free')) {
                                el.click();
                                return 'clicked: ' + text.substring(0, 60);
                            }
                        }
                        // fallback: 找最小的包含 "Free" 的可点击元素
                        const candidates = [];
                        for (const el of document.querySelectorAll('button, [role="button"], [role="radio"]')) {
                            if (el.offsetParent === null) continue;
                            const t = (el.textContent || '').trim();
                            if (/free/i.test(t) && !t.includes('Contact') && !t.includes('Log')) {
                                const r = el.getBoundingClientRect();
                                if (r.width > 20 && r.height > 20) {
                                    candidates.push({el, area: r.width * r.height});
                                }
                            }
                        }
                        if (candidates.length > 0) {
                            candidates.sort((a,b) => a.area - b.area);
                            candidates[0].el.click();
                            return 'clicked smallest: ' + candidates[0].el.textContent.substring(0, 60);
                        }
                        return null;
                    }
                """)
                if clicked_js:
                    print(f"  [onboarding] JS plan select: {clicked_js}")
                    clicked = True
            except Exception as e:
                print(f"  [onboarding] JS plan select failed: {e}")
            # fallback: Playwright 点击（只用 button，不用 a 标签避免导航）
            if not clicked:
                for label in ['Free', 'Meet Claude', 'Start for free', 'Continue with Free']:
                    btn = page.locator(f'button:has-text("{label}")').first
                    if await btn.count() > 0:
                        try:
                            await btn.click(timeout=3000)
                            print(f"  [onboarding] clicked plan button: {label}")
                            clicked = True
                            break
                        except Exception:
                            pass
            # fallback: JS click the smallest element with "free" text (skip tiny ones)
            if not clicked:
                try:
                    clicked_js = await page.evaluate("""
                        () => {
                            const candidates = [];
                            const all = document.querySelectorAll('button, a, [role="button"], div, span');
                            for (const el of all) {
                                if (el.offsetParent === null) continue;
                                const text = el.textContent.trim();
                                if (!/^free$/i.test(text) && !/meet claude/i.test(text) &&
                                    !/start for free/i.test(text) && !/get started/i.test(text)) continue;
                                const rect = el.getBoundingClientRect();
                                if (rect.width < 20 || rect.height < 15) continue;
                                if (rect.width > 500) continue;
                                const cursor = getComputedStyle(el).cursor;
                                candidates.push({el, area: rect.width * rect.height, clickable: cursor === 'pointer'});
                            }
                            if (candidates.length === 0) return null;
                            // prefer clickable, then smallest
                            candidates.sort((a, b) => (b.clickable - a.clickable) || (a.area - b.area));
                            candidates[0].el.click();
                            return candidates[0].el.textContent.trim().substring(0, 50);
                        }
                    """)
                    if clicked_js:
                        print(f"  [onboarding] JS-clicked plan: {clicked_js}")
                        clicked = True
                except Exception as e:
                    print(f"  [onboarding] plan click failed: {e}")

        # "Before your first chat" page — 直接点 Continue（不动 toggle）
        if not clicked and ('before your first' in page_lower or 'setting to review' in page_lower):
            print("  [onboarding] 'before your first chat' page")
            # 打印 toggle 状态
            try:
                toggle_info = await page.evaluate("""
                    () => {
                        const toggles = document.querySelectorAll('[role="switch"], button[role="switch"]');
                        return Array.from(toggles).filter(t => t.offsetParent !== null).map(t => ({
                            text: (t.closest('label') || t.parentElement)?.textContent?.trim()?.substring(0, 80) || '',
                            checked: t.getAttribute('aria-checked'),
                            state: t.getAttribute('data-state'),
                        }));
                    }
                """)
                print(f"  [onboarding] toggles: {json.dumps(toggle_info, ensure_ascii=False)[:300]}")
            except Exception:
                pass
            # 关掉"帮助改进"toggle — 直接点击所有可见 toggle（默认开启，点一下关掉）
            try:
                toggled = await page.evaluate("""
                    () => {
                        let count = 0;
                        const toggles = document.querySelectorAll('[role="switch"], button[role="switch"]');
                        for (const t of toggles) {
                            if (t.offsetParent === null) continue;
                            t.click();
                            count++;
                        }
                        return count;
                    }
                """)
                if toggled:
                    print(f"  [onboarding] clicked {toggled} toggle(s) to disable improve")
            except Exception:
                pass
            await asyncio.sleep(2)
            # 截图看当前状态
            try:
                await page.screenshot(path="screenshots/before_first_chat_after_toggle.png")
            except Exception:
                pass
            # 打印页面上所有可见按钮
            try:
                btns_info = await page.evaluate("""
                    () => {
                        const btns = document.querySelectorAll('button, a[role="button"]');
                        return Array.from(btns)
                            .filter(b => b.offsetParent !== null && b.textContent.trim())
                            .map(b => b.textContent.trim().substring(0, 40));
                    }
                """)
                print(f"  [onboarding] visible buttons: {btns_info}")
            except Exception:
                pass
            # 点 Continue / Start 按钮（重试两轮）
            for _attempt in range(3):
                for label in ['Continue', 'Start', 'Next', 'Got it', 'OK']:
                    btn = page.locator(f'button:has-text("{label}")').first
                    if await btn.count() > 0:
                        try:
                            await btn.click(timeout=5000)
                            print(f"  [onboarding] clicked: {label}")
                            clicked = True
                            break
                        except Exception as e:
                            print(f"  [onboarding] click {label} failed: {e}")
                if clicked:
                    break
                print(f"  [onboarding] Continue not found, retrying ({_attempt+1}/3)...")
                await asyncio.sleep(2)
            if not clicked:
                print(f"  [onboarding] WARNING: could not click Continue on 'before first chat' page!")
                try:
                    await page.screenshot(path="screenshots/before_first_chat_stuck.png")
                except Exception:
                    pass
            # 点完 toggle + Continue 后直接返回，让主流程验证 sessionKey
            # 200 保存，403 丢弃（跳登录页=封号）
            print("  [onboarding] improve disabled, returning to validate sessionKey...")
            await asyncio.sleep(2)
            return True

        # "Cowork lives in the desktop app" / 下载桌面端页面
        # 先保存 cookies（因为 Skip 后可能丢 session），再点 Skip
        if not clicked and any(kw in page_lower for kw in ['desktop app', 'cowork', 'download the app', 'download for']):
            print("  [onboarding] desktop app download page, saving cookies first...")
            try:
                context = page.context
                cookies = await context.cookies()
                sk = next((c["value"] for c in cookies if c["name"] == "sessionKey"), None)
                if sk:
                    import json as _json
                    os.makedirs(COOKIE_OUTPUT_DIR, exist_ok=True)
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    with open(os.path.join(COOKIE_OUTPUT_DIR, f"pre_skip_{ts}.json"), "w", encoding="utf-8") as _f:
                        _json.dump(cookies, _f, indent=2, ensure_ascii=False)
                    with open(os.path.join(COOKIE_OUTPUT_DIR, f"sk_pre_skip_{ts}.txt"), "w", encoding="utf-8") as _f:
                        _f.write(sk)
                    print(f"  [onboarding] pre-saved sessionKey: {sk[:60]}...")
            except Exception as e:
                print(f"  [onboarding] pre-save error: {e}")
            # 点 Skip
            for label in ['Skip', 'Not now', 'Maybe later']:
                btn = page.locator(f'button:has-text("{label}"), a:has-text("{label}")').first
                if await btn.count() > 0:
                    try:
                        await btn.click(timeout=3000)
                        print(f"  [onboarding] clicked: {label}")
                        clicked = True
                        break
                    except Exception:
                        pass
            if clicked:
                continue

        # "Don't improve" / "No thanks" / privacy opt-out
        # 跳过 toggle 操作（会触发设置更新请求，可能影响 session）
        # 只点按钮
        is_terms_page = "let's create your account" in page_lower or ('consumer terms' in page_lower and 'acceptable use' in page_lower)
        is_first_chat_page = 'before your first' in page_lower or 'setting to review' in page_lower
        if not is_terms_page and not is_first_chat_page and (not clicked or 'improve' in page_lower or 'help us' in page_lower):
            for label in [
                "Don't improve", "No, don't improve", "No thanks", "Decline",
                "Don't help improve", "No", "Opt out", "Skip",
                "don't improve", "no thanks", "decline", "skip",
            ]:
                btn = page.locator(f'button:has-text("{label}"), a:has-text("{label}")').first
                if await btn.count() > 0:
                    try:
                        await btn.click(timeout=3000)
                        print(f"  [onboarding] clicked: {label}")
                        clicked = True
                        break
                    except Exception:
                        pass

        # display name / full name input
        if not clicked or 'what should' in page_lower or 'call you' in page_lower:
            name_filled = False
            name_input = page.locator('input[type="text"]:not([type="hidden"])')
            count = await name_input.count()
            for i in range(count):
                inp = name_input.nth(i)
                if not await inp.is_visible():
                    continue
                val = await inp.input_value()
                if not val:
                    full_name = f"{first_name} {last_name}"
                    await human_type(page, f'input[type="text"]', full_name)
                    print(f"  [onboarding] filled name: {full_name}")
                    clicked = True
                    name_filled = True
                    break
                else:
                    name_filled = True  # already has a value
            # after filling name (or name already present), click submit arrow/button
            if name_filled or 'call you' in page_lower:
                await asyncio.sleep(1)
                # debug: 打印 cookies 和 URL
                try:
                    cookies = await context.cookies()
                    cookie_names = [c['name'] for c in cookies if 'claude' in c.get('domain', '')]
                    print(f"  [onboarding] pre-submit cookies: {cookie_names}")
                    log_safe_page_origin(
                        "  [onboarding] pre-submit origin: ", page.url
                    )
                except Exception:
                    pass
                # 用 Enter 提交
                try:
                    await page.keyboard.press("Enter")
                    print(f"  [onboarding] pressed Enter to submit name")
                    clicked = True
                    await asyncio.sleep(3)
                    log_safe_page_origin(
                        "  [onboarding] post-submit origin: ", page.url
                    )
                except Exception:
                    pass

        # Industry/role selection page

        # "What are you into?" — pick 3 topics then submit
        if not clicked and ('what are you into' in page_lower or 'pick three' in page_lower or 'topics to explore' in page_lower):
            print("  [onboarding] topics page, picking 3 topics...")
            # 检查是否已经选了3个（避免toggle取消）
            already_selected = await page.evaluate("""
                () => {
                    let count = 0;
                    document.querySelectorAll('button[aria-pressed="true"], button[data-state="active"], button.selected').forEach(el => {
                        if (el.offsetParent !== null) count++;
                    });
                    return count;
                }
            """)
            if already_selected >= 3:
                print(f"  [onboarding] already {already_selected} topics selected, skipping pick")
            else:
                # 用 Playwright 真实点击 topic 卡片
                topic_btns = page.locator('button, [role="button"], [role="option"]')
                count = await topic_btns.count()
                picked = 0
                last_btn = None
                for i in range(count):
                    if picked >= 3:
                        break
                    btn = topic_btns.nth(i)
                    try:
                        if not await btn.is_visible():
                            continue
                        box = await btn.bounding_box()
                        if not box or box['width'] < 40 or box['height'] < 30 or box['width'] > 500 or box['height'] > 200:
                            continue
                        text = (await btn.text_content() or '').strip()
                        if len(text) < 5 or len(text) > 100:
                            continue
                        if re.match(r'^(let|continue|next|skip|back|get started|i have|own topic)', text, re.I):
                            continue
                        await btn.click(timeout=2000)
                        print(f"  [onboarding] picked topic: {text[:40]}")
                        picked += 1
                        last_btn = btn
                        await asyncio.sleep(0.3)
                    except Exception:
                        continue
                print(f"  [onboarding] picked {picked} topics total")
            await asyncio.sleep(1)

            # 点击 "Let's go" 按钮
            lets_go_clicked = False
            for label in ["Let's go", "Let's Go", "Lets go", "Continue", "Next", "Start", "Get started"]:
                btn = page.locator(f'button:has-text("{label}")').first
                if await btn.count() > 0:
                    try:
                        await btn.click(timeout=3000)
                        print(f"  [onboarding] clicked: {label}")
                        lets_go_clicked = True
                        break
                    except Exception:
                        pass
            if not lets_go_clicked:
                # fallback: JS click
                try:
                    await page.evaluate("""() => {
                        const btns = document.querySelectorAll('button');
                        for (const b of btns) {
                            const t = b.textContent.trim().toLowerCase();
                            if ((t.includes("let") && t.includes("go")) || t === 'continue' || t === 'next') {
                                b.click();
                                return;
                            }
                        }
                    }""")
                    print("  [onboarding] JS-clicked Let's go")
                except Exception:
                    pass
            clicked = True
            continue

        # "All set" / "Where should we start" — final onboarding page
        # 不点建议卡片（会触发聊天请求，可能导致 session 失效），直接跳过
        if not clicked and ('all set' in page_lower or 'where should we start' in page_lower or 'ideas just for you' in page_lower):
            print("  [onboarding] 'All set' page, skipping to chat...")
            for label in ["I have my own topic", "Start a new chat", "Start chatting", "Skip", "Continue"]:
                btn = page.locator(f'button:has-text("{label}")').first
                if await btn.count() > 0:
                    try:
                        await btn.click(timeout=3000)
                        print(f"  [onboarding] clicked: {label}")
                        clicked = True
                        await asyncio.sleep(3)
                        break
                    except Exception:
                        pass
            # 检查是否到了聊天页面
            if any(k in page.url for k in ['/chat', '/new']):
                print("  [onboarding] reached chat page!")
                # 等弹窗出现
                await asyncio.sleep(5)
                # 截图看弹窗
                os.makedirs("screenshots", exist_ok=True)
                await page.screenshot(path=f"screenshots/dialog_{round_i}.png")
                print(f"  [onboarding] dialog screenshot: screenshots/dialog_{round_i}.png")
                # Claude Code 弹窗：有两个按钮，要点第二个（Later/稍后）
                # 用 dialog/modal 容器定位，取第二个按钮
                dismissed = False
                try:
                    # 方法1: 找 dialog/modal 里的按钮，点第二个
                    dialog_btns = await page.evaluate("""
                        () => {
                            // 找 dialog / modal 容器
                            const dialogs = document.querySelectorAll('[role="dialog"], [data-radix-dialog-content], [class*="modal"], [class*="dialog"], [class*="Dialog"]');
                            for (const dlg of dialogs) {
                                const btns = dlg.querySelectorAll('button');
                                if (btns.length >= 2) {
                                    // 点第二个按钮
                                    btns[1].click();
                                    return 'clicked 2nd button: ' + (btns[1].textContent || '').trim().substring(0, 40);
                                }
                            }
                            return null;
                        }
                    """)
                    if dialog_btns:
                        print(f"  [onboarding] {dialog_btns}")
                        dismissed = True
                        await asyncio.sleep(1)
                except Exception as e:
                    print(f"  [onboarding] dialog method1 failed: {e}")
                # 方法2: 按文字找 Later 按钮
                if not dismissed:
                    for label in ['Later', 'Maybe later', 'Not now', 'Skip for now', 'Skip']:
                        btn = page.locator(f'button:has-text("{label}")').first
                        if await btn.count() > 0:
                            try:
                                await btn.click(timeout=3000)
                                print(f"  [onboarding] dismissed dialog: {label}")
                                dismissed = True
                                await asyncio.sleep(1)
                                break
                            except Exception:
                                pass
                return True
            continue

        # Industry/role selection page — 这页是「Select your role」下拉框 + 「Set up later」链接，
        # 没有 Continue 按钮。直接点 Set up later 跳过最稳（角色个性化我们不需要，要的是 sessionKey）。
        # 旧逻辑点 candidates[0] 会点中下拉框本身、选不中角色、又找不到 Continue → 死循环 7 轮。
        if not clicked and ('industry' in page_lower or 'what do you do' in page_lower or 'what kind of work' in page_lower or 'role' in page_lower or 'field' in page_lower):
            print("  [onboarding] industry/role page, skipping via 'Set up later'...")
            # 1) 优先点 "Set up later" / 跳过类链接（button 或 a 或任意可点元素，精确文字匹配）
            skipped = False
            for label in ['Set up later', 'Skip for now', 'Skip', 'Maybe later', 'Not now', 'Later']:
                loc = page.get_by_text(label, exact=True)
                try:
                    if await loc.count() > 0:
                        await loc.first.click(timeout=3000)
                        print(f"  [onboarding] clicked: {label}")
                        clicked = True
                        skipped = True
                        await asyncio.sleep(1.5)
                        break
                except Exception:
                    pass
            # 2) 没有跳过链接才退化为：选下拉框第一个角色 + Continue
            if not skipped:
                try:
                    picked = await page.evaluate("""
                        () => {
                            // 打开 select your role 下拉
                            const trigger = [...document.querySelectorAll('button,[role="combobox"],[role="button"]')]
                                .find(el => el.offsetParent && /select your role|role/i.test(el.textContent||''));
                            if (trigger) { trigger.click(); return 1; }
                            return 0;
                        }
                    """)
                    if picked:
                        await asyncio.sleep(1)
                        # 选弹出的第一个 option
                        opt = page.locator('[role="option"], [role="menuitem"], li[role="option"]').first
                        if await opt.count() > 0:
                            await opt.click(timeout=3000)
                            print("  [onboarding] picked a role from dropdown")
                            await asyncio.sleep(1)
                except Exception as e:
                    print(f"  [onboarding] role dropdown pick failed: {e}")
                for label in ['Continue', 'Next', 'Submit', 'Get started', 'Set up later']:
                    btn = page.locator(f'button:has-text("{label}"), a:has-text("{label}")').first
                    if await btn.count() > 0:
                        try:
                            await btn.click(timeout=3000)
                            print(f"  [onboarding] clicked: {label}")
                            clicked = True
                            break
                        except Exception:
                            pass
            continue

        # generic Continue / Next / Get started / Start buttons
        if not clicked:
            for label in ["I have my own topic", "Let's go", 'Continue', 'Next', 'Get started', 'Start', 'OK', 'Got it',
                          'Sounds good', 'Let me in', 'Start chatting', 'Later', 'Maybe later', 'Not now', 'Skip for now']:
                btn = page.locator(f'button:has-text("{label}")').first
                if await btn.count() > 0:
                    try:
                        await btn.click(timeout=3000)
                        print(f"  [onboarding] clicked: {label}")
                        clicked = True
                        break
                    except Exception:
                        pass

        # click any radio/checkbox that isn't checked
        if not clicked:
            await page.evaluate("""
                () => {
                    document.querySelectorAll('input[type="radio"], input[type="checkbox"]').forEach(el => {
                        if (!el.checked && el.offsetParent !== null) el.click();
                    });
                }
            """)

        if not clicked:
            # nothing to click, maybe already done or unknown page
            print(f"  [onboarding] nothing to click on this page")
            screenshot_path = f"screenshots/stuck_{round_i}.png"
            os.makedirs("screenshots", exist_ok=True)
            await page.screenshot(path=screenshot_path)
            print(f"  [onboarding] screenshot saved: {screenshot_path}")
            # try one more wait
            await asyncio.sleep(2)
            if any(k in page.url for k in ['/chat', '/new']):
                print("  [onboarding] reached chat page, done!")
                return True

    print("  [onboarding] max rounds reached")
    return False


async def save_cookies(context, profile_id, email=None, email_password=None):
    cookies = await context.cookies()
    os.makedirs(COOKIE_OUTPUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_key = None
    important_cookies = {}
    for c in cookies:
        name = c.get("name", "")
        if name == "sessionKey":
            session_key = c["value"]
        if name in ("sessionKey", "__cf_bm", "cf_clearance", "activitySessionId") or name.startswith("__cf"):
            important_cookies[name] = c["value"]

    print(f"  found {len(cookies)} cookies, important: {list(important_cookies.keys())}")

    if session_key:
        filename = os.path.join(COOKIE_OUTPUT_DIR, f"sk_{profile_id}_{ts}.txt")
        with open(filename, "w", encoding="utf-8") as f:
            f.write(session_key)
        print(f"  sessionKey saved: {filename}")
        full_filename = os.path.join(COOKIE_OUTPUT_DIR, f"full_{profile_id}_{ts}.json")
        with open(full_filename, "w", encoding="utf-8") as f:
            json.dump(cookies, f, indent=2, ensure_ascii=False)
        print(f"  full cookies saved: {full_filename}")
        # 追加到统一账号文件
        if email:
            pwd = email_password or ""
            accounts_file = os.path.join(COOKIE_OUTPUT_DIR, "accounts.txt")
            with open(accounts_file, "a", encoding="utf-8") as f:
                f.write(f"{email}|{pwd}|{session_key}\n")
            print(f"  account saved to: {accounts_file}")
        # 导出标准 token（Claude 登录态就是 sessionKey），失败不影响主流程
        try:
            from common.session_export import save_claude_token
            save_claude_token(session_key, email)
        except Exception:
            print("  [WARN] failed to save Claude token")
        return session_key
    else:
        filename = os.path.join(COOKIE_OUTPUT_DIR, f"cookies_{profile_id}_{ts}.json")
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(cookies, f, indent=2, ensure_ascii=False)
        print(f"  no sessionKey, saved all cookies: {filename}")
        return None


# ========== main flow ==========

async def _get_and_verify_phone(page, max_attempts=2):
    """简化版手机验证，用于 re-login 场景"""
    for attempt in range(1, max_attempts + 1):
        pkey = None
        try:
            # 获取号码
            result = get_phone_number()
            if not result:
                # 尝试 hero-sms
                result = hero_get_phone_number()
            if not result:
                print(f"  [re-verify] no phone number available (attempt {attempt})")
                continue
            phone, pkey = result
            if not phone.startswith('+'):
                phone = '+' + phone

            # 填入号码
            phone_input = 'input[type="tel"], input[name="phone"], input[name="phoneNumber"]'
            await page.locator(phone_input).first.fill("")
            await asyncio.sleep(0.3)
            await human_type(page, phone_input, phone)
            print(f"  [re-verify] filled: {phone}")
            await asyncio.sleep(1)

            # 点发送
            send_btn = page.locator('button:has-text("Send"), button:has-text("Verify"), button[type="submit"]').first
            await send_btn.click(timeout=5000)
            print(f"  [re-verify] clicked send")
            await asyncio.sleep(3)

            # 检查错误
            error_text = await page.evaluate("""
                () => {
                    for (const sel of ['[role="alert"]', '.error', '[data-testid="error"]',
                        '.text-red-500', 'div[class*="error"]', 'span[class*="error"]']) {
                        const el = document.querySelector(sel);
                        if (el && el.offsetParent !== null && el.textContent.trim())
                            return el.textContent.trim();
                    }
                    return '';
                }
            """)
            if error_text:
                print(f"  [re-verify] error: {error_text}")
                release_phone(pkey)
                continue

            # 等待验证码
            sms_code = await asyncio.get_event_loop().run_in_executor(
                None, get_sms_code, pkey, 120, 5
            )
            if not sms_code:
                print(f"  [re-verify] sms timeout")
                release_phone(pkey)
                continue

            # 输入验证码
            code_selector = 'input[type="text"], input[name="code"], input[type="number"]'
            await human_type(page, code_selector, sms_code.strip())
            await asyncio.sleep(1)
            await page.locator('button:has-text("Verify"), button[type="submit"]').first.click()
            print(f"  [re-verify] code submitted: {sms_code}")
            await asyncio.sleep(3)
            return True

        except Exception as e:
            print(f"  [re-verify] error: {e}")
            if pkey:
                release_phone(pkey)
    return False


async def register(
    profile_id,
    account=None,
    account_store=None,
    account_lease=None,
):
    """Run one registration. Returns sessionKey on success, None on failure."""
    bb = BitBrowser()
    start_time = time.time()
    email = ""
    email_password = ""

    def check_timeout():
        elapsed = time.time() - start_time
        if elapsed > REGISTER_TIMEOUT:
            raise TimeoutError(f"registration timeout ({REGISTER_TIMEOUT}s)")

    print(f"\n[1/6] open BitBrowser...")
    browser_data = bb.open_browser(profile_id)
    ws_url = browser_data["ws"]
    print(f"  ws: {ws_url}")

    session_key = None
    try:
        async with async_playwright() as p:
            print("[2/6] connect Playwright...")
            browser = await p.chromium.connect_over_cdp(ws_url)
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()

            # 注入反检测脚本（通过 CDP 在页面 JS 执行前注入）
            stealth_js = """
                // 1. 隐藏 navigator.webdriver
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                try { delete navigator.__proto__.webdriver; } catch(e) {}

                // 2. 伪造 chrome 对象
                if (!window.chrome) {
                    window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
                }

                // 3. 伪造 Permissions API
                const origQuery = window.navigator.permissions?.query;
                if (origQuery) {
                    window.navigator.permissions.query = (params) => (
                        params.name === 'notifications' ?
                            Promise.resolve({state: Notification.permission}) :
                            origQuery(params)
                    );
                }

                // 4. 伪造 plugins
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [
                        {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1},
                        {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '', length: 1},
                        {name: 'Native Client', filename: 'internal-nacl-plugin', description: '', length: 1},
                    ],
                });

                // 5. 伪造 languages
                Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});

                // 6. 伪造 connection.rtt
                if (navigator.connection) {
                    Object.defineProperty(navigator.connection, 'rtt', {get: () => 50});
                }

                // 7. 隐藏 Headless/Automation 相关
                Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
                Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});

                // 8. 对抗 PerimeterX CDP 检测
                // PX 通过检测 window.cdc_adoQpoasnfa76pfcZLmcfl_* 等 CDP 注入的变量
                const cdcProps = Object.getOwnPropertyNames(window).filter(p =>
                    p.match(/^cdc_|^__cdc|^_cdp|^__cdp|^chrome_devtools/i)
                );
                cdcProps.forEach(p => { try { delete window[p]; } catch(e) {} });

                // 9. 隐藏 Error.stack 中的 CDP 痕迹
                const origPrepare = Error.prepareStackTrace;
                Error.prepareStackTrace = function(err, stack) {
                    const filtered = stack.filter(s => {
                        const fn = s.getFunctionName() || '';
                        const file = s.getFileName() || '';
                        return !fn.includes('cdp') && !file.includes('pptr') &&
                               !file.includes('playwright') && !file.includes('puppeteer');
                    });
                    if (origPrepare) return origPrepare(err, filtered);
                    return err + '\\n' + filtered.map(s => '    at ' + s).join('\\n');
                };

                // 10. 隐藏 Runtime.evaluate 注入的全局变量
                const origDefineProperty = Object.defineProperty;
                Object.defineProperty = function(obj, prop, desc) {
                    if (obj === window && typeof prop === 'string' &&
                        (prop.startsWith('cdc_') || prop.startsWith('__cdc'))) {
                        return obj;
                    }
                    return origDefineProperty.call(this, obj, prop, desc);
                };

                // 11. 伪造 window.outerWidth/outerHeight（自动化环境可能为 0）
                if (window.outerWidth === 0) {
                    Object.defineProperty(window, 'outerWidth', {get: () => window.innerWidth + 16});
                }
                if (window.outerHeight === 0) {
                    Object.defineProperty(window, 'outerHeight', {get: () => window.innerHeight + 88});
                }

                // 12. 隐藏 Notification.permission 异常
                if (Notification.permission === 'denied') {
                    Object.defineProperty(Notification, 'permission', {get: () => 'default'});
                }

                // 13. 对抗 iframe 检测 — 确保 contentWindow 正常
                const origGetter = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
                if (origGetter) {
                    Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
                        get: function() {
                            const w = origGetter.get.call(this);
                            if (w) {
                                try { Object.defineProperty(w.navigator, 'webdriver', {get: () => undefined}); } catch(e) {}
                            }
                            return w;
                        }
                    });
                }
            """
            # 通过 CDP 在每个 frame 创建时自动注入（比 add_init_script 更早）
            cdp = await context.new_cdp_session(page)
            await cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": stealth_js})
            # 也在当前页面执行一次
            await page.evaluate(f"() => {{{stealth_js}}}")
            # add_init_script 作为备份
            await context.add_init_script(f"() => {{{stealth_js}}}")
            print("  stealth injected (CDP + init_script)")

            # 清掉旧的 pre-save 文件，避免误用
            import glob as _glob
            for old_f in _glob.glob(os.path.join(COOKIE_OUTPUT_DIR, "sk_pre_skip_*.txt")):
                try:
                    os.remove(old_f)
                except Exception:
                    pass
            for old_f in _glob.glob(os.path.join(COOKIE_OUTPUT_DIR, "pre_skip_*.json")):
                try:
                    os.remove(old_f)
                except Exception:
                    pass

            # 如果没有邮箱，先尝试自注册，失败则从 emails.txt 读取
            if account is None:
                print("\n[3/6] get outlook email...")
                # 尝试自注册
                print("  [outlook] trying self-register...")
                registered_email, registered_password = await register_outlook(page)
                if registered_email:
                    account = ClaudeEmailAccount(
                        provider="OUTLOOK",
                        email=registered_email,
                        password=registered_password,
                        client_id="9e5f94bc-e8a4-4e73-b8be-63364c29d753",
                        refresh_token="",
                    )
                else:
                    # 自注册失败，从 emails.txt 读取
                    print("  [outlook] self-register failed, trying emails.txt...")
                    account = account_store.reserve_one()
                    if account is None:
                        raise RuntimeError("no email available")

            email = account.email
            email_password = account.password

            check_timeout()

            # visit Claude.ai
            print(f"\n  goto Claude.ai...")
            # 等待之前的页面跳转完成
            try:
                await page.wait_for_load_state("load", timeout=15000)
            except Exception:
                pass
            await page.goto(CLAUDE_LOGIN_URL, timeout=60000)
            await asyncio.sleep(5)
            log_safe_page_origin("  URL origin: ", page.url)

            # solve Cloudflare Turnstile
            await solve_turnstile(page, max_wait=60)
            check_timeout()

            # 确认登录表单真的出现（Turnstile 可能"假性通过"——iframe 没加载就被判通过，
            # 此时邮箱框/按钮根本不在，硬填会卡 30s 超时）。表单没出现就 reload 重解，最多 3 次。
            EMAIL_SEL = 'input[type="email"], input[name="email"], input[id="email"]'
            for cf_attempt in range(3):
                try:
                    await page.wait_for_selector(EMAIL_SEL, state="visible", timeout=15000)
                    break
                except Exception:
                    print(f"  [cf] login form not ready (likely false-pass), reload+resolve (attempt {cf_attempt+1}/3)")
                    try:
                        await page.goto(CLAUDE_LOGIN_URL, timeout=60000)
                        await asyncio.sleep(5)
                        await solve_turnstile(page, max_wait=60)
                    except Exception:
                        pass
                    check_timeout()

            # enter email
            print(f"  email: {email}")
            await human_type(page, 'input[type="email"], input[name="email"], input[id="email"]', email)
            await asyncio.sleep(1)

            magic_requested_at = time.time()
            if not await click_continue_email(page):
                print("  [warn] continue-email button not found in any language")
            check_timeout()

            # poll magic link from outlook inbox
            print("\n[4/6] get magic link...")
            # 优先用 OAuth token 方式（快，不需要浏览器）
            magic_link = await fetch_claude_magic_link(
                context,
                account,
                max_wait=60,
                received_after=magic_requested_at,
                account_lease=account_lease,
            )

            # 如果没收到，重新发一次
            if not magic_link:
                print("  magic link not received, resending...")
                # 回到 Claude 登录页重新发
                resend_requested_at = time.time()
                try:
                    await page.goto(CLAUDE_LOGIN_URL, timeout=30000)
                    await asyncio.sleep(5)
                    await solve_turnstile(page, max_wait=30)
                    await human_type(page, 'input[type="email"], input[name="email"], input[id="email"]', email)
                    await asyncio.sleep(1)
                    resend_requested_at = time.time()
                    await click_continue_email(page)
                    print("  resent magic link")
                except Exception as e:
                    print(f"  resend error: {e}")
                await asyncio.sleep(3)
                magic_link = await fetch_claude_magic_link(
                    context,
                    account,
                    max_wait=60,
                    received_after=resend_requested_at,
                    account_lease=account_lease,
                )

            if not magic_link:
                if account.provider == "NINEMALL":
                    raise NineMallMailboxError("magic_link_timeout")
                raise RuntimeError("magic link timeout, no email received")

            print("  magic link found")
            # open magic link
            await navigate_to_claude_magic_link(page, magic_link, timeout=60000)
            await asyncio.sleep(5)
            log_safe_page_origin("  URL origin: ", page.url)

            check_timeout()

            # registration form
            print("\n[5/6] fill registration form")
            log_safe_page_origin("  URL origin: ", page.url)
            await asyncio.sleep(2)

            first_name, last_name = generate_name()
            birth_year, birth_month, birth_day = generate_birthday()
            print(f"  name: {first_name} {last_name}")
            print(f"  birthday: {birth_year}-{birth_month:02d}-{birth_day:02d}")

            # check if birthday page first
            is_birthday = await handle_birthday_page(page, birth_year, birth_month, birth_day)
            if is_birthday:
                submit_btn = page.locator(
                    'button:has-text("Continue"), button:has-text("Submit"), '
                    'button:has-text("Next"), button[type="submit"]'
                ).first
                try:
                    await submit_btn.click(timeout=8000)
                    print("  birthday submitted")
                except Exception:
                    print("  birthday submit button not clickable, skip")
                await asyncio.sleep(3)
                log_safe_page_origin("  URL origin: ", page.url)
            check_timeout()

            # detect and fill form fields
            form_fields = await page.evaluate("""
                () => {
                    const fields = [];
                    document.querySelectorAll('input:not([type="hidden"]):not([type="checkbox"]), select').forEach(el => {
                        if (el.offsetParent !== null) {
                            fields.push({
                                tag: el.tagName.toLowerCase(),
                                type: el.type || '', name: el.name || '',
                                placeholder: el.placeholder || '', id: el.id || '',
                                label: el.closest('label')?.textContent?.trim() ||
                                       document.querySelector('label[for="' + el.id + '"]')?.textContent?.trim() || ''
                            });
                        }
                    });
                    return fields;
                }
            """)

            print(f"  {len(form_fields)} fields:")
            for f in form_fields:
                desc = f['label'] or f['placeholder'] or f['name'] or f['type'] or f['tag']
                print(f"    - {desc} ({f['tag']})")

            for f in form_fields:
                desc = f['label'] or f['placeholder'] or f['name'] or f['type'] or f['tag']
                field_type = f['type']
                field_tag = f['tag']
                field_name = (f['name'] + f['placeholder'] + f['label'] + f['id']).lower()
                selector_parts = []
                tag_prefix = field_tag
                if f['id']:
                    selector_parts.append(f'{tag_prefix}#{f["id"]}')
                if f['name']:
                    selector_parts.append(f'{tag_prefix}[name="{f["name"]}"]')
                if f['placeholder'] and field_tag == 'input':
                    selector_parts.append(f'input[placeholder="{f["placeholder"]}"]')
                selector = ', '.join(selector_parts) if selector_parts else f'{tag_prefix}[type="{field_type}"]'

                if field_type == 'tel':
                    continue
                if field_tag == 'select':
                    continue

                current_val = await page.locator(selector).first.input_value()
                if current_val:
                    print(f"  [{desc}] has value: {current_val}, skip")
                    continue

                auto_value = None
                if any(k in field_name for k in ['first', 'given', 'fname']):
                    auto_value = first_name
                elif any(k in field_name for k in ['last', 'family', 'surname', 'lname']):
                    auto_value = last_name
                elif any(k in field_name for k in ['name', 'user', 'display', 'nick']):
                    auto_value = f"{first_name} {last_name}"

                if auto_value:
                    await human_type(page, selector, auto_value)
                    print(f"  [{desc}] = {auto_value}")
                else:
                    print(f"  [{desc}] unknown field, skipping")

            # checkboxes
            cb_clicked = await page.evaluate("""
                () => {
                    let clicked = 0;
                    document.querySelectorAll('input[type="checkbox"]').forEach(el => {
                        if (!el.checked && el.offsetParent !== null) { el.click(); clicked++; }
                    });
                    document.querySelectorAll('[role="checkbox"]').forEach(el => {
                        if (el.getAttribute('aria-checked') !== 'true' && el.offsetParent !== null) { el.click(); clicked++; }
                    });
                    document.querySelectorAll('button[data-state="unchecked"], div[data-state="unchecked"]').forEach(el => {
                        if (el.offsetParent !== null) { el.click(); clicked++; }
                    });
                    return clicked;
                }
            """)
            if cb_clicked > 0:
                print(f"  checked {cb_clicked} checkboxes")

            # submit（magic-link 有时直接落 /new 已登录态、无注册表单/提交按钮 —— 容错跳过，
            # 别用默认 30s 阻塞 click 崩掉，否则已登录的号反而存不到 cookie）
            submit_btn = page.locator(
                'button:has-text("Create"), button:has-text("Continue"), '
                'button:has-text("Submit"), button:has-text("Next"), button[type="submit"]'
            ).first
            try:
                await submit_btn.click(timeout=8000)
                print("  submitted")
            except Exception:
                print("  no submit button (likely already logged in), skip form submit")
            await asyncio.sleep(3)
            log_safe_page_origin("  URL origin: ", page.url)
            check_timeout()

            # check for birthday page after submit
            is_birthday_after = await handle_birthday_page(page, birth_year, birth_month, birth_day)
            if is_birthday_after:
                submit_btn = page.locator(
                    'button:has-text("Continue"), button:has-text("Submit"), '
                    'button:has-text("Next"), button[type="submit"]'
                ).first
                try:
                    await submit_btn.click(timeout=8000)
                    print("  birthday submitted")
                except Exception:
                    print("  birthday(after) submit button not clickable, skip")
                await asyncio.sleep(3)
                log_safe_page_origin("  URL origin: ", page.url)

            # detect if phone verification needed
            await asyncio.sleep(2)

            page_text = await page.evaluate("() => document.body.innerText.toLowerCase()")
            # re-check page after short wait
            await asyncio.sleep(3)
            page_text = await page.evaluate("() => document.body.innerText.toLowerCase()")
            current_url = page.url
            needs_phone = (
                'phone' in page_text
                or 'verify' in page_text
                or 'verification' in page_text
                or 'sms' in page_text
                or 'enter your phone' in page_text
                or await page.locator('input[type="tel"]').count() > 0
            )
            is_logged_in = any(k in current_url for k in ['/chat', '/new', '/settings'])
            print(f"  needs_phone={needs_phone}, logged_in={is_logged_in}")
            print(f"  page preview: {page_text[:100]}")

            if not needs_phone:
                print("\n  no phone needed, skipping to onboarding!")
            elif is_logged_in:
                print("\n  already logged in, no phone needed!")
            else:
                # phone verification with auto-retry
                print("\n[6/6] phone verification")
                MAX_PHONE_ATTEMPTS = 2
                phone_verified = False

                for attempt in range(1, MAX_PHONE_ATTEMPTS + 1):
                    pkey = None
                    print(f"\n  --- attempt {attempt}/{MAX_PHONE_ATTEMPTS} ---")
                    check_timeout()
                    try:
                        phone, country_code, pkey = await asyncio.get_event_loop().run_in_executor(
                            None, get_phone_number
                        )
                        full_phone = f"+{country_code}{phone}"
                        phone_selector = 'input[type="tel"], input[name="phone"], input[name="phoneNumber"]'
                        await human_type(page, phone_selector, full_phone)
                        print(f"  filled: {full_phone}")
                        await asyncio.sleep(1)

                        send_btn = page.locator(
                            'button:has-text("Send"), button[type="submit"]'
                        ).first
                        await send_btn.click()
                        print("  clicked send")
                        await asyncio.sleep(3)

                        # check error
                        error_text = await page.evaluate("""
                            () => {
                                for (const sel of ['[role="alert"]', '.error', '[data-testid="error"]',
                                    '.text-red-500', 'div[class*="error"]', 'span[class*="error"]']) {
                                    const el = document.querySelector(sel);
                                    if (el && el.offsetParent !== null && el.textContent.trim())
                                        return el.textContent.trim();
                                }
                                return '';
                            }
                        """)
                        if error_text:
                            print(f"  error: {error_text}")
                            release_phone(pkey)
                            pkey = None
                            if attempt < MAX_PHONE_ATTEMPTS:
                                try:
                                    await page.locator('button:has-text("Try again"), a:has-text("Try again")').first.click(timeout=5000)
                                    print("  clicked Try again")
                                    await asyncio.sleep(2)
                                except Exception:
                                    pass
                            continue

                        # wait for sms code
                        sms_code = await asyncio.get_event_loop().run_in_executor(
                            None, get_sms_code, pkey, 120, 5
                        )
                        if not sms_code:
                            print("  sms timeout!")
                            release_phone(pkey)
                            pkey = None
                            if attempt < MAX_PHONE_ATTEMPTS:
                                try:
                                    await page.locator('button:has-text("Try again"), a:has-text("Try again")').first.click(timeout=5000)
                                    print("  clicked Try again")
                                    await asyncio.sleep(2)
                                except Exception:
                                    pass
                            continue

                        # enter code
                        code_selector = 'input[type="text"], input[name="code"], input[type="number"]'
                        await human_type(page, code_selector, sms_code.strip())
                        await asyncio.sleep(1)
                        await page.locator('button:has-text("Verify"), button[type="submit"]').first.click()
                        print("  code submitted")
                        await asyncio.sleep(3)

                        # check post-verify error
                        post_error = await page.evaluate("""
                            () => {
                                for (const sel of ['[role="alert"]', '.error', '[data-testid="error"]',
                                    '.text-red-500', 'div[class*="error"]', 'span[class*="error"]']) {
                                    const el = document.querySelector(sel);
                                    if (el && el.offsetParent !== null && el.textContent.trim())
                                        return el.textContent.trim();
                                }
                                return '';
                            }
                        """)
                        if post_error:
                            print(f"  verify error: {post_error}")
                            release_phone(pkey)
                            pkey = None
                            if attempt < MAX_PHONE_ATTEMPTS:
                                try:
                                    await page.locator('button:has-text("Try again"), a:has-text("Try again")').first.click(timeout=5000)
                                    print("  clicked Try again")
                                    await asyncio.sleep(2)
                                except Exception:
                                    pass
                            continue

                        phone_verified = True
                        print("  phone verified!")
                        break

                    except TimeoutError:
                        raise
                    except Exception as e:
                        print(f"  exception: {e}")
                        if pkey:
                            release_phone(pkey)
                        if attempt < MAX_PHONE_ATTEMPTS:
                            print("  retrying with new number...")
                        continue

                if not phone_verified:
                    print(f"\n  {MAX_PHONE_ATTEMPTS} attempts failed, aborting.")
                    mark_claude_account_error(
                        account, account_store, "phone_verify_failed"
                    )
                    return None

                await asyncio.sleep(3)

            # handle onboarding pages — 如果 session 丢失则重新登录重试
            MAX_ONBOARDING_RETRIES = 2
            for onboard_try in range(MAX_ONBOARDING_RETRIES + 1):
                onboard_result = await handle_onboarding(page, first_name, last_name)

                # 等待进入聊天页面
                await asyncio.sleep(5)
                log_safe_page_origin("  URL origin: ", page.url)

                from urllib.parse import urlparse as _urlparse
                url_path = _urlparse(page.url).path
                if '/chat' in url_path or '/new' in url_path:
                    break  # 成功进入聊天页
                # 可能还在 onboarding 但 cookie 已有效，也算成功
                if onboard_result is True and '/onboarding' in url_path:
                    break

                if onboard_result == "session_lost" or '/login' in url_path:
                    if onboard_try >= MAX_ONBOARDING_RETRIES:
                        print(f"  session lost {MAX_ONBOARDING_RETRIES + 1} times, giving up")
                        break
                    print(f"\n  session lost during onboarding, re-login attempt {onboard_try + 1}...")
                    try:
                        # 重新登录
                        await page.goto(CLAUDE_LOGIN_URL, timeout=30000)
                        await asyncio.sleep(5)
                        await solve_turnstile(page, max_wait=30)
                        print(f"  re-login email: {email}")
                        await human_type(page, 'input[type="email"], input[name="email"], input[id="email"]', email)
                        await asyncio.sleep(1)
                        relogin_requested_at = time.time()
                        await click_continue_email(page)
                        print("  clicked continue")

                        # 获取新 magic link（等几秒让新邮件到达，避免读到旧的）
                        print("  getting new magic link (waiting 10s for new email)...")
                        await asyncio.sleep(10)
                        re_magic = await fetch_claude_magic_link(
                            context,
                            account,
                            max_wait=60,
                            received_after=relogin_requested_at,
                            account_lease=account_lease,
                        )
                        if not re_magic:
                            print("  re-login: magic link not received")
                            break
                        print("  re-login magic link found")
                        await navigate_to_claude_magic_link(
                            page, re_magic, timeout=30000
                        )
                        await asyncio.sleep(5)
                        log_safe_page_origin("  re-login URL origin: ", page.url)

                        # 如果需要手机验证
                        try:
                            page_text = await page.evaluate("() => document.body.innerText")
                            if 'verify your phone' in page_text.lower() or 'verification code' in page_text.lower():
                                print("  re-login: phone verification needed")
                                phone_result = await _get_and_verify_phone(page)
                                if not phone_result:
                                    print("  re-login: phone verification failed")
                                    break
                                print("  re-login: phone verified!")
                                await asyncio.sleep(3)
                        except Exception:
                            pass
                        continue  # 重试 onboarding
                    except Exception:
                        print("  re-login error: registration_recovery_failed")
                        break
                else:
                    break  # 其他失败，不重试

            # 最终检查
            url_path = _urlparse(page.url).path
            if not ('/chat' in url_path or '/new' in url_path):
                # 尝试直接从当前浏览器 cookie 读取 sessionKey
                try:
                    cookies = await context.cookies()
                    sk_cookie = next((c["value"] for c in cookies if c["name"] == "sessionKey"), None)
                    if sk_cookie:
                        print("  found sessionKey in cookies")
                        session_key = sk_cookie
                        await save_cookies(context, profile_id, email=email, email_password=email_password)
                        mark_claude_account_used(account, account_store)
                        return session_key
                except Exception as e:
                    print(f"  cookie read error: {e}")

                print("  ERROR: not on chat page, not saving cookies")
                mark_claude_account_error(
                    account, account_store, "onboarding_stuck"
                )
                return None

            # 直接保存 cookie
            await asyncio.sleep(2)
            session_key = await save_cookies(context, profile_id, email=email, email_password=email_password)
            if session_key:
                mark_claude_account_used(account, account_store)
            else:
                print("  no sessionKey in cookies")
                mark_claude_account_error(
                    account, account_store, "no_session_key"
                )

    except TimeoutError as e:
        print(f"\n  TIMEOUT: {e}")
        if account is not None:
            mark_claude_account_error(account, account_store, "timeout")
    except Exception as e:
        if account is not None and account.provider == "NINEMALL":
            reason = (
                e.code
                if isinstance(e, NineMallMailboxError)
                else "registration_error"
            )
            print(f"\n  ERROR: {reason}")
            mark_claude_account_error(account, account_store, reason)
        else:
            print(f"\n  ERROR: {e}")
            import traceback
            traceback.print_exc()
            if account is not None:
                mark_claude_account_error(
                    account, account_store, str(e)[:100]
                )
    finally:
        try:
            bb.close_browser(profile_id)
            print("  browser closed")
        except Exception:
            pass
        await asyncio.sleep(3)  # 等浏览器进程完全退出
        try:
            bb.delete_browser(profile_id)
        except Exception:
            pass

    return session_key


async def main():
    parser = argparse.ArgumentParser(description="Claude.ai Auto Register")
    parser.add_argument("--count", "-n", type=int, default=1, help="number of accounts to register")
    parser.add_argument("--timeout", "-t", type=int, default=480, help="timeout per registration (seconds)")
    parser.add_argument("--concurrency", "-c", type=int, default=1, help="number of concurrent registrations")
    parser.add_argument("--emails", "-e", type=str, help="provider account file override")
    parser.add_argument("--email", type=str, help="single fixed email for debug")
    parser.add_argument("--password", type=str, default="", help="password for --email")
    parser.add_argument("--token", type=str, default="", help="refresh token for --email")
    parser.add_argument("--client-id", type=str, default="", help="client ID for --email")
    parser.add_argument("--node", type=str, default="none",
                        help="Clash 出口节点绕 claude 区域封锁：none=不走代理 / auto=自动探测 / 具体节点名")
    parser.add_argument("--proxy-port", type=str, default="7897", help="Clash mixed-port 代理端口")
    args = parser.parse_args()

    global REGISTER_TIMEOUT, CLAUDE_PROXY_NODE, CLAUDE_PROXY_PORT
    REGISTER_TIMEOUT = args.timeout
    CLAUDE_PROXY_PORT = args.proxy_port

    inherited_lease = lease_from_env()
    ipmart_settings = settings_from_env()
    prepare_claude_network(
        os.environ,
        account_lease=inherited_lease,
        ipmart_enabled=ipmart_settings.enabled,
    )
    configure_claude_proxy(
        args.node,
        inherited_lease,
        ipmart_enabled=ipmart_settings.enabled,
    )

    print("=" * 50)
    print("  Claude.ai Auto Register")
    print(f"  count={args.count}  concurrency={args.concurrency}  timeout={args.timeout}s")
    print("=" * 50)

    # 读取邮箱文件
    accounts, account_store = prepare_email_accounts(args)
    if EMAIL_PROVIDER == "NINEMALL" and len(accounts) < args.count:
        print(
            f"  NINEMALL accounts available: {len(accounts)}/{args.count}; "
            "running available accounts only"
        )
    if EMAIL_PROVIDER == "NINEMALL" and not accounts:
        print("  no NINEMALL accounts available")
        return
    if args.email:
        print(f"  using fixed email: {accounts[0].email}")
    elif args.emails:
        print(f"  loaded {len(accounts)} emails from {args.emails}")

    bb = BitBrowser()
    results = []
    results_lock = asyncio.Lock()
    sem = asyncio.Semaphore(args.concurrency)

    # 确定总数：有邮箱文件用文件数量，否则用 --count
    total = len(accounts)

    async def run_one(i):
        async with sem:
            # 错开启动时间，避免并发任务同时访问同一站点
            if i > 1:
                await asyncio.sleep(random.uniform(2, 8) * (i - 1))
            print(f"\n{'#' * 50}")
            print(f"  #{i}/{total}")
            print(f"{'#' * 50}")

            account = accounts[i - 1]
            if account is not None:
                print(f"\n  email from file: {account.email}")

            account_lease = inherited_lease
            if account_lease is None and ipmart_settings.enabled:
                try:
                    account_lease = await asyncio.to_thread(acquire_proxy)
                    print(
                        "  [proxy] IPMart proxy acquired: "
                        f"{account_lease.host}:{account_lease.port} "
                        f"exit={account_lease.exit_ip}"
                    )
                except IPMartProxyError as exc:
                    if account is not None and account.provider == "NINEMALL":
                        print("  FATAL: IPMart proxy unavailable")
                        mark_claude_account_error(
                            account, account_store, "registration_error"
                        )
                    else:
                        print(f"  FATAL: IPMart proxy unavailable: {exc}")
                    async with results_lock:
                        results.append({
                            "index": i,
                            "profile": "-",
                            "status": "ERROR",
                            "sk": None,
                        })
                    return

            ts = datetime.now().strftime("%m%d_%H%M%S") + f"_{i}"
            name = f"claude_{ts}"
            print(f"\n  create browser: {name}")
            profile_id = None
            for _retry in range(3):
                try:
                    profile_id = create_claude_profile(
                        bb, name, account_lease
                    )
                    break
                except Exception as e:
                    err_msg = str(e)
                    safe_category = (
                        e.category
                        if isinstance(e, IPMartBitBrowserError)
                        else None
                    )
                    if safe_category == "quota" or (
                        safe_category is None
                        and (
                            '最大创建窗口数' in err_msg
                            or '超过' in err_msg
                        )
                    ):
                        if safe_category is None:
                            print(f"\n  窗口数量已满，自动清理...")
                        else:
                            print(f"\n  {e}; window quota, cleaning up...")
                        bb.cleanup_browsers(keep=0)
                        continue
                    elif safe_category == "transient" or (
                        safe_category is None
                        and (
                            'TLS' in err_msg
                            or 'socket' in err_msg
                            or 'ECONNRESET' in err_msg
                            or 'network' in err_msg.lower()
                            or 'Timeout' in err_msg
                            or 'timeout' in err_msg
                        )
                    ):
                        print(f"  create browser network error (retry {_retry+1}/3): {err_msg[:80]}")
                        await asyncio.sleep(5)
                        continue
                    else:
                        if account is not None and account.provider == "NINEMALL":
                            print("  FATAL: create browser failed")
                            mark_claude_account_error(
                                account, account_store, "registration_error"
                            )
                            async with results_lock:
                                results.append({
                                    "index": i,
                                    "profile": name,
                                    "status": "ERROR",
                                    "sk": None,
                                })
                            return
                        raise
            if not profile_id:
                print(f"  FATAL: create browser failed after 3 retries")
                if account is not None and account.provider == "NINEMALL":
                    mark_claude_account_error(
                        account, account_store, "registration_error"
                    )
                async with results_lock:
                    results.append({"index": i, "profile": name, "status": "ERROR", "sk": None})
                return
            # 走 Clash 节点：更新窗口为 http 代理（绕 claude 区域封锁）
            if account_lease is None and CLAUDE_PROXY_NODE:
                try:
                    bb._post("/browser/update", {
                        "id": profile_id, "name": name, "proxyMethod": 2, "proxyType": "http",
                        "host": CLAUDE_PROXY_HOST, "port": CLAUDE_PROXY_PORT,
                        "browserFingerPrint": {"coreVersion": "130"},
                    })
                    print(f"  [proxy] window via {CLAUDE_PROXY_HOST}:{CLAUDE_PROXY_PORT} (node={CLAUDE_PROXY_NODE})")
                except Exception as e:
                    print(f"  [proxy] window update failed: {e}")
            try:
                sk = await register(
                    profile_id,
                    account=account,
                    account_store=account_store,
                    account_lease=account_lease,
                )
                async with results_lock:
                    results.append({"index": i, "profile": name, "status": "OK" if sk else "FAIL", "sk": sk})
            except Exception as e:
                if account is not None and account.provider == "NINEMALL":
                    print("  FATAL: registration_error")
                    mark_claude_account_error(
                        account, account_store, "registration_error"
                    )
                else:
                    print(f"  FATAL: {e}")
                async with results_lock:
                    results.append({"index": i, "profile": name, "status": "ERROR", "sk": None})

    await asyncio.gather(*[run_one(i) for i in range(1, total + 1)])

    # summary
    print(f"\n{'=' * 50}")
    print(f"  RESULTS: {len(results)} total")
    print(f"{'=' * 50}")
    ok = 0
    for r in results:
        tag = "OK" if r["sk"] else "FAIL"
        print(f"  #{r['index']} [{tag}] {r['profile']}")
        if r["sk"]:
            ok += 1
    print(f"\n  success: {ok}/{len(results)}")

    # 注册完成后自动验证所有新保存的 sessionKey
    if ok > 0:
        accounts_file = os.path.join(COOKIE_OUTPUT_DIR, "accounts.txt")
        if os.path.exists(accounts_file) and os.path.getsize(accounts_file) > 0:
            print(f"\n{'=' * 50}")
            print(f"  AUTO VALIDATE: running validate_keys.py on {accounts_file}")
            print(f"{'=' * 50}")
            import subprocess
            subprocess.run([sys.executable, "validate_keys.py", accounts_file], cwd=os.path.dirname(os.path.abspath(__file__)))


if __name__ == "__main__":
    asyncio.run(main())
