# SPDX-License-Identifier: GPL-3.0-or-later
"""公网暴露时的加固：口令限流、安全响应头、HTTPS 兜底、代理头解析。

**为什么必须加固**：本服务的访问口令等价于米家账号的全权限——能开智能
门锁、插座、燃气阀。放到公网后，真正要防的不是"被爆破"（32 字节随机
口令在数学上爆不出来），而是下面三件事：

1. **明文链路被截获**：HTTP 下车机浏览器与服务器之间任何一跳都能读到口令。
   → `FORCE_HTTPS=1` 时拒绝非 HTTPS 请求，防止反代配置漏掉 TLS 还浑然不知。
2. **车机被他人物理接触**：口令存在车机 localStorage 里，洗车、代驾、借车
   都能直接读走。→ 无法在代码层彻底解决，只能靠"用完即换"，见 README。
3. **扫描器高频探测打挂接口**：公网 IP 会在几十小时内被扫到。
   → 失败计数 + 临时封禁，全局并发也有上限。

限流状态放内存即可：单实例部署，重启丢失可接受（重启本身也不是攻击面）。
"""

import logging
import os
import time

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# 同一 IP 在窗口期内允许的失败次数，超出后封禁
MAX_FAILURES = int(os.environ.get("MAX_FAILURES", "6"))
FAIL_WINDOW = int(os.environ.get("FAIL_WINDOW", "900"))      # 15 分钟
LOCK_SECONDS = int(os.environ.get("LOCK_SECONDS", "1800"))   # 封 30 分钟

# 反代（Lucky/Nginx/Caddy/Cloudflare）后必须信任 X-Forwarded-For，否则限流会把
# 所有请求当成同一个 IP（反代地址），一次误输就锁死所有人
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"
# 容器本身只监听 HTTP，TLS 交给前面的反代。所以默认不做任何 HTTPS 强制：
# 反代漏配 TLS 时宁可先跑通，也不要让容器在中间插一脚把人挡在门外。
# 需要"见到明文就拒绝"这种兜底时再设 FORCE_HTTPS=1（反代必须传 X-Forwarded-Proto）。
FORCE_HTTPS = os.environ.get("FORCE_HTTPS", "0") == "1"
# HSTS 默认不发（0）。下发是不可撤销的：浏览器会记住这个域名（一般一年），
# 之后连 http 都自动升级成 https。容器只看见明文，根本不知道上游证书能撑多久，
# 一旦上游撤了证书，全站变成打不开，而容器没有任何办法把它撤回来。
# 要发就让反代自己发——它才是那个清楚 TLS 配置的人。
# 确需容器发时设 HSTS_MAX_AGE=31536000（秒）。
HSTS_MAX_AGE = int(os.environ.get("HSTS_MAX_AGE", "0"))
# 口令长度下限。默认 6——前置反代（Lucky 等）已经是一层验证时，口令只是
# 第二道门，卡到 16 位只会逼人把口令写进便签贴在显示器上。
# 真的把端口裸奔到公网、前面什么都不挂，请自己把它调高。
MIN_TOKEN_LENGTH = int(os.environ.get("MIN_TOKEN_LENGTH", "6"))
# 健康检查不参与 HTTPS 强制，否则容器 healthcheck 会一直失败
_HTTPS_EXEMPT = ("/healthz",)


