# 没有公网 IP：Cloudflare Tunnel

家里宽带拿不到公网 IP（运营商 NAT）时，用 Cloudflare Tunnel 把服务
"反向拉"出去。不需要开端口、不需要公网 IP，TLS 由 Cloudflare 终结。

**代价**：流量绕经 Cloudflare 边缘节点，国内访问可能比直连慢
（通常 100–300ms），车机在 4G 下刷开关够用，但不如直连跟手。

## 前置

- 一个域名，且 NS 已托管到 Cloudflare（免费套餐够用）
- 家里那台跑 Docker 的机器保持开机

## 步骤

### 1. 创建隧道

在 Cloudflare Zero Trust 控制台 → Networks → Tunnels → Create a tunnel，
选 **Cloudflared**，拿到一长串 token（形如 `eyJhIjoi...`）。

### 2. 起服务

在项目根目录新建 `docker-compose.tunnel.yml`：

```yaml
services:
  mihome-web:
    build: .
    restart: unless-stopped
    expose:
      - "9778"
    environment:
      TZ: Asia/Shanghai
      WEB_TOKEN: "${WEB_TOKEN:?}"
      TRUST_PROXY: "1"
      FORCE_HTTPS: "1"
      POLL_INTERVAL: "15"
    volumes:
      - ./data:/data

  cloudflared:
    image: cloudflare/cloudflared:latest
    restart: unless-stopped
    command: tunnel --no-autoupdate run
    environment:
      # 填控制台给的那串 token
      TUNNEL_TOKEN: "${TUNNEL_TOKEN:?}"
    depends_on:
      - mihome-web
```

```bash
echo 'WEB_TOKEN=你的随机口令' >> .env
echo 'TUNNEL_TOKEN=控制台给的token' >> .env
docker compose -f docker-compose.tunnel.yml up -d --build
```

### 3. 绑定域名

回到控制台该隧道的 **Public Hostnames** → Add：

| 字段 | 填 |
|---|---|
| Subdomain | `mihome` |
| Domain | 你的域名 |
| Service | `http://mihome-web:9778` |

保存后 `https://mihome.yourdomain.com` 即可访问。

## 建议再加一道锁

Cloudflare Zero Trust → Access → Applications，为该域名加策略，
类型选 **Email OTP** 或 **Service Token**。

只要加了这一层，即使 `WEB_TOKEN` 泄露，攻击者也进不来。

但**车机上每次输 OTP 会很烦**。折中做法：

- 用 **Service Token**：生成一对 Client ID / Secret，车机浏览器不支持自定义头，
  这条路走不通；
- 用 **Bypass 策略 + IP 白名单**：把常去地点的出口 IP 加白，其他一律拦。
  车机走 4G 时 IP 会变，不适用；
- **只靠 WEB_TOKEN + 强随机**（32 字节），并且**定期更换**。

如果车经常被他人使用（代驾、洗车、借车），强烈建议额外加 Access Email OTP，
宁可麻烦一点。

## 别忘了

- 车机浏览器访问一次后，口令存在 localStorage 里。**卖车或借车前清一次浏览器数据**。
- 隧道断了页面会打不开，但**后端仍在运行**，定时任务照常执行。
