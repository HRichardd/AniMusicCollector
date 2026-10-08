"""维基百科数据源测试。

这个模块是**新增的曲目主数据源**，它取代的是「罗马音 + 别名表自学习」
那套事后补救方案。所以它有两条硬要求：

    ① **不许猜** —— 解析不出来的格式返回空，让调用方回落到 AnimeThemes
    ② **不许拿错** —— 搜索兜底必须校验词条名，宁可漏不可错

下面每条测试都对应实现过程中**真实踩到的坑**，不是编的。
"""

from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault(
    "ANIMUSIC_CONFIG_DIR", os.path.join(_ROOT, "data", "_test_tmp", "config")
)

from animusic.models import ThemeKind  # noqa: E402
from animusic.http import RateLimited  # noqa: E402
from animusic.sources.wikipedia import (  # noqa: E402
    WIKI_BATCH_URL_MAX,
    WIKI_TRY_BATCH,
    WikipediaSource,
    _batch_chunks,
    _quote,
    candidate_titles,
    canon_title,
    parse_from_wikitext,
    parse_themes,
    title_matches,
)


# ==================================================================== 格式 A

LIST_FORMAT = """
; 「プランA」{{R|natalie240623}}
: [[DISH//]]による第一期のオープニングテーマ。作詞は[[北村匠海]]、作曲はDISH//。
; 「鎌倉STYLE」<ref>x</ref>
: [[ぼっちぼろまる]]による第一期のエンディングテーマ。
; 「[[鬼事/Fiction Love|鬼事]]」{{R|natalie679230}}
: [[中島健人]]による第二期のオープニングテーマ。
; 「今すぐOTETSUKI☆」
: 亜也子（[[鈴代紗弓]]）による第14話の挿入歌。
"""

# ==================================================================== 格式 B

TABLE_FORMAT = """
{| class="wikitable"
|+ オープニングテーマ
! 使用話 !! 曲名 !! 歌手 !! 作詞
|-
! 1 - 12
| [[花になって]] || [[緑黄色社会]] || 長屋晴子
|-
! 13 - 24
| colspan="2"| [[アンビバレント (Uruの曲)|アンビバレント]] || Uru
|}
"""

TABLE_WITH_INTERLUDE = """
{| class="wikitable"
|+ エンディングテーマ
! 使用話 !! 曲名 !! 歌手
|-
! 1 - 12
| [[アイコトバ]] || [[アイナ・ジ・エンド]]
|}
{| class="wikitable"
|+ 挿入歌
! 使用話 !! 曲名 !! 歌手
|-
! 3
| [[明日を訪ねて]] || [[XAI]]
|}
"""


class TestListFormat(unittest.TestCase):
    def test_parses_op_ed_with_japanese_names(self):
        got = parse_themes(LIST_FORMAT)
        pairs = [(str(t.kind), t.song_title, t.artists) for t in got]
        self.assertIn(("OP", "プランA", ["DISH//"]), pairs)
        self.assertIn(("ED", "鎌倉STYLE", ["ぼっちぼろまる"]), pairs)
        self.assertIn(("OP", "鬼事", ["中島健人"]), pairs)

    def test_piped_link_takes_display_text(self):
        got = {t.song_title: t for t in parse_themes(LIST_FORMAT)}
        self.assertIn("鬼事", got, "[[A|B]] 应取显示文本 B")

    def test_interlude_excluded(self):
        """挿入歌不属于 OP/ED，**必须被排除** —— 混进歌单就是错的。

        （列表格式里它们连 kind 都判不出来，于是直接被跳过；
        表格格式里则靠 `|+ 挿入歌` 标题把 kind 置空。
        两条路都不该让它出现。）
        """
        titles = [t.song_title for t in parse_themes(LIST_FORMAT)]
        self.assertNotIn("今すぐOTETSUKI☆", titles, "挿入歌混进来了")
        self.assertEqual(len(titles), 3)

    def test_sequence_extracted(self):
        got = {t.song_title: t for t in parse_themes(LIST_FORMAT)}
        self.assertEqual(got["鬼事"].sequence, 2, "第二期 → 2")
        self.assertEqual(got["プランA"].sequence, 1)

    def test_refs_and_templates_stripped(self):
        for t in parse_themes(LIST_FORMAT):
            self.assertNotIn("{{", t.song_title)
            self.assertNotIn("<ref", t.song_title)
            for a in t.artists:
                self.assertNotIn("[[", a)