class RateLimiter:
    """按 key（客户端 IP）统计失败次数并临时封禁。"""

    def __init__(self) -> None:
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def locked_for(self, key: str) -> int:
        """剩余封禁秒数；0 表示未封禁。"""
        until = self._locked_until.get(key, 0.0)
        remain = until - time.time()
        if remain <= 0:
            self._locked_until.pop(key, None)
            return 0
        return int(remain)

    def record_failure(self, key: str) -> None:
        now = time.time()
        hits = [t for t in self._failures.get(key, []) if now - t < FAIL_WINDOW]
        hits.append(now)
        self._failures[key] = hits
        if len(hits) >= MAX_FAILURES:
            self._locked_until[key] = now + LOCK_SECONDS
            self._failures.pop(key, None)
            logger.warning("口令连续错误 %d 次，封禁 %s %d 分钟",
                           len(hits), key, LOCK_SECONDS // 60)

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)
        self._locked_until.pop(key, None)

    def prune(self) -> None:
        """定期清理过期记录，避免公网扫描把内存撑大。"""
        now = time.time()
        for key, hits in list(self._failures.items()):
            kept = [t for t in hits if now - t < FAIL_WINDOW]
            if kept:
                self._failures[key] = kept
            else:
                self._failures.pop(key, None)
        for key, until in list(self._locked_until.items()):
            if until <= now:
                self._locked_until.pop(key, None)


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    """取真实客户端 IP。

    信任反代时必须读 X-Forwarded-For：Caddy/Nginx/Cloudflare 会把真实来源
    放在第一个位置。不信任时一律用直连地址——否则任何人都能伪造该头绕过
    限流（把自己伪装成一个新 IP）。
    """
    if TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real = request.headers.get("x-real-ip", "")
        if real:
            return real.strip()
    return request.client.host if request.client else "unknown"


def is_https(request: Request) -> bool:
    if TRUST_PROXY:
        proto = request.headers.get("x-forwarded-proto", "")
        if proto:
            return proto.split(",")[0].strip() == "https"
    return request.url.scheme == "https"


_last_prune = 0.0


async def secure_headers(request: Request, call_next):
    """统一加安全响应头，并在开启强制 HTTPS 时拦截明文请求。"""
    global _last_prune
    now = time.time()
    if now - _last_prune > 60:
        _last_prune = now
        limiter.prune()

    if FORCE_HTTPS and request.url.path not in _HTTPS_EXEMPT and not is_https(request):
        return JSONResponse(
            status_code=403,
            content={"detail": "必须通过 HTTPS 访问。若已配置反代，"
                               "请确认传递了 X-Forwarded-Proto: https"},
        )

    response = await call_next(request)
    headers = response.headers
    headers["X-Content-Type-Options"] = "nosniff"
    headers["X-Frame-Options"] = "DENY"
    headers["Referrer-Policy"] = "no-referrer"
    headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    # 前端唯一在 head 里跑的主题脚本已外部化为 theme.js，CSP 可以卡死到 self +
    # 内联样式（控件靠 style 属性）。不要再往页面里加内联 <script>，会被拦。
    headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    )
    # HSTS：默认不发（HSTS_MAX_AGE=0）。不带 includeSubDomains——家用 NAS 上
    # 同一个域名下往往还挂着别的纯 HTTP 服务，一并绑死会连累它们。
    if HSTS_MAX_AGE > 0 and is_https(request):
        headers["Strict-Transport-Security"] = f"max-age={HSTS_MAX_AGE}"
    # 前端文件一律不缓存：这是单机/内网服务，体积才几十 KB，
    # 而"改了 JS 但浏览器还在跑旧版本"是最难查的一类问题
    path = request.url.path
    if path == "/" or path.endswith((".html", ".js", ".css")):
        headers["Cache-Control"] = "no-store, must-revalidate"
    return response


def check_token_strength(token: str) -> None:
    """启动时校验口令强度。`token` 为空表示关掉了这层鉴权，直接放行。

    门槛（MIN_TOKEN_LENGTH，默认 6）刻意设得低。本项目的典型部署是
    「内网 NAS + 前置反代（Lucky/Nginx）+ 反代上再加一层验证」，口令只是
    第二道门，暴力破解会先在反代那层被拦掉。硬卡 16 位的结果通常不是
    更安全，而是口令被写进便签。
    """
    if not token:
        return
    if len(token) < MIN_TOKEN_LENGTH:
        raise RuntimeError(
            f"WEB_TOKEN 只有 {len(token)} 个字符，低于下限 {MIN_TOKEN_LENGTH}。"
            "要么加长，要么清空 WEB_TOKEN 彻底关掉这层（前面有反代时可行），"
            "也可以在 .env 里调低 MIN_TOKEN_LENGTH"
        )
    weak = {"123456", "password", "admin", "mihome", "token", "test"}
    if token.lower() in weak:
        raise RuntimeError("WEB_TOKEN 是常见弱口令，请更换")
