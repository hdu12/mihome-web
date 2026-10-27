# SPDX-License-Identifier: GPL-3.0-or-later
"""MiHome Web —— 米家设备的网页控制端（后端）。

业务逻辑全部复用 MiHome-Windows 的 app/core（GPL-3.0），本文件只负责
把它暴露成 HTTP 接口。所有米家网络调用都交给 server/jobs.py 的串行队列，
避免阻塞事件循环，也避免并发打爆云端接口。

部署提示：米家登录凭据落在 $HOME/.config/mijia-api/auth.json，容器里
HOME 指向 /data，因此 **/data 必须挂卷**，否则重建容器要重新扫码。
"""

import asyncio
import io
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import qrcode
import qrcode.image.svg
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import security
from .core import schedule_store
from .core.models import SceneInfo
from .core.service import ServiceError
from .jobs import PRIORITY_CONTROL, PRIORITY_DATA
from .state import AppState

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("mihome")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "15"))
# 设置后所有 /api 请求必须带该 token（header X-Token 或 ?token=）。
# 凭据是全权限米家账号，公网部署时务必设置。
WEB_TOKEN = os.environ.get("WEB_TOKEN", "").strip()

state: AppState | None = None
# sid -> {"data": dict, "status": str, "error": str|None, "started": bool}
_login_sessions: dict[str, dict] = {}


# ---------- 鉴权 ----------

def _token_matches(given: str | None) -> bool:
    """定长时间比较，避免逐字节比较被计时侧信道利用。"""
    if not given:
        return False
    return secrets.compare_digest(given, WEB_TOKEN)


async def require_token(request: Request,
                        x_token: str | None = Header(default=None),
                        token: str | None = Query(default=None)) -> None:
    if not WEB_TOKEN:
        return
    ip = security.client_ip(request)
    remain = security.limiter.locked_for(ip)
    if remain:
        raise HTTPException(
            status_code=429,
            detail=f"口令错误次数过多，已临时封禁 {remain // 60 + 1} 分钟")
    if _token_matches(x_token) or _token_matches(token):
        security.limiter.clear(ip)
        return
    security.limiter.record_failure(ip)
    raise HTTPException(status_code=401, detail="token 无效")


# ---------- 生命周期 ----------

def _check_data_writable(path: Path) -> None:
    """启动自检：数据卷写不进去时，故障会推迟到"扫码登录之后"才爆。

    症状是登录成功、重启又变未登录，报错埋在上游库的 PermissionError 里，
    很难往权限上想。所以启动时先探一下，一次把话说明白。

    只做检查，不尝试 chown —— 容器全程以普通 uid 运行，没有特权。
    """
    probe = path / ".write-probe"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        uid = getattr(os, "getuid", lambda: "?")()
        gid = getattr(os, "getgid", lambda: "?")()
        logger.error(
            "数据目录不可写：%s（%s）\n"
            "  容器进程 uid=%s gid=%s，挂载目录的属主必须是同一个 uid。\n"
            "  修法二选一：\n"
            "    1) 宿主机上执行 chown -R %s:%s ./data\n"
            "    2) ls -nd ./data 看属主数字，填进 .env 的 PUID/PGID\n"
            "  不修的话表现为「扫码登录成功，重启后又变未登录」。",
            path, exc, uid, gid, uid, gid)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global state
    state = AppState(asyncio.get_running_loop(), poll_interval=POLL_INTERVAL)
    state.load_cache()
    tasks = [
        asyncio.create_task(state.poll_loop(), name="poll"),
        asyncio.create_task(state.schedule_loop(), name="schedule"),
    ]
    from .core import _json_store
    logger.info("数据目录: %s", _json_store.data_dir())
    _check_data_writable(_json_store.data_dir())
    logger.info("米家凭据: %s", Path.home() / ".config" / "mijia-api" / "auth.json")
    security.check_token_strength(WEB_TOKEN)
    if not WEB_TOKEN:
        # 关掉口令是明确支持的用法（前面有反代时），不是配置失误。
        # 只有"没有反代又没口令"才值得警惕——那是真的裸奔。
        if security.TRUST_PROXY:
            logger.info("未设置 WEB_TOKEN：本服务不做鉴权，防护交给前置反代那层")
        else:
            logger.warning(
                "未设置 WEB_TOKEN 且未开启 TRUST_PROXY：接口完全公开。"
                "只适合本机回环或纯内网；公网请配口令或务必在反代上加验证")
    else:
        logger.info("已启用口令鉴权（%d 字符）", len(WEB_TOKEN))
    yield
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    state.jobs.shutdown()