class TestTableFormat(unittest.TestCase):
    """wikitable —— 实测踩过两个坑，都钉在这里。"""

    def test_parses_basic_table(self):
        got = parse_themes(TABLE_FORMAT)
        pairs = [(t.song_title, t.artists) for t in got]
        self.assertIn(("花になって", ["緑黄色社会"]), pairs)
        self.assertIn(("アンビバレント", ["Uru"]), pairs)

    def test_number_is_on_its_own_line(self):
        """**坑 1**：真实结构里「使用話」在 `!` 行、曲名在下一行 `|` 行。

        一开始我假设它们在同一组单元格（`! num | title || artist`），
        于是表格格式**一直解析出 0 条** —— 列表格式掩盖了这个 bug。
        """
        got = parse_themes(TABLE_FORMAT)
        self.assertTrue(got, "表格格式解析不出任何东西")
        self.assertTrue(any("1 - 12" in t.note for t in got), "话数没被带过来")

    def test_cell_attributes_stripped(self):
        """**坑 2**：`colspan="2"| Uru` 会把单元格属性混进艺人名。"""
        got = {t.song_title: t for t in parse_themes(TABLE_FORMAT)}
        self.assertEqual(got["アンビバレント"].artists, ["Uru"],
                         "单元格属性泄漏进艺人名了")

    def test_interlude_table_resets_kind(self):
        """**坑 3**：ED 表之后还有一张「挿入歌」表。

        如果 `|+` 标题解析不出 OP/ED 时保留上一个 kind，
        7 首插入歌会被当成 ED —— 那会污染歌单。
        """
        got = parse_themes(TABLE_WITH_INTERLUDE)
        titles = [t.song_title for t in got]
        self.assertIn("アイコトバ", titles)
        self.assertNotIn("明日を訪ねて", titles, "挿入歌被当成了 ED")

    def test_header_row_skipped(self):
        got = parse_themes(TABLE_FORMAT)
        for t in got:
            self.assertNotIn("曲名", t.song_title)
            self.assertNotIn("歌手", t.song_title)


class TestUnknownFormatIsEmpty(unittest.TestCase):
    """不认识就返回空 —— **不许猜**。"""

    def test_empty(self):
        self.assertEqual(parse_themes(""), [])

    def test_prose_only(self):
        self.assertEqual(parse_themes("本作の劇伴は3名が担当した。"), [])

    def test_no_theme_segments_in_wikitext(self):
        """整篇没有可识别的主题歌段 → 空（调用方据此回落 AnimeThemes）。"""
        self.assertEqual(
            parse_from_wikitext("== あらすじ ==\n何かの説明。\n== スタッフ ==\n人名。"),
            [],
        )


class TestSectionSelection(unittest.TestCase):
    def test_skips_non_theme_section_and_uses_later_one(self):
        """**坑 4**：段落名会误命中。

        实测 `薬屋のひとりごと (アニメ)` 的第一个候选段落是「音楽」——
        讲的是**劇伴（配乐）作曲家**，一个主题歌都没有。
        真正的主题歌栏在后面。所以必须依次试，取第一个有结果的。
        """
        wt = """
== 音楽 ==
本作の劇伴は桶狭間ありさ、Kevin Penkin、神前暁の3名が担当した。

== 主題歌 ==
; 「花になって」
: [[緑黄色社会]]によるオープニングテーマ。
"""
        got = parse_from_wikitext(wt)
        self.assertEqual(len(got), 1, "应跳过「音楽」段，取到后面真正的主题歌")
        self.assertEqual(got[0].song_title, "花になって")


# ==================================================================== 标题校验

