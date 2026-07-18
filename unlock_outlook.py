# -*- coding: utf-8 -*-
"""
Outlook Account Batch Unlock Script
Uses BitBrowser + Playwright — reuses the same PX press-and-hold logic as registration.

Usage:
  python unlock_outlook.py --input outlook_accounts/accounts_xxx.txt
  python unlock_outlook.py --input emails_locked.txt --concurrency 2
  python unlock_outlook.py --input outlook_accounts/accounts_xxx.txt --proxy-file proxies.txt
  python unlock_outlook.py   (auto-finds latest locked file)

Input file format (---- separated, one per line):
  email----password
  email----password----any_extra_fields...

Output (unlock_results/):
  unlocked_*.txt          successfully unlocked
  needs_phone_*.txt       requires SMS — cannot auto-unlock
  failed_*.txt            failed / timeout
"""

import argparse, asyncio, os, random, re, sys, time
from datetime import datetime

# 顶部加载 .env（真实环境变量优先），保持仓库内无明文凭据
try:
    from config import EZCAPTCHA_API_KEY as _EZCAPTCHA_KEY, EZCAPTCHA_API_BASE as _EZCAPTCHA_BASE
except Exception:
    _EZCAPTCHA_KEY = os.environ.get("EZCAPTCHA_API_KEY", "")
    _EZCAPTCHA_BASE = os.environ.get("EZCAPTCHA_API_BASE", "https://api.ez-captcha.com")

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")

import requests
from playwright.async_api import async_playwright

# ── Config ───────────────────────────────────────────────────────────
BITBROWSER_API  = os.environ.get("BITBROWSER_API", "http://127.0.0.1:54345")
EZCAPTCHA_KEY   = _EZCAPTCHA_KEY
EZCAPTCHA_BASE  = _EZCAPTCHA_BASE
OUTPUT_DIR      = "unlock_results"
SCREENSHOT_DIR  = "screenshots_unlock"
UNLOCK_TIMEOUT  = 300   # seconds per account

DEFAULT_PROXIES = [
    "tiantian1_custom_zone_US_sid_61816963_time_5:Zhq249161@us.ipwo.net:7878",
    "tiantian1_custom_zone_US_sid_81769847_time_5:Zhq249161@us.ipwo.net:7878",
    "tiantian1_custom_zone_US_sid_68657662_time_5:Zhq249161@us.ipwo.net:7878",
    "tiantian1_custom_zone_US_sid_71333778_time_5:Zhq249161@us.ipwo.net:7878",
    "tiantian1_custom_zone_US_sid_29976524_time_5:Zhq249161@us.ipwo.net:7878",
]


# ── BitBrowser ───────────────────────────────────────────────────────
_BROWSER_CLIENT = None


def _fingerprint_provider():
    return (
        os.environ.get("FINGERPRINT_BROWSER")
        or os.environ.get("BROWSER_PROVIDER")
        or "bitbrowser"
    ).strip().lower()


def _bb_post(path, data=None):
    global _BROWSER_CLIENT
    provider = _fingerprint_provider()
    if provider not in {"bitbrowser", "bit", "bb"}:
        if _BROWSER_CLIENT is None:
            from bitbrowser import BitBrowser
            _BROWSER_CLIENT = BitBrowser()
        return _BROWSER_CLIENT._post(path, data or {})
    r = requests.post(f"{BITBROWSER_API}{path}", json=data or {}, timeout=120)
    r.raise_for_status()
    res = r.json()
    if not res.get("success"):
        raise Exception(f"BitBrowser: {res.get('msg', '?')}")
    return res

def _parse_proxy(s):
    if not s: return None
    pt = "http"
    for pfx in ["socks5://", "http://", "https://"]:
        if s.lower().startswith(pfx):
            pt = pfx.split("://")[0]; s = s[len(pfx):]
    s = s.replace(",", "@", 1) if "@" not in s and "," in s else s
    m = re.match(r'^(.+):(.+)@(.+):(\d+)$', s)
    if m:
        return {"type": pt, "username": m.group(1), "password": m.group(2),
                "host": m.group(3), "port": m.group(4)}
    m2 = re.match(r'^(.+):(\d+)$', s)
    if m2:
        return {"type": pt, "host": m2.group(1), "port": m2.group(2)}
    return None

