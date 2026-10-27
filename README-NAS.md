# NAS 部署说明

面向群晖 DSM（Container Manager）和威联通（Container Station）。
已经有 Docker 的 Linux 机器可以直接跳到第 3 节。

## 0. 部署前必读

**请先关掉其它正在跑的米家客户端**：本机上的 Python 服务、桌面版
MiHome-Windows 都要退出。

原因：多个实例共用同一份登录凭据时会各自刷新 token，互相顶掉线，
表现是"这边刚登录，那边就提示要重新扫码"。

## 1. 上传文件

用 File Station（或 scp）在 NAS 上建一个目录，例如
`/volume1/docker/mihome-web`，把解压出来的这些放进去：

```
Dockerfile
docker-compose.yml
requirements.txt
server/          后端
web/             前端
data/            凭据与缓存（见下方说明）
.env             实际配置（自己建）
```

> **关于 data/ 里的凭据**
> 包里的 `data/.config/mijia-api/auth.json` 是你已经登录好的米家账号凭据，
> 带上它可以省掉一次扫码。**它是全权限凭据**（能开锁、开插座），
> 压缩包别随手传给别人。不想带就删掉这个文件，部署后在页面上重新扫码即可。

## 2. 生成 .env

把 `.env.example` 复制为 `.env`，或用下面这份（记得改值）：

```bash
PORT=9778
WEB_TOKEN=粘贴32位随机串
PUID=10001
PGID=10001
PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
```

**生成口令**（本机或 NAS 上跑一下）：

```bash
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

**口令可以关，也可以随时开**

`WEB_TOKEN` 留空 = **关掉这层鉴权**，页面打开直接进，不再要求输入口令。
填上值就重新开启。改完 `.env` 重启容器即生效：

```bash
sudo docker compose up -d      # 改完 .env 后执行，会自动重建容器
```

前面有 Lucky 兜着的时候，关掉是合理选择——反正外面还有一道门。

**关于长度**：下限是 `MIN_TOKEN_LENGTH`，默认 **6 字符**，低于它服务拒绝启动。
前面有反代时 6~12 位够用；如果打算把端口直接暴露到公网（没有反代），
请用上面那条命令生成的 32 位随机串。

### 权限怎么处理（二选一，都不用给容器提权）

容器**全程以普通用户运行，从不以 root 启动，也不会去改你宿主机上的文件属主**。
写入 `./data` 靠的是「容器 uid == 目录属主」这个简单的对应关系。

**方式 A（推荐，一条命令搞定）** —— 把目录交给容器默认的 10001：

```bash
cd /volume1/docker/mihome-web
sudo chown -R 10001:10001 data
```

`PUID` / `PGID` 保持默认 `10001` 不用改。

**方式 B** —— 不动宿主机文件，反过来让容器用你的账号身份跑：

```bash
ls -nd data
# drwxr-xr-x 2 1026 100 4096 ...   ← 第一列数字是 uid=1026、gid=100
```

把那两个数字填进 `.env`：

```bash
PUID=1026
PGID=100
```

两种方式效果一样，选顺手的。配错了不会静默失败——启动日志第一屏就会打
`数据目录不可写`，并直接告诉你当前 uid 和该怎么改。

> 容器也不需要网络特权、不需要 `privileged`、不需要挂 Docker socket。
> 它只往外连米家云和监听一个端口。

## 3. 启动

### 群晖 DSM 7+（Container Manager 图形界面）

1. 套件中心安装 Container Manager
2. Container Manager → **项目** → 新增
3. 项目路径选 `/volume1/docker/mihome-web`
4. 它会读到 `docker-compose.yml`，一路下一步
5. 首次会构建镜像，取决于网速，通常 2–5 分钟
6. 容器状态变绿后访问 `http://<NAS的IP>:9778`

### 命令行（推荐，排错更方便）

```bash
cd /volume1/docker/mihome-web
sudo docker compose up -d --build
sudo docker compose logs -f      # 看启动日志，Ctrl+C 退出
sudo docker compose down         # 停止
```

### 构建卡在拉基础镜像？换源（国内必看）

第一次构建要先下载 `python:3.12-slim` 基础镜像。国内直连 Docker Hub
基本拉不动，很多 NAS 自带的加速器又需要账号登录：

```
ERROR [internal] load metadata for docker.io/library/python:3.12-slim
failed to resolve source metadata for docker.io/library/python:3.12-slim:
  unexpected status from HEAD request to https://docker.fnnas.com/...: 401 Unauthorized
```

**这跟本项目无关，是镜像地址的问题。** 飞牛的 `docker.fnnas.com` 就属于这种：
它返回 401 却**不给 `WWW-Authenticate` 头**，Docker 拿不到认证入口，连"去换
token"这一步都走不了，所以直接失败。

已默认改走 DaoCloud 的公共源（实测 token 流程可用），`Dockerfile` 里是：

```dockerfile
ARG PYTHON_BASE=docker.m.daocloud.io/library/python:3.12-slim
FROM ${PYTHON_BASE}
```

换源不用改 Dockerfile，在 `.env` 里加一行就行：

