"""第6、7节离线回归：所有API均为内存假响应，不访问账号或生产库。"""
import json
import os
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock

from animusic.http import HttpClient, RateLimited
from animusic.models import Anime, Theme, Origin, Season, Candidate, DecisionStatus
from animusic.orchestrator import Orchestrator
from animusic.sources.wikipedia import WikipediaSource, title_variants, theme_sections, WikiResponseError
from animusic.sources.wiki_matching import parse_scoped, related_animation_pages
from animusic.store import Store, ThemeIdentityConflict


def ja_song(title='日文歌曲', kind='オープニング'):
    return f'== 主題歌 ==\n; 「{title}」\n: [[歌手甲]]による{kind}テーマ。\n'


def zh_song(title='中文原文', kind='片頭曲'):
    return f'== 主題曲 ==\n=== {kind} ===\n*「{title}」\n**歌：[[歌手乙]]\n'


class MemoryWiki:
    def __init__(self, pages=None, hits=None, aliases=None):
        self.pages = pages or {}
        self.hits = hits or {}
        self.aliases = aliases or {}
        self.calls = []
        self.client = HttpClient(rps=0, retries=0)
        self.client.get_json = Mock(side_effect=self.get)

    def source(self):
        source = WikipediaSource(client=self.client)
        source.begin_round()
        return source

    def get(self, url, **kwargs):
        parsed = urlparse(url)
        lang = parsed.hostname.split('.')[0]
        args = parse_qs(parsed.query)
        self.calls.append((lang, args))
        if args.get('list') == ['search']:
            return {'query': {'search': [{'title': t} for t in self.hits.get((lang, args['srsearch'][0]), [])]}}
        titles = args['titles'][0].split('|')
        pages, converted = [], []
        for title in titles:
            canonical = self.aliases.get((lang, title), title)
            if canonical != title:
                converted.append({'from': title, 'to': canonical})
            value = self.pages.get((lang, canonical))
            if isinstance(value, Exception):
                raise value
            if value is None:
                pages.append({'title': canonical, 'missing': True})
            else:
                pages.append({'title': canonical, 'revisions': [{'slots': {'main': {'content': value}}}]})
        return {'query': {'pages': pages, 'converted' if lang == 'zh' else 'redirects': converted}}