def create_browser(name="unlock", proxy_str=None):
    data = {"name": name, "remark": "outlook unlock",
            "proxyMethod": 2, "browserFingerPrint": {"coreVersion": "130"}}
    p = _parse_proxy(proxy_str)
    if p:
        data.update({"proxyType": p.get("type", "http"),
                     "host": p["host"], "port": p["port"]})
        if p.get("username"): data["proxyUserName"] = p["username"]
        if p.get("password"): data["proxyPassword"] = p["password"]
    else:
        data["proxyType"] = "noproxy"
    return _bb_post("/browser/update", data)["data"]["id"]

def open_browser(pid):
    d = _bb_post("/browser/open", {"id": pid})["data"]
    return d.get("ws") or d.get("webdriver")

def close_browser(pid):
    try: _bb_post("/browser/close", {"id": pid})
    except Exception: pass

def delete_browser(pid):
    try: _bb_post("/browser/delete", {"id": pid})
    except Exception: pass

def cleanup_stale_browsers():
    """启动时清理所有残留的 unlock/scan profile"""
    try:
        page, cleaned = 0, 0
        while True:
            r = _bb_post("/browser/list", {"page": page, "pageSize": 100})
            items = r.get("data", {}).get("list", [])
            if not items: break
            for item in items:
                name = item.get("name", "")
                if any(name.startswith(p) for p in ["unlock_", "scan_", "quick_check"]):
                    close_browser(item["id"])
                    delete_browser(item["id"])
                    cleaned += 1
            total = r.get("data", {}).get("totalNum", 0)
            page += 1
            if page * 100 >= total: break
        if cleaned:
            print(f"[startup] cleaned {cleaned} stale browser profiles")
    except Exception as e:
        print(f"[startup] cleanup error: {e}")


# ── EZCaptcha PX ────────────────────────────────────────────────────
def solve_px(page_url, app_id="PXzC5j78di", max_wait=90):
    try:
        resp = requests.post(f"{EZCAPTCHA_BASE}/createTask", json={
            "clientKey": EZCAPTCHA_KEY,
            "task": {"type": "PerimeterX", "websiteURL": page_url, "websiteKey": app_id}
        }, timeout=30)
        d = resp.json()
        if d.get("errorId", 1) != 0:
            print(f"    [px] error: {d.get('errorDescription', d)}")
            return None
        tid = d["taskId"]
        print(f"    [px] task {tid}")
        start = time.time()
        while time.time() - start < max_wait:
            time.sleep(5)
            r2 = requests.post(f"{EZCAPTCHA_BASE}/getTaskResult",
                               json={"clientKey": EZCAPTCHA_KEY, "taskId": tid},
                               timeout=30).json()
            if r2.get("status") == "ready":
                sol = r2.get("solution", {})
                print(f"    [px] solved! keys={list(sol.keys())}")
                return sol
            if r2.get("status") == "failed":
                print("    [px] failed"); return None
        print("    [px] timeout"); return None
    except Exception as e:
        print(f"    [px] error: {e}"); return None


# ── Page state classifier ────────────────────────────────────────────
def classify(text, url):
    t, u = text.lower(), url.lower()
    if "account.microsoft.com" in u and "unlock" not in u: return "logged_in"
    if "account.live.com" in u and "proofs" in u:          return "logged_in"
    if "fido/create" in u or "fido/update" in u:           return "fido_setup"
    if "setting up your passkey" in t or "passkey" in t:   return "fido_setup"
    if any(x in t for x in ["your account has been locked", "we've locked",
                              "locked for your protection", "帐户已锁定"]): return "locked"
    if "let's prove you're human" in t or "press and hold" in t: return "px_challenge"
    if any(x in t for x in ["enter the code", "we texted", "we sent", "verification code",
                              "验证码", "短信"]): return "sms_verify"
    if any(x in t for x in ["verify your identity", "unusual activity"]): return "verify_needed"
    if "something went wrong" in t: return "error_page"
    if "chrome-error://" in u:      return "net_error"
    if "enter your password" in t:  return "login_form"
    if any(x in t for x in ["email or phone", "sign in", "enter your email"]): return "email_form"
    return "unknown"

async def snap(page, tag, name):
    path = f"{SCREENSHOT_DIR}/{tag}_{name}.png"
    try: await page.screenshot(path=path)
    except Exception: pass
    url = page.url
    try: text = await page.evaluate("() => document.body.innerText")
    except Exception: text = ""
    state = classify(text, url)
    print(f"    [{name}] {state}  {url[:60]}")
    return state, text


