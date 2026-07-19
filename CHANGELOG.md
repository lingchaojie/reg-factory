# 更新日志

## 2026-07-19 — NexaCard OTP 服务 WebUI 与本地 API

**新增**
- WebUI 新增 `NexaCard OTP` 配置组、密码遮挡、Google 鉴权/重新鉴权/状态控件、本地健康检查和常驻服务启动入口。
- 新增仅监听本地地址的 `POST /v1/otp` 调用说明；成功响应只返回 `otp`。轮询间隔（x）和最大次数（y）默认分别为 3 秒与 100 次，保存后在下一次请求读取。
- 服务使用原生 Chrome 强制直连，不使用代理或指纹浏览器；Google OAuth 凭据、token 和 Chrome profile 统一放在 Git 忽略的私有目录。

**授权说明**
- Access Token 约一小时后会自动刷新；真正的撤销或 `invalid_grant` 才要求重新鉴权。未由 Google 明确给出的七天 refresh-token 到期时间仅显示为估算值。

---

## 2026-07-19 — Octo Browser provider 与 Clash/直连预检

**新增**
- 新增 Octo Browser 指纹浏览器适配：设置 `FINGERPRINT_BROWSER=octo`（兼容 `octobrowser`、`octo_browser`）即可使用；默认 provider 仍为 BitBrowser。
- Octo 的 profile 创建、更新、列出和删除使用 Public API，profile 启动和停止使用运行中客户端的 Local API。需 Base 或更高套餐 API 权限和主账号 token；规范配置键为 `OCTO_API_TOKEN`、`OCTO_PUBLIC_API_BASE`、`OCTO_LOCAL_API_BASE`，其中 Public 默认值是完整 automation base `https://app.octobrowser.net/api/v2/automation`；旧无 `_BASE` 键仅只读兼容，不含明文凭据。
- WebUI 新增 `OCTO_HEADLESS` 全局复选框，默认关闭；开启后所有共享 Octo provider 启动均传递 `headless: true`。
- `IPMART_ENABLED=0` 时新增 Clash 端口预检：仅在 `CLASH_PROXY` 可达时走既有 Clash 路由，否则清除代理环境并直连；`--proxy ""` 可显式强制直连。

**边界**
- `IPMART_ENABLED=1` 的 Outlook → Graph → mailbox → Claude 链路维持同一 IPMart 租约且 fail-closed；ChatGPT、Grok 保持既有路由行为。
- Octo 适配不实现 one-time profile 模式，使用既有普通 profile 生命周期。

---

## 2026-07-19 — Claude Platform 个人账户注册

- 新增 `register_claude_api.py` 独立入口，以及 `run_full_flow.py` / `register_three_platforms.py` 的 `claude_api` 选择；WebUI 增加“Claude API 注册”卡片并对密码、refresh token、client ID 的字段和命令预览统一脱敏。
- Claude 家族专用流程在启动前选择 NINEMALL，并严格匹配 Anthropic/Claude 新邮件、解析 magic link 与数字验证码两类验证产物；选定后超时也不会回退 OUTLOOK。包含 ChatGPT 或 Grok 的混合流程在启动前直接选择 OUTLOOK，不会先尝试 NINEMALL，因此不是 NINEMALL 失败后的回退。
- Claude Platform 与 Claude 网页号使用独立台账：`mail_used_claude_api.txt`、`mail_error_claude_api.txt`、`emails_used_claude_api.txt`、`emails_error_claude_api.txt`；登录会话保存到 `cookies/claude_api/`。
- 邮箱台账增加跨进程锁、fsync 原子写入与双用途事务回滚；并发进程不会重复取得同一邮箱，也不会在部分写入后留下单边预留。
- Claude Platform 的账号时限覆盖代理获取、profile 创建/打开、CDP 连接与注册状态机；同步浏览器 API 由守护 owner 串行执行，只有确认 close 完成后才允许 delete，未确认清理时不写成功/失败终态。
- 会话 cookie 与索引采用耐中断发布，编排器转发日志会遮蔽完整邮箱；包含 `claude_api` 的子进程失败会传播为非零退出状态。
- 当前范围仅选择 Claude Platform 个人账户：不创建组织，不创建 API Key，也不执行充值、付费或支付操作。

---

## 2026-07-19 — Claude 专用 NINEMALL 邮箱渠道

- Claude 默认使用 NINEMALL 四列账号顺序（`email----password----client_id----refresh_token`）；`EMAIL_PROVIDER=OUTLOOK` 保留旧 Outlook 顺序和兼容流程。
- NINEMALL 通过 AppleEmail HTTPS `POST` 轮询邮件，严格无 Outlook 浏览器回退；纯 Claude 流程使用该渠道，非 Claude 行为保持不变。

