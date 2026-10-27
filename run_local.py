# SPDX-License-Identifier: GPL-3.0-or-later
"""本地 / 测试环境启动入口。

把 `.env` 的解析收在这里，shell 脚本和 Windows 双击启动共用同一份逻辑，
避免两边解析规则漂移（Docker 部署走 compose，不经过本文件）。

监听 0.0.0.0 而不是 127.0.0.1：测试环境要让同网段的手机、车机连进来。
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    """极简 .env 解析：只支持 KEY=VALUE，# 注释与空行忽略。

    已存在的环境变量优先（`setdefault`），这样命令行临时覆盖仍然有效。
    """
    if not path.exists():
        print(f"缺少 {path}，请先复制 .env.example 为 .env 并填入 WEB_TOKEN", file=sys.stderr)
        raise SystemExit(1)
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def main() -> None:
    load_dotenv(ROOT / ".env")

    # 数据目录与米家凭据都收在项目 data 下，和容器里的 /data 卷一一对应
    data_dir = ROOT / "data"
    os.environ.setdefault("MIHOME_DATA_DIR", str(data_dir))
    os.environ.setdefault("MIJIA_AUTH_DIR", str(data_dir / ".config" / "mijia-api"))
    (data_dir / ".config" / "mijia-api").mkdir(parents=True, exist_ok=True)

    if not os.environ.get("WEB_TOKEN"):
        print("警告: 未设置 WEB_TOKEN，接口完全公开", file=sys.stderr)

    import uvicorn

    port = int(os.environ.get("PORT", "9778"))
    print(f"数据目录: {os.environ['MIHOME_DATA_DIR']}")
    print(f"访问地址: http://0.0.0.0:{port}  （局域网用本机 IP 加该端口）")
    uvicorn.run("server.main:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