# ── Skip passkey / FIDO setup ────────────────────────────────────────
async def skip_fido(page):
    for sel in ['button:has-text("Cancel")', 'button:has-text("取消")',
                'button:has-text("Skip")',   'button:has-text("Not now")',
                'button:has-text("Maybe later")', 'button:has-text("Do it later")']:
        try:
            btn = page.locator(sel).filter(
                has_not=page.locator('[aria-label="Close"],[data-testid="dismissIcon"]')
            ).first
            if await btn.count() > 0 and await btn.is_visible():
                txt = (await btn.text_content() or "").strip()
                print(f"    skip passkey: '{txt}'")
                await btn.click(timeout=5000)
                return True
        except Exception:
            pass
    try:
        await page.goto("https://account.microsoft.com/", timeout=20000,
                        wait_until="domcontentloaded")
        return True
    except Exception:
        pass
    return False


# ── Press-and-hold (same logic as register_outlook_standalone.py) ────
async def _press_hold(page, box):
    bx, by, bw, bh = box['x'], box['y'], box['width'], box['height']
    cx = bx + bw * random.uniform(0.35, 0.65)
    cy = by + bh * random.uniform(0.40, 0.70)
    sx, sy = random.uniform(200, 800), random.uniform(200, 400)
    await page.mouse.move(sx, sy)
    await asyncio.sleep(random.uniform(0.3, 0.8))
    steps = random.randint(15, 30)
    ctrl_x = (sx + cx) / 2 + random.uniform(-100, 100)
    ctrl_y = (sy + cy) / 2 + random.uniform(-80, 80)
    for step in range(1, steps + 1):
        t = step / steps
        mx = (1-t)**2*sx + 2*(1-t)*t*ctrl_x + t**2*cx + random.uniform(-1.5, 1.5)
        my = (1-t)**2*sy + 2*(1-t)*t*ctrl_y + t**2*cy + random.uniform(-1.5, 1.5)
        await page.mouse.move(mx, my)
        await asyncio.sleep(random.uniform(0.005, 0.025))
    await asyncio.sleep(random.uniform(0.1, 0.3))
    await page.mouse.down()
    hold = random.uniform(8, 18)
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - t0 < hold:
        await page.mouse.move(cx + random.uniform(-0.8, 0.8),
                              cy + random.uniform(-0.8, 0.8))
        await asyncio.sleep(random.uniform(0.08, 0.25))
    await page.mouse.up()
    return hold

async def _find_hold_target(page):
    for sel in ['button:has-text("Press and hold")', 'button:has-text("按住不放")',
                'button:has-text("长按")', '#px-captcha']:
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0:
                box = await btn.bounding_box()
                if box and box['width'] > 30: return box
        except Exception: pass
    try:
        iframes = page.locator('iframe[src*="hsprotect.net"]')
        for i in range(await iframes.count()):
            box = await iframes.nth(i).bounding_box()
            if box and box['width'] > 50 and box['height'] > 30: return box
    except Exception: pass
    for f in page.frames:
        if not f.url or f.url == "about:blank" or f == page.main_frame: continue
        try:
            for sel in ['#px-captcha', 'button[class*="hold"]', 'button']:
                btns = f.locator(sel)
                for bi in range(await btns.count()):
                    box = await btns.nth(bi).bounding_box()
                    if box and box['width'] > 30 and box['height'] > 20: return box
        except Exception: pass
    return None