class TestWikiScope(unittest.TestCase):
    def test_season_in_song_descriptions_excludes_old_season_and_keeps_uncertain_ova(self):
        text = ('== 主題歌（旧版） ==\n=== 第1期 ===\n; 「旧OP」\n: 歌手によるオープニングテーマ。\n'
                '== 主題歌（OVA） ==\n;「OVA」\n:歌手によるエンディングテーマ。\n'
                '== 主題歌（新版） ==\n;「第一期」\n:歌手による第1期のオープニングテーマ。\n'
                ';「第三期OP」\n:歌手による第3期のオープニングテーマ。\n'
                ';「第三期ED」\n:歌手による第3期のエンディングテーマ。\n')
        themes, _ = parse_scoped(text, '作品', ['作品 第3期'], 'ja')
        self.assertEqual([(t.kind, t.song_title) for t in themes], [('ED', 'OVA'), ('OP', '第三期OP'), ('ED', '第三期ED')])
        self.assertTrue(themes[0].scope_review)

    def test_chinese_definition_season_labels_and_row_types_select_only_target(self):
        table = ('{| class="wikitable"\n! 類別 !! 歌曲 !! 演唱\n|-\n'
                 '|片头曲 || {{单双书号转换|{{lang|ja|曲名}}}} || {{lang|ja|艺人}}\n|}\n')
        text = '== 主題曲（旧版） ==\n=== 第1期 ===\n旧版\n== 主題歌（新版） ==\n'
        text += '; 第1季\n' + table.replace('曲名', '第一季歌曲')
        text += '; 第3季\n' + table.replace('曲名', '第三季歌曲')
        themes, _ = parse_scoped(text, '作品', ['作品 第三季'], 'zh')
        self.assertEqual([(t.kind, t.song_title, t.artists) for t in themes], [('OP', '第三季歌曲', ['艺人'])])

    def test_adaptation_in_season_introduction_maps_original_work_without_part_math(self):
        text = ('== 第一季 ==\n=== 主題曲（第一季） ===\n;片頭曲\n:;「错季」\n::主唱：甲\n'
                '== 第六季 ==\n内容改编自原作第七部《[[原作标题]]》。\n'
                '=== 主題曲（第六季） ===\n;片頭曲\n:;「正确OP」\n::主唱：{{lj|甲}}\n'
                ';片尾曲\n:;「正确ED」\n::主唱：{{lang|ja|乙}}\n')
        themes, _ = parse_scoped(text, '动画总页', ['原作标题 Part7'], 'zh', '原作标题')
        self.assertEqual([(t.kind, t.song_title, t.artists) for t in themes],
                         [('OP', '正确OP', ['甲']), ('ED', '正确ED', ['乙'])])

    def test_stage_suffix_preserves_internal_numbers(self):
        variants = title_variants('作品 Part7 2nd & 3rd STAGE')
        self.assertIn('作品 Part7', variants)
        self.assertNotIn('作品', variants)
        self.assertIn('作品 Part7 2nd & 3rd STAGE', variants)

    def test_nested_theme_sections_include_children_until_peer(self):
        text = '== 主題歌 ==\n=== OP ===\n曲A\n==== 内訳 ====\n曲B\n== 各話 ==\n混入禁止\n'
        self.assertIn('曲B', theme_sections(text)[0])
        self.assertNotIn('混入禁止', theme_sections(text)[0])

    def test_selects_sixth_season_from_explicit_work_heading_not_part_number(self):
        text = ('== 主題歌 ==\n=== 1st Season ===\n; 「別の季」\n: 歌手によるオープニングテーマ。\n'
                '=== 6th Season ===\n; 「対象のOP」\n: 歌手によるオープニングテーマ。\n'
                '== 各話リスト ==\n=== 6th Season『スティール・ボール・ラン』 ===\nPart7\n')
        themes, locations = parse_scoped(text, '総合アニメ', ['作品 Part7'], 'ja', 'スティール・ボール・ラン')
        self.assertEqual([t.song_title for t in themes], ['対象のOP'])
        self.assertEqual(locations, ['主題歌 / 6th Season'])

    def test_name_without_season_collects_all_instead_of_first(self):
        text = '== 主題歌 ==\n=== 1st Season ===\n;「甲」\n:歌手によるオープニングテーマ。\n=== 2nd Season ===\n;「乙」\n:歌手によるオープニングテーマ。'
        themes, _ = parse_scoped(text, '総合', ['作品'], 'ja')
        self.assertEqual([t.song_title for t in themes], ['甲', '乙'])

    def test_theme_sections_under_different_season_parents_do_not_mix(self):
        text = '== 第1期 ==\n' + ja_song('第一季').replace('== 主題歌 ==', '=== 主題歌 ===')
        text += '\n== 第2期 ==\n' + ja_song('第二季').replace('== 主題歌 ==', '=== 主題歌 ===')
        themes, _ = parse_scoped(text, '作品', ['作品 第2期'], 'ja')
        self.assertEqual([t.song_title for t in themes], ['第二季'])
        themes, _ = parse_scoped(text, '作品', ['作品'], 'ja')
        self.assertEqual([t.song_title for t in themes], ['第一季', '第二季'])

    def test_related_links_are_animation_main_templates_only(self):
        text = '== 漫画 ==\n{{Main|無関係}}\n== WEBアニメ ==\n[[普通リンク]]\n{{Main|正しいアニメ}}\n{{Main|en:Other}}\n'
        self.assertEqual(related_animation_pages(text), ['正しいアニメ'])

    def test_chinese_lists_tables_nested_headers_and_unknown_kind(self):
        texts = [zh_song(), '== 主题歌 ==\n=== 片尾曲 ===\n;《结尾》\n:演唱：[[歌手甲]]\n',
                 '== 主題曲 ==\n{| class="wikitable"\n|+ 片頭曲\n! 使用話 !! 曲名 !! 歌手\n|-\n| 1-3 || [[表格歌曲]] || [[歌手丙]]\n|}\n']
        for text, title, kind in zip(texts, ['中文原文', '结尾', '表格歌曲'], ['OP', 'ED', 'OP']):
            with self.subTest(title=title):
                themes, _ = parse_scoped(text, '作品', ['作品'], 'zh')
                self.assertEqual([(t.song_title, t.kind) for t in themes], [(title, kind)])
                self.assertTrue(themes[0].artists)
        themes, _ = parse_scoped('== 主題曲 ==\n*[[曲名原文]]\n**歌：[[演唱者]]\n**作詞：另一人\n', '作品', ['作品'], 'zh')
        self.assertEqual([(t.song_title, t.kind, t.artists) for t in themes], [('曲名原文', None, ['演唱者'])])

    def test_language_templates_preserve_original_title_and_artist(self):
        themes, _ = parse_scoped("== 主題曲 ==\n*'''{{lj|架空の曲}}'''\n**歌：{{lj|架空歌手}}\n", '作品', ['作品'], 'zh')
        self.assertEqual([(t.song_title, t.artists, t.kind) for t in themes], [('架空の曲', ['架空歌手'], None)])
        themes, _ = parse_scoped(ja_song('架空器乐').replace('歌手甲', '作曲家甲作曲・編曲'), '作品', ['作品'], 'ja')
        self.assertEqual(themes[0].artists, ['作曲家甲'])


