"""后台任务线程 —— 把耗时操作挪出 UI 线程。

设计文档 §8.6 / 附录 G.7 的硬规则：
    **GUI 里任何网络 I/O 都必须离开 UI 线程。**
    实测最无害的「只是探一下端口」也能让界面每几秒冻结 2.4 秒。

这个 `TaskWorker` 是通用壳子：给一个可调用对象，它在线程里跑，
把进度和结果通过信号发回来。拉番单、匹配、推送全部复用它。
"""

from __future__ import annotations

import traceback
import inspect
from threading import Event
from typing import Any, Callable

from PySide6.QtCore import QThread, Signal
from ..orchestrator import OperationCancelled


class TaskWorker(QThread):
    """在后台线程里执行 `fn`，把进度与结果发回 UI 线程。

    `fn` 若接受 `progress` 关键字参数，就会收到一个可调用的进度回调。

    `cleanup` 在**线程结束前、仍在这个线程里**执行（`finally`）。
    唯一的用法是收掉该线程自己的 sqlite 连接（`Store.close_thread`）——
    sqlite 只允许创建连接的线程关它，所以这件事**只能在线程内部做**，
    UI 线程上的 `close()` 关不掉它（实测会抛 ProgrammingError）。
    """

    progressed = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str, str)      # (简短错误, 详细堆栈)
    cancelled = Signal(str)

    def __init__(self, fn: Callable[..., Any], *args, label: str = "", parent=None,
                 cleanup: Callable[[], Any] | None = None, **kwargs):
        super().__init__(parent)
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.label = label or getattr(fn, "__name__", "任务")
        self.cleanup = cleanup
        self._stop_event = Event()

    def request_stop(self) -> None:
        self._stop_event.set()
        self.requestInterruption()

    def should_stop(self) -> bool:
        return self._stop_event.is_set()

    def run(self) -> None:  # noqa: D102
        terminal, payload = "succeeded", None
        try:
            payload = self._run_job()
        except OperationCancelled as e:
            terminal, payload = "cancelled", str(e)
        except Exception as e:
            terminal, payload = "failed", (f"{type(e).__name__}: {e}", traceback.format_exc())
        finally:
            if self.cleanup is not None:
                try:
                    self.cleanup()
                except Exception as e:
                    if terminal == "succeeded":
                        terminal, payload = "failed", (f"线程资源清理失败：{e}", traceback.format_exc())
        # 先清理连接再报告结果；UI仍须等待finished才能解锁或销毁线程。
        if terminal == "failed":
            self.failed.emit(*payload)
        elif terminal == "cancelled":
            self.cancelled.emit(payload)
        else:
            self.succeeded.emit(payload)

    def _run_job(self):
        if self.should_stop():
            raise OperationCancelled("任务开始前已停止")
        parameters = inspect.signature(self.fn).parameters
        variadic = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())
        kwargs = dict(self.kwargs)
        for key, value in (("progress", self.progressed.emit), ("should_stop", self.should_stop)):
            p = parameters.get(key)
            if variadic or (p is not None and p.kind != inspect.Parameter.POSITIONAL_ONLY):
                kwargs.setdefault(key, value)
        # 不能在业务TypeError后重跑函数，第一次调用可能已产生写入。
        return self.fn(*self.args, **kwargs)