# ── Core unlock logic ─────────────────────────────────────────────────
async def unlock_account(page, context, email, password, tag):
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    # ── Step 1: Login ────────────────────────────────────────────────
    await page.goto("https://login.live.com/login.srf",
                    timeout=60000, wait_until="domcontentloaded")
    await asyncio.sleep(2)

    net_err_count = 0
    for i in range(20):
        state, _ = await snap(page, tag, f"L{i:02d}")
        if state == "logged_in":  return "already_ok"
        if state == "sms_verify": return "needs_phone"
        if state == "fido_setup":
            await skip_fido(page); await asyncio.sleep(4)
            return "unlocked"
        if state in ("locked", "px_challenge"): break
        if state == "net_error":
            net_err_count += 1
            if net_err_count >= 3: return "failed_net_error"
            await page.goto("https://login.live.com/login.srf",
                            timeout=60000, wait_until="domcontentloaded")
            await asyncio.sleep(3); continue
        if state == "email_form":
            try:
                await page.locator('input[type="email"],input[name="loginfmt"]').first.fill(email, timeout=15000)
                await page.keyboard.press("Enter")
            except Exception as e:
                print(f"    fill(email) error: {e}")
            await asyncio.sleep(3); continue
        if state == "login_form":
            try:
                pwd = page.locator('input[type="password"]').first
                if await pwd.count() > 0 and not await pwd.input_value():
                    await pwd.fill(password, timeout=15000)
                await page.keyboard.press("Enter")
            except Exception as e:
                print(f"    fill(pwd) error: {e}")
            await asyncio.sleep(3); continue
        for sel in ['#idSIButton9', 'button:has-text("Next")',
                    'input[type="submit"]', 'button[type="submit"]']:
            b = page.locator(sel).first
            if await b.count() > 0 and await b.is_visible():
                await b.click(timeout=8000); await asyncio.sleep(2); break
        else:
            await asyncio.sleep(2)

    state, _ = await snap(page, tag, "L_final")
    if state == "logged_in":  return "already_ok"
    if state == "sms_verify": return "needs_phone"
    if state == "fido_setup":
        await skip_fido(page); await asyncio.sleep(4)
        return "unlocked"

    # ── Step 2: PX press-and-hold + unlock flow ──────────────────────
    press_count   = 0
    max_press     = 5
    no_btn_rounds = 0
    px_api_tried  = False
    net_err_count = 0

    for i in range(60):
        state, _ = await snap(page, tag, f"U{i:02d}")

        if state == "logged_in":  return "unlocked"
        if state == "sms_verify": return "needs_phone"
        if state == "fido_setup":
            await skip_fido(page); await asyncio.sleep(4)
            return "unlocked"
        if state == "error_page":
            tried = False
            for sel in ['button:has-text("Try again")', 'button:has-text("重试")',
                        'button:has-text("再试一次")', 'a:has-text("Try again")']:
                b = page.locator(sel).first
                if await b.count() > 0 and await b.is_visible():
                    try: await b.click(timeout=8000)
                    except Exception: pass
                    await asyncio.sleep(5); tried = True; break
            if not tried:
                await page.go_back(); await asyncio.sleep(3)
            continue
        if state == "net_error":
            net_err_count += 1
            if net_err_count >= 5: return "failed_net_error"
            await page.goto("https://login.live.com/login.srf",
                            timeout=60000, wait_until="domcontentloaded")
            await asyncio.sleep(3); continue

        if state == "locked":
            for sel in ['button[type="submit"]', 'button:has-text("Next")',
                        'button:has-text("下一步")', 'input[type="submit"]']:
                b = page.locator(sel).first
                if await b.count() > 0 and await b.is_visible():
                    try: await b.click(timeout=8000)
                    except Exception: pass
                    await asyncio.sleep(6); break
            continue

        if state == "px_challenge":
            if press_count < max_press:
                box = await _find_hold_target(page)
                if box:
                    press_count += 1
                    held = await _press_hold(page, box)
                    print(f"    held {held:.1f}s (#{press_count})")
                    await asyncio.sleep(random.uniform(3, 6))
                    no_btn_rounds = 0
                else:
                    no_btn_rounds += 1
                    print(f"    no hold target (round {no_btn_rounds})")
                    await asyncio.sleep(3)
            elif not px_api_tried:
                px_api_tried = True
                print("    fallback: EZCaptcha PX API...")
                sol = solve_px(page_url=page.url)
                if sol:
                    for key in ['_pxCaptcha', '_px3', '_px2', '_pxhd', '_pxvid', '_pxde']:
                        if key in sol:
                            await context.add_cookies([{
                                "name": key, "value": str(sol[key]),
                                "domain": ".live.com", "path": "/"
                            }])
                    tok = sol.get("token") or sol.get("uuid")
                    if tok:
                        await page.evaluate(f"""() => {{
                            const h = document.querySelector('input[name="_pxCaptcha"]');
                            if (h) h.value = "{tok}";
                        }}""")
                    await page.reload(timeout=15000); await asyncio.sleep(5)
                else:
                    print("    PX API failed — giving up"); break
            else:
                print("    all PX attempts exhausted"); break
            continue

        # Generic next/submit for intermediate steps
        for sel in ['button[type="submit"]', 'button:has-text("Next")',
                    'button:has-text("下一步")', '#idSIButton9', 'input[type="submit"]']:
            b = page.locator(sel).first
            if await b.count() > 0 and await b.is_visible():
                await b.click(timeout=8000); await asyncio.sleep(4); break
        else:
            await asyncio.sleep(3)

    state, _ = await snap(page, tag, "U_final")
    if state == "logged_in":  return "unlocked"
    if state == "sms_verify": return "needs_phone"
    if state == "fido_setup":
        await skip_fido(page); await asyncio.sleep(4)
        return "unlocked"
    return f"failed_{state}"