class TestTitleVerification(unittest.TestCase):
    """搜索兜底必须校验词条名 —— 宁可漏，不可错。"""

    def test_rejects_unrelated_page(self):
        """**真实踩到的误收**：找『元祖！バンドリちゃん』却接受了
        『ひみつのアッコちゃん』—— 完全无关的另一部动画。
        这比「没找到」更糟：找错了会静默写入错误的主题歌。
        """
        self.assertFalse(title_matches("元祖！バンドリちゃん", "ひみつのアッコちゃん"))

    def test_accepts_same_anime_with_suffix(self):
        self.assertTrue(title_matches("薬屋のひとりごと", "薬屋のひとりごと (アニメ)"))
        self.assertTrue(title_matches("私を喰べたい、ひとでなし",
                                      "私を喰べたい、ひとでなし (アニメ)"))

    def test_accepts_despite_symbols_and_year(self):
        """**真实踩到的误杀**：『キャッツ♥アイ (2025)』被拒掉了正确的
        『キャッツ・アイ』—— 因为 `♥` 没被清掉、年份也没剥。
        """
        self.assertTrue(title_matches("キャッツ♥アイ (2025)", "キャッツ・アイ"))

    def test_rejects_different_anime(self):
        self.assertFalse(title_matches("ウィッチウォッチ", "デュエル・マスターズ (アニメ)"))
        self.assertFalse(title_matches("永久のユウグレ", "逃げ上手の若君"))

    def test_canon_strips_punctuation_and_year(self):
        self.assertEqual(canon_title("キャッツ♥アイ (2025)"), "キャッツアイ")
        self.assertEqual(canon_title("キャッツ・アイ"), "キャッツアイ")
        self.assertEqual(canon_title("薬屋のひとりごと (アニメ)"), "薬屋のひとりごと")

    def test_empty_safe(self):
        self.assertFalse(title_matches("", "x"))
        self.assertFalse(title_matches("x", ""))
        self.assertEqual(canon_title(""), "")


# ==================================================================== 缓存与统计

class FakeWikiClient:
    """假 HTTP 客户端：记录调用次数，返回预置 wikitext。

    两种请求形状都要认（必修 2 之后两种都在用）：
      · `action=query&titles=A|B|C` —— 批量，主路径（`_revisions`）
      · `action=parse&page=X`       —— 单条，只在搜索兜底之后用到
    """

    def __init__(self, pages: dict[str, str], searches: dict[str, list[str]] | None = None):
        self.pages = pages
        self.searches = searches or {}
        self.calls: list[str] = []

    def get_json(self, url, **kw):
        import urllib.parse

        self.calls.append(url)
        if "list=search" in url:
            q = urllib.parse.unquote(url.split("srsearch=")[1].split("&")[0])
            return {"query": {"search": [{"title": t} for t in self.searches.get(q, [])]}}
        if "titles=" in url:
            raw = urllib.parse.unquote(url.split("titles=")[1].split("&")[0])
            pages = []
            for t in raw.split("|"):
                if t in self.pages:
                    # 词条存在（`""` = 存在但没有主题歌栏 —— 与「不存在」必须分开）
                    pages.append({"pageid": 1, "title": t,
                                  "revisions": [{"slots": {"main": {
                                      "content": self.pages[t]}}}]})
                else:
                    pages.append({"title": t, "missing": True})
            return {"batchcomplete": True, "query": {"pages": pages}}

        page = urllib.parse.unquote(url.split("page=")[1].split("&")[0])
        if page in self.pages:
            return {"parse": {"wikitext": self.pages[page]}}
        return {"error": {"code": "missingtitle"}}


