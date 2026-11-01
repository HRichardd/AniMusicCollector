"""维基查询范围与中日文主题歌解析；不从模糊曲名或部数推断季数。"""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import quote, unquote

from .wikipedia import (WikiTheme, ThemeFetchResult, WikiResponseError, ThemeKind,
                        clean_wikitext, canon_title, title_matches, title_variants,
                        parse_themes, _kind_of, _table_cell)

MAX_SECTION = 120000
MAX_CANDIDATES = 36
MAX_SEARCHES = 4
MAX_RELATED = 2
HEADING = re.compile(r'^(={2,6})\s*(.*?)\s*\1\s*$', re.M)
THEME = re.compile(r'主[題题][歌曲]|オープニング|エンディング|テーマソング|片[頭头尾]曲|^音[楽乐]$')
SEASON = re.compile(r'(?i)(\d+)(?:st|nd|rd|th)\s*Season|Season\s*(\d+)|第([\d一二三四五六七八九十]+)[期季]|シーズン\s*(\d+)')
PART = re.compile(r'(?i)Part\s*(\d+)')
ANIMATION = re.compile(r'アニメ|動[畫画]|动画')


@dataclass
class Section:
    title: str
    level: int
    start: int
    body: int
    end: int


def sections(text: str) -> list[Section]:
    matches = list(HEADING.finditer(text))
    out = []
    for i, match in enumerate(matches):
        end = next((m.start() for m in matches[i + 1:] if len(m[1]) <= len(match[1])), len(text))
        out.append(Section(clean_wikitext(match[2]), len(match[1]), match.start(), match.end(), end))
    return out


def season_key(title: str) -> str:
    match = SEASON.search(title)
    if not match:
        return ''
    value = next(v for v in match.groups() if v)
    return str({'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6,
                '七': 7, '八': 8, '九': 9, '十': 10}.get(value, value))


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
    expected, actual = season_key(name), season_key(title)
    if expected and actual and expected != actual:
        return False
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


def scoped_theme_sections(text: str, names: list[str], work_title: str = '', page_title: str = '') -> list[tuple[str, str]]:
    headings = sections(text)
    roots = [s for s in headings if THEME.search(s.title)]
    roots = [s for s in roots if not any(p.start < s.start < p.end for p in roots)]
    wanted = {season_key(n) for n in names if season_key(n)}
    keys = [canon_title(work_title)] if work_title else [canon_title(v) for n in names for v in title_variants(n)]
    mapped = set()
    for heading in headings:
        if not season_key(heading.title):
            continue
        lead_end = next((s.start for s in headings if heading.body <= s.start < heading.end), heading.end)
        lead = text[heading.body:min(lead_end, heading.body + 1000)]
        title_match = any(k and k in canon_title(heading.title) for k in keys)
        adaptation = work_title and any(re.search(r'改[编編]自|原作\s*[：:=]', line)
            and canon_title(work_title) in canon_title(clean_wikitext(line)) for line in lead.splitlines())
        if title_match or adaptation:
            mapped.add(season_key(heading.title))
    if mapped:
        wanted = wanted & mapped if wanted else mapped
    parent_seasons = {season_key(p.title) for root in roots for p in headings
                      if p.start < root.start < p.end and season_key(p.title)}
    if len(parent_seasons) > 1 and len(wanted) != 1:
        raise WikiResponseError('词条已找到，目标季度范围不明确', 'scope_ambiguous')
    selected = []
    unspecified = []
    for root in roots:
        if root.end - root.body > MAX_SECTION:
            raise WikiResponseError('主题歌章节超过安全长度，需人工核对', 'section_too_large')
        root_season = season_key(root.title)
        if root_season and wanted:
            if root_season in wanted:
                selected.append((root.title, text[root.body:root.end]))
            continue
        groups = [s for s in headings if root.body <= s.start < root.end and season_key(s.title)]
        # 同一季下面可能还有子栏目；只把最外层季标题当作互斥分组。
        groups = [s for s in groups if not any(p.start < s.start < p.end for p in groups)]
        if not groups:
            # 多季以「主题歌」散布在各季父章节时，也必须先选择目标父章节。
            parents = [s for s in headings if s.start < root.start < s.end and season_key(s.title)]
            if parents:
                parent = max(parents, key=lambda s: s.level)
                if wanted and season_key(parent.title) not in wanted:
                    continue
                if not wanted:
                    if not any(k and k in canon_title(parent.title) for k in keys):
                        raise WikiResponseError('词条已找到，目标季度范围不明确', 'scope_ambiguous')
                selected.append((parent.title + ' / ' + root.title, text[root.body:root.end]))
                continue
            body = text[root.body:root.end]
            labels = list(re.finditer(r'(?m)^;\s*(第[\d一二三四五六七八九十]+[期季])\s*$', body))
            if labels:
                if len(wanted) != 1:
                    raise WikiResponseError('词条已找到，目标季度范围不明确', 'scope_ambiguous')
                for i, label in enumerate(labels):
                    if season_key(label[1]) in wanted:
                        end = labels[i + 1].start() if i + 1 < len(labels) else len(body)
                        selected.append((root.title + ' / ' + label[1], body[label.end():end]))
                continue
            # 季数也会写在歌曲说明中，不能只认章节标题；不收未标季数的邻近曲目。
            blocks = list(re.finditer(r'(?ms)^;[^\n]*\n:.*?(?=^;|\Z)', body))
            tagged = [(m.group(), season_key(clean_wikitext(m.group()))) for m in blocks]
            if any(key for _, key in tagged):
                if len(wanted) != 1:
                    raise WikiResponseError('词条已找到，目标季度范围不明确', 'scope_ambiguous')
                matching = [block for block, key in tagged if key in wanted]
                if matching:
                    selected.append((root.title + ' / 第' + next(iter(wanted)) + '期（曲目说明）', '\n'.join(matching)))
                continue
            unspecified.append((root.title, body))
            continue
        matches = [g for g in groups if season_key(g.title) in wanted]
        if not wanted or len(matches) > 1:
            raise WikiResponseError('词条已找到，目标季度范围不明确', 'scope_ambiguous')
        if not matches:
            continue
        group = matches[0]
        selected.append((root.title + ' / ' + group.title, text[group.body:group.end]))
    if not selected and unspecified and len(roots) == 1 and (not wanted or season_key(page_title) in wanted):
        selected = unspecified
    if not selected and roots:
        raise WikiResponseError('词条已找到，未能定位目标季度主题歌', 'scope_ambiguous')
    return selected