app = FastAPI(title="MiHome Web", version="0.1.0", lifespan=lifespan)
# 安全响应头 + 强制 HTTPS 兜底，对静态文件同样生效
app.middleware("http")(security.secure_headers)


def svc() -> AppState:
    if state is None:
        raise HTTPException(status_code=503, detail="服务尚未就绪")
    return state


# ---------- 状态与登录 ----------

@app.get("/api/auth")
async def api_auth():
    """前端启动时先问一句：这个部署要不要口令。

    故意不加鉴权依赖——它只回一个布尔值，没有敏感信息。前端不能靠
    "本地存过 token 没有"来判断，否则口令被关掉后页面会卡在一个
    多余的输入框上，用户随便敲一个也能进，只会让人以为坏了。
    """
    return {"required": bool(WEB_TOKEN)}


@app.get("/api/status", dependencies=[Depends(require_token)])
async def api_status():
    s = svc()
    return {
        "logged_in": s.logged_in,
        "api_version": s.service.api_version(),
        "poll_interval": s.poll_interval,
        "updated_at": s.updated_at,
        "last_error": s.last_error,
    }


@app.post("/api/login/begin", dependencies=[Depends(require_token)])
async def login_begin():
    s = svc()
    try:
        data = await s.jobs.submit(s.service.qr_login_begin, PRIORITY_CONTROL)
    except ServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if data is None:
        # 本地凭据仍有效，无需扫码
        return {"need_qr": False}
    sid = uuid.uuid4().hex[:12]
    _login_sessions[sid] = {
        "data": data, "status": "pending", "error": None, "started": False}
    return {"need_qr": True, "sid": sid, "qr_url": data.get("loginUrl", "")}


@app.get("/api/login/qr/{sid}.svg", dependencies=[Depends(require_token)])
async def login_qr(sid: str, token: str | None = Query(default=None)):
    """把登录 URL 渲染成二维码 SVG。

    用 qrcode 的纯 Python SVG 后端，避免为了一张二维码引入 pillow。
    """
    session = _login_sessions.get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="登录会话不存在或已过期")
    url = session["data"].get("loginUrl", "")
    if not url:
        raise HTTPException(status_code=400, detail="登录数据缺少 loginUrl")
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml")


@app.post("/api/login/wait/{sid}", dependencies=[Depends(require_token)])
async def login_wait(sid: str):
    """启动并查询扫码等待。

    上游 qr_login_wait 是阻塞长轮询（可达两分钟），放在串行队列里跑一次，
    前端反复调本接口取状态即可。
    """
    s = svc()
    session = _login_sessions.get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="登录会话不存在或已过期")

    if not session["started"]:
        session["started"] = True

        def _wait() -> None:
            s.service.qr_login_wait(session["data"])

        async def _runner() -> None:
            try:
                await s.jobs.submit(_wait, PRIORITY_CONTROL)
            except ServiceError as exc:
                session["status"] = "error"
                session["error"] = str(exc)
                return
            except Exception as exc:  # noqa: BLE001
                session["status"] = "error"
                session["error"] = str(exc)
                return
            session["status"] = "ok"
            s.reload_service()
            _login_sessions.pop(sid, None)
            asyncio.create_task(s.poll_once())

        asyncio.create_task(_runner())

    return {"status": session["status"], "error": session["error"]}