class TestCachingAndStats(unittest.TestCase):
    def _src(self, pages):
        return WikipediaSource(client=FakeWikiClient(pages))

    def test_wikitext_cached(self):
        pages = {"A (アニメ)": "== 主題歌 ==\n; 「X」\n: [[歌手]]によるオープニングテーマ。"}
        src = self._src(pages)
        c = FakeWikiClient(pages)
        src.http = c
        src.fetch_themes(["A"])
        n1 = len(c.calls)
        src._wt_cache.clear()          # 只清 wikitext 缓存，保留统计
        src.fetch_themes(["A"])
        self.assertEqual(len(c.calls), n1 * 2, "缓存按词条生效，不该请求两次")
        # 同一个词条第二次读应命中缓存
        before = len(c.calls)
        src.fetch_themes(["A"])
        self.assertEqual(len(c.calls), before, "词条应有缓存")

    def test_prefers_anime_suffix(self):
        """先试 (アニメ) 子词条 —— 动画的主题歌栏通常在那里。"""
        pages = {"A (アニメ)": "== 主題歌 ==\n; 「X」\n: [[歌手]]によるオープニングテーマ。"}
        src = self._src(pages)
        themes, page = src.fetch_themes(["A"])
        self.assertEqual(page, "A (アニメ)")
        self.assertEqual(len(themes), 1)

    def test_falls_back_to_search_with_verification(self):
        pages = {"A (アニメ)": "", "A": "", "A (TV)": ""}
        pages["A (TV)"] = "== 主題歌 ==\n; 「Y」\n: [[歌手]]によるエンディングテーマ。"
        c = FakeWikiClient(pages, searches={"A アニメ": ["A (TV)"]})
        src = WikipediaSource(client=c)
        themes, page = src.fetch_themes(["A"])
        self.assertEqual(page, "A (TV)")
        self.assertEqual(themes[0].song_title, "Y")

    def test_search_result_rejected_when_unrelated(self):
        """搜索返回无关词条时必须拒掉，而不是拿去用。"""
        pages = {"Z (TV)": "== 主題歌 ==\n; 「Y」\n: [[歌手]]によるエンディングテーマ。"}
        c = FakeWikiClient(pages, searches={"A アニメ": ["Z (TV)"]})
        src = WikipediaSource(client=c)
        themes, page = src.fetch_themes(["A"])
        self.assertEqual(themes, [])
        self.assertEqual(page, "")
        self.assertGreaterEqual(src.stats["rejected_title"], 1)

    def test_missing_page_is_empty_not_error(self):
        src = self._src({})
        themes, page = src.fetch_themes(["不存在的东西"])
        self.assertEqual(themes, [])
        self.assertEqual(page, "")


# ==================================================================== 批量取正文（必修 2）
#
# 为什么要有批量：真机实测一份番单，维基 **62 次调用 → 120 次网络请求**
# （58 次重试、28 次 429、44.2 秒退避，一个词条都没问到），而其中
# **51 次是在问注定不存在的词条** —— 一个候选名一次请求。
# 换成 `action=query&prop=revisions&titles=A|B|C` 之后，
# 一次请求能问一整套候选名。
#
# ⚠️ 批量最大的风险不是慢，是**静默换了选中的词条**：
# `action=parse` 会自己跟重定向、自己做标题归一化，换成 `query` 之后
# 这两件事要我们自己映射回候选名。映射丢了不会报错，只会「找不到词条」
# —— 表现和「维基没收录」一模一样。所以下面这几条测试专门钉映射。

OP_FORMAT = "== 主題歌 ==\n; 「X」\n: [[歌手]]によるオープニングテーマ。"
ED_FORMAT = "== 主題歌 ==\n; 「Y」\n: [[歌手]]によるエンディングテーマ。"


class TestCandidateTitles(unittest.TestCase):
    def test_order_matches_the_sequential_scan(self):
        """候选名顺序**必须**等于原来的三重循环（name → variants → suffixes）。"""
        got = candidate_titles(["A 第2期"])
        self.assertEqual(got[:3],
                         ["A 第2期 (アニメ)", "A 第2期 (テレビアニメ)", "A 第2期"])
        self.assertIn("A (アニメ)", got)            # 剥掉季数后缀那一路
        self.assertEqual(len(got), len(set(got)), "不许重复（重复就是白发请求）")

    def test_empty_names_are_skipped(self):
        self.assertEqual(candidate_titles(["", None]), [])