def _kind(text: str):
    if re.search(r'片[頭头](?:主[題题])?曲|開場曲|开场曲|\bOP\b', text, re.I):
        return ThemeKind.OP
    if re.search(r'片尾(?:主[題题])?曲|結尾曲|结尾曲|\bED\b', text, re.I):
        return ThemeKind.ED
    return _kind_of(text)


def _artist(text: str) -> str:
    for line in text.splitlines():
        match = re.search(r'(?:演唱|主唱|歌手|歌唱|歌)\s*[：:]\s*(.+)', clean_wikitext(line))
        if match:
            return match[1].split('，')[0].split('；')[0].strip()
    return ''


def parse_chinese(text: str, page: str, context: str = '') -> list[WikiTheme]:
    out = []
    normalized = []
    current_kind = _kind(context)
    for line in text.splitlines():
        if re.fullmatch(r';\s*片[頭头尾](?:主[題题])?曲\s*', line):
            current_kind = _kind(line)
            continue
        if re.match(r'^:;+\s*', line):
            line = re.sub(r'^:;+\s*', '; ' + (str(current_kind) + ' ' if current_kind else ''), line)
        normalized.append(line)
    text = '\n'.join(normalized)
    # 定义列表和星号列表均按同级条目分块，演唱信息可位于下一层。
    blocks = re.findall(r'(?ms)^(?:;|\*(?!\*))\s*(.*?)(?=^(?:;|\*(?!\*))|\Z)', text)
    for block in blocks:
        if re.search(r'插[入曲]|劇中歌|剧中歌', block[:60]):
            continue
        first = block.splitlines()[0]
        title_match = re.search(r'[「『《"](.+?)[」』》"]', clean_wikitext(first))
        if title_match:
            title = title_match[1]
        else:
            title = clean_wikitext(first).strip('：: ')
            title = re.sub(r'^(?:片[頭头尾]曲|主[題题][歌曲])\s*[：:]?\s*', '', title)
        if not title or len(title) > 180 or re.match(r'^(?:歌|作詞|作词|作曲|編曲|编曲)[：:]', title):
            continue
        out.append(WikiTheme(kind=_kind(first) or _kind(context), song_title=title,
                             artists=[_artist(block)] if _artist(block) else [],
                             page=page, raw=block[:1000], fmt='list'))
    # 表头决定列含义，支持一行多列和分行单元格；不猜测使用话数列是曲名。
    for table in re.findall(r'(?s)\{\|.*?\|\}', text):
        caption = re.search(r'(?m)^\|\+\s*(.*)', table)
        kind = _kind(caption[1]) if caption else _kind(context)
        headers = []
        rows = []
        cells = []
        for line in table.splitlines()[1:]:
            line = line.strip()
            if line.startswith('|-') or line == '|}':
                if cells:
                    rows.append(cells)
                    cells = []
            elif line.startswith('!'):
                headers.extend(_table_cell(c) for c in re.split(r'!!', line[1:]))
            elif line.startswith('|') and not line.startswith('|+'):
                cells.extend(_table_cell(c) for c in re.split(r'\|\|', line[1:]))
        title_col = next((i for i, h in enumerate(headers) if re.search(r'曲名|歌名|歌曲', h)), None)
        artist_col = next((i for i, h in enumerate(headers) if re.search(r'歌手|演唱|主唱', h)), None)
        type_col = next((i for i, h in enumerate(headers) if re.search(r'類型|类型|類別|类别', h)), None)
        if title_col is None:
            continue
        for row in rows:
            if title_col >= len(row):
                continue
            title = row[title_col].strip('「」『』《》" ')
            artist = row[artist_col] if artist_col is not None and artist_col < len(row) else ''
            row_kind = _kind(row[type_col]) if type_col is not None and type_col < len(row) else kind
            if title:
                out.append(WikiTheme(kind=row_kind, song_title=title, artists=[artist] if artist else [],
                                     page=page, raw=' | '.join(row), fmt='table'))
    return out