# ── Worker ────────────────────────────────────────────────────────────
async def worker(accounts, proxy, worker_id, results, sem):
    async with sem:
        for email, password, raw_line in accounts:
            tag = f"w{worker_id}"
            pid = None
            print(f"\n[worker-{worker_id}] {email}")
            try:
                pid = create_browser(f"unlock_{worker_id}", proxy)
                ws  = open_browser(pid)
                if not ws:
                    raise Exception("no WS url from BitBrowser")

                async with async_playwright() as pw:
                    browser = await pw.chromium.connect_over_cdp(ws)
                    ctx  = browser.contexts[0] if browser.contexts else await browser.new_context()
                    page = ctx.pages[0]       if ctx.pages       else await ctx.new_page()

                    outcome = await asyncio.wait_for(
                        unlock_account(page, ctx, email, password, tag),
                        timeout=UNLOCK_TIMEOUT
                    )

                print(f"[worker-{worker_id}] {email} => {outcome}")
                results.append((email, password, raw_line, outcome))

            except asyncio.TimeoutError:
                print(f"[worker-{worker_id}] {email} => timeout")
                results.append((email, password, raw_line, "timeout"))
            except Exception as e:
                print(f"[worker-{worker_id}] {email} => error: {e}")
                results.append((email, password, raw_line, f"error: {str(e)[:80]}"))
            finally:
                if pid:
                    close_browser(pid)
                    delete_browser(pid)


# ── File I/O ──────────────────────────────────────────────────────────
def load_accounts(path):
    accounts = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"): continue
            parts = line.split("----")
            if len(parts) >= 2:
                accounts.append((parts[0].strip(), parts[1].strip(), line))
            else:
                print(f"[warn] skip: {line[:60]}")
    return accounts

def scan_all_accounts():
    """Scan outlook_accounts/ for all registered accounts, skip those already unlocked."""
    reg_dir = "outlook_accounts"
    unlock_dir = "unlock_results"

    # Collect already-unlocked emails
    unlocked_emails = set()
    if os.path.isdir(unlock_dir):
        for uf in os.listdir(unlock_dir):
            if uf.startswith("unlocked_clean_") and uf.endswith(".txt"):
                with open(os.path.join(unlock_dir, uf), "r", encoding="utf-8") as f:
                    for line in f:
                        parts = line.strip().split("----")
                        if parts and parts[0]:
                            unlocked_emails.add(parts[0].lower())

    # Collect all registered accounts, deduplicate by email, skip unlocked
    seen = set()
    accounts = []
    if os.path.isdir(reg_dir):
        for af in sorted(os.listdir(reg_dir)):
            if af.startswith("accounts_") and af.endswith(".txt"):
                with open(os.path.join(reg_dir, af), "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"): continue
                        parts = line.split("----")
                        if len(parts) >= 2:
                            email_lc = parts[0].lower()
                            if email_lc not in seen and email_lc not in unlocked_emails:
                                accounts.append((parts[0].strip(), parts[1].strip(), line))
                                seen.add(email_lc)

    if unlocked_emails:
        print(f"[auto] Skipping {len(unlocked_emails)} already-unlocked accounts")
    print(f"[auto] {len(accounts)} new accounts to unlock from {reg_dir}/")
    return accounts

def load_proxies(path):
    if not path:
        return [None]   # no proxy by default
    if not os.path.exists(path):
        return DEFAULT_PROXIES
    proxies = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                proxies.append(line)
    return proxies or DEFAULT_PROXIES