---

## 2026-07-18 — IPMart 固定网关 SID 租约迁移

> 本条取代并修正下方同日的 access-key 获取代理方案；历史条目保留用于追溯，旧模式不再受支持。

**迁移**
- IPMart 配置改为控制台固定网关的主机、端口、用户名模板和密码；用户名模板只把控制台所示 SID 数字替换为 `{sid}`，WebUI 对模板和密码均作密钥遮挡且不提供 IPMart 连接测试按钮。
- `IPMART_MAX_ATTEMPTS` 是默认 `3` 的正整数尝试上限，不存在硬编码三次上限；同一个 SID 租约贯穿 Outlook BitBrowser、OAuth/Graph、Graph 邮箱读取与 Claude BitBrowser。
- 正常首候选成功恰好执行两次专用出口检查：租约获取时一次、进入 Claude 前一次。`--dry-run` 不生成 SID、不检查出口，也不创建 profile 或账号。

**网络边界**
- 启用 IPMart 的默认 Outlook → Claude 流程不需要 Clash，也不使用继承的 HTTP 代理；ChatGPT/Grok 不在本次迁移内，保留原行为。
- IPMart 粘性时长由控制台/套餐决定，常见范围为 5-30 分钟，整轮必须在同一 SID 有效期内完成。

---

## 2026-07-18 — IPMart 每账号独立代理接入 BitBrowser

**新增**
- 新增 IPMart HTTP 代理 provider：每轮请求一个美国代理，验证真实出口 IP，失败或出口重复时最多重试 3 次，并用本地 JSONL 台账避免后续账号复用出口。
- `run_full_flow.py` 在 Outlook 注册前获取代理，把同一租约传给 Outlook 与 Claude 的临时 BitBrowser profile；进入 Claude 前复检同一出口，变化时终止本轮。
- `.env.example`、`config.py` 与 WebUI 配置页新增 `IPMART_*` 配置；accessKey 在 WebUI 使用密码框，未提供会消耗分配的连接测试按钮。

**行为边界**
- `IPMART_ENABLED=0` 时保持原 Clash/现有代理逻辑；启用后不回退到 Clash、直连或已有 BitBrowser profile。
- API 获取请求绕过系统代理，使用 IPMart 的来源 IP 白名单鉴权；默认 `US`、单代理、30 分钟粘性。
- `--dry-run` 不请求代理；严格唯一性要求只运行一个顺序编排进程。

**测试**
- 覆盖配置解析、接口参数、代理验活、出口变化、重复出口、密钥脱敏、台账、环境传递及 Outlook/Claude/全流程集成。

---

## 2026-07-14 — Grok 纯 HTTP 协议注册（不开浏览器） + WebUI 接入 + 移除 ruyi 版

