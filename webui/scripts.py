# -*- coding: utf-8 -*-
"""
webui/scripts.py — GUI 的数据核心：把每个入口脚本的命令行参数、以及 .env 配置项
结构化成 schema，前端据此自动生成表单。新增脚本/配置项只改这里。

参数类型(type)对应前端控件：
  bool   -> 勾选框(store_true，勾上才追加 --flag)
  int    -> 数字输入
  str    -> 文本输入
  choice -> 下拉(配 choices)
  multi  -> 多选(nargs="+"，选中的值空格分隔追加在 --flag 后)
positional=True 表示位置参数(不带 --，直接拼值)。
"""

# ============================================================ 入口脚本 schema
SCRIPTS = [
    # ---------------------------------------------------------------- 主流程
    {
        "id": "run_full_flow",
        "file": "run_full_flow.py",
        "category": "主流程",
        "title": "四平台端到端全流程",
        "desc": "支持 Claude、Claude Platform、ChatGPT、Grok；Claude 家族专用流程在启动前选择 NINEMALL，混合流程在启动前直接选择 OUTLOOK。",
        "args": [
            {"flag": "--platforms", "type": "multi", "choices": ["claude", "claude_api", "chatgpt", "grok"],
             "default": ["claude"], "help": "四个平台(可多选)"},
            {"flag": "--rounds", "type": "int", "default": 1, "help": "循环注册轮数(0=无限循环)"},
            {"flag": "--codex", "type": "bool", "default": False, "help": "chatgpt 注册后走 Codex OAuth 导入 SUB2API(会接码过手机)"},
            {"flag": "--grok-sub2api", "type": "bool", "default": False, "help": "grok 注册后把 SSO 转成 SUB2API Grok OAuth 账号"},
            {"flag": "--import-c2a", "type": "bool", "default": False, "help": "chatgpt 注册后即时导入 chatgpt2api(需配 CHATGPT2API_*)"},
            {"flag": "--codex-manual-phone", "type": "bool", "default": False, "help": "Codex add-phone 手动模式(自己在浏览器填号收码)"},
            {"flag": "--codex-group", "type": "str", "default": "", "help": "SUB2API 目标分组名(默认取配置)"},
            {"flag": "--grok-sub2api-group", "type": "str", "default": "", "help": "SUB2API Grok 分组名(默认取配置)"},
            {"flag": "--skip-email", "type": "bool", "default": False, "help": "跳过邮箱注册，直接用下面指定的邮箱"},
            {"flag": "--email", "type": "str", "default": "", "help": "配合 --skip-email：现成邮箱"},
            {"flag": "--password", "type": "str", "default": "", "secret": True, "help": "配合 --email 的密码"},
            {"flag": "--token", "type": "str", "default": "", "secret": True, "help": "配合 --skip-email：refresh token"},
            {"flag": "--client-id", "type": "str", "default": "", "secret": True, "help": "配合 --skip-email：OAuth client_id"},
            {"flag": "--node", "type": "str", "default": "auto", "help": "claude/grok 走的 Clash 节点"},
            {"flag": "--email-attempts", "type": "int", "default": 30, "help": "邮箱注册最多尝试次数"},
            {"flag": "--platform-timeout", "type": "int", "default": 600, "help": "平台注册单号超时(秒)"},
            {"flag": "--email-confirm-before-register", "type": "bool", "default": False,
             "help": "邮箱注册页打开后自动点确认，再开始填写"},
            {"flag": "--dry-run", "type": "bool", "default": False, "help": "只打印将执行的命令，不真注册(安全预览)"},
        ],
    },
    {
        "id": "register_three_platforms",
        "file": "register_three_platforms.py",
        "category": "主流程",
        "title": "四平台注册(已有邮箱)",
        "desc": "用现成邮箱或所选邮箱渠道的账号池注册四个平台；Claude 家族专用流程在启动前选择 NINEMALL，混合流程在启动前直接选择 OUTLOOK。",
        "args": [
            {"flag": "--from-pool", "type": "bool", "default": False, "help": "从所选邮箱渠道的账号池取一个账号"},
            {"flag": "--email", "type": "str", "default": "", "help": "指定邮箱(不从池取时)"},
            {"flag": "--password", "type": "str", "default": "", "secret": True, "help": "邮箱密码"},
            {"flag": "--token", "type": "str", "default": "", "secret": True, "help": "邮箱 refresh_token"},
            {"flag": "--client-id", "type": "str", "default": "", "secret": True, "help": "邮箱 OAuth client_id"},
            {"flag": "--platforms", "type": "multi", "choices": ["claude", "claude_api", "chatgpt", "grok"],
             "default": ["claude", "chatgpt", "grok"], "help": "四个平台(可多选)"},
            {"flag": "--parallel", "type": "bool", "default": False, "help": "并行跑各平台(默认顺序)"},
            {"flag": "--loop", "type": "bool", "default": False, "help": "持续从池取号循环注册(常驻)"},
            {"flag": "--codex", "type": "bool", "default": False, "help": "chatgpt 后走 Codex OAuth"},
            {"flag": "--grok-sub2api", "type": "bool", "default": False, "help": "grok 后导入 SUB2API Grok 渠道"},
            {"flag": "--grok-sub2api-group", "type": "str", "default": "", "help": "SUB2API Grok 分组名(默认取配置)"},
            {"flag": "--import-c2a", "type": "bool", "default": False, "help": "chatgpt 后导入 chatgpt2api"},
            {"flag": "--node", "type": "str", "default": "auto", "help": "Grok Clash 节点"},
            {"flag": "--timeout", "type": "int", "default": 600, "help": "单平台超时(秒)"},
        ],
    },
    {
        "id": "oauth_codex",
        "file": "oauth_codex.py",
        "category": "主流程",
        "title": "Codex OAuth 授权 → SUB2API",
        "desc": "用已存 cookie 重登 ChatGPT，走 OAuth 拿带 refresh_token 的凭据建到 SUB2API/CPA。默认直接接码过 add-phone。",
        "args": [
            {"flag": "--cookie", "type": "str", "default": "", "help": "cookie 文件路径(默认最新 full_*.json)"},
            {"flag": "--phone-skip", "type": "int", "default": 0, "help": "先赌免手机直连次数(0=直接接码不赌)"},
            {"flag": "--manual-phone", "type": "bool", "default": False, "help": "手动模式：自己在浏览器填号+输码"},
            {"flag": "--phone", "type": "str", "default": "", "help": "半自动：脚本填该号(E.164)+选WhatsApp，你只手输码"},
            {"flag": "--group", "type": "str", "default": "", "help": "SUB2API 目标分组(默认取配置)"},
            {"flag": "--skip-cpa", "type": "bool", "default": False, "help": "不推 CPA(默认配好就推)"},
            {"flag": "--keep", "type": "bool", "default": False, "help": "失败保留窗口便于排查"},
        ],
    },
    # ---------------------------------------------------------------- 单平台注册
    {
        "id": "register_chatgpt",
        "file": "register_chatgpt.py",
        "category": "单平台注册",
        "title": "ChatGPT 注册",
        "desc": "ChatGPT 单平台注册(可绕过邮箱池指定邮箱)。",
        "args": [
            {"flag": "--count", "type": "int", "default": 1, "help": "注册数量"},
            {"flag": "--concurrency", "type": "int", "default": 1, "help": "并发数"},
            {"flag": "--timeout", "type": "int", "default": 480, "help": "单号超时(秒)"},
            {"flag": "--email", "type": "str", "default": "", "help": "指定邮箱(绕过池)"},
            {"flag": "--password", "type": "str", "default": "", "secret": True, "help": "邮箱密码"},
            {"flag": "--refresh-token", "type": "str", "default": "", "secret": True, "help": "Outlook refresh_token"},
            {"flag": "--client-id", "type": "str", "default": "", "secret": True, "help": "Outlook OAuth client_id"},
            {"flag": "--import-c2a", "type": "bool", "default": False, "help": "注册后导入 chatgpt2api"},
            {"flag": "--codex", "type": "bool", "default": False, "help": "注册后走 Codex OAuth"},
            {"flag": "--codex-manual-phone", "type": "bool", "default": False, "help": "Codex 手动填号收码"},
            {"flag": "--keep-on-fail", "type": "bool", "default": False, "help": "失败保留窗口"},
        ],
    },
    {
        "id": "register_grok",
        "file": "register_grok_http.py",
        "category": "单平台注册",
        "title": "Grok 注册",
        "desc": "Grok 纯 HTTP 协议注册；可把注册出的 SSO 直接转成 SUB2API Grok OAuth 账号。需能过 Cloudflare 的干净节点 + 临时邮箱 + CapSolver key。",
        "args": [
            {"flag": "--count", "type": "int", "default": 1, "help": "注册数量"},
            {"flag": "--node", "type": "str", "default": "auto", "help": "Clash 出口节点(过 grok CF，如 '美国 01'，留 auto 自动探测)"},
            {"flag": "--provider", "type": "choice",
             "choices": ["", "yyds", "gptmail", "cfmail", "moemail", "custom"], "default": "",
             "help": "临时邮箱来源(留空=用 .env 的 TEMP_EMAIL_PROVIDER；需在「配置」页填好对应 key)"},
            {"flag": "--sub2api", "type": "bool", "default": False,
             "help": "注册成功后导入 SUB2API，目标渠道必须是 grok"},
            {"flag": "--sub2api-group", "type": "str", "default": "",
             "help": "SUB2API Grok 分组名(留空取 SUB2API_GROK_GROUP)"},
            {"flag": "--mailbox-attempts", "type": "int", "default": 6,
             "help": "发码域名被拒时单号自动更换邮箱次数"},
            {"flag": "--code-timeout", "type": "int", "default": 75,
             "help": "单个临时邮箱等待验证码秒数"},
            {"flag": "--rotate-every", "type": "int", "default": 5,
             "help": "批量每 N 个账号重新探测出口节点(0=不轮换)"},
        ],
    },
    {
        "id": "register_claude",
        "file": "register.py",
        "category": "单平台注册",
        "title": "Claude 注册",
        "desc": "Claude 单平台注册(claude.ai 区域封锁需走干净节点)。",
        "args": [
            {"flag": "--count", "type": "int", "default": 1, "help": "注册数量"},
            {"flag": "--concurrency", "type": "int", "default": 1, "help": "并发数"},
            {"flag": "--timeout", "type": "int", "default": 480, "help": "单号超时(秒)"},
            {"flag": "--email", "type": "str", "default": "", "help": "指定邮箱(调试)"},
            {"flag": "--password", "type": "str", "default": "", "secret": True, "help": "邮箱密码"},
            {"flag": "--token", "type": "str", "default": "", "secret": True, "help": "refresh token"},
            {"flag": "--client-id", "type": "str", "default": "", "secret": True, "help": "OAuth client_id"},
            {"flag": "--node", "type": "str", "default": "none", "help": "Clash 节点(none=不切)"},
        ],
    },
    {
        "id": "register_claude_api",
        "file": "register_claude_api.py",
        "category": "单平台注册",
        "title": "Claude API 注册",
        "desc": "注册 platform.claude.com 个人账户并保存登录会话；不创建组织、不充值。",
        "args": [
            {"flag": "--count", "type": "int", "default": 1, "help": "注册数量"},
            {"flag": "--concurrency", "type": "int", "default": 1, "help": "并发数"},
            {"flag": "--timeout", "type": "int", "default": 480, "help": "单号超时(秒)"},
            {"flag": "--email", "type": "str", "default": "", "help": "指定邮箱(调试)"},
            {"flag": "--password", "type": "str", "default": "", "secret": True, "help": "邮箱密码"},
            {"flag": "--token", "type": "str", "default": "", "secret": True, "help": "refresh token"},
            {"flag": "--client-id", "type": "str", "default": "", "secret": True, "help": "OAuth client_id"},
            {"flag": "--node", "type": "str", "default": "none", "help": "Clash 节点(none=不切)"},
        ],
    },
    {
        "id": "register_github",
        "file": "register_github.py",
        "category": "单平台注册",
        "title": "GitHub 注册",
        "desc": "GitHub 注册(含 Arkose 验证视觉求解，需配 VISION_*/VOTE_*)。",
        "args": [
            {"flag": "--auto", "type": "bool", "default": False, "help": "走完整流程(含取 launch code)"},
            {"flag": "--email", "type": "str", "default": "", "help": "指定邮箱(默认从 _outlook_pool 取)"},
            {"flag": "--password", "type": "str", "default": "", "secret": True, "help": "邮箱密码"},
            {"flag": "--no-keep", "type": "bool", "default": False, "help": "结束后删窗口(默认保留)"},
            {"flag": "--timeout", "type": "int", "default": 600, "help": "超时(秒)"},
        ],
    },
    # ---------------------------------------------------------------- 养号 / 邮箱
    {
        "id": "outlook_reg_loop",
        "file": "outlook_reg_loop.py",
        "category": "养号/邮箱",
        "title": "Outlook 自注册养号",
        "desc": "持续自注册 Outlook，产出到 _outlook_pool/ 与 emails.txt。count=0 为无限循环。",
        "args": [
            {"flag": "--count", "type": "int", "default": 0, "help": "注册数量(0=无限循环)"},
            {"flag": "--target-pool", "type": "int", "default": 0, "help": "池达到此数量就停(0=不限)"},
            {"flag": "--max-press", "type": "str", "default": "3", "help": "人机验证按住次数上限"},
            {"flag": "--confirm-before-register", "type": "bool", "default": False,
             "help": "注册页打开后仅在出现数据许可页时才点确认(正常表单不点)"},
            {"flag": "--timeout", "type": "int", "default": 180, "help": "单号注册超时(秒)"},
            {"flag": "--sleep", "type": "int", "default": 5, "help": "每次注册间隔(秒)"},
            {"flag": "--sleep-when-full", "type": "int", "default": 60, "help": "池达标时的休眠间隔(秒)"},
            {"flag": "--no-rotate", "type": "bool", "default": False,
             "help": "不轮换 Clash 节点，固定用当前节点(也可用 OUTLOOK_NO_ROTATE=1)"},
        ],
    },
    {
        "id": "unlock_outlook",
        "file": "unlock_outlook.py",
        "category": "养号/邮箱",
        "title": "解锁被锁 Outlook",
        "desc": "批量解锁被锁账号，结果分类输出到 unlock_results/。",
        "args": [
            {"flag": "--input", "type": "str", "default": "", "help": "账号文件(每行 email----password；默认找最新 locked)"},
            {"flag": "--proxy-file", "type": "str", "default": "", "help": "住宅代理池文件"},
            {"flag": "--concurrency", "type": "int", "default": 1, "help": "并发数"},
        ],
    },
    {
        "id": "extract_graph_tokens",
        "file": "extract_graph_tokens.py",
        "category": "养号/邮箱",
        "title": "提取 Graph refresh_token",
        "desc": "用账号密码换 Microsoft Graph refresh_token(免浏览器)，输出到 outlook_accounts/。",
        "args": [
            {"flag": "accounts_file", "type": "str", "default": "", "positional": True,
             "help": "账号文件(每行 email----password----...；留空自动扫 unlock_results/)"},
            {"flag": "--email", "type": "str", "default": "", "help": "单个邮箱"},
            {"flag": "--password", "type": "str", "default": "", "secret": True, "help": "单个邮箱密码"},
            {"flag": "--concurrency", "type": "int", "default": 5, "help": "并发数"},
        ],
    },
    {
        "id": "mailbox_broker",
        "file": "mailbox_broker.py",
        "category": "养号/邮箱",
        "title": "共享取码服务(常驻)",
        "desc": "并行流水线时起共享取码服务，避免三窗口并发登录同一邮箱。常驻运行。",
        "args": [
            {"flag": "--host", "type": "str", "default": "127.0.0.1", "help": "监听地址"},
            {"flag": "--port", "type": "int", "default": 8765, "help": "监听端口"},
            {"flag": "--idle", "type": "int", "default": 480, "help": "空闲会话回收秒数"},
        ],
    },
    # ---------------------------------------------------------------- 导出 / 上传
    {
        "id": "upload_tokens",
        "file": "upload_tokens.py",
        "category": "导出/上传",
        "title": "上传标准 token",
        "desc": "把 tokens/ 下标准 token 上传到 CPA/SUB2API/webchat2api；Grok 会导入 SUB2API Grok 渠道。",
        "args": [
            {"flag": "target", "type": "choice", "choices": ["all", "chatgpt", "grok"],
             "default": "all", "positional": True, "help": "上传目标"},
        ],
    },
    {
        "id": "export_chatgpt2api",
        "file": "export_chatgpt2api.py",
        "category": "导出/上传",
        "title": "导出/上传 chatgpt2api",
        "desc": "聚合普通网页号导入 chatgpt2api(--post 直传 / 默认导出 txt)。",
        "args": [
            {"flag": "--post", "type": "str", "default": "", "help": "直接 POST 到的 host(留空只导出文件)"},
            {"flag": "--key", "type": "str", "default": "", "help": "chatgpt2api admin key(配合 --post)"},
            {"flag": "--json", "type": "bool", "default": False, "help": "导出 JSON(默认一行一个 access_token)"},
            {"flag": "--out", "type": "str", "default": "", "help": "输出文件路径"},
        ],
    },
    {
        "id": "export_accounts",
        "file": "export_accounts.py",
        "category": "导出/上传",
        "title": "导出账号 cookie",
        "desc": "导出已注册账号 cookie 供直登扩展使用(无参=全部平台)。",
        "args": [],
    },
]