def save_results(results, ts):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    unlocked   = [r for r in results if r[3] in ("unlocked", "already_ok")]
    needs_ph   = [r for r in results if r[3] == "needs_phone"]
    failed     = [r for r in results if r[3] not in ("unlocked", "already_ok", "needs_phone")]

    def write(name, rows):
        p = os.path.join(OUTPUT_DIR, f"{name}_{ts}.txt")
        with open(p, "w", encoding="utf-8") as f:
            for email, password, raw, outcome in rows:
                f.write(f"{raw}----{outcome}\n")
        print(f"  {name:<22s} {len(rows):4d}  -> {p}")

    print(f"\n{'='*55}")
    write("unlocked", unlocked)
    write("needs_phone", needs_ph)
    write("failed", failed)
    print(f"{'─'*55}")
    print(f"  Total     : {len(results)}")
    print(f"  Unlocked  : {len(unlocked)}")
    print(f"  NeedsPhone: {len(needs_ph)}")
    print(f"  Failed    : {len(failed)}")
    print(f"{'='*55}")

    # Convenience: write just email----password for unlocked accounts
    ok_path = os.path.join(OUTPUT_DIR, f"unlocked_clean_{ts}.txt")
    with open(ok_path, "w", encoding="utf-8") as f:
        for email, password, _, _ in unlocked:
            f.write(f"{email}----{password}\n")
    if unlocked:
        print(f"\n  Clean unlocked list: {ok_path}")


# ── Main ──────────────────────────────────────────────────────────────
def find_latest_input():
    """Auto-find most recent accounts file to unlock."""
    for d, pat in [
        ("check_results",   "locked_for_unlock_"),
        ("outlook_accounts","accounts_"),
    ]:
        if not os.path.isdir(d): continue
        files = sorted(
            [f for f in os.listdir(d) if f.startswith(pat) and f.endswith(".txt")],
            reverse=True
        )
        if files:
            return os.path.join(d, files[0])
    return None

async def run(accounts_or_file, proxies, concurrency):
    if isinstance(accounts_or_file, str):
        accounts = load_accounts(accounts_or_file)
        label = accounts_or_file
    else:
        accounts = accounts_or_file
        label = f"(auto-scanned, {len(accounts)} accounts)"

    if not accounts:
        print("[error] no accounts found"); return

    print(f"Input     : {label}")
    print(f"Accounts  : {len(accounts)}")
    print(f"Concurrency: {concurrency}")
    print(f"Proxies   : {len(proxies)}")

    results = []
    sem     = asyncio.Semaphore(concurrency)
    chunks  = [[] for _ in range(concurrency)]
    for i, acc in enumerate(accounts):
        chunks[i % concurrency].append(acc)

    await asyncio.gather(*[
        worker(chunks[i], proxies[i % len(proxies)], i, results, sem)
        for i in range(concurrency)
        if chunks[i]
    ])

    save_results(results, datetime.now().strftime("%Y%m%d_%H%M%S"))

def main():
    parser = argparse.ArgumentParser(
        description="Batch Outlook Account Unlock",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python unlock_outlook.py --input outlook_accounts/accounts_20260414_124527.txt
  python unlock_outlook.py --input emails_locked.txt --concurrency 2
  python unlock_outlook.py --input emails.txt --proxy-file proxies.txt --concurrency 3
  python unlock_outlook.py                          (auto-scan all accounts, skip unlocked)
""")
    parser.add_argument("--input", "-i", default=None,
        help="Input file (email----password per line). "
             "Auto-scans outlook_accounts/ and skips already-unlocked if omitted.")
    parser.add_argument("--proxy-file", "-p", default=None,
        help="Proxy list file (one per line)")
    parser.add_argument("--concurrency", "-c", type=int, default=1,
        help="Parallel workers (default: 1)")
    args = parser.parse_args()

    if args.input:
        if not os.path.exists(args.input):
            print(f"[error] file not found: {args.input}")
            sys.exit(1)
        accounts_or_file = args.input
    else:
        # Auto-scan all accounts, skip already unlocked
        accounts_or_file = scan_all_accounts()
        if not accounts_or_file:
            print("[info] No new accounts to unlock.")
            sys.exit(0)

    cleanup_stale_browsers()
    proxies = load_proxies(args.proxy_file)
    asyncio.run(run(accounts_or_file, proxies, args.concurrency))

if __name__ == "__main__":
    main()