class TestBatchResponseMapping(unittest.TestCase):
    """`action=query` 的返回形状要自己映射回候选名。"""

    class Client:
        def __init__(self, payload):
            self.payload = payload
            self.urls: list[str] = []

        def get_json(self, url, **kw):
            self.urls.append(url)
            return self.payload

    def _one(self, payload, titles=("A (アニメ)",)):
        c = self.Client(payload)
        src = WikipediaSource(client=c)
        return src, src._revisions(list(titles)), c

    def test_plain_page_maps_back_to_the_candidate(self):
        src, got, _ = self._one({"query": {"pages": [
            {"pageid": 1, "title": "A (アニメ)",
             "revisions": [{"slots": {"main": {"content": OP_FORMAT}}}]}]}})
        self.assertEqual(got, {"A (アニメ)": OP_FORMAT})
        self.assertEqual(src.stats["page_hits"], 1)

    def test_redirect_maps_back_to_the_candidate(self):
        """`redirects=1` 以前是 `action=parse` 自己跟的。

        跟丢了不会报错 —— 只会「这个词条没找到」，与「维基没收录」长得
        一模一样（红线：不许把我们的失败说成数据源没有）。
        """
        src, got, _ = self._one({"query": {
            "redirects": [{"from": "A (アニメ)", "to": "A (テレビアニメ)"}],
            "pages": [{"pageid": 1, "title": "A (テレビアニメ)",
                       "revisions": [{"slots": {"main": {"content": OP_FORMAT}}}]}]}})
        self.assertEqual(got, {"A (アニメ)": OP_FORMAT})
        self.assertEqual(src.stats["page_hits"], 1)

    def test_normalization_then_redirect_chain(self):
        """`A_(アニメ)` → 归一化成 `A (アニメ)` → 再重定向。两跳都要跟。"""
        src, got, _ = self._one({"query": {
            "normalized": [{"from": "A_(アニメ)", "to": "A (アニメ)"}],
            "redirects": [{"from": "A (アニメ)", "to": "A (テレビアニメ)"}],
            "pages": [{"pageid": 1, "title": "A (テレビアニメ)",
                       "revisions": [{"slots": {"main": {"content": OP_FORMAT}}}]}]}},
            titles=("A_(アニメ)",))
        self.assertEqual(got, {"A_(アニメ)": OP_FORMAT})

    def test_real_normalized_entry_shape(self):
        """⚠️ 用的是**真机探测抄回来的**条目，不是照文档编的。

        `.tmp/probe_batch_wiki.py` 实测（1 次请求）：
        `鬼滅の刃_(アニメ)` → `normalized: [{'fromencoded': False,
        'from': '鬼滅の刃_(アニメ)', 'to': '鬼滅の刃 (アニメ)'}]`。
        注意多出来的 `fromencoded` 键 —— 所以映射只能按 `from`/`to` 取值，
        不能假设条目里只有这两个键。
        """
        src, got, _ = self._one({"query": {
            "normalized": [{"fromencoded": False,
                            "from": "鬼滅の刃_(アニメ)", "to": "鬼滅の刃 (アニメ)"}],
            "pages": [{"pageid": 1, "title": "鬼滅の刃 (アニメ)",
                       "revisions": [{"slots": {"main": {"content": OP_FORMAT}}}]}]}},
            titles=("鬼滅の刃_(アニメ)",))
        self.assertEqual(got, {"鬼滅の刃_(アニメ)": OP_FORMAT})

    def test_real_redirect_entry_shape(self):
        """`.tmp/probe_redirect_wiki.py` 实测（2 次请求）：

            redirects = [{'from': '!', 'to': '感嘆符'}, {'from': '!!', 'to': '感嘆符'}]
            pages     = ['感嘆符', '氷の城壁']

        —— 请求名 `!` / `!!` 在 `pages` 里**根本不存在**，只有目标名在。
        映射丢了的后果是这两个候选名被判成「没问到」，进而整条链断开：
        而 `action=parse` 时代这件事是免费的（它自己跟重定向）。
        """
        payload = {"query": {
            "redirects": [{"from": "!", "to": "感嘆符"}, {"from": "!!", "to": "感嘆符"}],
            "pages": [{"pageid": 1, "title": "感嘆符",
                       "revisions": [{"slots": {"main": {"content": OP_FORMAT}}}]}]}}
        src, got, _ = self._one(payload, titles=("!", "!!"))
        self.assertEqual(got, {"!": OP_FORMAT, "!!": OP_FORMAT})
        self.assertEqual(src.stats["titles_unanswered"], 0)

    def test_content_shape_without_rvslots_is_read(self):
        """没带 `rvslots` 的形状是 `content: {"*": …}` —— 认了它才敢换接口。"""
        src, got, _ = self._one({"query": {"pages": [
            {"pageid": 1, "title": "A (アニメ)",
             "revisions": [{"content": {"*": OP_FORMAT}}]}]}})
        self.assertEqual(got, {"A (アニメ)": OP_FORMAT})

    def test_missing_flag_is_not_found(self):
        src, got, _ = self._one({"query": {"pages": [
            {"title": "A (アニメ)", "missing": True}]}})
        self.assertEqual(got, {"A (アニメ)": None})
        self.assertEqual(src.stats["not_found"], 1)
        self.assertEqual(src.stats["titles_unanswered"], 0)

    def test_absent_page_in_response_is_unanswered_not_missing(self):
        """服务端答了但没给这一条 —— 既不是「有」也不是「没有」。"""
        src, got, _ = self._one({"query": {"pages": []}})
        self.assertEqual(got, {})
        self.assertEqual(src.stats["titles_unanswered"], 1)
        self.assertEqual(src.stats["not_found"], 0)

    def test_existing_page_without_readable_content_is_unanswered(self):
        """⚠️ 词条**存在**却读不出正文 = 返回形状和我们预期的不一样。

        这种情况**不许**记成「取到了，只是没有主题歌栏」—— 那等于把
        「我们没读懂返回」说成「维基没收录」，方向正好反（§6）。
        记成「没问到」还有一个好处：日志那行账会立刻不平不了，
        恒等式仍然成立，用户看到的是「没问到」而不是「维基没有」。
        """
        src, got, _ = self._one({"query": {"pages": [
            {"pageid": 1, "title": "A (アニメ)"}]}})
        self.assertEqual(got, {})
        self.assertEqual(src.stats["titles_unanswered"], 1)
        self.assertEqual(src.stats["not_found"], 0)
        self.assertEqual(src.stats["page_hits"], 0)
        self.assertNotIn("A (アニメ)", src._wt_cache, "读不懂的东西不许缓存")