@app.post("/api/logout", dependencies=[Depends(require_token)])
async def logout():
    """删除本地凭据，回到未登录状态。"""
    s = svc()
    auth_path = Path.home() / ".config" / "mijia-api" / "auth.json"
    try:
        auth_path.unlink(missing_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"删除凭据失败: {exc}") from exc
    s.reload_service()
    return {"ok": True}


# ---------- 设备 ----------

def _device_json(s: AppState, d) -> dict:
    return {
        "did": d.did,
        "name": d.name,
        "model": d.model,
        "home_name": d.home_name,
        "room_name": d.room_name,
        "online": d.online,
        "spec_type": d.spec_type,
        # 灯组成员：前端据此隐藏组内单灯、只保留灯组本身
        "group_did": d.group_did,
        "group_name": d.group_name,
        "power": s.known_power.get(d.did),
        "metric": s.metrics.get(d.did),
    }


@app.get("/api/devices", dependencies=[Depends(require_token)])
async def api_devices():
    s = svc()
    return {
        "devices": [_device_json(s, d) for d in s.devices],
        "updated_at": s.updated_at,
        "last_error": s.last_error,
        "events": s.drain_events(),
    }


@app.post("/api/refresh", dependencies=[Depends(require_token)])
async def api_refresh():
    """立即刷新一次（后台执行，不阻塞返回）。"""
    s = svc()
    asyncio.create_task(s.poll_once())
    return {"ok": True}


@app.get("/api/devices/{did}/detail", dependencies=[Depends(require_token)])
async def device_detail(did: str):
    s = svc()
    try:
        detail = await s.jobs.submit(lambda: s.service.device_detail(did), PRIORITY_DATA)
    except ServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "did": detail.did,
        "name": detail.name,
        "model": detail.model,
        "props": [
            {
                "name": p.name,
                "desc": p.desc,
                "type": p.type,
                "readable": p.readable,
                "writable": p.writable,
                "range": list(p.range) if p.range else None,
                "value_list": p.value_list or [],
            }
            for p in detail.props
        ],
        "actions": [{"name": a.name, "desc": a.desc} for a in detail.actions],
    }


@app.get("/api/devices/{did}/props", dependencies=[Depends(require_token)])
async def read_props(did: str):
    """批量读取当前值，一次请求替代逐个轮询。"""
    s = svc()
    try:
        detail = await s.jobs.submit(lambda: s.service.device_detail(did), PRIORITY_DATA)
        names = [p.name for p in detail.props if p.readable]
        if not names:
            return {"values": {}}
        values = await s.jobs.submit(
            lambda: s.service.read_props(did, names), PRIORITY_DATA)
    except ServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"values": values}


class PropWrite(BaseModel):
    name: str
    value: object


@app.post("/api/devices/{did}/prop", dependencies=[Depends(require_token)])
async def write_prop(did: str, body: PropWrite):
    s = svc()
    # 按 spec 声明的类型收敛：云端对类型不匹配的属性写入会直接报错
    value = await _coerce_value(s, did, body.name, body.value)
    try:
        await s.jobs.submit(
            lambda: s.service.write_prop(did, body.name, value), PRIORITY_CONTROL)
    except ServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if body.name == "on":
        s.known_power[did] = bool(value)
        s.bump(did)
    return {"ok": True, "value": value}


async def _coerce_value(s: AppState, did: str, name: str, value):
    try:
        detail = await s.jobs.submit(lambda: s.service.device_detail(did), PRIORITY_DATA)
    except Exception:  # noqa: BLE001 - 拿不到 spec 就原样下发，让云端判定
        return value
    for prop in detail.props:
        if prop.name != name:
            continue
        try:
            if prop.type == "bool":
                if isinstance(value, str):
                    return value.strip().lower() in ("1", "true", "on", "yes")
                return bool(value)
            if prop.type in ("int", "uint"):
                return int(float(value))
            if prop.type == "float":
                return float(value)
        except (TypeError, ValueError):
            return value
        return value
    return value


