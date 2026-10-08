"""CDP 封装（两个目标共用）。"""

from .client import CDPBrowser, CDPError, CDPPage, port_open

__all__ = ["CDPBrowser", "CDPPage", "CDPError", "port_open"]
