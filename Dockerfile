# 基础镜像地址可配置。国内直连 Docker Hub 基本拉不动，而部分 NAS 自带的
# 加速器（如飞牛的 docker.fnnas.com）需要账号登录、且 401 响应里不带
# WWW-Authenticate，Docker 拿不到认证入口，只能报
#   failed to resolve source metadata ... 401 Unauthorized
# 默认走 DaoCloud 的公共镜像源，实测 token 流程可用。
# 换源只要在 .env 里改 PYTHON_BASE（或 docker-compose.yml 的 build.args），
# 不用动这个文件。
ARG PYTHON_BASE=docker.m.daocloud.io/library/python:3.12-slim
FROM ${PYTHON_BASE}

# HOME 指向 /data：米家登录凭据由 mijiaAPI 写到 $HOME/.config/mijia-api/auth.json，
# 把 HOME 放到挂载卷里，容器重建后不必重新扫码。
# TZ 必须显式设置：定时任务用 datetime.now() 判定，容器默认 UTC 会让
# "每天 7:00 开灯" 实际在北京时间 15:00 触发。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/data \
    MIHOME_DATA_DIR=/data \
    MIJIA_AUTH_DIR=/data/.config/mijia-api \
    TZ=Asia/Shanghai

WORKDIR /app

# Debian 的软件源同样换成国内镜像。不换的话 apt-get update 会在
# deb.debian.org 上卡很久甚至超时；tzdata 虽小，卡住一样构建失败。
# 留空则保持官方源（境外机器或想排除变量时用）。
# 注意 bookworm 之后是 deb822 格式（/etc/apt/sources.list.d/debian.sources），
# 老格式是 /etc/apt/sources.list，两种都扫一遍才不至于换了个寂寞。
ARG DEBIAN_MIRROR=mirrors.aliyun.com
RUN set -eux; \
    if [ -n "${DEBIAN_MIRROR}" ]; then \
        grep -rl "deb.debian.org" /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null \
            | xargs -r sed -i "s|deb.debian.org|${DEBIAN_MIRROR}|g" || true; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends tzdata; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
# 国内构建可加 --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
# 注意：清华镜像缺少 mijiaAPI 包，实测不可用
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r requirements.txt

COPY server ./server
COPY web ./web

# 源码和静态资源必须对所有用户可读。COPY 进来的文件属主是 root，而容器
# 以非 root 运行（默认 10001，或 compose 里 user: 指定的 uid）。如果构建
# 上下文里的文件权限是 700——Windows 打包、部分同步工具、某些 NAS 上的
# 解压都会产生这种权限——非 root 进程连源码都读不了，容器一启动就
#   PermissionError: [Errno 13] Permission denied: '/app/server/__init__.py'
# 然后无限重启。这里显式放开读权限，不去依赖构建上下文的权限状态。
# /app 只需要读，不需要写，所以给 a+rX 而不是 a+rwX。
RUN chmod -R a+rX /app

# 全程非 root，进程里没有任何一步需要特权：
# 写权限靠「容器 uid == 挂载目录属主」来对齐，而不是靠 root 去 chown。
# 宿主机上把 ./data 交给 10001，或反过来用 compose 的 user: 指定成
# ./data 的属主 uid，两边一致就能直接读写。
# 凭据是全权限米家账号，业务进程顶着 root 跑等于把家门钥匙挂在门把上。
RUN useradd --uid 10001 --user-group --no-create-home \
        --shell /usr/sbin/nologin mihome \
    && mkdir -p /data \
    && chown 10001:10001 /data

USER 10001

VOLUME ["/data"]
EXPOSE 9778

# 用标准库探活，避免为 healthcheck 额外装 curl
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9778/healthz', timeout=4).read()"

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "9778"]
