"""维基查询范围与中日文主题歌解析；不从模糊曲名或部数推断季数。"""
from __future__ import annotations

import re
from urllib.parse import quote, unquote

from .wikipedia import ThemeFetchResult, clean_wikitext, canon_title, title_matches, title_variants

from .wiki_songs import (MAX_SECTION, HEADING, THEME, SEASON, ANIMATION, Section, sections,
                         season_key, scoped_theme_sections, parse_chinese, parse_scoped, scope_songs)

MAX_CANDIDATES = 36
MAX_SEARCHES = 4
MAX_RELATED = 2
PART = re.compile(r'(?i)Part\s*(\d+)')


def names_for_language(names: list[str], language: str) -> list[str]:
    unique = list(dict.fromkeys(n.strip() for n in names if n and n.strip()))[:3]
    # 编排层优先传日文名；中文站先查没有假名的中文名，日文原名仍保留作身份依据。
    return sorted(unique, key=lambda n: bool(re.search(r'[ぁ-ヿ]', n)) != (language == 'ja'))


def query_candidates(names: list[str], language: str) -> list[tuple[str, str]]:
    suffixes = (' (アニメ)', ' (テレビアニメ)', '') if language == 'ja' else ('', ' (動畫)', ' (动画)')
    out = []
    for name in names:
        for base in title_variants(name):
            for suffix in suffixes:
                item = (base + suffix, name)
                if not any(t == item[0] for t, _ in out):
                    out.append(item)
    return out[:MAX_CANDIDATES]


def _same_work(name: str, title: str) -> bool:
    p1, p2 = PART.search(name), PART.search(title)
    if p1 and p2 and p1[1] != p2[1]:
        return False
    return any(title_matches(base, title) for base in title_variants(name))


def related_animation_pages(text: str) -> list[str]:
    targets = []
    for sec in sections(text):
        if not ANIMATION.search(sec.title):
            continue
        body = text[sec.body:min(sec.end, sec.body + 16000)]
        # 只跟随动画章节的主条目/详细条目指示，不爬正文中的普通链接。
        for match in re.finditer(r'\{\{\s*(?:Main|Main2|See also|詳細|主条目|主條目)\s*\|([^{}]+)\}\}', body, re.I):
            for arg in match[1].split('|'):
                arg = re.sub(r'^\[\[|\]\]$', '', arg.strip())
                arg = unquote(arg).replace('_', ' ')
                if not arg or '=' in arg or ':' in arg or arg.startswith(('/', '#')) or re.search(r'について|参照|参见|詳|详情', arg):
                    continue
                target = arg.split('#', 1)[0]
                if target and target not in targets:
                    targets.append(target)
    return targets[:MAX_RELATED]




def work_aliases(text: str):
    values = re.findall(r'(?mi)^\|\s*(?:日文[名名稱称]+|japanese|タイトル|標題|标题|title)\s*=\s*([^\n]+)', text)
    return [clean_wikitext(v) for v in values if clean_wikitext(v)]


def reference_work(text: str, names: list[str], canonical: str):
    bases = [canon_title(v) for name in names for v in title_variants(name)]
    if canon_title(canonical) in bases:
        return ''
    # 重定向到系列总页时，从页面明确的链接／标题关系恢复具体作品身份。
    candidates = [*work_aliases(text), *re.findall(r'\[\[([^\]|#]+)(?:[^\]]*)\]\]', text)]
    for candidate in sorted(set(candidates), key=lambda s: (-len(s), s)):
        key = canon_title(candidate)
        if len(key) >= 4 and key != canon_title(canonical) and any(key in base for base in bases):
            if any(season_key(h.title) and key in canon_title(h.title) for h in sections(text)):
                return candidate
    seasons = {season_key(h.title) for h in sections(text) if season_key(h.title)}
    canonical_key = canon_title(canonical)
    if len(seasons) > 1 and any(canonical_key and canonical_key in base and canonical_key != base for base in bases):
        # 已知具体作品重定向到其系列总页，即使尚无映射也不能冒充“无季数普通作品”。
        return title_variants(names[0])[-1]
    return ''


def song_result(themes, url, diag, reason='', message=''):
    typed, unknown = [], []
    seen = set()
    for song in themes:
        key = (song.kind, song.song_title, tuple(song.artists))
        if key in seen:
            continue
        seen.add(key)
        song.source_language = diag['source_language']
        if song.kind is None:
            unknown.append(dict(song_title=song.song_title, artists=song.artists, raw=song.raw,
                source_language=song.source_language, article_url=url, section=song.section,
                scope_review=song.scope_review, season_keys=list(song.season_keys),
                scope_mode=diag.get('scope_mode', ''), target_seasons=diag.get('target_seasons', [])))
        else:
            typed.append(song)
    diag['untyped_themes'] = unknown
    diag['sections'] = list(dict.fromkeys(s.section for s in themes))
    if not reason and unknown:
        reason = 'type_unconfirmed'
        message = (message + '；' if message else '') + '歌曲类型待确认（OP／ED／插入曲）'
    return ThemeFetchResult(themes=typed, article_url=url, reason_code=reason, message=message, diagnostics=diag)


def fallback_result(result):
    if not result.all_season_themes:
        return None
    from dataclasses import replace
    songs = [replace(s, scope_review='未能定位目标季，已全季收录，待人工审核') for s in result.all_season_themes]
    diag = dict(result.diagnostics, scope_mode='all_seasons_fallback', scope_resolved=False, review_required=True)
    return song_result(songs, result.article_url, diag, message='未能定位目标季，已全季收录，待人工审核')