```bash
PYTHON_BASE=docker.m.daocloud.io/library/python:3.12-slim
# 备选：docker.1ms.run/library/python:3.12-slim
```

顺手把 Debian 的 apt 源也换了（`DEBIAN_MIRROR=mirrors.aliyun.com`），
不然下一步会卡在 `deb.debian.org`，看起来像是另一个问题。

**想彻底不依赖公共镜像源**：手动拉一次、改成官方名字，之后构建就用本地缓存：

```bash
docker pull docker.m.daocloud.io/library/python:3.12-slim
docker tag  docker.m.daocloud.io/library/python:3.12-slim python:3.12-slim
# 再把 .env 里的 PYTHON_BASE 改成 python:3.12-slim
```

## 4. 首次访问

```
http://<NAS的IP>:9778/?token=你填的WEB_TOKEN
```

页面会先要一次口令，之后存在浏览器 localStorage，直接访问 IP 即可。

同一个 WiFi 下用手机、车机的浏览器打开这个地址就能用。

## 5. 前面挂 Lucky 做 HTTPS（可选）

**容器只监听 HTTP，不碰证书。** TLS 全部由 Lucky 负责，所以容器这边
不需要挂证书、不需要开 443、也不需要任何 HTTPS 相关配置。

Lucky → Web 服务 → 添加规则：

| 字段 | 填 |
|---|---|
| 前端域名 | `mi.你的域名.com`（不带端口） |
| 监听端口 | 对外要用的 HTTPS 端口，比如 `9412` |
| 后端地址 | `http://<NAS的局域网IP>:9778` |
| 协议 | **http**（容器没有 TLS，填 https 会握手失败） |

然后在该规则的 HTTPS 设置里选证书（Lucky 支持自动申请 Let's Encrypt）。

### 后端地址那一栏怎么填（最常见的 502 原因）

`9778` 是**容器**的端口，`9412` 是**Lucky** 的端口，两者互不相干，不需要
把容器改成 9412。真正要填对的是 Lucky 用什么地址找到容器：

| Lucky 的部署方式 | 后端地址填 |
|---|---|
| 宿主网络（host network，Docker 版 Lucky 常用） | `http://127.0.0.1:9778` |
| 桥接网络（bridge） | `http://<NAS的局域网IP>:9778` |
| 与 mihome-web 在同一自定义网络 | `http://mihome-web:9778` |

**桥接网络下填 `127.0.0.1` 一定 502**——那是 Lucky 容器自己的回环，
里面没有你的服务。判断方法：在 Lucky 容器里执行
`curl -I http://127.0.0.1:9778/healthz`，通就是对的。

### 用非标准端口（比如 https://mi.example.com:9412）

完全可以。这几件事要对上：

1. **证书要覆盖 `mi.example.com`**。Lucky 申请 Let's Encrypt 时域名填
   `mi.example.com`（或泛域名 `*.example.com`），和地址栏里的主机名逐字一致。
2. **路由器/防火墙放行 9412/tcp**，映射到 NAS 的 9412。容器那个 9778
   **不要**映射到公网，它是内网的最后一跳。
3. **Lucky 里前端域名填 `mi.example.com`，不要带 `:9412`**。端口是监听项，
   不是域名的一部分。填成带端口的写法多数版本匹配不上，会落到默认站点
   或者证书报错——症状是打开是一个空白页或别人的站点。
4. **口令按地址分别存**。浏览器的 localStorage 是按 origin 隔离的，
   `https://mi.example.com:9412` 和 `http://192.168.1.x:9778` 是两份存储。
   在外面输过口令，回家用内网 IP 还要再输一次——这是正常现象，不是 bug。
5. **别给这个域名开 HSTS**。HSTS 是**按主机名**生效、**忽略端口**的。一旦
   下发，`mi.example.com` 的任何端口都只能走 https，以后想临时用 http 调试
   就没机会了。容器默认不发（`HSTS_MAX_AGE=0`），Lucky 那边也别开。

### 要确认的转发头

容器靠这两个头判断来源和协议：

- **`X-Forwarded-For`** —— 真实客户端 IP。Lucky 默认会透传。
  不传的话所有访问在容器眼里都是同一个 IP，**一个人输错 6 次口令，
  全家人一起被锁 30 分钟**。
- **`X-Forwarded-Proto`** —— 只有在你设了 `FORCE_HTTPS=1` 时才需要。
  默认是 0，不用管。

### 三个别做的事

1. **别在容器上开 `FORCE_HTTPS=1`**，除非你能确认 Lucky 传了
   `X-Forwarded-Proto: https`。传了没事，没传就是整站 403，
   而且页面白屏看不出原因。
2. **别在容器上开 `HSTS_MAX_AGE`**。HSTS 一旦发出去浏览器就记住了，
   一两年内这个域名只能走 https，撤都撤不掉。要开就在 Lucky 里开。
3. **别把 Lucky 的后端地址写成 `https://`**，容器那边是明文，握手直接失败。

配好之后：`https://mi.你的域名.com:9412` 正常，`http://<NAS的IP>:9778`
也照样能用（内网直连，方便排查）。

