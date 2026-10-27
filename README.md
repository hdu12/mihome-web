# MiHome Web

米家设备的**网页控制端**，移植自 [MiHome-Windows](https://github.com/huanyuejue/MiHome-Windows)。

后端常驻在 Docker 里，手机、平板、电脑浏览器打开就能用——不用开着 Windows 电脑，定时任务也不再依赖"程序必须运行中"。

底层复用原项目的 `app/core`（米家 API 适配层、设备规格解析、定时判定），未重写任何米家协议逻辑。

## 快速开始

**方式一：直接拉现成镜像**（不想在本机构建，NAS 上最快）：

```bash
docker pull hdu123/mihome-web:latest
```

镜像由 GitHub Actions 在每次代码更新后自动构建推送。

**方式二：本地构建**：

```bash
# 建议先设访问口令（全权限米家凭据，别裸奔）
echo 'WEB_TOKEN=换个你自己的口令' > .env

docker compose up -d --build
```

打开 `http://<你的机器IP>:9778`，页面会先要口令——输入 `.env` 里的 `WEB_TOKEN` 即可
（存在浏览器 localStorage，`?token=口令` 的写法也仍然支持，首次进入后地址栏会自动抹掉口令）。

用米家 App 扫码登录。凭据落在 `./data/` 里，容器重建不用重扫。

### 不用 Docker：本机 / 测试环境

机器上没有 Docker 时可以直接跑（Windows、Linux、macOS 都行）：

```bash
python -m venv .venv
# Windows: .venv\Scripts\python.exe -m pip install -r requirements.txt
#   国内加 -i https://mirrors.aliyun.com/pypi/simple/（清华源缺 mijiaAPI）
./.venv/bin/python -m pip install -r requirements.txt

cp .env.example .env    # 填 WEB_TOKEN
./run-local.sh          # Windows 直接双击 start.bat
```

监听 `0.0.0.0:9778`，同一 WiFi 下的手机、车机可以用 `http://<本机IP>:9778/?token=口令` 访问。

两个脚本最终都调用 `run_local.py`，`.env` 解析和数据目录规则只有这一份实现，避免 shell 与批处理各自漂移。

**局域网连不上先查防火墙**（Windows 默认拦入站）。用管理员身份打开 PowerShell 执行一次：

```powershell
New-NetFirewallRule -DisplayName "MiHome Web 9778" -Direction Inbound `
  -Protocol TCP -LocalPort 9778 -Action Allow -Profile Private,Domain
```

按端口放行而不是按程序放行——Python 路径变了（换 venv、升级运行时）不用重新加规则。若网络被 Windows 识别为「公用」，把 `Public` 也加进 `-Profile`，但那等于在咖啡厅 WiFi 上也开着端口，测试完记得关。

米家凭据位置由 `MIJIA_AUTH_DIR` 指定，默认 `data/.config/mijia-api/`；不设置时回落到 `~/.config/mijia-api/`（与桌面版 MiHome-Windows、mijiaAPI CLI 共用同一份凭据）。
注意：**两边同时运行会各自刷新 token**，可能互相把对方顶下线，长期用建议各留一份。

## 功能对照

| 功能 | 状态 | 说明 |
|---|:--:|---|
| 扫码登录 | 保留 | 二维码由后端渲染成 SVG，凭据持久化到数据卷 |
| 设备列表 | 保留 | 按家庭 / 房间分组，在线状态，可筛选 |
| 开关控制 | 保留 | 乐观更新 + 版本号防回弹（见下） |
| 设备详情 | 保留 | 按 spec 自动生成控件：开关 / 滑块 / 下拉 / 输入 |
| 动作执行 | 保留 | 带二次确认，文本类动作弹输入框 |
| 场景执行 | 保留 | 米家 App 里的手动场景，点击即执行 |
| 定时开关 | 保留 | 支持增删、启用/停用；**服务常驻，不再需要开着电脑** |
| 温湿度读数 | 保留 | 卡片副标题显示 |
| 小爱语音 | 保留 | 选中在线音箱 + 文本指令 |
| 多键面板虚拟开关 | 保留 | core 层已处理 `<父did>.sN` 的 siid 映射 |
| 系统托盘 | 移除 | 浏览器无对应物 |
| 桌面挂件（置顶 / 透明度 / 拖动） | 移除 | 浏览器做不到 |
| 开机自启动 | 移除 | 改由 Docker `restart: unless-stopped` 承担 |
| 主题跟随系统 / 界面缩放 | 升级 | 白天/夜间手动切换（顶栏按钮，localStorage 记住），首次访问跟随系统；缩放用浏览器自带 |
| 设备图标抓取 | 移除 | 原实现要逐型号抓 CDN，首屏会慢；改为纯文本卡片 |
| 设备卡片交互 | 重做 | 左右分栏：点左半进详情、右半开关电源；开启时整卡绿色高亮。触控设备（车机）自动再放大一档 |

## 架构

```
浏览器 (原生 JS，无构建)
    │  HTTP /api/*
    ▼
FastAPI (server/main.py)          ── 路由 + 鉴权 + 二维码
    │  await jobs.submit(...)
    ▼
串行任务队列 (server/jobs.py)      ── 单线程；上游 requests.Session 非线程安全
    │
    ▼
core 层 (server/core/)            ── 从原项目原样移植，未重写
    │
    ▼
mijiaAPI → 米家云端 HTTPS
```

原项目 `app/core` 分层设计（`service.py` 是唯一 import mijiaAPI 的模块、core 几乎不碰 Qt）是这次能低成本移植的前提。

**只改了两处**：`jobs.py` 用 `loop.call_soon_threadsafe` 替代 `QTimer` 投递；`_json_store.py` 的数据目录从 `%LOCALAPPDATA%` 改成识别 `/data`。

### 两处沿用的关键设计

**1. 操作版本号防回弹**
轮询是后台定时请求，用户点开关时可能已有一条旧结果在途。若直接覆盖，按钮会"点了又弹回去"。所以每次本地操作给该设备 `gen+1`，轮询提交时快照 `gen`，回来时 `gen` 已变的设备直接丢弃。原项目 `tests/race_test.py` 守的就是这个行为。

**2. 离线设备的开关值不可信**
米家云端对离线设备返回的是最后一次在线时的缓存值，轮询时显式跳过。

## 目录

```
server/
├── main.py                  FastAPI 路由
├── state.py                 设备状态、轮询循环、定时调度
├── jobs.py                  串行任务队列（替代原 Qt 版）
├── security.py              口令限流、安全响应头、HTTPS 兜底
└── core/                    从 MiHome-Windows 移植（GPL-3.0）
    ├── service.py           全项目唯一 import mijiaAPI 的模块
    ├── models.py            数据模型
    ├── cache.py             设备列表磁盘缓存
    ├── schedule_store.py    定时规则持久化
    ├── scheduler.py         定时判定（纯逻辑）
    ├── spec_zh.py           规格中文名兜底
    ├── icon_store.py / net_guard.py / _json_store.py
web/
├── index.html
├── app.js                   原生 JS，为老车机 WebView 做了语法降级
└── style.css
deploy/
├── Caddyfile                自动 HTTPS 反代配置
└── CLOUDFLARE-TUNNEL.md     无公网 IP 时的方案
docker-compose.yml           内网/本地
docker-compose.prod.yml      公网（应用 + Caddy）
```

## 公网访问（车机场景）

**先记住一件事：米家设备的控制 100% 走云端 HTTPS，不走局域网。**
所以后端**不需要跑在家里**——丢到任意一台 VPS 上就行，家里什么都不用开。
这大幅简化了公网访问：不用折腾家里的公网 IP、端口映射、DDNS。

三种方案按你的条件选：

| 你的条件 | 方案 | HTTPS | 上手 |
|---|---|---|:--:|
| **有 VPS / 云服务器** | 直接部署到 VPS | Caddy 自动签 | ★ 最推荐 |
| 家里有公网 IP + 域名 | 家里跑 + Caddy 反代 | Caddy 自动签 | 要域名解析 + 80/443 放行 |
| 都没有（运营商 NAT） | Cloudflare Tunnel | Cloudflare 终结 | 免费，但国内略慢 |

### 方案一：部署到 VPS（推荐）

```bash
git clone <你的仓库> && cd mihome-web
cp .env.example .env
# 填 DOMAIN=mihome.你的域名.com，WEB_TOKEN=32位随机串
docker compose -f docker-compose.prod.yml up -d --build
```

域名 A 记录指向 VPS，Caddy 会自动申请并续期 Let's Encrypt 证书。

> 境外/中国香港 VPS 不需要备案；国内 VPS 绑域名需先备案。

### 方案二：家里跑 + 公网 IP

同上，只是机器换成家里的。需要：公网 IP、域名解析、路由器放行 80/443。

### 方案三：Cloudflare Tunnel

不需要公网 IP。详见 [`deploy/CLOUDFLARE-TUNNEL.md`](deploy/CLOUDFLARE-TUNNEL.md)。

## 安全

访问口令 = **你家米家账号的全权限**，包括智能门锁、插座、燃气阀。公网部署前请把下面几条看完。

**已在代码里做的：**

- 口令定长时间比较（防计时侧信道）
- 连续错 6 次封 IP 30 分钟（`MAX_FAILURES` / `LOCK_SECONDS` 可调）
- 口令可开可关：`WEB_TOKEN` 留空即关闭这层鉴权（依赖前置反代），填值即恢复
- 口令长度下限 `MIN_TOKEN_LENGTH`（默认 **6 字符**）：低于下限**拒绝启动**；前缀反代兜底时短口令够用，端口裸奔公网请用 32 位随机串
- 前端通过 `/api/auth` 探测是否要口令，关闭时不会弹多余输入框
- 安全响应头：CSP、X-Frame-Options: DENY、Referrer-Policy 等
- `FORCE_HTTPS=1` 时拒绝任何非 HTTPS 请求，防止反代漏配 TLS 还浑然不知
- `TRUST_PROXY=1` 时按 `X-Forwarded-For` 计真实 IP（否则一次误输会锁死所有人）
- 容器非 root（uid 10001）运行
- **容器不管 TLS**：只监听 HTTP，证书和 HTTPS 全交给前面的反代。
  HSTS 默认不发（`HSTS_MAX_AGE=0`）——它是不可撤销的，容器看不见上游
  证书能撑多久，误发会让整个域名一年内只能走 https。要发就让反代发。

**需要你自己做的：**

1. **口令用 32 字节随机串**，别用能记住的密码：
   ```bash
   python -c "import secrets;print(secrets.token_urlsafe(32))"
   ```
2. **必须走 HTTPS**。HTTP 下口令在链路上明文裸奔，中间任何一跳都能读到。
3. **车机被他人接触 = 口令泄露**。口令存在车机 localStorage，洗车、代驾、借车都能直接读走。
   - 车经常外借 → 加 Cloudflare Access 的 Email OTP 那一层
   - 卖车/借车前 → 清一次浏览器数据，然后换口令重启服务
4. **`./data` 目录含米家凭据**，别提交 git、别打包分享、别丢到公共网盘。
5. 公网 IP 会在几十小时内被扫描器发现，这是常态。上面那几条不做好，等于把家门钥匙挂在门上。

## 车机浏览器兼容

车机 WebView 通常落后手机好几代。前端已针对性降级：

- 不用 `?.` / `??` / `CSS.escape` / 对象展开（老 Chromium 上是**语法错误**，整份脚本不执行，页面白屏）
- 补了 `NodeList.forEach` polyfill（要 Chrome 51+）
- CSS 不用 `inset` 简写

仍需 Chrome 55+（2016 年后）。若车机更老，打开是白屏，可先在手机浏览器验证功能是否正常，再考虑换车机。

界面按 5 英寸左右做了响应式：卡片单列、抽屉占满屏、触控目标 ≥ 44px。

## 常见问题

**定时任务时间不对**
检查 `TZ`。容器默认 UTC，`docker-compose.yml` 里已设 `Asia/Shanghai`，改过就别漏。

**容器重建后要重新扫码**
`./data` 没挂上，或挂到了别的路径。确认 `docker compose config` 里 volumes 生效。

**扫码后一直转圈**
后端 `qr_login_wait` 是阻塞长轮询（可达两分钟），期间会占住串行队列。网络不通时前端会自动重试；若持续失败，看 `docker compose logs -f` 里的具体错误。

**某个设备打不开详情**
常见于仅蓝牙连接、云端未下发 `spec_type` 的设备（手表、第三方牙刷等）。米家没有公开它的功能规格，原项目与本项目都无法提供控制面板。

**推送不是实时的**
前端每 5 秒拉一次状态（`REFRESH_MS`），后端每 `POLL_INTERVAL` 秒刷一次云端。调小会更及时，但会增加米家云端请求量。开关操作本身是即时下发、乐观更新的，点下去就有反馈。

## 许可

本项目沿用上游 **GPL-3.0-or-later**。

- `server/core/` 及米家业务逻辑来自 [MiHome-Windows](https://github.com/huanyuejue/MiHome-Windows)（GPL-3.0）
- 依赖 [mijia-api](https://github.com/Do1e/mijia-api)（GPL-3.0）

> 分发前端 JS 与后端代码同样须遵循 GPL-3.0。自行部署为内部服务不构成分发，但对外提供或分发代码时必须开源。

本程序不含任何担保。使用风险自负，并请遵守小米的服务条款。
