"""后台任务线程 —— 把耗时操作挪出 UI 线程。

设计文档 §8.6 / 附录 G.7 的硬规则：
    **GUI 里任何网络 I/O 都必须离开 UI 线程。**
    实测最无害的「只是探一下端口」也能让界面每几秒冻结 2.4 秒。

这个 `TaskWorker` 是通用壳子：给一个可调用对象，它在线程里跑，
把进度和结果通过信号发回来。拉番单、匹配、推送全部复用它。
"""

from __future__ import annotations

import traceback
from typing import Any, Callable

from PySide6.QtCore import QThread, Signal


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

    def __init__(self, fn: Callable[..., Any], *args, label: str = "", parent=None,
                 cleanup: Callable[[], Any] | None = None, **kwargs):
        super().__init__(parent)
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.label = label or getattr(fn, "__name__", "任务")
        self.cleanup = cleanup

    def run(self) -> None:  # noqa: D102
        try:
            self._run_job()
        finally:
            if self.cleanup is not None:
                try:
                    self.cleanup()
                except Exception:  # noqa: BLE001 - 清理失败不能盖掉真正的任务结果
                    pass

    def _run_job(self) -> None:
        try:
            result = self.fn(*self.args, progress=self.progressed.emit, **self.kwargs)
        except TypeError as e:
            # 该可调用对象不接受 progress 参数 —— 退回无参调用
            if "progress" not in str(e):
                self.failed.emit(f"{type(e).__name__}: {e}", traceback.format_exc())
                return
            try:
                result = self.fn(*self.args, **self.kwargs)
            except Exception as e2:  # noqa: BLE001
                self.failed.emit(f"{type(e2).__name__}: {e2}", traceback.format_exc())
                return
        except Exception as e:  # noqa: BLE001 - 后台线程必须吞掉一切，否则界面无反馈
            self.failed.emit(f"{type(e).__name__}: {e}", traceback.format_exc())
            return
        self.succeeded.emit(result)