class ActionRun(BaseModel):
    name: str
    params: object = None


@app.post("/api/devices/{did}/action", dependencies=[Depends(require_token)])
async def run_action(did: str, body: ActionRun):
    s = svc()
    try:
        await s.jobs.submit(
            lambda: s.service.run_action(did, body.name, body.params),
            PRIORITY_CONTROL)
    except ServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


class PowerBody(BaseModel):
    state: bool | None = None


@app.post("/api/devices/{did}/power", dependencies=[Depends(require_token)])
async def set_power(did: str, body: PowerBody):
    """state 为空表示切换（toggle）。"""
    s = svc()
    try:
        if body.state is None:
            new_state = await s.toggle_power(did)
        else:
            await s.set_power(did, body.state)
            new_state = body.state
    except ServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "power": new_state}


# ---------- 场景 ----------

@app.get("/api/scenes", dependencies=[Depends(require_token)])
async def api_scenes():
    s = svc()
    try:
        scenes = await s.jobs.submit(lambda: s.service.scene_list(None), PRIORITY_DATA)
    except ServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"scenes": [vars(sc) for sc in scenes]}


class SceneRun(BaseModel):
    scene_id: str
    home_id: str
    name: str = ""


@app.post("/api/scenes/run", dependencies=[Depends(require_token)])
async def run_scene(body: SceneRun):
    s = svc()
    scene = SceneInfo(scene_id=body.scene_id, name=body.name, home_id=body.home_id)
    try:
        await s.jobs.submit(lambda: s.service.run_scene(scene), PRIORITY_CONTROL)
    except ServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


# ---------- 小爱语音 ----------

class VoiceBody(BaseModel):
    did: str
    text: str


@app.post("/api/voice", dependencies=[Depends(require_token)])
async def voice(body: VoiceBody):
    """把文本指令发给小爱音箱执行（execute-text-directive）。"""
    s = svc()
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="指令不能为空")
    try:
        await s.jobs.submit(
            lambda: s.service.run_action(
                body.did, "execute-text-directive", [text, 0]),
            PRIORITY_CONTROL)
    except ServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


# ---------- 定时开关 ----------

@app.get("/api/schedules", dependencies=[Depends(require_token)])
async def list_schedules():
    rules = schedule_store.load()
    did_to_name = {d.did: d.name for d in svc().devices}
    return {
        "rules": [
            {**rule, "device_name": did_to_name.get(rule.get("did", ""), "（设备已移除）")}
            for rule in rules
        ]
    }


class ScheduleAdd(BaseModel):
    did: str
    time: str
    action: str


@app.post("/api/schedules", dependencies=[Depends(require_token)])
async def add_schedule(body: ScheduleAdd):
    rule = schedule_store.add(body.did, body.time, body.action)
    if rule is None:
        raise HTTPException(status_code=400, detail="时刻或动作不合法")
    return {"ok": True, "rule": rule}


@app.delete("/api/schedules/{rule_id}", dependencies=[Depends(require_token)])
async def remove_schedule(rule_id: str):
    if not schedule_store.remove(rule_id):
        raise HTTPException(status_code=404, detail="规则不存在")
    return {"ok": True}


class ScheduleToggle(BaseModel):
    enabled: bool


@app.post("/api/schedules/{rule_id}/enabled", dependencies=[Depends(require_token)])
async def toggle_schedule(rule_id: str, body: ScheduleToggle):
    if not schedule_store.set_enabled(rule_id, body.enabled):
        raise HTTPException(status_code=404, detail="规则不存在")
    return {"ok": True}


@app.get("/healthz")
async def healthz():
    """存活探针：不鉴权、不碰米家接口，供 Docker healthcheck 使用。"""
    return {"ok": True}


# ---------- 前端 ----------

# 静态站点必须最后挂载：mount("/") 会兜住所有路径，
# 放在它之后定义的路由将永远匹配不到。
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
