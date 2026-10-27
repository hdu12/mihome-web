# SPDX-License-Identifier: GPL-3.0-or-later
"""串行任务执行器：所有米家网络调用都排进这一个后台线程。

移植自原项目 app/core/jobs.py，去掉 Qt 依赖。
原实现用 `QTimer` 每 16ms 把结果从 worker 线程取回主线程执行回调；
Web 版改为 `loop.call_soon_threadsafe` 把结果写回 asyncio Future，
语义等价且不依赖事件循环以外的任何 GUI 机制。

**为什么仍然串行**（沿用原项目的结论）：
上游 requests.Session 没有线程安全承诺，且每次操作自带防限流延时，
并发只会带来竞态而不会更快。单线程队列天然节流。

**优先级**沿用原项目的三级设计：用户直接操作（开关/动作/语音）插队
到数据轮询之前，后台杂活（图标、产品页名称）排最后——否则一次产品页
抓取（实测 2.4 秒）会把"点了开关半天没反应"变成常态。
"""

import itertools
import logging
import queue
import threading
from typing import Any, Callable, NamedTuple

logger = logging.getLogger(__name__)

PRIORITY_CONTROL = 0   # 用户直接操作：开关、动作、语音
PRIORITY_DATA = 1      # 数据轮询：设备列表、开关状态、温湿度
PRIORITY_IDLE = 2      # 后台杂活：图标、产品页名称


class _Task(NamedTuple):
    fn: Callable[[], Any]
    future: Any


class JobExecutor:
    """提交同步阻塞函数到后台线程，返回可 await 的 asyncio.Future。

    用法（在 async 路由里）::

        devices = await jobs.submit(service.list_devices, PRIORITY_DATA)
    """

    def __init__(self, loop):
        self._loop = loop
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="mijia-jobs", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            _, task = item
            try:
                result = task.fn()
            except Exception as exc:  # noqa: BLE001 - 结果要回传给调用方
                self._deliver(task, None, exc)
            else:
                self._deliver(task, result, None)

    def _deliver(self, task: _Task, result: Any, exc: Exception | None) -> None:
        def _set() -> None:
            if task.future.done():
                return
            if exc is not None:
                task.future.set_exception(exc)
            else:
                task.future.set_result(result)

        try:
            self._loop.call_soon_threadsafe(_set)
        except RuntimeError:
            # 事件循环已关闭（服务正在停止），没有接收方了，丢弃结果
            logger.debug("事件循环已关闭，丢弃任务结果", exc_info=True)

    def submit(self, fn: Callable[[], Any], priority: int = PRIORITY_DATA) -> Any:
        """排入队列并返回 Future；priority 数值越小越先执行。"""
        future = self._loop.create_future()
        self._queue.put((priority, _Task(fn, future)))
        return future

    def shutdown(self) -> None:
        self._stop.set()