def fetch_scoped(source, names: list[str]) -> ThemeFetchResult:
    names = names_for_language(names, source.language)
    trace = []
    visited = set()
    best = None

    def inspect(page: str, text: str, how: str, original: str, depth: int = 0, work: str = ''):
        nonlocal best
        canonical = source._page_titles.get(page, page)
        if canonical in visited:
            return None
        visited.add(canonical)
        source._inspected_pages[page] = text
        url = f'https://{source.host}/wiki/{quote(canonical.replace(" ", "_"))}'
        trace.append({'candidate': page, 'canonical': canonical, 'query_name': original, 'how': how})
        work = work or reference_work(text, names, canonical)
        source._query_stage = '解析动画歌曲及季度范围'
        chosen, all_themes, scope = scope_songs(text, canonical, names, source.language, work)
        diag = dict(scope, source_language=source.language, query_stage=source._query_stage, lookup_trace=list(trace))
        if all_themes:
            if not scope['scope_resolved']:
                result = song_result(chosen, url, diag, 'scope_ambiguous', '词条已找到，未能定位目标季度，待中文补充或全季审核')
                result.all_season_themes = all_themes
                return result
            return song_result(chosen, url, diag, message='部分歌曲季度归属不确定，已保留供人工审核' if scope['review_required'] else '')
        no_section = not any(THEME.search(s.title) for s in sections(text))
        result = ThemeFetchResult(article_url=url, reason_code='no_theme_section' if no_section else 'no_usable_themes',
            message='词条无动画歌曲栏目' if no_section else '未解析出可用动画歌曲', diagnostics=diag)
        if best is None or not no_section:
            best = result
        if depth < 2:
            for target in related_animation_pages(text):
                if target in visited:
                    trace.append({'candidate': target, 'how': 'related_skipped', 'reason': 'already_visited'})
                    continue
                got = source.wikitext_many([target])
                related = got.get(target)
                if related:
                    # 主条目链接仍可能被误编；目标需有原作对应标题或明确的改编关系。
                    reference = canon_title(work or canonical)
                    mapped = any(season_key(s.title) and reference in canon_title(s.title) for s in sections(related))
                    related_sections = sections(related)
                    for season in related_sections:
                        if not season_key(season.title):
                            continue
                        end = next((s.start for s in related_sections if season.body <= s.start < season.end), season.end)
                        introduction = related[season.body:min(end, season.body + 1000)]
                        if any(re.search(r'改[编編]自|原作\s*[：:=]', line)
                               and reference in canon_title(clean_wikitext(line)) for line in introduction.splitlines()):
                            mapped = True
                    lead = related[:min(related.find('\n==') if '\n==' in related else len(related), 3000)]
                    adapted = any(re.search(r'(?:原作|原著)\s*[=：:]|改編自|改编自', line) and reference in canon_title(clean_wikitext(line))
                                  for line in lead.splitlines())
                    same_title = reference == canon_title(source._page_titles.get(target, target))
                    if not reference or not (mapped or adapted or same_title):
                        trace.append({'candidate': target, 'how': 'related_skipped', 'reason': 'work_identity_unconfirmed'})
                        continue
                    multi_season = len({season_key(s.title) for s in related_sections if season_key(s.title)}) > 1
                    found = inspect(target, related, 'related_animation', original, depth + 1,
                                    (work or canonical) if mapped or multi_season else '')
                    if found:
                        return found
        return None

    candidates = query_candidates(names, source.language)
    for start in range(0, len(candidates), 12):
        chunk = candidates[start:start + 12]
        got = source.wikitext_many([p for p, _ in chunk])
        for page, original in chunk:
            text = got.get(page)
            if text:
                found = inspect(page, text, 'exact' if page == original else 'normalized_candidate', original)
                if found:
                    return found
    # 每个已知名称先获得一次机会，避免首个长名的全半角变体吃掉全部搜索预算。
    searches = list(dict.fromkeys([*names, *(v for n in names for v in title_variants(n))]))[:MAX_SEARCHES]
    for name in searches:
        source.stats['search_used'] += 1
        hits = source.search(name + (' アニメ' if source.language == 'ja' else ' 动画'), 5)[:3]
        keep = hits if source.language == 'zh' else [hit for hit in hits if _same_work(name, hit)]
        source.stats['rejected_title'] += len(hits) - len(keep)
        got = source.wikitext_many(keep)
        for hit in keep:
            if got.get(hit):
                variants = [hit, *source._page_variants.get(hit, [])]
                original_fields = work_aliases(got[hit])
                identity = any(_same_work(n, v) for n in names for v in variants)
                identity = identity or any(_same_work(n, v) for v in original_fields for n in names)
                if not identity:
                    source.stats['rejected_title'] += 1
                    trace.append({'candidate': hit, 'how': 'search_rejected', 'reason': 'work_identity_unconfirmed'})
                    continue
                found = inspect(hit, got[hit], 'search', name)
                if found:
                    return found
    if best:
        best.diagnostics['lookup_trace'] = list(trace)
        return best
    return ThemeFetchResult(reason_code='article_not_found', message='未找到匹配词条',
        diagnostics={'source_language': source.language, 'query_stage': '查找词条', 'lookup_trace': trace})