### 出问题时的排查顺序（由内往外，三层）

```bash
# 第 1 层：容器自己活着吗（在 NAS 宿主机上执行）
curl -I http://127.0.0.1:9778/healthz          # 期望 200

# 第 2 层：从 Lucky 的视角能连到容器吗（在 Lucky 容器里执行，或换机器执行）
curl -I http://<NAS的IP>:9778/healthz          # 期望 200

# 第 3 层：走域名（在你自己的电脑上执行）
curl -I https://mi.example.com:9412/healthz       # 期望 200
```

哪一层断，问题就在那一层：

- 第 1 层失败 → 容器没起来或端口没映射，看 `docker compose logs`
- 第 2 层失败 → Lucky 的后端地址填错（bridge 网络填了 `127.0.0.1`），
  或防火墙挡了内网访问
- 第 3 层失败 → 端口映射/防火墙没放行 9412，或证书域名不匹配

> **想让 9778 只对内网开放？** 现在的端口映射是 `0.0.0.0:9778`，同一个
> 局域网里任何设备都能绕过 Lucky 直连（口令鉴权还在，只是不加密）。
> 收紧的话把 `docker-compose.yml` 里那行改成 `- "127.0.0.1:9778:9778"`——
> **前提是 Lucky 用宿主网络**（此时 `127.0.0.1` 就是 NAS 本机）。
> Lucky 是桥接网络或跑在别的机器上就保持原样，否则它自己也连不上，
> 表现为整站 502。

## 6. 常见问题

**构建卡在拉 python 基础镜像 / pip 下载慢**
国内网络访问 Docker Hub 和 PyPI 都不稳，三种情况分开处理：

- **报 `401 Unauthorized` 或 `failed to resolve source metadata`**
  → 是镜像地址的问题，不是项目的问题。飞牛自带的 `docker.fnnas.com` 需要
  账号登录且不给 `WWW-Authenticate`，Docker 无法认证。按第 3 节「构建卡在
  拉基础镜像」换源，改 `.env` 里的 `PYTHON_BASE`。
- **卡在 `deb.debian.org`** → 改 `.env` 里的 `DEBIAN_MIRROR`（默认已是阿里云）。
- **pip 下载慢或超时** → `.env` 里 `PIP_INDEX_URL` 改成阿里云（默认已给）。

**端口被占用**
群晖的 DSM 自身常用 5000/5001，Web Station 常用 80/8080/8000。
默认用的是 9778，撞上了就改 `.env` 里的 `PORT`，然后 `docker compose up -d` 重建。
容器内部固定监听 9778，`PORT` 只影响宿主机这一侧的映射。

**页面能开但一直是"未登录"**
先看启动日志里有没有这句：

```bash
docker compose logs | grep "数据目录不可写"
```

有 → 就是第 2 节的权限没对上，按提示改 `chown` 或 `PUID/PGID`。
没有 → 多半是凭据过期，删掉 `data/.config/mijia-api/auth.json` 重新扫码。

**输错一次口令，所有人都登不进去，提示封禁中**
这是限流把所有请求当成了同一个来源 IP —— 反代没传 `X-Forwarded-For`，
或者容器这边 `TRUST_PROXY=0`。确认 `.env` 里 `TRUST_PROXY=1`，
再检查 Lucky 规则里的转发设置。改完重启容器，封禁记录在内存里，
重启即清空。

**经 Lucky 访问报 502 / 502 Bad Gateway**
按三个常见原因依次排：① 后端地址填了 `https://`（容器没有证书，必须 `http://`）；
② **Lucky 是桥接网络却填了 `127.0.0.1`**（见第 5 节那张表，改成 NAS 局域网 IP）；
③ 端口和 `.env` 的 `PORT` 不一致（默认 9778）。在 Lucky 所在的机器上
`curl -I http://<NAS的IP>:9778/healthz`，通了就是 Lucky 的地址配置问题。

**打开是空白页，或者显示了别的站点**
Lucky 的前端域名填成了带端口的写法（`mi.example.com:9412`），匹配不上就落到
默认站点去了。域名栏只填主机名，端口填在"监听端口"，两处分开。

**经 Lucky 访问整页 403，内容是"必须通过 HTTPS 访问"**
容器侧开了 `FORCE_HTTPS=1`，但 Lucky 没传 `X-Forwarded-Proto`。
改回 `FORCE_HTTPS=0`（推荐），或去 Lucky 里补上这个头。

**换机器 / 重新扫码**
删掉 `data/.config/mijia-api/auth.json`，重启容器，页面上重新扫码。

**更新到新版本**
替换 `server/`、`web/`、`Dockerfile` 后重新构建：

```bash
sudo docker compose up -d --build
```

`data/` 在卷里，凭据和定时规则不会丢。

## 7. 安全提醒

- `WEB_TOKEN` 关联的是米家账号的完整控制权限，**一定要设**
- 家里局域网用没问题；要在公司或车上远程控制，请先想清楚暴露面，
  参考 `README.md` 的公网部署章节（Caddy 自动 HTTPS / Cloudflare Tunnel）
- `data/` 目录不要提交到任何 Git 仓库