**新增**
- **`register_grok_http.py` — Grok 纯 HTTP 协议注册**：集成 [HM2899/grokcli-2api](https://github.com/HM2899/grokcli-2api) 的 `xconsole_client` 协议库（已 vendored 到 repo 根目录）。全程不开浏览器，直连 accounts.x.ai 完成发码 / 验码 / 建号 / 取 sso，成功后落标准 grok sso token 到 `tokens/grok/<email>.sso.json`。
  - 复用本项目现有基建：Clash 节点切换、临时邮箱（`common.temp_email`）、CapSolver 打码、`common.session_export` 落盘。
  - 依赖：能过 Cloudflare 的干净节点 + 可用临时邮箱 provider（`TEMP_EMAIL_PROVIDER`）+ `CAPSOLVER_API_KEY`。
  - 用法：`python register_grok_http.py --count 1`，或 `--node "美国 01"` 指定节点。

**改进**
- **WebUI「Grok 注册」改走 HTTP 协议版**：`webui/scripts.py` 的 `register_grok` 任务指向 `register_grok_http.py`，参数精简为 `--count` / `--node`。
- **三平台 / 端到端编排的 grok 分支改走 HTTP 协议版**：`register_three_platforms.py` 不再调用旧的浏览器版。

**移除**
- **删除 `register_grok_ruyi.py`（ruyiPage/Firefox 版）**：验证码掩码输入框在浏览器内无法稳定通过，已被 HTTP 协议版取代。

**测试**
- HTTP 协议版实测 `success: 1/1`：发码 / 验码 / Turnstile / 建号 / 取 sso 全通，token 正常落盘。

---

## 2026-07-13 — Outlook 按住验证拟人化 + 节点探测轮换 + WebUI 精简

**新增**
- **`common/human_mouse.py` 拟人鼠标模块**（纯 stdlib，无新依赖）：借鉴 [LoseNine/ruyipage](https://github.com/LoseNine/ruyipage) 的 WindMouse / human_move 运动算法（其 Firefox 内核级免检测不可移植，但轨迹/抖动算法可移植）。
  - `windmouse_path()`：重力 + 随机风力 + 速度钳制的逼近轨迹，天然变速（中段快、两端慢）带过冲，取代原来的简单二次贝塞尔。
  - `tremor_offsets()`：用 **Ornstein-Uhlenbeck 过程**生成按住期间的**自相关**微抖动（有动量 + 回中，像真人手的生理震颤）。自检 lag-1 自相关 0.98，对照白噪声 0.02。
  - `human_press_and_hold(page, cx, cy, is_done, max_hold, min_hold)`：完整按住序列——WindMouse 逼近 → 落点停顿 → down → OU 抖动循环（每 ~0.5s 轮询 `is_done()`，进度满后加真人反应延迟再 up）。
  - 自检入口 `python -m common.human_mouse`：校验轨迹连续/精确命中/速度非均匀、抖动自相关高于阈值。
- **Clash 节点「探测优先」轮换**（`outlook_reg_loop.py`）：切节点前先用 Clash `/delay` 探测延迟，**跳过超时节点**，在一批候选里挑延迟最低的再切换，不再把整次 attempt（~3min）浪费在死节点上。可调 `CLASH_MAX_LATENCY_MS`（默认 2500）、`CLASH_PROBE_BATCH`（默认 8）。
- **`--no-rotate` / `OUTLOOK_NO_ROTATE=1` 开关**：固定使用当前节点，不探测/不切换，也不连 Clash 控制器。WebUI 养号面板已同步该开关（及原先漏配的 `--sleep-when-full`）。

**改进**
- **按住验证鼠标运动去机器人特征**：`register_outlook_standalone.py` 原按住期间是纯正弦波漂移（完全周期性）、`register.py` 是 ±2px 均匀随机抖动（白噪声无动量），两者 PerimeterX 行为模型都易判；两处入口均改用 `human_press_and_hold`，复用各自原有的「captcha 消失=已通过」判定作 `is_done` 回调。
- **去掉节点区域亲和（日本优先）**：改为按名称平等轮换 + 探测选优；保留 CN/直连节点排除、会话内去重轮换、IP 变更验证。

**修复**
- **数据确认页误点**：`_maybe_confirm_before_register` 现在先按 body 文本判定是否真出现数据许可/`privacynotice` 页，只有命中才点允许/接受，避免正常表单页误点页脚/cookie 条上的 `OK`/`确定` 链接打乱流程。
- **`TargetClosedError` 崩溃**：按住过程中页面/context 关闭（节点掉线或验证通过后导航销毁上下文）时，fallback 不再对已死页面二次 `mouse.down/up`；识别到 closed/TargetClosed 即标记未过、交外层循环判定。

**移除**
- **WebUI 拿掉 Gmail 注册内嵌页**：清空 `webui/scripts.py` 的 `EMBED_PAGES`（原唯一 Gmail 条目），侧边栏「功能 / 🌐 Gmail 注册」按钮、iframe 视图、接码助手随之隐藏（通用 iframe 基建与 `/api/sms/*` 端点保留备用）。

**测试**
- 拟人按住实测过真 PerimeterX：Outlook 自注册一次跑 5 attempt，2 次成功（`pb74z...` / `hfgcz...`），按住 2~3 次后 `captcha 元素已消失 → passed`，链路走通（注册 → 过验证 → 抽 Graph token → 写池）。
- 节点探测轮换实测：连续跳过 HK 中转 ×3 + SG 死节点（各 ~4s），选中台湾/香港活节点（106~137ms）并确认出口 IP 变更。
- 失败样例（与本次改动无关）：一次 `password input not found`（繁中页/慢节点密码步骤未在 10s 内渲染）、一次 `TargetClosedError`（节点掉线，已由上面的修复覆盖）。

**说明**
- 拟人运动只解决「鼠标行为像不像人」这一可控维度，真实通过率仍受 IP 信誉/会话上下文影响。可调参：`HUMAN_MOUSE_TREMOR_PX`（抖动幅度）、`HUMAN_MOUSE_DEBUG`、`OUTLOOK_REG_MAX_PRESS`。

### 同批提交的 Gmail Android 增量（既有未提交工作，非本次会话作者所写，按代码实况归纳）
- **`gmail_register_local.py` 大幅扩充**（+1400 余行）：新增 ADB accessibility-tree 驱动的注册/手机验证链路（`adb_ui_nodes` / `adb_find_node` / `adb_tap_node` / `adb_fill_node` / `adb_auto_phone_verification` / `adb_complete_post_phone_flow`），Appium 侧姓名/下一步/创建个人账户/手机号录入等步骤函数，注册后**二次登录**（`second_login_flow`）与**手机 2FA**（`enable_phone_2fa`）流程，账号状态断点续跑（`save_account_state` / `load_account_state` / `resume_registration_flow`）、人工接管判定（`manual_handoff_result`），以及 `ensure_appium_server` 自启。
- **`scripts/watch_appium.ps1`**（新增）：单实例 Appium 看门狗，绑定独立 `ANDROID_ADB_SERVER_PORT`，UiAutomator2 崩溃后自动拉起。
- **`gmail_android/tests/test_post_registration.py`**（新增）：注册后流程测试。
- **配置项补全**：根/`gmail_android` 的 `.env.example` 新增 BlueStacks 实例、ADB/Appium 端口、`AUTO_*` 自动化开关、`NODE_*` 节点探测参数、`RECAPTCHA_AUTO_SOLVE`、`SMSMAN_*_GMAIL` 等；`.gitignore` 忽略 `gmail_android/.runstate/` 与 `logs/`。
- ⚠️ 此部分未经本次会话验证，仅据 diff 与函数签名归纳，可能与实际行为有出入。

## 2026-07-11 — Gmail Android 注册优化（reCAPTCHA 自动解 + SMS 国家筛选）

**新增**
- **`gmail_android/recaptcha_android.py` 视觉自动解 reCAPTCHA v2**：通过 ADB accessibility tree 定位 WebView 里的 reCAPTCHA 节点（`recaptcha-anchor` checkbox、`rc-imageselect` 挑战窗口），Appium 点击/截图，调用 `common/agent_captcha.py` 视觉投票识别图块。
  - **WebView 节点等待**：`solve()` 入口加 12s 等待循环，解决"检测到 reCAPTCHA 文字但 accessibility tree 还没暴露节点"的时机问题（之前立即返回 `False`）。
  - **挑战类型自适应**：点 checkbox 后若直接通过（绿勾）则返回 `True`；若弹图片挑战则循环识别提交，最多 `max_rounds` 轮（默认 8）。
  - **二登默认启用**：`RECAPTCHA_AUTO_SOLVE=1` 时自动调用，失败仍回退人工。要求 `VISION_API_KEY` 已配置（否则 `usable()` 返回 `False`）。
- **SMS 国家筛选**（`sms_provider.py` + `config.py`）：
  - **firefox.fun 白名单**：新增 `SMS_COUNTRY_GMAIL`（逗号分隔国家码，如 `"33,44"` = 法国/英国），`_request_firefox_number` 循环传 `country=<code>` 向接码平台请求指定国家号码；空值保持原有"任意国家"行为。
  - **sms-man 多国支持**：`SMSMAN_COUNTRY_GMAIL` 现支持逗号分隔（如 `"155,100"` = 法国 id=155、英国 id=100），`_request_smsman_number` 逐个国家 ID 尝试租号。
  - **三 provider 级联**：`request_number()` 优先 firefox.fun（有库存且过黑名单/白名单）→ sms-man（按配置国家列表）→ hero-sms（兜底）。
- **BlueStacks 自动化**（`bluestacks.py`）：实例启动、ADB 连接、Google 账户清理、Appium UiAutomator2 server 安装的统一封装，支持 `AUTO_PREPARE_EMULATOR=1` 时自动准备干净实例。
- **Clash 节点切换**（`proxy_switch.py`）：mihomo/Clash API 封装，支持节点延迟探测、区域关键词过滤（`NODE_REGION_KEYWORDS`）、Google 连通性探测（`proxy_probe`），配合 `AUTO_SWITCH_NODE=1` 在注册前自动切可用节点。
- **流程协调器**（`coordinator.py`）：封装 Appium session 初始化、模拟器准备、节点切换、SMS provider 配置检查的编排逻辑，供 `gmail_register_local.py` 调用。

**改进**
- **sms-man 优先多次接码号**：`prefer_multi=True` 时优先选 `can_receive_multiple_sms=True` 的号码（Gmail 二登可能再要一次验证码）。
- **配置项补全**：`config.py` 新增 `AUTO_PREPARE_EMULATOR`、`AUTO_START_APPIUM`、`AUTO_SWITCH_NODE`、`AUTO_STOP_EMULATOR`、`KEEP_EMULATOR_ON_MANUAL_HANDOFF`、`BLUESTACKS_INSTANCE`、`BLUESTACKS_ADB_PORT`、`APPIUM_SYSTEM_PORT`、`SECOND_LOGIN_AFTER_SIGNUP`、`ENABLE_2FA_AFTER_LOGIN`、`RECAPTCHA_AUTO_SOLVE`、`RECAPTCHA_SOLVE_ROUNDS` 等，统一从 `.env` 读取。

**测试**
- 实测视觉 reCAPTCHA 解题：点 checkbox 直接过 ✅、3×3 图片挑战（"select all traffic lights"）✅；12s 等待修复后稳定检测到节点。
- 实测 SMS 国家筛选：firefox.fun 法国/英国无库存时回退 sms-man，成功租到 `+447446302327`（英国，multi=True），Google 接受号码（未拒绝"used too many times"），但 sms-man 该号段 180s 内未收到 Google 短信（VoIP 虚拟号限制）。
- 实测默认流程（`SMS_COUNTRY_GMAIL` 空）：firefox.fun 返回菲律宾 +63 号被黑名单过滤，sms-man 返回马来西亚 +60 号被 Google 拒绝（"used too many times"）。

**说明**
- reCAPTCHA 视觉解题需要 `VISION_API_KEY` + `VISION_MODEL`（推荐 `gpt-5.5` 或 `claude-opus-4`），每轮挑战约消耗 1 次视觉 API 调用（截图 + 题干）。
- SMS 国家筛选仅在接码平台有库存且号段未被 Google 风控时有效；虚拟号段（如 sms-man 英国 `+4474xx`）可能收不到 Google 短信，建议测试后再大规模使用。
- 新增模块（`bluestacks.py`、`proxy_switch.py`、`coordinator.py`、`recaptcha_android.py`）为 `gmail_android/` 独立实现，不影响现有 Outlook/ChatGPT 注册流程。

## 2026-07-06 — AdsPower 指纹浏览器适配

**新增**
- 新增 `adspower.py`，把 AdsPower Local API 封装成现有 BitBrowser 兼容接口，支持 profile 创建、启动、关闭、删除、列表，以及旧脚本使用的 `/browser/*` 兼容调用。
- 新增 `FINGERPRINT_BROWSER=bitbrowser|adspower` provider 开关；默认仍为 BitBrowser，设置为 `adspower` 后 `BitBrowser()` 会自动返回 AdsPower 适配器。
- 新增 AdsPower 配置项：`ADSPOWER_API`、`ADSPOWER_API_KEY`、`ADSPOWER_GROUP_ID`。

**适配**
- 通用注册入口、ChatGPT/GitHub/Grok/Codex OAuth、邮箱 broker、Outlook 自注册/解锁等现有浏览器调用路径适配 AdsPower。
- WebUI 配置页新增“指纹浏览器”分组，`FINGERPRINT_BROWSER` 支持下拉切换 BitBrowser / AdsPower；状态灯会按当前 provider 显示 BitBrowser 或 AdsPower。
- README、`.env.example`、安装提示同步为 BitBrowser / AdsPower 双 provider 说明。

**测试**
- 已验证 AdsPower `/status` 连通、provider 工厂切换、WebUI 后端导入、`run_full_flow.py --platforms chatgpt --codex --rounds 100 --import-c2a --dry-run` 编排。
- 真实创建 AdsPower profile 需要填写 `ADSPOWER_API_KEY`；未填写时 AdsPower 返回 `Require api-key`，属于本地配置缺失。

## 2026-06-25 — sms-man 接码过 Codex add-phone + 全自动 OAuth 链路（移除订阅模块）

**新增**
- **`common/sms.py` 接入 sms-man.com（API v2.0）为主用接码平台**：按 pkey 前缀路由（`smsman_<id>` → sms-man，`hero_<id>` → hero-sms，否则 firefox.fun），优先级 **sms-man → firefox.fun → hero-sms**。
  - **OpenAI 服务自动解析**：`_smsman_resolve_app` 按名称（"openai"）在 `/applications` 子串匹配出 application_id（OpenAI/ChatGPT = **2754**），免硬编码；适配 sms-man 返回 **dict-keyed-by-id** 且字段为 `title`（非 list/name）的实际格式。
  - **按便宜的排序**：`_smsman_rank_countries` 经 `/get-prices` 价格升序，`_smsman_get_phone` 最便宜国家优先逐个试租。
  - **账号级错误快速失败**：余额不足 / token 失效返回 `FATAL` 立即中止，不再空刷 170+ 国家。
  - CLI 辅助：`python -m common.sms applications|countries|balance|prices openai`。
- **`common/oauth_codex.py` add-phone 全自动接码**：遇 OpenAI add-phone 自动填号 + 接 SMS 验证码过号。
  - **WhatsApp→SMS 修正**：OpenAI 默认 WhatsApp 投递，`_select_sms_if_present` 在填号前后多语言点选 "Text message" 切到 SMS（sms-man 投 SMS）。
  - **自动换号重试**：手机号大概率被风控拒，`handle_add_phone` 最多重试 `CODEX_ADDPHONE_ATTEMPTS` 次（默认 8），逐号换租。
  - **每次尝试先关窗口重登**：`make_reset_page` 工厂在每次授权尝试前 teardown 旧窗口 / 开新窗口 / 重载 cookie / 重登，避免复用窗口导致 OpenAI 风控决策不重新 roll。
  - **`authorize_with_retry`** 统一编排：`gen_auth_url` 4× 退避重试（SUB2API tiantianai.co 偶发不可达）、`asyncio.wait_for` 硬上限防 `drive_authorize` 卡死、consent 点击用精确 `button[data-dd-action-name="Continue"]` 选择器 + churn-breaker 重 goto。
- **`register_chatgpt.py` Cloudflare Turnstile 过墙**：`_is_cf_blocked`/`_click_turnstile`/`_switch_cf_node` —— AWS 机房 IP 触发整页 managed challenge（转圈无可点元素）时自动切非 AWS 节点；边界 IP 有可点框时先点。邮箱验证码支持重发兜底（`_click_resend_code`/`_renavigate_resend`，含 zh-TW "重新傳送電郵"）。

**移除**
- 删除 Codex 订阅（baxigpt）相关：`activate_plus.py`、`common/plus_baxi.py`，及 `config.py`/`.env.example`/README 中 `BAXI_API`/`BAXI_CARDS` 配置与说明。Codex 进 SUB2API/CPA 的正路统一为 `oauth_codex.py`（带真 `refresh_token`）。

**配置**
- `.env.example` 新增 `SMSMAN_TOKEN` / `SMSMAN_APP_ID_OPENAI`、`CODEX_ADDPHONE_ATTEMPTS=8` / `CODEX_SMS_TIMEOUT=150` / `CODEX_PHONE_SKIP_ATTEMPTS=0`。

**说明**
- 实测全流程（`run_full_flow --codex`）：邮箱注册 → CF 过墙 → ChatGPT 注册 → 邮件验证码（含重发）→ add-phone（sms-man 接 SMS 过号）→ consent → callback → SUB2API 建号（type=oauth）全链路打通。
- 实测 8/8 新号均要求 add-phone（手机要求**绑账号非绑会话**），phone-skip 对新号无效，故默认 `CODEX_PHONE_SKIP_ATTEMPTS=0`。
- sms-man 需 USD 余额 > $13；接码 token 走 `.env`，代码零明文。

## 2026-06-12 — vision_solver 过 hCaptcha（canvas 点击 + 拖拽）

**新增**
- **`vision_solver` 新增 `canvas_grid` 模式**：解新版 hCaptcha。实测发现现代 hCaptcha 把**整个挑战渲染进单个 `<canvas>`（500×470）**，无任何可枚举/可点的 DOM tile，原 `grid_select`（点 DOM 元素）不适用。新 driver `solve_canvas_grid`：
  - **稳定截图**（`_shot_canvas_stable`）：先强制等图加载，再要求连续帧字节一致才采用，避免截到渐入/加载中的半成品。
  - **像素坐标点击**（`_click_canvas_cell`）：按 bbox/截图尺寸比换算 dpr，直接 `click(position=...)` 点 canvas 对应格中心。
  - **网格几何**（`overlay_grid_numbers` 四边内缩）：实测 500×470 上内缩 top0.30/bottom0.036/左右0.164，把编号网格框定到真实图块区，保证点中心对齐。
  - **题型自动判别**（`_infer_layout`）：题干含 "the item/thing"(单数) 且无 "all/each" → 单选（`vote_answer`，只点 1 格、永不空选）；"card/different" → 1×3 卡片单选；"select all" → 多选（`vote_picklist`）。空共识时兜底点最高票一格，避免空提交浪费轮次。
- **`vision_solver` 新增 `canvas_drag` 模式**：解拖拽类挑战（把 piece 拖到 target）。`solve_canvas_drag` + `vote_points`（各模型给 `FROM=(x,y) TO=(x,y)` 归一化坐标、取各点中位数抗离群）+ `_drag_on_canvas`（`page.mouse` down/move 分步带抖动/up 模拟人手）。预置 `presets/hcaptcha_drag.json`。
- 预置 `presets/hcaptcha.json` 改写为 `canvas_grid`（`frame_match=["frame=challenge"]`，题干 `#prompt-question`，提交 `.button-submit`）。

**测试**
- 点击型对 live demo（`https://accounts.hcaptcha.com/demo`）三个测试 sitekey 各跑 3 轮：**8/9 通过**，唯一失败为空选卡死，修复后（单选路由 + 兜底点击）复跑全过。
- 拖拽型 demo 不发拖拽题（三种探针证实该 demo + 测试 key 只发 "Tap the item provides shade" 一种 3×3 点击题），故用本地合成 canvas 谜题（蓝球拖进红框）验证机制：**3/3 命中**，中位数投票纠正了个别模型偏差。

**说明**
- 真实 hCaptcha 拖拽/滑块题需 live 复现后再校准坐标系与题型判别；点击型已可用，拖拽机制已验证、链路就绪。
- 网关/key 复用现有视觉投票池变量（`.env`），代码零明文。`screenshots_vision/` 已入 `.gitignore`。

## 2026-06-08 — GitHub 注册 + Arkose 验证 agent-captcha 视觉求解

**新增**
- **`register_github.py`**：GitHub 注册主流程。单页表单（邮箱/密码/用户名/国家，只认 `Create account` 不误点 `Continue with Google`），提交触发 **Arkose FunCaptcha**（octocaptcha 包裹），过验证后浏览器登录 Outlook 取 launch code → 建号 → 存 cookie。`--auto` 跑完整流程，无参数为探索模式（填到验证停、保留窗口）。
- **`common/agent_captcha.py`**：Arkose 验证**视觉投票求解器**（不依赖传统打码平台）。
  - **变体自动分派**（按拼图题目文本，不硬编码）：`sequence`（4 图标逐环序列匹配）/ `rotate`（3D 物体朝向匹配）/ `character`（小人踩格，模型分歧大、默认跳过换窗口）。轮数从 "x of N" 解析、候选张数从 `.pip` 进度点数。
  - **多模型并发投票**：`vote_answer()` 让 gemini-3.5-flash / gpt-5.5 / gemini-3.1-pro / claude-opus 并发判断、多数表决；平票优先级 gemini-flash > gpt-5.5 > gemini-pro > opus（实测 claude 在拼图上偏弱，权重最低）。整轮 deadline 55s 防慢模型拖垮，空票自动重试。
  - **图像处理**：候选裁剪放大（`shot_element`）+ 拼成带编号网格（`stitch_options_grid`）+ 本地秒级增强 + 控体积 JPEG（`enhance_local`，避免大图传输超时空票）；可选 gpt-image-2 保真增强（`enhance_image`）。
  - **复盘标注**：每轮落 `screenshots_github/REVIEW_rN.png`，红框=最终选择、彩框=各模型投票，便于人工核对。
  - **协议自适应**：OpenAI 兼容网关走 `/v1/chat/completions`；claude/opus 走 Anthropic 原生 `/v1/messages`（base 以 `/claude` 结尾自动识别），图片 base64 按 JPEG/PNG 头自适应 media_type。
- `config.py` 新增 agent-captcha 配置项（全走 `.env`）：`VISION_API_BASE/KEY`、`VISION_MODEL`、`IMAGE_EDIT_BASE2/KEY2`、`VOTE_ZZ_BASE/KEY`、`VOTE_GPT_KEY`、`VOTE_OPUS_BASE/KEY`、`GEMMA_API_BASE/KEY`；`.env.example` 补齐占位与说明。

**说明**
- Arkose 验证关已实测可通过（sequence 变体 10/10、rotate 5 轮通过）；`character` 变体模型间分歧大，默认遇到即换窗口重试（最多 8 次）赌到易解变体。
- GitHub 对批量 Outlook 邮箱有风控（验证后提示 "This email can't be used"），整套自动化流程完整，邮源需配可用邮箱。
- 网关/key 一律走环境变量（`.env`），代码零明文，符合项目约定。

## 2026-06-07 — chatgpt2api 普通网页号导入

**新增**
- **`export_chatgpt2api.py`**：把注册落下的普通 ChatGPT 网页号聚合成 chatgpt2api（basketikun/chatgpt2api）的批量导入格式。`common/session_export.py:build_chatgpt2api_account` 把网页 session 转成导入对象（只认 `access_token`，**不带 `type:"codex"`**，否则会被对端当 codex 源），注册成功时顺手落 `tokens/chatgpt/c2a-*.json`。
- **`register_chatgpt.py --import-c2a`**：注册成功后用刚抓到的 session 即时 `POST <host>/api/accounts` 把 token 导入 chatgpt2api（默认关）。host/key 取 `config.CHATGPT2API_URL` / `CHATGPT2API_KEY`（走 `.env`），也可 `--c2a-url` / `--c2a-key` 覆盖。单号导入失败只告警，不影响注册成功判定。
- `--import-c2a` 逐层透传：`run_full_flow.py` → `register_three_platforms.py` → `register_chatgpt.py`（只对 chatgpt 平台生效，claude/grok 不受影响）。
- `config.py` 新增 `CHATGPT2API_URL` / `CHATGPT2API_KEY`（默认空，从 `.env` 读）。

**优化**
- `export_chatgpt2api.py` 新增 `import_accounts(host, key, accounts)`（不抛异常版，返回 `(ok, msg)`），供注册脚本逐个号上传时调用；命令行 `--post` 仍用原 `post_accounts`。
- `run_full_flow.py` 顺带提交已有的多轮循环（`--rounds` / `--round-sleep`，支持有限轮数与无限循环）。

**说明**
- 普通网页号无真 `refresh_token`，`access_token` 约 10 天过期后对端无法续期，属预期（codex/OAuth 三件套号仍走 `oauth_codex.py` + CPA/SUB2API）。
- 对端 API 路径是 `/api/accounts`（`/accounts` 是网页 UI），需 `Authorization: Bearer <admin key>`；重复 token 对端按 skipped 幂等处理。

## 2026-06-06 - Gmail Android/Appium 本地注册包

**新增**
- 新增 `gmail_android/` 模块，包含 Gmail Android 注册流程、Appium helper API、`.env` 配置加载、SMS provider 骨架和 Windows 安装脚本。
- 新增 BlueStacks 直接安装/配置脚本：`gmail_android/scripts/install_bluestacks.ps1`，支持配置 ADB、`Pie64_12`、`127.0.0.1:5675`、`900x1600 @ 240dpi`。
- 新增一键安装入口：`gmail_android/scripts/install_all_windows.ps1`，用于 GitHub Release 安装包解压后的环境初始化。
- 新增 Release 构建脚本：`gmail_android/scripts/build_release.ps1`，支持可选附带固定版本 BlueStacks 安装器。
- 新增 `gmail_android/offline/bluestacks/.gitkeep`，预留固定版本 BlueStacks 安装器目录；安装器二进制不进 git，后续随 Release 附件打包。

**优化**
- 根 `.env.example` 增加 Gmail Android/Appium 相关环境变量：`APPIUM_SERVER`、`ANDROID_DEVICE`、`GMAIL_USERNAME_PREFIX`、`ACCEPT_TERMS`、`SMS_PROJECT_ID_GMAIL`、`HERO_SMS_SERVICE_GMAIL` 等。
- 根 `requirements.txt` 增加 `Appium-Python-Client` 和 `selenium`。
- 根 README 增加 Gmail Android 安装包的安装、配置、运行和 Release 打包说明。
- README 补充 GitHub Release 安装包上传流程，覆盖网页上传和 `gh release` 命令两种方式。
- 根 README 前置条件补充 Gmail/谷歌邮箱注册所需的 BlueStacks、Android SDK/ADB、Node/Appium 和 Gmail App。

**安全边界**
- Gmail 手机/SMS/CAPTCHA 和 Google 额外安全验证默认由人工完成；脚本支持 `--resume-after-phone` 续跑。
- `--accept-terms` 仅在操作者明确同意 Google Privacy and Terms 后使用。
- `sms_provider.py` 仅作为后续合规内部接码 provider 的环境变量接口骨架，当前不默认接入 Gmail 安全验证自动化。

## 2026-06-04 — Codex 订阅授权 + 上传 SUB2API / CPA

**新增**
- **`oauth_codex.py`**：账号走 Codex OAuth 换取**带 `refresh_token` 的正式凭据**，一步建到
  **SUB2API**（`type=oauth`）并推 **CPA**，解决网页 session 无 refresh_token、下游过期 401。
- **接码支持 WhatsApp**：遇 OpenAI add-phone 手机验证，用 `--manual-phone` 在浏览器手动填号 +
  输码，**推荐 WhatsApp 可接码号段**（普通虚拟号易被拒）。
- 配套：`activate_plus.py` 激活码开通 Plus / Codex 订阅；`upload_tokens.py` 一键上传到
  CPA / SUB2API / webchat2api。
- 订阅地址 / 激活码全部走环境变量（见 `.env.example`）。

**优化**
- README 补全「Codex 订阅授权 & token 上传」「项目结构 / 模块职责」「典型一条龙用法」，适配多人协作。
- 清理冗余代码，半成品路径标注 WIP。