class TestBatchFailure(unittest.TestCase):
    class Boom:
        def __init__(self, exc):
            self.exc = exc
            self.urls: list[str] = []

        def get_json(self, url, **kw):
            self.urls.append(url)
            raise self.exc

    def test_failed_batch_is_unanswered_and_not_retried_one_by_one(self):
        """⚠️ 批量失败时**不许**退回逐个重问。

        退回就是原来那个形状（62 次调用 / 120 次请求），
        批量省下来的请求会在失败时全部还回去。
        """
        c = self.Boom(TimeoutError("梯子断了"))
        src = WikipediaSource(client=c, rps=0)
        page, how = src.find_article(["A"])
        self.assertEqual((page, how), (None, "none"))
        self.assertEqual(src.stats["titles_unanswered"], 3)
        self.assertEqual(src.stats["not_found"], 0, "没问到 ≠ 词条不存在")
        self.assertEqual(src.stats["api_failed"], 2, "1 次批量 + 1 次搜索兜底都失败")
        self.assertEqual(len([u for u in c.urls if "titles=" in u]), 1)
        self.assertNotIn("A (アニメ)", src._wt_cache, "失败**不许**写进缓存")

    def test_breaker_propagates_through_the_batch_path(self):
        c = self.Boom(RateLimited("被限流", retry_after=25.0,
                                  host="ja.wikipedia.org", breaker=True))
        src = WikipediaSource(client=c, rps=0)
        with self.assertRaises(RateLimited):
            src.find_article(["A"])
        self.assertEqual(src.stats["titles_unanswered"], 3,
                         "熔断时这一批候选名也要记成「没问到」")
        self.assertEqual(src.stats["rate_limit_stopped"], 1)
        self.assertIn("25", src.blocked_reason)


