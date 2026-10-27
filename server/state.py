# SPDX-License-Identifier: GPL-3.0-or-later
"""全局状态：设备列表、开关记忆、后台轮询与定时调度。

两处沿用原项目踩过坑的设计，Web 端同样会踩：

1. **操作版本号（gen）防回弹**
   轮询是后台定时请求，用户点开关时可能已有一条旧结果在途。旧结果回来
   若直接覆盖，按钮会"点了又弹回去"。因此每次本地操作都给该设备 gen+1，
   轮询提交时快照 gen，回来时 gen 已变的设备直接丢弃结果。
   （原项目 tests/race_test.py 就是守这个行为的）

2. **离线设备的云端开关值不可信**
   米家云端对离线设备返回的是最后一次在线时的缓存值。原项目显式跳过
   离线设备，这里保持一致。
"""

import asyncio
import time
import logging
from datetime import datetime

from . import jobs as jobs_mod
from .core import cache, schedule_store
from .core.models import DeviceInfo
from .core.scheduler import Scheduler
from .core.service import MijiaService, ServiceError

logger = logging.getLogger(__name__)

# 轮询与定时判定的间隔（秒）。轮询只影响状态刷新，不影响操作响应速度
POLL_INTERVAL = 15
SCHEDULE_INTERVAL = 20


