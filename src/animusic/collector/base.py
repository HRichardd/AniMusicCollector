"""Collector 抽象 —— 读与写拆开（DESIGN.md §5.8）。

为什么拆：
    搜索可以直连 `music.163.com/api/search/get`（已实测，且返回 alias 番剧标注），
    快、不需要客户端、不要求窗口可见；
    写入歌单必须走官方客户端（用户明确要求，且避开风控面最大的那一环）。
    两边的最优实现不同，失败模式也互不干扰。

上层编排逻辑对具体实现完全无感知。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Candidate, HealthStatus, PlaylistRef


@runtime_checkable
class SearchCollector(Protocol):
    def health(self) -> HealthStatus: ...

    def search(self, keyword: str, limit: int = 10) -> list[Candidate]: ...


@runtime_checkable
class PlaylistCollector(Protocol):
    def health(self) -> HealthStatus: ...

    def list_playlists(self) -> list[PlaylistRef]: ...

    def ensure_playlist(self, name: str) -> PlaylistRef: ...

    def add_to_playlist(self, song_id: str, playlist: PlaylistRef) -> bool: ...


class CollectorError(RuntimeError):
    pass