def script_by_id(sid):
    for s in SCRIPTS:
        if s["id"] == sid:
            return s
    return None


# ============================================================ 外部工具链接
# 不在本机跑的 web 服务/工具，面板上以"打开链接"卡片呈现(新标签打开)。
EXTERNAL_LINKS = [
]


# ============================================================ 内嵌功能页
# 直接在面板里 iframe 内嵌的外部页面 + 可选 sms-man 接码助手。
# 空列表时前端不渲染「功能」导航区(app.js: if(EMBEDS.length))，内嵌视图/接码助手一并隐藏。
# Gmail 注册已下线；如需重新内嵌某页，按下面注释里的字段格式往列表里加即可。
#   {"id": "xxx", "title": "标题", "url": "https://...", "desc": "说明",
#    "sms_helper": True, "sms_service_default": "google"}
EMBED_PAGES = []


# ============================================================ .env 配置 schema
# group: 分组标题；key: 变量名；required: 是否必填(运行对应功能时)；help: 说明；
# secret: True 时前端用密码框；default: 模板默认值(仅展示)。
ENV_SCHEMA = [
    {"group": "Claude 邮箱渠道", "items": [
        {"key": "EMAIL_PROVIDER", "type": "choice", "choices": ["NINEMALL", "OUTLOOK"],
         "default": "NINEMALL", "help": "Claude 邮箱渠道；默认 NINEMALL API 取信"},
        {"key": "NINEMALL_EMAIL_FILE", "default": "mail.txt", "help": "NINEMALL 四列账号文件"},
        {"key": "NINEMALL_API_BASE", "default": "https://www.appleemail.top", "help": "NINEMALL 取信 API 根地址"},
        {"key": "NINEMALL_API_PASSWORD", "secret": True, "help": "小苹果服务访问密码；未启用时留空"},
        {"key": "NINEMALL_HTTP_TIMEOUT", "type": "int", "default": 30, "help": "单次取信请求超时秒数"},
        {"key": "NINEMALL_POLL_INTERVAL", "type": "int", "default": 5, "help": "Claude 邮件轮询间隔秒数"},
    ]},
    {"group": "Codex K12 控制台", "tests": [{"target": "k12", "label": "测试 K12 控制台"}], "items": [
        {"key": "K12_CONSOLE_URL", "default": "http://127.0.0.1:8806",
         "help": "主 WebUI 内嵌的 Codex K12 地址。本地地址会由主 WebUI 自动管理进程。"},
        {"key": "K12_AUTO_START", "type": "choice", "choices": ["1", "0"], "default": "1",
         "help": "启动主 WebUI 时是否自动拉起本地 Codex K12 服务。"},
    ]},
    {"group": "Clash 代理(节点切换/出口)", "tests": [{"target": "clash", "label": "测试 Clash 连通"}], "items": [
        {"key": "CLASH_SECRET", "required": True, "secret": True,
         "help": "Clash Verge → 设置 → 外部控制器(External Controller) 里设的 secret/密钥。"
                 "若该处留空,这里也留空。设了密钥不填会连不上控制器(节点切换失效)。"},
        {"key": "CLASH_API", "default": "http://127.0.0.1:9097",
         "help": "Clash 控制器地址。Clash Verge 默认端口 9097,mihomo 内核默认 9090。在 外部控制器 页可看到。"},
        {"key": "CLASH_PROXY", "default": "http://127.0.0.1:7897",
         "help": "Clash 混合代理端口(mixed-port),脚本走它出网。Verge 默认 7897。"},
        {"key": "CLASH_GROUP", "default": "GLOBAL",
         "help": "决定出口的代理组名。global 模式下填 GLOBAL;规则模式填你的节点选择组名。"},
    ]},
    {"group": "IPMart 每账号代理", "items": [
        {"key": "IPMART_ENABLED", "type": "choice", "choices": ["0", "1"], "default": "0",
         "help": "设为 1 启用 IPMart 控制台提供的固定网关 SID 模式。"},
        {"key": "IPMART_PROXY_HOST",
         "help": "IPMart 控制台显示的固定网关主机名。"},
        {"key": "IPMART_PROXY_PORT", "type": "int",
         "help": "IPMart 控制台显示的固定网关端口。"},
        {"key": "IPMART_PROXY_USERNAME_TEMPLATE", "secret": True,
         "help": "复制控制台代理用户名，并只把其中的 SID 数字替换为 {sid}。"},
        {"key": "IPMART_PROXY_PASSWORD", "secret": True,
         "help": "IPMart 控制台显示的代理密码。"},
        {"key": "IPMART_MAX_ATTEMPTS", "type": "int", "default": 3,
         "help": "自行设置获取或验证失败时的最大尝试次数，必须为正整数。"},
        {"key": "IPMART_IP_CHECK_URL", "default": "https://api.ipify.org?format=json",
         "help": "通过固定网关验证出口 IP 的地址；默认使用 ipify JSON 接口。"},
    ]},
    {"group": "指纹浏览器", "tests": [{"target": "bitbrowser", "label": "测试 指纹浏览器连通"}], "items": [
        {"key": "FINGERPRINT_BROWSER", "type": "choice", "choices": ["bitbrowser", "adspower"],
         "default": "bitbrowser", "help": "选择当前指纹浏览器"},
        {"key": "BITBROWSER_API", "default": "http://127.0.0.1:54345", "help": "比特浏览器本地 API"},
        {"key": "ADSPOWER_API", "default": "http://127.0.0.1:50325", "help": "AdsPower 本地 API"},
        {"key": "ADSPOWER_API_KEY", "secret": True, "help": "AdsPower API key，未启用鉴权时留空"},
        {"key": "ADSPOWER_GROUP_ID", "default": "0", "help": "AdsPower 新建 profile 的分组 ID"},
    ]},
    {"group": "短信接码", "tests": [{"target": "smsman", "label": "测试 sms-man"}, {"target": "firefox", "label": "测试 firefox.fun"}], "items": [
        {"key": "SMS_TOKEN", "secret": True, "help": "firefox.fun 接码 token"},
        {"key": "HERO_SMS_API_KEY", "secret": True, "help": "hero-sms.com 备用接码 key"},
        {"key": "SMSMAN_TOKEN", "secret": True, "help": "sms-man.com 接码 key(Codex add-phone 主用)"},
        {"key": "SMSMAN_APP_ID_OPENAI", "default": "openai", "help": "sms-man OpenAI 服务 id(openai 自动解析为 2754)"},
        {"key": "SMSMAN_APP_ID_GMAIL", "default": "google", "help": "sms-man Gmail/Google 服务 id(google 自动解析;接码助手默认用它)"},
        {"key": "SMSMAN_COUNTRY_ID_OPENAI", "default": "0", "help": "国家 id(0=随机/按价格)"},
    ]},
    {"group": "打码平台(可选)", "items": [
        {"key": "CAPSOLVER_API_KEY", "secret": True, "help": "CapSolver 打码 key"},
        {"key": "EZCAPTCHA_API_KEY", "secret": True, "help": "EZ-Captcha 打码 key(解锁 Outlook 用)"},
        {"key": "YESCAPTCHA_API_KEY", "secret": True, "help": "YesCaptcha key(GitHub Arkose 备用)"},
    ]},
    {"group": "Outlook 自注册", "items": [
        {"key": "OUTLOOK_PROXIES", "help": "Outlook 自注册住宅代理池(换行/逗号分隔)"},
    ]},
    {"group": "临时邮箱(Grok 注册取码)", "tests": [{"target": "yyds", "label": "测试 YYDS"}], "items": [
        {"key": "TEMP_EMAIL_PROVIDER", "type": "choice",
         "choices": ["yyds", "gptmail", "moemail", "cfmail", "custom"], "default": "yyds",
         "help": "Grok 注册默认用的临时邮箱 provider(需配好对应 key)。也可在「Grok 注册」表单里临时指定。"},
        {"key": "YYDS_API_KEY", "secret": True, "help": "YYDS Mail key(profile 页,AC- 开头)"},
        {"key": "YYDS_BASE_URL", "default": "https://maliapi.215.im", "help": "YYDS Mail API 根地址；可粘贴 vip.215.im 或完整 /v1/accounts 地址，程序会自动纠正"},
        {"key": "GPTMAIL_API_KEY", "secret": True, "help": "GPTMail key(mail.chatgpt.org.uk)"},
        {"key": "MOEMAIL_API_KEY", "secret": True, "help": "MoeMail key(自部署)"},
        {"key": "MOEMAIL_BASE_URL", "help": "MoeMail 自部署地址"},
        {"key": "CFMAIL_ADMIN_PASSWORD", "secret": True, "help": "Cloudflare Temp Email admin 密码(自部署)"},
        {"key": "CFMAIL_BASE_URL", "help": "Cloudflare Temp Email 地址"},
    ]},
    {"group": "SUB2API(Codex / Grok 导入)", "items": [
        {"key": "SUB2API_URL", "help": "SUB2API 管理接口地址"},
        {"key": "SUB2API_EMAIL", "help": "SUB2API 登录邮箱"},
        {"key": "SUB2API_PASSWORD", "secret": True, "help": "SUB2API 登录密码"},
        {"key": "SUB2API_GROUP", "default": "codex", "help": "Codex 目标分组名(platform=openai)"},
        {"key": "SUB2API_GROK_GROUP", "default": "grok", "help": "Grok 目标分组名(platform=grok，需后台先建好)"},
        {"key": "SUB2API_GROK_PROXY_ID", "type": "int", "default": 0,
         "help": "可选：SUB2API 后台代理 ID，供 Grok SSO 转 OAuth 使用；0=不指定"},
    ]},
    {"group": "CPA(codex 授权文件导入)", "items": [
        {"key": "CPA_URL", "help": "CPA 管理接口地址"},
        {"key": "CPA_MGMT_KEY", "secret": True, "help": "CPA 管理 key"},
    ]},
    {"group": "chatgpt2api(普通网页号)", "items": [
        {"key": "CHATGPT2API_URL", "help": "chatgpt2api host(用 --import-c2a 时必填)"},
        {"key": "CHATGPT2API_KEY", "secret": True, "help": "chatgpt2api admin key"},
    ]},
    {"group": "webchat2api(Grok sso)", "items": [
        {"key": "WEBCHAT2API_URL", "help": "webchat2api 地址(用 Grok 时)"},
        {"key": "WEBCHAT2API_KEY", "secret": True, "help": "webchat2api key"},
    ]},
    {"group": "Codex add-phone 接码调参", "items": [
        {"key": "CODEX_PHONE_SKIP_ATTEMPTS", "default": "0", "help": "先赌免手机次数(0=直接接码)"},
        {"key": "CODEX_ADDPHONE_ATTEMPTS", "default": "2", "help": "接码换号上限次数"},
        {"key": "CODEX_SMS_TIMEOUT", "default": "150", "help": "单号等码超时(秒)"},
        {"key": "SMS_COUNTRY_BLACKLIST_OPENAI", "default": "261,63", "help": "拉黑号段(dialing code)"},
    ]},
    {"group": "GitHub Arkose 视觉投票(可选)", "items": [
        {"key": "VISION_API_BASE", "help": "主视觉网关(OpenAI 兼容)"},
        {"key": "VISION_API_KEY", "secret": True, "help": "主视觉网关 key"},
        {"key": "VOTE_ZZ_BASE", "help": "投票中转网关(gemini/gpt)"},
        {"key": "VOTE_ZZ_KEY", "secret": True, "help": "投票网关 gemini key"},
        {"key": "VOTE_GPT_KEY", "secret": True, "help": "投票网关 gpt key"},
        {"key": "VOTE_OPUS_BASE", "help": "claude opus 专用网关"},
        {"key": "VOTE_OPUS_KEY", "secret": True, "help": "opus 网关 key"},
    ]},
]


# 所有 schema 里出现的 .env key（用于读 .env 时补齐未在模板里的项不丢）
def env_keys():
    keys = []
    for g in ENV_SCHEMA:
        for it in g["items"]:
            keys.append(it["key"])
    return keys