class AppState:
    def __init__(self, loop: asyncio.AbstractEventLoop, poll_interval: int = POLL_INTERVAL):
        self._loop = loop
        self.jobs = jobs_mod.JobExecutor(loop)
        self.service = MijiaService()
        self.poll_interval = poll_interval

        self.devices: list[DeviceInfo] = []
        # did -> True/False/None；None 表示"已确认无开关能力"
        self.known_power: dict[str, bool | None] = {}
        # did -> 温湿度展示文案（如 "28.3°C 60%"）
        self.metrics: dict[str, str] = {}
        # did -> 操作版本号，本地操作过就 +1，用于丢弃过期的轮询结果
        self.power_gen: dict[str, int] = {}
        self.updated_at: float = 0.0
        self.last_error: str | None = None
        self._refreshing = False
        self._events: list[dict] = []

    # ---------- 登录 ----------

    @property
    def logged_in(self) -> bool:
        try:
            return bool(self.service.login_status())
        except Exception:  # noqa: BLE001 - 未登录/凭据异常都算未登录
            return False

    def reload_service(self) -> None:
        """扫码成功后重建客户端，让新凭据生效。"""
        self.service = MijiaService()
        self.known_power.clear()
        self.metrics.clear()
        self.devices = []

    # ---------- 刷新 ----------

    async def refresh_devices(self) -> list[DeviceInfo]:
        devices = await self.jobs.submit(
            self.service.list_devices, jobs_mod.PRIORITY_DATA)
        names = {d.did: d.name for d in devices}
        try:
            # 把未改名的英文默认名换成 spec 里的中文产品名（无额外网络请求）
            localized = await self.jobs.submit(
                lambda: self.service.localized_product_names(list(names), names),
                jobs_mod.PRIORITY_IDLE)
            for dev in devices:
                if dev.did in localized:
                    dev.name = localized[dev.did]
        except Exception:  # noqa: BLE001 - 显示名锦上添花，失败不影响主流程
            logger.debug("本地化设备名失败", exc_info=True)
        self.devices = devices
        return devices

    async def poll_once(self) -> None:
        """刷新设备列表 + 开关状态 + 温湿度。"""
        if self._refreshing or not self.logged_in:
            return
        self._refreshing = True
        try:
            await self.refresh_devices()
            # 离线设备的云端开关值是缓存，不可信，跳过
            dids = [d.did for d in self.devices if d.online]
            if not dids:
                self.updated_at = time.time()
                self.last_error = None
                return

            snapshot = dict(self.power_gen)
            states = await self.jobs.submit(
                lambda: self.service.power_states(dids), jobs_mod.PRIORITY_DATA)
            for did, value in states.items():
                if snapshot.get(did, 0) != self.power_gen.get(did, 0):
                    continue  # 期间用户操作过，这条结果是旧的，丢弃
                self.known_power[did] = value

            try:
                metrics = await self.jobs.submit(
                    lambda: self.service.read_metrics(dids), jobs_mod.PRIORITY_DATA)
                for did, text in (metrics or {}).items():
                    if text:
                        self.metrics[did] = text
            except Exception:  # noqa: BLE001 - 读数只影响副标题
                logger.debug("读取温湿度失败", exc_info=True)

            cache.save(self.devices, self.known_power, self.metrics)
            self.updated_at = time.time()
            self.last_error = None
        except ServiceError as exc:
            self.last_error = str(exc)
            logger.warning("轮询失败: %s", exc)
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            logger.warning("轮询失败: %s", exc, exc_info=True)
        finally:
            self._refreshing = False

    async def poll_loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 后台循环不能因异常退出
                logger.exception("轮询循环异常")
            await asyncio.sleep(self.poll_interval)

    # ---------- 本地操作（乐观更新 + 版本号） ----------

    def bump(self, did: str) -> None:
        self.power_gen[did] = self.power_gen.get(did, 0) + 1

    async def set_power(self, did: str, state: bool) -> None:
        self.bump(did)
        await self.jobs.submit(
            lambda: self.service.set_power_state(did, state),
            jobs_mod.PRIORITY_CONTROL)
        self.known_power[did] = state

    async def toggle_power(self, did: str) -> bool:
        current = self.known_power.get(did)
        self.bump(did)
        # 已知当前状态时直接写反值，省掉一次读往返（云端单程 0.3~0.8s）
        new_state = await self.jobs.submit(
            lambda: self.service.toggle_power(did, current),
            jobs_mod.PRIORITY_CONTROL)
        self.known_power[did] = new_state
        return new_state

    # ---------- 定时开关 ----------

    async def schedule_loop(self) -> None:
        """每分钟判定一次定时任务；错过不补执行（沿用原项目取舍）。"""
        scheduler = Scheduler(schedule_store.load())
        while True:
            try:
                rules = schedule_store.load()
                scheduler.set_rules(rules)
                result = scheduler.tick()
                for rule in result.due:
                    await self._fire_rule(rule, scheduler)
                for rule in result.missed:
                    self.push_event(
                        "missed",
                        f"定时任务已错过：{rule.get('time')} "
                        f"{'开' if rule.get('action') == 'on' else '关'}"
                        f"（今天不再补执行）")
                    logger.info("定时任务错过: %s", rule)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("定时调度循环异常")
            await asyncio.sleep(SCHEDULE_INTERVAL)

    async def _fire_rule(self, rule: dict, scheduler: Scheduler) -> None:
        did = rule.get("did", "")
        action = rule.get("action")
        try:
            await self.set_power(did, action == "on")
        except Exception as exc:  # noqa: BLE001
            logger.warning("定时任务执行失败 %s: %s", rule.get("id"), exc)
            self.push_event("schedule_failed", f"定时任务执行失败：{exc}")
            return
        today = datetime.now().strftime("%Y-%m-%d")
        schedule_store.mark_fired(rule.get("id", ""), today)
        scheduler.mark_fired(rule.get("id", ""))
        logger.info("定时任务已执行: %s", rule)

    # ---------- 事件（前端提示用） ----------

    def push_event(self, kind: str, message: str) -> None:
        self._events.append({
            "kind": kind,
            "message": message,
            "at": datetime.now().strftime("%H:%M:%S"),
        })
        del self._events[:-50]

    def drain_events(self) -> list[dict]:
        events = list(self._events)
        self._events.clear()
        return events

    def load_cache(self) -> None:
        """启动时先用磁盘缓存渲染首屏，省去等待一轮全量拉取。"""
        cached = cache.load()
        if cached is None:
            return
        devices, known, metrics = cached
        self.devices = devices
        self.known_power = known
        self.metrics = metrics
