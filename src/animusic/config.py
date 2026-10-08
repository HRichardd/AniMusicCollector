"""配置加载 —— 实现 DESIGN.md §5.1 的 settings.yaml。

用 dataclass 而非 pydantic：省掉一条传递依赖（pydantic 会带来
pydantic_core / annotated-types / typing-extensions），
而这里的校验需求很轻。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from typing import Any

import yaml

from . import constants


@dataclass
class NeteaseConfig:
    #: ⚠️ **默认为空是刻意的**：这两个路径是**机器特有**的
    #: （原来是写死的 `D:\CloudMusic\cloudmusic.exe` 与 Chrome 的安装路径）。
    #: 换台机器、换个安装位置，"看似有值的错路径"比"空着让用户自己选"更坏 ——
    #: 后者至少会说清楚"你还没配"。首次启动由界面上的「浏览…」选择并写回本文件。
    client_path: str = ""
    debug_port: int = constants.NETEASE_DEBUG_PORT
    rate_limit_rps: float = constants.WRITE_RATE_RPS
    playlist_name_template: str = "{yy}年{m}月"

    @property
    def configured(self) -> bool:
        return bool(self.client_path.strip())


@dataclass
class BrowserConfig:
    #: 见 `NeteaseConfig.client_path` 上面那段注释（同样刻意留空）
    exe_path: str = ""
    debug_port: int = constants.BROWSER_DEBUG_PORT
    #: 浏览器「用户数据目录」(profile)：Cookie / 登录态 / 缓存都在里面。
    #: 换浏览器时界面会按家族换成对应目录（见 `animusic/browsers.py`）。
    #: ⚠️ 默认值写成 `%LOCALAPPDATA%` 这种**可搬移**形式（`Config.browser_profile`
    #: 会展开它）：配置文件（含 `config/settings.example.yaml` 模板）里不该出现
    #: `C:\Users\<某个人>\...` 这种绝对路径 —— 换台机器/换个用户名就失效。
    user_data_dir: str = r"%LOCALAPPDATA%\AniMusicCollector\browser-profile"
    start_url: str = "https://bgm.tv/"

    @property
    def configured(self) -> bool:
        return bool(self.exe_path.strip())


@dataclass
class BangumiConfig:
    uid: str | None = None
    collection_type: int = constants.BANGUMI_COLLECTION_TYPE_WATCHING
    read_paths: list[str] = field(default_factory=lambda: ["api_tab", "dom"])
    page_size: int = constants.BANGUMI_PAGE_SIZE
    only_target_season: bool = True
    include_cross_season: bool = True


@dataclass
class FetchConfig:
    source_priority: list[str] = field(
        default_factory=lambda: ["bangumi", "animethemes", "anilist"]
    )
    animethemes_cache_ttl: float = 3600.0
    #: 日文维基响应的磁盘缓存时长（秒）。
    #:
    #: 为什么要缓存：维基查询是**限速 1 请求/秒**的，而一部番要试好几种词条名
    #: （正文 / (アニメ) / (テレビアニメ) / 剥掉季数后缀），实测一部"找不到"的番
    #: 就是 6 次请求 ≈ 6 秒 —— 用户实测"重新拉一次要 4 分钟"就是这么来的。
    #: 缓存命中后重跑几乎是瞬时的。0 = 不缓存（调试用）。
    #: 内容变化很慢，一天足够；新上架的 OP 靠「增量重跑」在更长的周期里捡。
    wikipedia_cache_ttl: float = 86400.0
    #: AniList 响应的磁盘缓存时长（秒）。
    #:
    #: AniList 只做**辅助**（番单桥接 + 热度，不给曲目），而它每次跑固定
    #: 2 次请求（`max_pages=2`）—— 一季的番单一天里基本不动，缓存 1 小时
    #: 与 AnimeThemes 同档。0 = 不缓存（调试用）。
    #:
    #: ⚠️ 它能开缓存的前提是 `HttpClient` 的缓存键**带上请求体**（已修）：
    #: AniList 是 GraphQL，同一个 URL、不同 body（第 1/2 页、不同季度）。
    anilist_cache_ttl: float = 3600.0

    # ---- 外部数据源的 HTTP 预算（日文维基 / AnimeThemes / AniList）
    #
    # 依据见 `constants.FETCH_HTTP_TIMEOUT` 那段实测。
    #
    # 放成旋钮而不是写死在代码里：**网络状况是机器特有的**。
    # 梯子稳的人可以把 `http_retries` 调到 4、`http_timeout` 调到 30；
    # 梯子差的人反过来调小，宁可早点认输也不要卡 2 分钟。
    http_timeout: float = constants.FETCH_HTTP_TIMEOUT
    http_retries: int = constants.FETCH_HTTP_RETRIES
    #: 单次请求超过这个秒数就在日志里记一条「慢请求」；0 = 不记。
    slow_request_seconds: float = constants.SLOW_REQUEST_SECONDS

    # ---- HTTP 429（限流）的策略，依据见 `constants` 里「HTTP 429」那一节
    #
    # 真机实测：服务端通过 `Retry-After: 25` 明说「等 25 秒」，
    # 而当时的代码等 0.5 秒就重试 —— 28 个失败的 URL × 3 次尝试 = 84 次请求
    # （占维基请求的 70%）+ 44.2 秒退避，换来 **0 个成功**。
    #
    #: **本步为限流等待的总预算（秒）**。为什么是「预算」而不是「单次上限」：
    #: 真机的形态是**成功与 429 交替**，而成功会把连续计数清零 ——
    #: 于是熔断不触发，每条失败的 URL 都老实等 25 秒 ⇒ 28 × 25 ≈ 700 秒，
    #: 比原来要修的 163 秒还糟 4 倍。预算用完就不再等，直接放弃该 URL。
    rate_limit_wait_budget: float = constants.RATE_LIMIT_WAIT_BUDGET
    #: 同一个 URL 为 429 最多等几次重试
    rate_limit_retries: int = constants.RATE_LIMIT_RETRIES
    #: 同一域名**连续**这么多次 429 就熔断：本步不再向它发请求。
    #: 一次「找不到词条」的番要试 7~13 个候选名 —— 不熔断就会全撞一遍
    rate_limit_breaker: int = constants.RATE_LIMIT_BREAKER


@dataclass
class MatchConfig:
    auto_threshold: float = constants.AUTO_THRESHOLD
    review_threshold: float = constants.REVIEW_THRESHOLD
    max_queries_per_theme: int = constants.MAX_QUERIES_PER_THEME
    early_accept_score: float = constants.EARLY_ACCEPT_SCORE
    prefer_full_size: bool = True
    # 「仅艺人」查询会返回该艺人的多首歌，取宽一点让打分器挑
    search_limit: int = 15


@dataclass
class Config:
    netease: NeteaseConfig = field(default_factory=NeteaseConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    bangumi: BangumiConfig = field(default_factory=BangumiConfig)
    fetch: FetchConfig = field(default_factory=FetchConfig)
    match: MatchConfig = field(default_factory=MatchConfig)
    path: str = ""

    # ---------------------------------------------------------------- 加载
    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        p = path or os.path.join(constants.config_dir(), "settings.yaml")
        cfg = cls(path=p)
        if not os.path.exists(p):
            return cfg
        try:
            with open(p, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as e:
            raise RuntimeError(f"配置文件读取失败：{p} —— {e}") from None
        if not isinstance(raw, dict):
            raise RuntimeError(f"配置文件格式不对（应为字典）：{p}")

        for section_name in ("netease", "browser", "bangumi", "fetch", "match"):
            section = getattr(cfg, section_name)
            data = raw.get(section_name) or {}
            if not isinstance(data, dict):
                continue
            valid = {f.name for f in fields(section)}
            for k, v in data.items():
                if k in valid and v is not None:
                    setattr(section, k, v)
        return cfg

    def save(self, path: str | None = None) -> None:
        p = path or self.path or os.path.join(constants.config_dir(), "settings.yaml")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        payload = {
            name: {
                f.name: getattr(getattr(self, name), f.name)
                for f in fields(getattr(self, name))
            }
            for name in ("netease", "browser", "bangumi", "fetch", "match")
        }
        with open(p, "w", encoding="utf-8") as f:
            yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)

    # ---------------------------------------------------------------- 便捷
    @property
    def browser_profile(self) -> str:
        return os.path.expandvars(self.browser.user_data_dir)

    def netease_launch_args(self) -> list[str]:
        return [
            f"--remote-debugging-port={self.netease.debug_port}",
            "--disable-features=CalculateNativeWinOcclusion",
        ]

    def browser_launch_args(self) -> list[str]:
        return [
            f"--user-data-dir={self.browser_profile}",
            f"--remote-debugging-port={self.browser.debug_port}",
            "--no-first-run",
            "--no-default-browser-check",
            self.browser.start_url,
        ]


def ensure_default_config() -> str:
    """首次运行时落一份 settings.yaml，方便用户改。"""
    p = os.path.join(constants.config_dir(), "settings.yaml")
    if not os.path.exists(p):
        os.makedirs(constants.config_dir(), exist_ok=True)
        Config().save(p)
    return p