def parse_scoped(text: str, page: str, names: list[str], language: str, work_title: str = ''):
    out = []
    locations = []
    for location, body in scoped_theme_sections(text, names, work_title, page):
        # 拆开嵌套OP/ED标题，让中文列表和无caption的表格继承明确类型。
        headings = sections(body)
        typed = [s for s in headings if _kind(s.title)]
        if typed:
            parts = [(location, body[:typed[0].start])]
            parts.extend((location + ' / ' + s.title, body[s.body:s.end]) for s in typed)
        else:
            parts = [(location, body)]
        for section, segment in parts:
            if language == 'ja' and _kind(section):
                label = 'オープニングテーマ' if _kind(section) == ThemeKind.OP else 'エンディングテーマ'
                segment = re.sub(r'(?m)^:\s*(.+)$', lambda m: m[0] if _kind(m[1]) else
                                 ': ' + m[1] + 'による' + label, segment)
                if '{|' in segment and '|+' not in segment:
                    segment = re.sub(r'(?m)^(\{\|[^\n]*)(\n)', r'\1\2|+ ' + label + '\n', segment)
            themes = parse_chinese(segment, page, section) if language == 'zh' else parse_themes(segment, page)
            for theme in themes:
                if theme.is_interlude:
                    continue
                theme.source_language = language
                theme.section = section
                out.append(theme)
                if section not in locations:
                    locations.append(section)
    unique = {}
    for theme in out:
        unique.setdefault((theme.kind, theme.song_title, tuple(theme.artists)), theme)
    return list(unique.values()), locations


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
        source._query_stage = '选择主题歌章节'
        try:
            if depth == 0 and season_key(original) and season_key(canonical) and season_key(original) != season_key(canonical):
                raise WikiResponseError('词条指向其它季度，未导入曲目', 'scope_ambiguous')
            themes, locations = parse_scoped(text, canonical, names, source.language, work)
        except WikiResponseError as exc:
            if exc.error_code != 'scope_ambiguous':
                raise
            return ThemeFetchResult(article_url=url, reason_code='scope_ambiguous', message=str(exc),
                diagnostics={'source_language': source.language, 'query_stage': source._query_stage,
                             'error_code': exc.error_code, 'lookup_trace': list(trace)})
        trace.append({'candidate': page, 'canonical': canonical, 'query_name': original, 'how': how})
        diag = {'source_language': source.language, 'query_stage': '解析主题歌',
                'sections': locations, 'lookup_trace': list(trace)}
        if themes:
            typed = [t for t in themes if t.kind is not None]
            unknown = [dict(song_title=t.song_title, artists=t.artists, raw=t.raw,
                            source_language=source.language, article_url=url, section=t.section)
                       for t in themes if t.kind is None]
            diag['untyped_themes'] = unknown
            return ThemeFetchResult(themes=typed, article_url=url,
                reason_code='type_unconfirmed' if unknown else '',
                message='已找到主题曲，OP/ED 类型待确认' if unknown else '', diagnostics=diag)
        no_section = not any(THEME.search(s.title) for s in sections(text))
        result = ThemeFetchResult(article_url=url, reason_code='no_theme_section' if no_section else 'no_usable_themes',
            message='词条无主题歌栏目' if no_section else '未解析出可用 OP/ED', diagnostics=diag)
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
                    found = inspect(target, related, 'related_animation', original, depth + 1, work or canonical)
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
                original_fields = re.findall(r'(?m)^\|\s*日文[名名稱称]+\s*=\s*([^\n]+)', got[hit])
                identity = any(_same_work(name, v) for v in variants)
                identity = identity or any(canon_title(clean_wikitext(v)) == canon_title(n)
                                           for v in original_fields for n in names)
                if not identity:
                    source.stats['rejected_title'] += 1
                    continue
                found = inspect(hit, got[hit], 'search', name)
                if found:
                    return found
    return best or ThemeFetchResult(reason_code='article_not_found', message='未找到匹配词条',
        diagnostics={'source_language': source.language, 'query_stage': '查找词条', 'lookup_trace': trace})