class TestBatchChunking(unittest.TestCase):
    def test_short_titles_go_in_one_chunk(self):
        self.assertEqual(_batch_chunks(["A", "B", "C"]), [["A", "B", "C"]])

    def test_long_titles_are_split_by_encoded_url_bytes(self):
        """日文标题编码后 ~9 字节/字 —— 50 个就是 7.5KB，代理会回 414。"""
        long_name = "あ" * 40                       # 编码后 360 字节
        titles = [f"{long_name}{i}" for i in range(20)]
        chunks = _batch_chunks(titles)
        self.assertGreater(len(chunks), 1)
        for ch in chunks:
            self.assertLessEqual(sum(len(_quote(t)) + 1 for t in ch),
                                 WIKI_BATCH_URL_MAX)
        self.assertEqual([t for ch in chunks for t in ch], titles, "切块不许丢/不许乱序")

    def test_later_chunk_is_only_asked_when_the_earlier_one_missed(self):
        """一批没命中才问下一批 —— 否则批量会把「注定 404 的猜测」全带上。"""
        names = ["A 第2期", "B 第1期", "C 第3期"]     # 18 个候选名 → 2 批
        cands = candidate_titles(names)
        self.assertGreater(len(cands), WIKI_TRY_BATCH)
        hit = cands[WIKI_TRY_BATCH]                  # 命中在第 2 批的第 1 个
        src = WikipediaSource(client=FakeWikiClient({hit: OP_FORMAT}))
        page, how = src.find_article(names)
        self.assertEqual((page, how), (hit, "exact"))
        self.assertEqual(len([u for u in src.http.calls if "titles=" in u]), 2)

    def test_first_chunk_hit_means_the_second_is_never_asked(self):
        names = ["A 第2期", "B 第1期", "C 第3期"]
        hit = candidate_titles(names)[0]
        src = WikipediaSource(client=FakeWikiClient({hit: OP_FORMAT}))
        page, _ = src.find_article(names)
        self.assertEqual(page, hit)
        self.assertEqual(len([u for u in src.http.calls if "titles=" in u]), 1)


class TestCandidateAccounting(unittest.TestCase):
    """⚠️ 日志那一行必须能逐项对账（§6）：

        候选名 = 命中缓存 + 查无此词条 + 取到正文 + 没问到
        titles_asked = 查无此词条 + 取到正文 + 没问到
    """

    def _check(self, src, cached=0):
        s = src.stats
        cand = s["titles_cached"] + s["titles_asked"]
        self.assertEqual(
            cand, s["titles_cached"] + s["not_found"] + s["page_hits"]
            + s["titles_unanswered"], f"候选名对不上账：{s}")
        self.assertEqual(
            s["titles_asked"],
            s["not_found"] + s["page_hits"] + s["titles_unanswered"],
            f"问过的候选名对不上账：{s}")

    def test_identity_holds_for_hits_and_misses(self):
        src = WikipediaSource(client=FakeWikiClient({"A (アニメ)": OP_FORMAT}))
        src.find_article(["A"])
        self._check(src)

    def test_identity_counts_cache_reuse_on_the_second_call(self):
        """同一批候选名问第二遍时全走进程内缓存 —— 0 次请求，但账要平。"""
        c = FakeWikiClient({"A (アニメ)": OP_FORMAT})
        src = WikipediaSource(client=c)
        src.find_article(["A"])
        n = len(c.calls)
        src.find_article(["A"])
        self.assertEqual(len(c.calls), n, "第二次应当全部命中缓存")
        self.assertGreaterEqual(src.stats["titles_cached"], 3)
        self._check(src)

    def test_identity_holds_when_the_request_fails(self):
        src = WikipediaSource(client=TestBatchFailure.Boom(TimeoutError("断了")), rps=0)
        src.find_article(["A"])
        self._check(src)