class TestWikiLanguageFlow(unittest.TestCase):
    def test_japanese_success_never_requests_chinese(self):
        memory = MemoryWiki({('ja', '作品 (アニメ)'): ja_song()})
        result = memory.source().fetch_result(['中文作品', '作品'])
        self.assertTrue(result.themes)
        self.assertTrue(all(lang == 'ja' for lang, _ in memory.calls))

    def test_search_uses_japanese_name_not_only_first_chinese(self):
        memory = MemoryWiki({('ja', '日本の作品 (アニメ)'): ja_song()},
                            {('ja', '日本の作品 アニメ'): ['日本の作品 (アニメ)']})
        # 精确候选不会命中：实际标题用另一空白形式，但搜索结果可读。
        memory.pages[('ja', '日本の作品 (2026年のアニメ)')] = memory.pages.pop(('ja', '日本の作品 (アニメ)'))
        memory.hits[('ja', '日本の作品 アニメ')] = ['日本の作品 (2026年のアニメ)']
        result = memory.source().fetch_result(['中文译名', '日本の作品'])
        self.assertTrue(result.themes)
        self.assertEqual(result.diagnostics['lookup_trace'][-1]['how'], 'search')
        self.assertEqual(result.diagnostics['lookup_trace'][-1]['query_name'], '日本の作品')

    def test_manga_to_anime_uses_scope_and_no_fixed_work_mapping(self):
        manga, anime = '架空原作', '別名の総合アニメ'
        text = ('== 主題歌 ==\n=== 1st Season ===\n;「誤った季」\n:歌手によるオープニングテーマ。\n'
                '=== 6th Season ===\n;「正しい季」\n:歌手によるエンディングテーマ。\n'
                '== 各話 ==\n=== 6th Season『架空原作』 ===\n')
        memory = MemoryWiki({('ja', manga): '== WEBアニメ ==\n{{Main|' + anime + '}}', ('ja', anime): text})
        result = memory.source().fetch_result([manga + ' 2nd & 3rd STAGE'])
        self.assertEqual([t.song_title for t in result.themes], ['正しい季'])
        self.assertIn('related_animation', [t['how'] for t in result.diagnostics['lookup_trace']])

    def test_wrong_related_link_and_cycle_are_not_followed_forever(self):
        memory = MemoryWiki({('ja', '作品'): '== アニメ ==\n{{Main|別のアニメ}}',
                            ('ja', '別のアニメ'): ja_song('混入禁止')})
        result = memory.source().fetch_result(['作品'])
        self.assertFalse(result.themes)
        self.assertLessEqual(len(memory.calls), 18)
        self.assertEqual(len([c for c in memory.calls if c[1].get('titles') == ['別のアニメ']]), 1)
        memory = MemoryWiki({('ja', '作品'): '== アニメ ==\n{{Main|作品アニメ}}',
                             ('ja', '作品アニメ'): '原作：作品\n== アニメ ==\n{{Main|作品}}'})
        result = memory.source().fetch_result(['作品'])
        self.assertFalse(result.themes)
        self.assertLessEqual(len(memory.calls), 18)

    def test_casual_work_mention_on_wrong_related_page_is_insufficient(self):
        memory = MemoryWiki({('ja', '原作甲'): '== アニメ ==\n{{Main|無関係作品}}',
                            ('ja', '無関係作品'): '原作甲にも言及した評論。\n' + ja_song('混入禁止')})
        result = memory.source().fetch_result(['原作甲'])
        self.assertFalse(result.themes)

    def test_each_known_name_gets_search_budget_before_spelling_variants(self):
        memory = MemoryWiki({('ja', '別名作品 (2026年のアニメ)'): ja_song().replace('による', 'による第2期の')},
                            {('ja', '別名作品 アニメ'): ['別名作品 (2026年のアニメ)']})
        result = memory.source().fetch_result(['長い作品！ 第2期', '別名作品'])
        self.assertTrue(result.themes)
        self.assertTrue(any(args.get('srsearch') == ['別名作品 アニメ'] for _, args in memory.calls))

    def test_chinese_conversion_and_real_fallback_language(self):
        memory = MemoryWiki({('zh', '作品與約定'): zh_song()}, aliases={('zh', '作品与约定'): '作品與約定'})
        result = memory.source().fetch_result(['作品与约定', '作品の約束'])
        self.assertEqual(result.diagnostics['source_language'], 'zh')
        self.assertEqual([t.song_title for t in result.themes], ['中文原文'])
        self.assertIn('zh.wikipedia.org', result.article_url)
        self.assertTrue(any(args.get('converttitles') == ['1'] for lang, args in memory.calls if lang == 'zh'))
        self.assertEqual([r['source_language'] for r in result.diagnostics['language_results']], ['ja', 'zh'])

    def test_network_and_rate_limit_do_not_fallback(self):
        for error, reason in [(TimeoutError('超时'), 'timeout'),
                              (RateLimited('限流', retry_after=20, breaker=True), 'rate_limited'),
                              (ValueError('响应异常'), 'parse_error')]:
            with self.subTest(reason=reason):
                memory = MemoryWiki()
                memory.client.get_json.side_effect = error
                source = memory.source()
                result = source.fetch_result(['作品'])
                self.assertEqual(result.reason_code, reason)
                self.assertIsNone(source._zh_source)
    def test_japanese_ambiguity_falls_back_but_chinese_scope_is_independent(self):
        ambiguous = ('== 主題歌 ==\n=== 1st Season ===\n;「甲」\n:歌手によるオープニングテーマ。\n'
                     '=== 2nd Season ===\n;「乙」\n:歌手によるエンディングテーマ。')
        memory = MemoryWiki({('ja', '作品'): ambiguous, ('zh', '作品'): zh_song('中文安全结果').replace('== 主題曲 ==', '== 主題曲（第3季） ==')})
        source = memory.source()
        result = source.fetch_result(['作品 第3期'])
        self.assertEqual([t.song_title for t in result.themes], ['中文安全结果'])
        self.assertEqual(result.reason_code, '')
        self.assertEqual(result.diagnostics['language_results'][0]['reason_code'], 'scope_ambiguous')
        source.begin_round()
        self.assertEqual(source.fetch_result(['作品 第3期']).themes[0].song_title, '中文安全结果')
        memory.pages[('zh', '作品')] = ambiguous
        result = memory.source().fetch_result(['作品 第3期'])
        self.assertEqual(result.reason_code, '')
        self.assertEqual([t.song_title for t in result.themes], ['甲', '乙'])
        self.assertEqual(result.diagnostics['scope_mode'], 'all_seasons_fallback')
        self.assertEqual([r['reason_code'] for r in result.diagnostics['language_results']],
                         ['scope_ambiguous', 'scope_ambiguous'])

    def test_japanese_ambiguity_then_chinese_network_failure_keeps_both_reasons(self):
        memory = MemoryWiki({('ja', '作品'): ja_song('前季').replace('== 主題歌 ==', '== 主題歌（第1期） =='),
                            ('zh', '作品'): TimeoutError('中文超时')})
        result = memory.source().fetch_result(['作品 第3期'])
        self.assertEqual(result.reason_code, 'timeout')
        self.assertIn('未能定位目标季度', result.message)
        self.assertIn('中文超时', result.message)
        self.assertEqual([r['reason_code'] for r in result.diagnostics['language_results']], ['scope_ambiguous', 'timeout'])

    def test_unknown_type_is_preserved_not_guessed(self):
        memory = MemoryWiki({('zh', '作品'): '== 主題曲 ==\n*[[シュークリーム]]\n**歌：[[鈴音ひとみ]]\n'})
        result = memory.source().fetch_result(['作品'])
        self.assertEqual(result.reason_code, 'type_unconfirmed')
        self.assertFalse(result.themes)
        self.assertEqual(result.diagnostics['untyped_themes'][0]['artists'], ['鈴音ひとみ'])

    def test_chinese_search_validates_original_japanese_field(self):
        jp, cn = '人付き合いの物語', '繁體中文題名'
        memory = MemoryWiki({('zh', cn): '| 日文名稱 = ' + jp + '\n' + zh_song()},
                            {('zh', jp + ' 动画'): [cn]})
        result = memory.source().fetch_result([jp])
        self.assertEqual([t.song_title for t in result.themes], ['中文原文'])
        memory.pages[('zh', cn)] = '| 日文名稱 = 無関係な作品\n' + zh_song('错误曲目')
        self.assertFalse(memory.source().fetch_result([jp]).themes)

    def test_dual_failure_reports_both_and_budget_cannot_be_bypassed(self):
        memory = MemoryWiki({('zh', '作品'): TimeoutError('中文超时')})
        result = memory.source().fetch_result(['作品'])
        self.assertEqual(result.reason_code, 'timeout')
        self.assertIn('未找到', result.message)
        self.assertIn('中文超时', result.message)
        source = MemoryWiki().source()
        source._request_budget = {'remaining': 0}
        result = source._fetch_single(['作品'])
        self.assertEqual(result.reason_code, 'lookup_budget_exceeded')

    def test_metadata_orders_chinese_name_even_when_japanese_has_no_kana(self):
        memory = MemoryWiki({('zh', '中文名称'): zh_song()})
        result = memory.source().fetch_result(['日本漢字', '中文名称'], title_jp='日本漢字', title_cn='中文名称')
        first_zh = next(args for lang, args in memory.calls if lang == 'zh')
        self.assertEqual(first_zh['titles'][0].split('|')[0], '中文名称')
        self.assertTrue(result.themes)

    def test_conflicting_sequel_redirect_is_preserved_for_manual_fallback(self):
        memory = MemoryWiki({('ja', '作品 第3期'): ja_song('错误续作')},
                            aliases={('ja', '作品 第2期 (アニメ)'): '作品 第3期'})
        result = memory.source().fetch_result(['作品 第2期'])
        self.assertEqual(result.reason_code, '')
        self.assertEqual([t.song_title for t in result.themes], ['错误续作'])
        self.assertEqual(result.diagnostics['scope_mode'], 'all_seasons_fallback')
        self.assertEqual(result.diagnostics['language_results'][0]['reason_code'], 'scope_ambiguous')

    def test_language_caches_do_not_mix_and_new_round_refetches(self):
        memory = MemoryWiki({('ja', '作品'): '== あらすじ ==\n内容', ('zh', '作品'): zh_song()})
        source = memory.source()
        first = source.fetch_result(['作品'])
        count = len(memory.calls)
        self.assertTrue(first.themes)
        source.fetch_result(['作品'])
        self.assertGreaterEqual(len(memory.calls), count)  # 搜索不做空结果缓存，正文各语言独立缓存。
        self.assertNotEqual(source._wt_cache['作品'], source._zh_source._wt_cache['作品'])
        source.begin_round()
        previous = len(memory.calls)
        self.assertTrue(source.fetch_result(['作品']).themes)
        self.assertGreater(len(memory.calls), previous)


class TestWikiPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'state.db')
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.season = Season.parse('2026Q4')
        self.sid = self.store.get_season_id(2026, 4)
        self.aid = self.store.upsert_anime(self.sid, Anime(season_key='2026Q4', title_jp='作品', bgm_id=1))

    def test_language_metadata_idempotence_and_manual_data_protection(self):
        theme = Theme(anime_id=self.aid, kind='OP', song_title='原文', artists=['来源艺人'], source='wikipedia',
                      origin=Origin.SEARCH, raw={'source_language': 'ja', 'article_url': 'https://ja.wikipedia.org/wiki/作品'})
        tid = self.store.upsert_theme(self.aid, theme)
        self.store.conn.execute("UPDATE theme SET song_title='人工名称',artists='[\"人工艺人\"]',origin='manual',included=0 WHERE id=?", (tid,))
        self.store.set_decision(tid, DecisionStatus.SKIPPED, decided_by='human')
        self.store.mark_pushed('历史歌单', '123', tid)
        theme.raw = {'source_language': 'zh', 'article_url': 'https://zh.wikipedia.org/wiki/作品'}
        self.assertEqual(self.store.upsert_theme(self.aid, theme), tid)
        row = self.store.theme_list(self.sid, False)[0]
        self.assertEqual((row['song_title'], row['artists'], row['included']), ('人工名称', '["人工艺人"]', 0))
        self.assertEqual(len(json.loads(row['raw'])['wikipedia_sources']), 2)
        self.assertEqual(self.store.upsert_theme(self.aid, theme), tid)
        self.assertEqual(len(self.store.theme_list(self.sid, False)), 1)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(len(json.loads(self.store.theme_list(self.sid, False)[0]['raw'])['wikipedia_sources']), 2)
        theme.song_title = '疑似译名'
        with self.assertRaises(ThemeIdentityConflict):
            self.store.upsert_theme(self.aid, theme)
        self.assertEqual(len(self.store.theme_list(self.sid, False)), 1)

    def test_unknown_confirmation_survives_restart_and_requery(self):
        from animusic.gui.session import WizardSession
        memory = MemoryWiki({('zh', '作品'): '== 主題曲 ==\n*[[原文曲名]]\n**歌：[[歌手]]\n'})
        source = memory.source()
        orch = Orchestrator.__new__(Orchestrator)
        orch.store, orch.wikipedia = self.store, source
        orch.progress, orch.should_stop = lambda _: None, None
        orch.refresh_themes(self.season)
        state = self.store.theme_fetch_rows(self.sid)[0]
        self.assertEqual((state['status'], state['reason_code']), ('failed', 'type_unconfirmed'))
        self.assertEqual(self.store.theme_list(self.sid), [])
        session = WizardSession(store=self.store, season=self.season)
        item = state['diagnostics']['untyped_themes'][0]
        session.confirm_wiki_type(self.aid, item, 'ED')
        row = self.store.theme_list(self.sid)[0]
        self.assertEqual((row['kind'], row['song_title']), ('ED', '原文曲名'))
        self.store.close()
        self.store = Store(self.path)
        orch.store = self.store
        orch.refresh_themes(self.season)
        self.assertEqual(len(self.store.theme_list(self.sid)), 1)
        self.assertEqual(self.store.theme_fetch_rows(self.sid)[0]['status'], 'success')
        self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM anime').fetchone()[0], 1)

    def test_missing_choice_table_upgrade_is_repeatable_and_preserves_old_rows(self):
        tid = self.store.upsert_theme(self.aid, Theme(anime_id=self.aid, kind='OP', song_title='旧曲',
            source='wikipedia', origin=Origin.MANUAL, included=False))
        self.store.set_decision(tid, DecisionStatus.SKIPPED, decided_by='human')
        self.store.mark_pushed('旧歌单', '123', tid)
        tables = ('anime', 'theme', 'candidate', 'decision', 'pushed', 'theme_fetch')
        before = {name: [tuple(r) for r in self.store.conn.execute(f'SELECT * FROM {name}')] for name in tables}
        self.store.conn.execute('DROP TABLE wiki_type_choice')
        self.store._commit()
        for _ in range(2):
            self.store.close()
            self.store = Store(self.path)
            self.assertEqual(before, {name: [tuple(r) for r in self.store.conn.execute(f'SELECT * FROM {name}')]
                                      for name in tables})
            self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM wiki_type_choice').fetchone()[0], 0)

    def test_export_keeps_language_article_section_and_attribution(self):
        import csv
        from pathlib import Path
        from animusic.sources.wikipedia import ATTRIBUTION
        self.store.upsert_theme(self.aid, Theme(anime_id=self.aid, kind='OP', song_title='中文页原曲名',
            source='wikipedia', origin=Origin.SEARCH, raw={'source_language': 'zh',
                'article_url': 'https://zh.wikipedia.org/wiki/作品', 'section': '主題曲 / 片頭曲'}))
        orch = Orchestrator.__new__(Orchestrator)
        orch.store = self.store
        paths = orch.export(self.season, os.path.join(self.tmp.name, 'report'))
        with open(paths['csv'], encoding='utf-8-sig', newline='') as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual((row['source'], row['source_language'], row['source_section']),
                         ('wikipedia', 'zh', '主題曲 / 片頭曲'))
        self.assertIn('zh.wikipedia.org', row['source_url'])
        self.assertIn(ATTRIBUTION, Path(paths['sources']).read_text(encoding='utf-8'))
