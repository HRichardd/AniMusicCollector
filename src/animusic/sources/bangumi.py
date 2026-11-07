"""Bangumi「在看」列表及当季日本 TV 分类列表读取。

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
import time
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qs, quote, unquote, urlsplit

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

# 分类浏览与收藏页的名称结构相似，但没有 small 时主链接就是原名。
# 结构和筛选同时校验，登录页/验证页不能冒充正常空列表。
SEASON_EXTRACT_JS = r"""
(() => {
  const list = document.querySelector('#browserItemList');
  const sidebar = document.querySelector('#columnSubjectBrowserB');
  const selected = sidebar ? Array.from(sidebar.querySelectorAll('a.focus'))
      .map(a => a.textContent.trim()) : [];
  const rows = list ? Array.from(list.querySelectorAll('li.item')).map(li => {
    const a = li.querySelector('h3 a[href*="/subject/"]');
    const small = li.querySelector('h3 small');
    const name = a ? a.textContent.trim() : '';
    const original = small ? small.textContent.trim() : '';
    const id = a && new URL(a.href, location.href).pathname.match(/^\/subject\/(\d+)$/);
    const info = li.querySelector('p.info');
    return {bgm_id: id ? Number(id[1]) : 0,
      title_cn: original ? name : null, title_jp: original || name,
      info: info ? info.textContent.trim() : '',
      url: a ? a.href : ''};
  }) : [];
  const current = document.querySelector('.page_inner .p_cur');
  const currentPage = current ? Number(current.textContent.trim()) : null;
  const requestedPage = Number(new URL(location.href).searchParams.get('page') || 1);
  const hasNext = Array.from(document.querySelectorAll('.page_inner a[href]')).some(a => {
    const u = new URL(a.href, location.href);
    return u.pathname === location.pathname && Number(u.searchParams.get('page')) > requestedPage;
  });
  return JSON.stringify({url: location.href,
    valid: !!list && !!sidebar && selected.includes('TV') && selected.includes('日本'),
    current_page: currentPage, has_next: hasNext, rows});
})()
"""

SEASON_MAX_PAGES = 100  # 异常保护，不采用网站显示的11页作为结果边界。


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

    @staticmethod
    def season_browser_url(year: int, month: int, page: int = 1, *, host: str = 'bgm.tv') -> str:
        return (f"https://{host}/anime/browser/tv/{quote('日本')}/airtime/{year}-{month}"
                f"?sort=title&page={page}")

    @staticmethod
    def _season_page(payload: str, year: int, month: int, page_no: int) -> dict:
        try:
            data = json.loads(payload)
            if not isinstance(data, dict) or data.get('valid') is not True:
                raise ValueError('不是有效的 TV／日本分类列表（可能需要登录或通过站点验证）')
            url = urlsplit(data['url'])
            expected = f'/anime/browser/tv/日本/airtime/{year}-{month}'
            if (url.scheme != 'https' or url.hostname not in ('bgm.tv', 'bangumi.tv')
                    or unquote(url.path).rstrip('/') != expected
                    or parse_qs(url.query).get('page', ['1']) != [str(page_no)]):
                raise ValueError('未到达指定月份或页码')
            if data.get('current_page') not in (None, page_no):
                raise ValueError('分页回退，无法确认番单完整性')
            if not isinstance(data.get('rows'), list) or not isinstance(data.get('has_next'), bool):
                raise ValueError('列表结构不符')
            return data
        except (ValueError, TypeError, KeyError) as exc:
            raise BangumiReadError(f'{year}-{month} 第{page_no}页读取失败：{exc}') from exc

    @staticmethod
    def _season_subject(row: dict, year: int, month: int) -> BangumiSubject:
        try:
            if not isinstance(row, dict):
                raise ValueError('条目结构不符')
            subject = BangumiSource._to_subject_dom(row)
            if subject.bgm_id <= 0 or not subject.all_names():
                raise ValueError('条目缺少有效 ID 或名称')
            # 只读首播信息起始处，避免把后面的制作信息中的日期当成首播日。
            match = re.match(r'^(?:\d+话\s*/\s*)?(\d{4})(?:年|[-/.])?'
                             r'(?:(\d{1,2})(?:月|[-/.])?(?:(\d{1,2})日?)?)?',
                             (row.get('info') or '').strip())
            if match:
                y, m, d = match.groups()
                if int(y) != year or (m and int(m) != month):
                    raise ValueError(f'条目 {subject.bgm_id} 的首播年月与筛选条件不符')
                if m and d:
                    subject.air_date = date(int(y), int(m), int(d))
            return subject
        except (ValueError, TypeError, AttributeError) as exc:
            raise BangumiReadError(f'{year}-{month} 番剧条目异常：{exc}') from exc

    def read_season_all(self, season: Season, *, check_cancelled=None, progress=None) -> ReadResult:
        """三个首播月份全部读完才返回；个人在看读取不经过此路径。"""
        check = check_cancelled or (lambda: None)
        report = progress or (lambda message: None)
        subjects: dict[int, BangumiSubject] = {}
        check()
        # 地区筛选需要登录；沿用已有标签页域名，避免切换站点丢失登录态。
        existing_tab = self.bridge.find_bangumi_tab()
        existing_url = getattr(existing_tab, 'url', '')
        host = urlsplit(existing_url).hostname if isinstance(existing_url, str) else None
        host = host if host in ('bgm.tv', 'bangumi.tv') else 'bgm.tv'
        tab, page = self.bridge.open_tab_and_wait(
            self.season_browser_url(season.year, season.start_month, host=host), timeout=25)
        try:
            for month in range(season.start_month, season.start_month + 3):
                month_seen: set[int] = set()
                for page_no in range(1, SEASON_MAX_PAGES + 1):
                    check()
                    if month != season.start_month or page_no != 1:
                        time.sleep(0.3)
                        check()
                        if not self.bridge.navigate_and_wait(
                                page, self.season_browser_url(season.year, month, page_no, host=host), timeout=25):
                            raise BangumiReadError(f'{season.year}-{month} 第{page_no}页加载超时')
                    check()
                    report(f'读取 Bangumi {season.year}年{month}月 第{page_no}页…')
                    data = self._season_page(self.bridge.eval_in_tab(tab, SEASON_EXTRACT_JS),
                                             season.year, month, page_no)
                    batch = [self._season_subject(row, season.year, month) for row in data['rows']]
                    if not batch:
                        break
                    ids = {s.bgm_id for s in batch}
                    if not ids - month_seen:
                        raise BangumiReadError(f'{season.year}-{month} 第{page_no}页重复，无法确认番单完整性')
                    month_seen.update(ids)
                    for subject in batch:
                        old = subjects.get(subject.bgm_id)
                        if old is None:
                            subjects[subject.bgm_id] = subject
                        else:
                            if old.title_jp and subject.title_jp and old.title_jp != subject.title_jp:
                                raise BangumiReadError(f'条目 {subject.bgm_id} 原名冲突')
                            if old.air_date and subject.air_date and old.air_date != subject.air_date:
                                raise BangumiReadError(f'条目 {subject.bgm_id} 首播日期冲突')
                            for field in ('title_cn', 'title_jp', 'air_date', 'url'):
                                if not getattr(old, field):
                                    setattr(old, field, getattr(subject, field))
                    if not data['has_next']:
                        break
                else:
                    raise BangumiReadError(f'{season.year}-{month} 达到{SEASON_MAX_PAGES}页保护上限')
            check()
            return ReadResult(list(subjects.values()), 'season_browser', f'{len(subjects)} 条')
        finally:
            try:
                page.close()
            finally:
                self.bridge.close_tab(tab)

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