class TestBatchOverRealHttp(unittest.TestCase):
    """过一遍**真的** `HttpClient` 与 URL 拼装（本地回环服务器，不联网）。

    为什么还要这一层：上面那些假客户端直接吃拼好的 `url` 字符串，
    它们证明不了「`titles=A|B|C` 编码之后服务端还能拆回三个标题」。
    而 `|` 编码成 `%7C` 之后还算不算分隔符，是整个必修 2 的地基 ——
    猜错的话表现是「所有候选名都查无此词条」，与「维基没收录」一模一样。
    """

    OP = OP_FORMAT

    def setUp(self):
        import json
        import threading
        import urllib.parse
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.paths: list[str] = []
        pages = {"A (アニメ)": self.OP}
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.paths.append(self.path)
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                out = []
                for t in q.get("titles", [""])[0].split("|"):
                    if t in pages:
                        out.append({"pageid": 1, "title": t, "revisions": [
                            {"slots": {"main": {"content": pages[t]}}}]})
                    else:
                        out.append({"title": t, "missing": True})
                body = json.dumps({"batchcomplete": True,
                                   "query": {"pages": out}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):      # 别把访问日志打到测试输出里
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.addCleanup(self.srv.server_close)

    def _src(self):
        from unittest import mock

        from animusic import sources
        from animusic.http import HttpClient

        url = f"http://127.0.0.1:{self.srv.server_address[1]}/w/api.php"
        patcher = mock.patch.object(sources.wikipedia, "WIKI_API", url)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 缓存关掉：这条测试要数**真的发出去了几次**，命中缓存会把它藏起来
        return WikipediaSource(client=HttpClient(rps=0, retries=0, cache_ttl=0))

    def _query(self, i: int) -> dict:
        import urllib.parse

        return urllib.parse.parse_qs(urllib.parse.urlparse(self.paths[i]).query)

    def test_the_server_receives_all_candidates_in_one_request(self):
        src = self._src()
        themes, page = src.fetch_themes(["A"])
        self.assertEqual(page, "A (アニメ)")
        self.assertEqual(themes[0].song_title, "X")
        self.assertEqual(len(self.paths), 1, "3 个候选名应当只发 1 次请求")

        q = self._query(0)
        self.assertEqual(q["action"], ["query"])
        self.assertEqual(q["prop"], ["revisions"])
        self.assertEqual(q["rvslots"], ["main"])
        self.assertEqual(q["rvprop"], ["content"])
        self.assertEqual(q["redirects"], ["1"])
        # ⚠️ 这一条是地基：`|` 编码成 `%7C` 之后，服务端（这里是 parse_qs，
        #    与 MediaWiki 解码 query 的方式一致）必须还能拆回 3 个标题。
        self.assertEqual(len(q["titles"][0].split("|")), 3)
        self.assertIn("A (アニメ)", q["titles"][0].split("|"))

    def test_long_candidate_lists_are_split_across_requests(self):
        """候选名多于一瓶时按批切 —— 每批都不超过上限。"""
        src = self._src()
        names = [f"番{i} 第2期" for i in range(6)]       # 36 个候选名 → 3 批
        self.assertGreater(len(candidate_titles(names)), WIKI_TRY_BATCH)
        src.find_article(names)
        batch = [self._query(i) for i in range(len(self.paths))
                 if "titles" in self.paths[i]]
        self.assertGreater(len(batch), 1)
        for q in batch:
            titles = q["titles"][0].split("|")
            self.assertLessEqual(len(titles), WIKI_TRY_BATCH)
        self.assertEqual(sum(len(q["titles"][0].split("|")) for q in batch),
                         len(candidate_titles(names)), "整份候选名一个都不能漏")


if __name__ == "__main__":
    unittest.main(verbosity=2)
