"""数据源客户端。"""

from .browser_bridge import BrowserBridge, BrowserBridgeError
from .bangumi import BangumiReadError, BangumiSource, ReadResult

__all__ = [
    "BrowserBridge",
    "BrowserBridgeError",
    "BangumiSource",
    "BangumiReadError",
    "ReadResult",
]
