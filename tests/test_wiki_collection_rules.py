"""审核后的动画歌曲收录规则；全部假响应、临时库，不驱动真实账号。"""
import csv
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import closing

from test_wiki_matching import MemoryWiki, ja_song, zh_song
from animusic.models import ThemeKind, Anime, Theme, Season, Candidate
from animusic.sources.wiki_songs import parse_scoped, scope_songs
from animusic.sources.wikipedia import title_variants
from animusic.store import Store, SCHEMA
from animusic.orchestrator import Orchestrator
from animusic.gui.session import WizardSession
from animusic.config import Config
from animusic.query_builder import build_queries
from animusic.scorer import Target, anime_alias_match


def seasons():
    return ('== 主題歌 ==\n=== 第1期 ===\n;「前季OP」\n:甲によるオープニングテーマ。\n'
            '=== 第2期 ===\n;「当季ED」\n:乙によるエンディングテーマ。\n'
            '== 挿入歌 ==\n;「未标季插曲」\n:丙による劇中歌。\n')


class TestApprovedWikiRules(unittest.TestCase):
    def test_no_season_collects_every_animation_season_and_insert(self):
        result = MemoryWiki({('ja', '作品'): seasons()}).source().fetch_result(['作品'])
        self.assertEqual([(str(t.kind), t.song_title) for t in result.themes],
                         [('OP', '前季OP'), ('ED', '当季ED'), ('IN', '未标季插曲')])
        self.assertEqual(result.reason_code, '')
        self.assertEqual(len(result.diagnostics['language_results']), 1)

    def test_target_season_keeps_uncertain_insert_without_known_previous_season(self):
        result = MemoryWiki({('ja', '作品'): seasons()}).source().fetch_result(['作品 第2期'])
        self.assertEqual([t.song_title for t in result.themes], ['当季ED', '未标季插曲'])
        self.assertTrue(result.themes[1].scope_review)
        self.assertEqual(result.themes[0].scope_review, '')
        self.assertTrue(result.diagnostics['review_required'])

    def test_no_season_liar_game_cours_are_not_seasons(self):
        body = '== テレビアニメ ==\n=== 主題歌 ===\n'
        for title, cour in [('OP1', 1), ('OP2', 2)]:
            body += f';「{title}」\n:甲による第1期第{cour}クールのオープニングテーマ。\n'
        result = MemoryWiki({('ja', 'LIAR GAME'): body}).source().fetch_result(['LIAR GAME', '欺诈游戏'])
        self.assertEqual([t.song_title for t in result.themes], ['OP1', 'OP2'])
        self.assertEqual(result.diagnostics['scope_mode'], 'all_seasons')

    def test_tetsuryo_excludes_drama_even_with_op_ed_labels(self):
        body = ('== テレビドラマ ==\n=== 主題歌 ===\n;「真人歌曲」\n:甲によるオープニングテーマ。\n'
                '== テレビアニメ ==\n=== 主題歌（テレビアニメ） ===\n'
                ';「GO! GO! RAILWAY!!」\n:亜咲花によるオープニングテーマ。\n'
                ';「わたし未来線」\n:てつりょー会によるエンディングテーマ。\n')
        memory = MemoryWiki({('ja', '鉄道むすめ'): body}, aliases={('ja', 'てつりょー！meet with 鉄道むすめ'): '鉄道むすめ'})
        result = memory.source().fetch_result(['てつりょー！meet with 鉄道むすめ', '铁旅！meet with 铁道少女'])
        self.assertEqual([t.song_title for t in result.themes], ['GO! GO! RAILWAY!!', 'わたし未来線'])

    def test_chinese_media_ancestor_blocks_drama_and_accepts_animation(self):
        text = '== 電視劇 ==\n' + zh_song('真人').replace('== 主題曲 ==', '=== 主題曲 ===')
        text += '== 電視動畫 ==\n' + zh_song('动画').replace('== 主題曲 ==', '=== 主題曲 ===')
        songs, _ = parse_scoped(text, '作品', ['作品'], 'zh')
        self.assertEqual([s.song_title for s in songs], ['动画'])

    def test_unknown_media_on_mixed_page_is_not_assumed_animation(self):
        text = '== 電視劇 ==\n真人说明\n' + zh_song('无归属')
        self.assertFalse(parse_scoped(text, '混合页', ['混合页'], 'zh')[0])

    def test_japanese_row_comments_choose_drug_season_three(self):
        text = ('== 主題歌 ==\n{| class="wikitable"\n|+ オープニングテーマ\n'
                '! 使用話 !! 曲名 !! 歌手 !! 作曲\n|-\n! 1-12<!-- 第1期 -->\n| 前季 || 甲 || 作者\n'
                '|-\n! 49-<!-- 第3期・第1クール -->\n| 雲を抜け風下へ私だけ || ヨルシカ || 作者\n|}\n')
        selected, _, diag = scope_songs(text, '薬屋のひとりごと (アニメ)', ['薬屋のひとりごと 第3期'], 'ja')
        self.assertTrue(diag['scope_resolved'])
        self.assertEqual([(s.song_title, s.artists, s.season_keys) for s in selected],
                         [('雲を抜け風下へ私だけ', ['ヨルシカ'], ('3',))])

    def test_ref_headline_season_does_not_override_row_comment(self):
        text = ('== 主題歌 ==\n{|\n|+ オープニングテーマ\n! 使用話 !! 曲名 !! 歌手\n|-\n'
                '!49-<!-- 第3期 -->\n| 正确<ref>第1期新闻</ref> || 甲\n|}\n')
        selected, _, diag = scope_songs(text, '作品', ['作品 第3期'], 'ja')
        self.assertTrue(diag['scope_resolved'])
        self.assertEqual(selected[0].season_keys, ('3',))

    def test_insert_episode_uses_explicit_mapping_in_same_song_column(self):
        text = ('== 主題歌 ==\n{|\n|+ オープニングテーマ\n! 使用話 !! 曲名 !! 歌手\n|-\n'
                '!1-12<!-- 第1期 -->\n| 旧OP || 甲\n|-\n!49-<!-- 第3期 -->\n| 新OP || 乙\n|}\n'
                '{|\n|+ 挿入歌\n! 使用話 !! 曲名 !! 歌手\n|-\n!3\n| 旧插曲 || 丙\n|-\n!50\n| 新插曲 || 丁\n|}\n')
        selected, _, diag = scope_songs(text, '作品', ['作品 第3期'], 'ja')
        self.assertTrue(diag['scope_resolved'])
        self.assertEqual([s.song_title for s in selected], ['新OP', '新插曲'])

    def test_later_animation_history_does_not_change_song_type_or_season(self):
        text = ('== 主題歌 ==\n=== 5th Season ===\n;「当季OP」\n:甲によるオープニングテーマ。\n'
                ':オープニング映像は2nd Season以来。\n=== 6th Season ===\n;「当季ED」\n:乙によるエンディングテーマ。\n'
                ':昔はオープニングテーマの候補だった。\n')
        selected, _, _ = scope_songs(text, '系列', ['系列 第6期'], 'ja')
        self.assertEqual([(str(s.kind), s.song_title) for s in selected], [('ED', '当季ED')])

    def test_explicit_song_reuse_can_belong_to_more_than_one_season(self):
        text = ('== 主題歌 ==\n=== 1st Season ===\n;「复用曲」\n:甲によるエンディングテーマ。また5th Seasonでも使用。\n')
        selected, _, diag = scope_songs(text, '系列', ['系列 第5期'], 'ja')
        self.assertTrue(diag['scope_resolved'])
        self.assertEqual(selected[0].kind, ThemeKind.ED)

    def test_chinese_bold_season_tables_and_rowspan_types(self):
        table = ('{|\n! 類別 !! 使用話 !! 曲名 !! 歌手 !! 作曲\n|-\n'
                 '| rowspan="2"|片頭曲\n|1 || OP甲 || rowspan="2"|歌手甲 || 作者\n|-\n'
                 '|2 || OP乙 || 作者\n|-\n| rowspan="2"|插曲\n|3 || 插甲 || 歌手乙 || 作者\n|-\n'
                 '|4 || 插乙 || 歌手丙 || 作者\n|}\n')
        text = "== 主題曲 ==\n'''第1季'''\n" + table.replace('OP甲', '旧OP')
        text += "'''第3季'''\n" + table
        songs, _ = parse_scoped(text, '作品 (動畫)', ['作品 第三季'], 'zh')
        self.assertEqual([(str(s.kind), s.song_title, s.artists) for s in songs],
                         [('OP', 'OP甲', ['歌手甲']), ('OP', 'OP乙', ['歌手甲']),
                          ('IN', '插甲', ['歌手乙']), ('IN', '插乙', ['歌手丙'])])

    def test_nested_chinese_labels_and_book_title_templates(self):
        text = ('== 電視動畫 ==\n=== 主題曲 ===\n;第1季\n:;片頭曲\n'
                '::;{{單雙書名號轉換|{{lang|ja|あぶく}}}}（第一季度）\n:::演唱：ヨルシカ\n'
                ':;插曲\n::;{{單雙書名號轉換|插入歌}}\n:::演唱：歌手\n')
        songs, _ = parse_scoped(text, 'LIAR GAME', ['LIAR GAME'], 'zh')
        self.assertEqual([(str(s.kind), s.song_title) for s in songs], [('OP', 'あぶく'), ('IN', '插入歌')])

    def test_japanese_unknown_scope_tries_chinese_then_all_seasons_review(self):
        memory = MemoryWiki({('ja', '作品'): seasons(), ('zh', '作品'): zh_song('中文全季')})
        result = memory.source().fetch_result(['作品 第3期'])
        self.assertEqual([t.song_title for t in result.themes], ['中文全季'])
        self.assertEqual(result.reason_code, '')
        self.assertEqual(result.diagnostics['scope_mode'], 'all_seasons_fallback')
        self.assertIn('全季收录', result.message)
        self.assertTrue(result.themes[0].scope_review)
        self.assertEqual([r['reason_code'] for r in result.diagnostics['language_results']], ['scope_ambiguous', 'scope_ambiguous'])

    def test_chinese_missing_reuses_current_japanese_all_seasons(self):
        result = MemoryWiki({('ja', '作品'): seasons()}).source().fetch_result(['作品 第3期'])
        self.assertEqual(len(result.themes), 3)
        self.assertEqual(result.diagnostics['source_language'], 'ja')
        self.assertTrue(all(t.scope_review for t in result.themes))
        self.assertEqual(result.diagnostics['language_results'][1]['reason_code'], 'article_not_found')

    def test_chinese_resolves_season_instead_of_falling_back(self):
        memory = MemoryWiki({('ja', '作品'): seasons(),
                             ('zh', '作品'): zh_song('目标季').replace('== 主題曲 ==', '== 主題曲（第3季） ==')})
        result = memory.source().fetch_result(['作品 第3期'])
        self.assertEqual([s.song_title for s in result.themes], ['目标季'])
        self.assertTrue(result.diagnostics['scope_resolved'])

    def test_chinese_timeout_is_not_masked_by_all_seasons(self):
        result = MemoryWiki({('ja', '作品'): seasons(), ('zh', '作品'): TimeoutError('网络超时')}).source().fetch_result(['作品 第3期'])
        self.assertEqual(result.reason_code, 'timeout')
        self.assertFalse(result.themes)

    def test_scope_conflict_is_retained_for_review(self):
        text = '== 主題歌（第1期） ==\n;「冲突」\n:歌手による第2期のエンディングテーマ。\n'
        selected, _, diag = scope_songs(text, '作品', ['作品 第2期'], 'ja')
        self.assertEqual(selected[0].season_keys, ('1', '2'))
        self.assertTrue(selected[0].scope_review)
        self.assertFalse(diag['scope_resolved'])

    def test_full_steel_ball_run_redirect_maps_specific_title(self):
        jp = 'スティール・ボール・ラン ジョジョの奇妙な冒険 2nd & 3rd STAGE'
        cn = '飙马野郎 JOJO的奇妙冒险 第二&第三赛段'
        text = ('== 主題歌 ==\n=== 1st Season ===\n;「别季」\n:甲によるオープニングテーマ。\n'
                '=== 6th Season ===\n;「SPIN」\n:Kroiによるオープニングテーマ。\n'
                '== 各話 ==\n=== 6th Season『スティール・ボール・ラン』 ===\n[[スティール・ボール・ラン]]\n')
        memory = MemoryWiki({('ja', 'ジョジョの奇妙な冒険 (テレビアニメ)'): text},
                            aliases={('ja', title_variants(jp)[1]): 'ジョジョの奇妙な冒険 (テレビアニメ)'})
        result = memory.source().fetch_result([jp, cn], title_jp=jp, title_cn=cn)
        self.assertEqual([s.song_title for s in result.themes], ['SPIN'])
        self.assertEqual(result.diagnostics['scope_mode'], 'series')

    def test_chinese_search_japanese_field_verifies_full_name(self):
        jp = 'スティール・ボール・ラン ジョジョの奇妙な冒険 2nd & 3rd STAGE'
        cn = '飙马野郎 JOJO的奇妙冒险 第二&第三赛段'
        manga = '|japanese=スティール・ボール・ラン\n== 動畫 ==\n{{Main|JoJo的奇妙冒險 (電視動畫)}}'
        anime = ('== 第六季 ==\n内容改编自原作第七部《[[飆馬野郎]]》。\n'
                 '=== 主題曲（第六季） ===\n;片頭曲\n:;「SPIN」\n::主唱：Kroi\n')
        memory = MemoryWiki({('zh', '飆馬野郎'): manga, ('zh', 'JoJo的奇妙冒險 (電視動畫)'): anime},
                            {('zh', jp + ' 动画'): ['飆馬野郎']})
        result = memory.source().fetch_result([jp, cn], title_jp=jp, title_cn=cn)
        self.assertEqual([s.song_title for s in result.themes], ['SPIN'])
        self.assertEqual(result.diagnostics['source_language'], 'zh')
        self.assertIn('飙马野郎 JOJO的奇妙冒险', title_variants(cn))

    def test_insert_only_success_and_no_fake_ending_type(self):
        result = MemoryWiki({('ja', '作品'): '== 挿入歌 ==\n;「歌」\n:甲による劇中歌。'}).source().fetch_result(['作品'])
        self.assertEqual(result.reason_code, '')
        self.assertEqual(result.themes[0].kind, ThemeKind.IN)
        plan = build_queries('歌', ['歌手'], ['作品'], 'IN')
        self.assertTrue(any('插入曲' in q for q in plan.queries))
        self.assertFalse(any('片尾曲' in q for q in plan.queries))
        target = Target('歌', ['歌手'], ['作品'], kind='IN')
        self.assertEqual(anime_alias_match(['TV动画《作品》插入曲'], target), 1.0)
        self.assertLess(anime_alias_match(['TV动画《作品》片尾曲'], target), 1.0)

    def test_unmapped_series_is_reviewed_after_chinese_fallback(self):
        text = seasons()
        memory = MemoryWiki({('ja', '系列 (テレビアニメ)'): text},
                            aliases={('ja', '具体作品 系列 (アニメ)'): '系列 (テレビアニメ)'})
        result = memory.source().fetch_result(['具体作品 系列'])
        self.assertEqual(result.diagnostics['scope_mode'], 'all_seasons_fallback')
        self.assertEqual(len(result.themes), 3)

    def test_song_duplicate_is_not_saved_twice(self):
        body = ja_song() + ja_song()
        result = MemoryWiki({('ja', '作品'): body}).source().fetch_result(['作品'])
        self.assertEqual(len(result.themes), 1)

    def test_unknown_type_is_not_guessed(self):
        result = MemoryWiki({('zh', '作品'): '== 主題曲 ==\n*曲名\n**歌：歌手'}).source().fetch_result(['作品'])
        self.assertEqual(result.reason_code, 'type_unconfirmed')
        self.assertEqual(result.diagnostics['untyped_themes'][0]['song_title'], '曲名')

    def test_background_music_is_not_offered_as_unknown_song(self):
        text = '== 音楽 ==\n{|\n|+ サウンドトラック\n! 使用話 !! 曲名 !! 歌手\n|-\n|1 || 配乐 || 作者\n|}\n'
        self.assertFalse(parse_scoped(text, '作品 (アニメ)', ['作品'], 'ja')[0])


class TestInsertMigrationAndPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'state.db')

    def test_old_constraints_migrate_without_cascading_or_losing_objects(self):
        old = SCHEMA.replace("'OP','ED','IN'", "'OP','ED'")
        with patch('animusic.store.SCHEMA', old), patch.object(Store, '_migrate_theme_kinds', return_value=None):
            store = Store(self.path)
            sid = store.get_season_id(2026, 4)
            aid = store.upsert_anime(sid, Anime(season_key='2026Q4', title_jp='作品', bgm_id=7))
            tid = store.upsert_theme(aid, Theme(anime_id=aid, kind=ThemeKind.OP, song_title='旧曲', artists=['歌手'], source='wikipedia'))
            store.conn.execute("CREATE INDEX custom_theme_title ON theme(song_title)")
            store.conn.execute("CREATE TRIGGER custom_theme_note AFTER UPDATE OF note ON theme BEGIN SELECT 1; END")
            store.conn.execute("INSERT INTO decision(theme_id,status,note) VALUES(?, 'skipped','人工排除')", (tid,))
            store.upsert_candidate(tid, Candidate(theme_id=tid, netease_song_id='123', name='候选'))
            store.mark_pushed('仅测试台账', '123', tid)
            store.conn.execute("INSERT INTO push_selection(season_id,netease_song_id,theme_id,target_playlist) VALUES(?,'123',?,'仅离线选择')", (sid, tid))
            store.conn.execute('UPDATE theme SET included=0 WHERE id=?', (tid,))
            store.conn.commit()
            before = dict(store.conn.execute('SELECT * FROM theme WHERE id=?', (tid,)).fetchone())
            linked = {table: [tuple(r) for r in store.conn.execute('SELECT * FROM ' + table)]
                      for table in ('candidate', 'decision', 'pushed', 'push_selection')}
            store.close()
        for _ in range(2):
            store = Store(self.path)
            self.assertEqual(dict(store.conn.execute('SELECT * FROM theme WHERE id=?', (tid,)).fetchone()), before)
            self.assertEqual(store.conn.execute('SELECT note FROM decision WHERE theme_id=?', (tid,)).fetchone()[0], '人工排除')
            self.assertEqual(len(store.conn.execute("SELECT * FROM sqlite_master WHERE name IN ('custom_theme_title','custom_theme_note')").fetchall()), 2)
            self.assertFalse(store.conn.execute('PRAGMA foreign_key_check').fetchall())
            self.assertEqual(store.conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            for table, expected in linked.items():
                self.assertEqual([tuple(r) for r in store.conn.execute('SELECT * FROM ' + table)], expected)
            new = store.upsert_theme(aid, Theme(anime_id=aid, kind=ThemeKind.IN, song_title='插入曲', artists=[], source='wikipedia'))
            item = dict(source_language='ja', article_url='https://ja.wikipedia.org/wiki/test', song_title='未知曲', artists=[])
            store.confirm_wiki_type(aid, item, 'IN')
            self.assertEqual(store.wiki_type_choice(aid, item), 'IN')
            self.assertEqual(store.conn.execute('SELECT kind FROM theme WHERE id=?', (new,)).fetchone()[0], 'IN')
            store.close()

    def test_fallback_insert_save_edit_exclude_export_and_retry(self):
        store = Store(self.path)
        self.addCleanup(store.close)
        season = Season.parse('2026Q4'); sid = store.get_season_id(2026, 4)
        aid = store.upsert_anime(sid, Anime(season_key=season.key, title_jp='作品 第3期', bgm_id=7))
        memory = MemoryWiki({('ja', '作品'): seasons(), ('zh', '作品'): '== 插曲 ==\n*插入曲原名\n**歌：歌手'})
        orch = Orchestrator.__new__(Orchestrator)
        orch.store, orch.wikipedia = store, memory.source()
        orch.progress, orch.should_stop = lambda _: None, None
        orch.refresh_themes(season)
        rows = store.theme_list(sid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['kind'], 'IN')
        self.assertIn('全季收录', json.loads(rows[0]['raw'])['scope_review'])
        session = WizardSession(store=store, season=season)
        row = session._theme_rows(sid)[0][0]
        row['included'] = False; row['theme_included'] = False
        session.save_themes([row], confirm=False)
        orch.refresh_themes(season)
        self.assertEqual(store.theme_list(sid, only_included=False)[0]['included'], 0)
        store.conn.execute('UPDATE theme SET included=1 WHERE id=?', (rows[0]['id'],)); store.conn.commit()
        paths = orch.export(season, os.path.join(self.tmp.name, 'report'))
        with open(paths['csv'], encoding='utf-8-sig', newline='') as stream:
            exported = list(csv.DictReader(stream))
        self.assertEqual(exported[0]['kind'], 'IN')
        self.assertIn('全季收录', exported[0]['scope_review'])
        self.assertEqual(exported[0]['scope_mode'], 'all_seasons_fallback')

    def test_migration_failure_rolls_back_both_tables(self):
        old = SCHEMA.replace("'OP','ED','IN'", "'OP','ED'")
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executescript(old)
            conn.execute('PRAGMA foreign_keys=OFF')
            conn.execute("INSERT INTO theme(anime_id,kind,song_title,artists,source) VALUES(999,'OP','保留曲','[]','manual')")
            conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            Store(self.path)
        with closing(sqlite3.connect(self.path)) as conn:
            for table in ('theme', 'wiki_type_choice'):
                sql = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (table,)).fetchone()[0]
                self.assertNotIn("'IN'", sql)
            self.assertEqual(conn.execute('SELECT song_title FROM theme').fetchone()[0], '保留曲')
            self.assertFalse(conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '%kind_migration'").fetchall())

    def test_unknown_type_confirmed_as_insert_preserves_fallback_metadata(self):
        store = Store(self.path)
        self.addCleanup(store.close)
        season = Season.parse('2026Q4'); sid = store.get_season_id(2026, 4)
        aid = store.upsert_anime(sid, Anime(season_key=season.key, title_jp='作品 第3期', bgm_id=7))
        memory = MemoryWiki({('ja', '作品'): seasons(), ('zh', '作品'): '== 主題曲 ==\n*未知类型原文\n**歌：歌手'})
        orch = Orchestrator.__new__(Orchestrator)
        orch.store, orch.wikipedia = store, memory.source()
        orch.progress, orch.should_stop = lambda _: None, None
        orch.refresh_themes(season)
        state = store.theme_fetch_rows(sid)[0]
        self.assertEqual(state['reason_code'], 'type_unconfirmed')
        item = state['diagnostics']['untyped_themes'][0]
        WizardSession(store=store, season=season).confirm_wiki_type(aid, item, 'IN')
        row = store.theme_list(sid)[0]
        self.assertEqual(row['kind'], 'IN')
        self.assertEqual(json.loads(row['raw'])['scope_mode'], 'all_seasons_fallback')
        self.assertIn('全季收录', store.theme_fetch_rows(sid)[0]['message'])

    def test_new_precise_query_clears_old_review_without_changing_identity(self):
        store = Store(self.path)
        self.addCleanup(store.close)
        sid = store.get_season_id(2026, 4)
        aid = store.upsert_anime(sid, Anime(season_key='2026Q4', title_jp='作品', bgm_id=7))
        song = Theme(anime_id=aid, kind=ThemeKind.IN, song_title='歌', artists=[], source='wikipedia',
                     raw=dict(source_language='ja', scope_review='需审核', scope_mode='all_seasons_fallback'))
        tid = store.upsert_theme(aid, song)
        song.raw = dict(source_language='ja', scope_review='', scope_mode='season', season_keys=['3'])
        self.assertEqual(store.upsert_theme(aid, song), tid)
        raw = json.loads(store.theme_list(sid)[0]['raw'])
        self.assertEqual(raw['scope_review'], '')
        self.assertEqual(raw['scope_mode'], 'season')


try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox
    from animusic.gui.pages import ThemePage
    HAS_QT = True
except ImportError:
    HAS_QT = False


@unittest.skipUnless(HAS_QT, '需要PySide6')
class TestInsertGui(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_insert_label_edit_filter_statistics_and_review(self):
        # 只构造页面并断言，不进入事件循环，也不触发网络任务。
        row = dict(included=True, anime='作品', anime_id=7, kind='IN', song_title='歌',
                   artists='艺人', seq=1, scope_review='季度归属不确定，待人工审核')
        page = ThemePage([row], [])
        self.addCleanup(page.close)
        self.assertEqual(page.model.data(page.model.index(0, 2), Qt.DisplayRole), '插入曲')
        self.assertEqual(page.model.data(page.model.index(0, 2), Qt.EditRole), 'IN')
        page.model.setData(page.model.index(0, 2), '插入曲', Qt.EditRole)
        self.assertEqual(page.all_rows()[0]['kind'], 'IN')
        page.filter_combo.setCurrentText('仅插入曲')
        self.assertEqual(len(page.visible_rows()), 1)
        self.assertIn('插入曲 1', page.stat.text())
        page.filter_combo.setCurrentText('范围待审核')
        self.assertEqual(len(page.visible_rows()), 1)

    def test_manual_type_confirmation_emits_insert_code_without_modal(self):
        page = ThemePage([], [])
        self.addCleanup(page.close)
        page.selected_anime_id = 7
        item = dict(song_title='歌', artists=[])
        page.untyped_combo.addItem('歌', item)
        result = []
        page.confirm_type_requested.connect(lambda aid, value, kind: result.append((aid, value, kind)))
        self.assertEqual(page.type_combo.currentData(), '')
        page.type_combo.setCurrentIndex(page.type_combo.findData('IN'))
        with patch.object(QMessageBox, 'question') as question, patch.object(QMessageBox, 'information') as info:
            page._confirm_type()
            question.assert_not_called(); info.assert_not_called()
        self.assertEqual(result, [(7, item, 'IN')])
