"""Bangumi「在看」列表读取。

对应 DESIGN.md §5.3，选择器与路径均已实测确认（附录 F）。

三条路径，配置 `bangumi.read_paths` 决定顺序，逐条尝试：

    api_tab    ⭐ 主力  后台开临时标签页导航到 API URL，读 body.innerText
                        顶层导航不受同源策略约束 → 无 CORS 问题
    dom         兜底    解析用户已打开的收藏页 DOM
    api_inpage  ✗ 已移除 实测被 CORS 拦死（api.bgm.tv 不认 https://bgm.tv 这个源）

实测数据（UID 已匿名，2026-10 时点）：
    在看 5 / 想看 85 / 看过 398 / 抛弃 17，收藏页每页 24 条，API limit 上限 100。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date

from .. import constants
from ..models import BangumiSubject, Season, SeasonBucket
from .browser_bridge import BrowserBridge, BrowserBridgeError

# 收藏页 DOM 提取（实测选择器，附录 F.2 路径 A）
DOM_EXTRACT_JS = """
(function () {
  var items = document.querySelectorAll('#browserItemList > li.item');
  var out = [];
  for (var i = 0; i < items.length; i++) {
    var li = items[i];
    var id = (li.id || '').replace('item_', '');
    if (!id) {
      var cb = li.querySelector('[data-subject-id]');
      if (cb) id = cb.getAttribute('data-subject-id') || '';
    }
    var h3 = li.querySelector('h3');
    var a = h3 ? h3.querySelector('a') : null;
    var smalls = [];
    if (h3) {
      var ss = h3.querySelectorAll('small, span');
      for (var j = 0; j < ss.length; j++) {
        var t = (ss[j].textContent || '').trim();
        if (t) smalls.push(t);
      }
    }
    var inner = li.querySelector('.inner');
    var txt = ((inner ? inner.textContent : li.textContent) || '').replace(/\\s+/g, ' ');
    var m = txt.match(/(\\d{4})年(\\d{1,2})月(\\d{1,2})日/);
    out.push({
      bgm_id: id,
      title_cn: a ? (a.textContent || '').trim() : null,
      title_jp: smalls.length ? smalls[0] : null,
      air_date: m ? (m[1] + '-' + ('0' + m[2]).slice(-2) + '-' + ('0' + m[3]).slice(-2)) : null,
      url: 'https://bgm.tv/subject/' + id
    });
  }
  return JSON.stringify(out);
})()
"""


@dataclass
class ReadResult:
    subjects: list[BangumiSubject]
    path: str
    detail: str = ""


class BangumiReadError(RuntimeError):
    pass


class BangumiSource:
    def __init__(
        self,
        bridge: BrowserBridge | None = None,
        uid: str | None = None,
        read_paths: list[str] | None = None,
        page_size: int = constants.BANGUMI_PAGE_SIZE,
        collection_type: int = constants.BANGUMI_COLLECTION_TYPE_WATCHING,
    ):
        self.bridge = bridge or BrowserBridge()
        self._uid = uid
        self.read_paths = read_paths or ["api_tab", "dom"]
        self.page_size = min(page_size, 100)  # 实测上限就是 100
        self.collection_type = collection_type
        self._uid_cache: str | None = None

    # ---------------------------------------------------------------- UID

    @property
    def uid(self) -> str:
        if self._uid:
            return str(self._uid)
        if self._uid_cache:
            return self._uid_cache
        found = self.bridge.bangumi_uid()
        if not found:
            raise BangumiReadError(
                "读不到 Bangumi 登录用户 —— 请在已接管的浏览器里登录 bgm.tv，"
                "或在 config/settings.yaml 里填 bangumi.uid"
            )
        self._uid_cache = found
        return found

    # ---------------------------------------------------------------- 入口

    def read_watching(self) -> ReadResult:
        errors: list[str] = []
        for path in self.read_paths:
            fn = getattr(self, f"_read_via_{path}", None)
            if fn is None:
                errors.append(f"{path}: 未知路径")
                continue
            try:
                subjects = fn()
                if subjects:
                    return ReadResult(subjects, path, f"{len(subjects)} 条")
                errors.append(f"{path}: 返回空")
            except Exception as e:  # noqa: BLE001 - 逐条降级，需要吞掉各种异常
                errors.append(f"{path}: {type(e).__name__}: {e}")
        raise BangumiReadError("所有读取路径均失败：\n  " + "\n  ".join(errors))

    def diagnose(self) -> list[tuple[str, bool, str, float]]:
        """依次试所有路径，返回 (路径, 是否成功, 说明, 耗时秒)。"""
        import time

        out = []
        for path in [p for p in ["api_tab", "dom"] if hasattr(self, f"_read_via_{p}")]:
            t0 = time.monotonic()
            try:
                subs = getattr(self, f"_read_via_{path}")()
                out.append((path, True, f"{len(subs)} 条", time.monotonic() - t0))
            except Exception as e:  # noqa: BLE001
                out.append((path, False, f"{type(e).__name__}: {e}", time.monotonic() - t0))
        return out

    # ---------------------------------------------------------------- 路径 C：API 标签页

    def _api_url(self, offset: int) -> str:
        return (
            f"{constants.BANGUMI_API}/v0/users/{self.uid}/collections"
            f"?subject_type=2&type={self.collection_type}"
            f"&limit={self.page_size}&offset={offset}"
        )

    def _read_via_api_tab(self) -> list[BangumiSubject]:
        import time

        items: list[dict] = []
        offset = 0
        total: int | None = None

        # 复用同一个标签页翻页，而不是每页开关一次
        url0 = self._api_url(0)
        tab, page = self.bridge.open_tab_and_wait(url0, timeout=20)
        try:
            while True:
                text = self.bridge.read_text(tab)
                data = self._parse_api_body(text)
                batch = data.get("data") or []
                items.extend(batch)
                if total is None:
                    total = data.get("total")
                limit = data.get("limit") or self.page_size

                # 终止条件用 total，而不是「返回条数 < limit」
                # —— 后者在总数正好是 limit 整数倍时会多跑一次空请求
                got = offset + len(batch)
                if not batch or (total is not None and got >= total):
                    break
                if len(batch) < limit and total is None:
                    break

                offset += limit
                nxt = self._api_url(offset)
                self.bridge.navigate_and_wait(page, nxt, timeout=20)
                time.sleep(0.3)
        finally:
            page.close()
            self.bridge.close_tab(tab)

        return [self._to_subject(x) for x in items]

    @staticmethod
    def _parse_api_body(text: str) -> dict:
        t = (text or "").strip()
        if not t:
            raise BangumiReadError("API 返回空内容")
        try:
            return json.loads(t)
        except ValueError:
            head = t[:200].replace("\n", " ")
            raise BangumiReadError(f"API 返回的不是 JSON（前 200 字符）：{head}") from None

    @staticmethod
    def _to_subject(item: dict) -> BangumiSubject:
        sub = item.get("subject") or {}
        d = sub.get("date") or item.get("date")
        air: date | None = None
        if d:
            try:
                air = date.fromisoformat(str(d)[:10])
            except ValueError:
                air = None
        return BangumiSubject(
            bgm_id=int(sub.get("id") or item.get("subject_id") or 0),
            title_cn=sub.get("name_cn") or None,
            title_jp=sub.get("name") or None,
            air_date=air,
            url=f"https://bgm.tv/subject/{sub.get('id') or item.get('subject_id') or ''}",
        )

    # ---------------------------------------------------------------- 路径 A：DOM

    @property
    def collection_url(self) -> str:
        return f"https://bgm.tv/anime/list/{self.uid}/do"

    def _read_via_dom(self) -> list[BangumiSubject]:
        import time

        tab = self.bridge.find_tab(r"bgm\.tv/anime/list/.*/(do|collect|wish)")

        subjects: list[BangumiSubject] = []
        opened_here = False
        if tab is None:
            tab, page = self.bridge.open_tab_and_wait(self.collection_url, timeout=25)
            opened_here = True
        else:
            page = self.bridge.page(tab)
            page.wait_ready(timeout=25, expect_url=tab.url)
        try:
            page_no = 1
            seen: set[int] = set()
            while True:
                payload = self.bridge.eval_in_tab(tab, DOM_EXTRACT_JS) or "[]"
                try:
                    rows = json.loads(payload)
                except ValueError:
                    rows = []
                for r in rows:
                    s = self._to_subject_dom(r)
                    if s.bgm_id and s.bgm_id not in seen:
                        seen.add(s.bgm_id)
                        subjects.append(s)

                # 终止条件：没有「下一页」链接
                # 5 条时 .page_inner 根本不存在，逻辑要容忍
                has_next = self.bridge.eval_in_tab(
                    tab,
                    f"!!document.querySelector('.page_inner a.p[href*=\"page={page_no + 1}\"]')",
                )
                if not has_next:
                    break
                page_no += 1
                self.bridge.navigate_and_wait(
                    page, f"{self.collection_url}?page={page_no}", timeout=20
                )
                time.sleep(0.3)
        finally:
            page.close()
            if opened_here:
                self.bridge.close_tab(tab)

        return subjects

    @staticmethod
    def _to_subject_dom(r: dict) -> BangumiSubject:
        air: date | None = None
        if r.get("air_date"):
            try:
                air = date.fromisoformat(str(r["air_date"])[:10])
            except ValueError:
                air = None
        return BangumiSubject(
            bgm_id=int(r.get("bgm_id") or 0),
            title_cn=(r.get("title_cn") or "").strip() or None,
            title_jp=(r.get("title_jp") or "").strip() or None,
            air_date=air,
            url=r.get("url") or "",
        )

    # ---------------------------------------------------------------- 季度归属

    @staticmethod
    def classify(subject: BangumiSubject, target: Season) -> SeasonBucket:
        """三分类（§5.3.5）。

        边界情况：年番（如光之美少女）首播在 Q1 但全年在播，
        按首播日会永远归到 Q1 —— 所以 cross_season 要能被收进来。
        """
        if subject.air_date is None:
            return SeasonBucket.UNKNOWN
        q = constants.quarter_of_month(subject.air_date.month)
        if (subject.air_date.year, q) == (target.year, target.quarter):
            return SeasonBucket.TARGET
        if (subject.air_date.year, q) < (target.year, target.quarter):
            return SeasonBucket.CROSS_SEASON
        return SeasonBucket.OTHER

    @classmethod
    def filter_by_season(
        cls,
        subjects: list[BangumiSubject],
        target: Season,
        include_cross_season: bool = True,
    ) -> list[tuple[BangumiSubject, SeasonBucket]]:
        want = {SeasonBucket.TARGET}
        if include_cross_season:
            want.add(SeasonBucket.CROSS_SEASON)
            want.add(SeasonBucket.UNKNOWN)
        out = []
        for s in subjects:
            b = cls.classify(s, target)
            if b in want:
                out.append((s, b))
        return out
