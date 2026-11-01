"""动画歌曲结构解析：保留季度证据，范围不确定时交给人工审核。"""
from __future__ import annotations

from dataclasses import dataclass
import re

from .wikipedia import (WikiTheme, ThemeKind, WikiResponseError, clean_wikitext,
                        canon_title, _artist_from_desc, _seq_of)

MAX_SECTION = 120000
HEADING = re.compile(r'^(={2,6})\s*(.*?)\s*\1\s*$', re.M)
THEME = re.compile(r'主[題题][歌曲]|オープニング|エンディング|テーマソング|片[頭头尾](?:主[題题])?曲|挿入歌|插(?:入曲|曲)|[劇剧]中歌|^音[楽乐]$')
SEASON = re.compile(r'(?i)(\d+)(?:st|nd|rd|th)\s*Season|Season\s*(\d+)|第([\d一二三四五六七八九十]+)[期季](?!度)|シーズン\s*(\d+)')
ANIMATION = re.compile(r'アニメ|動[畫画]|动画')
LIVE = re.compile(r'ドラマ|実写|真人|電視劇|电视剧|舞台|テレビ映画')
NON_SONG = re.compile(r'サウンドトラック|劇伴|配[楽乐]|\bBGM\b|キャラクターソング|角色歌', re.I)


@dataclass
class Section:
    title: str
    level: int
    start: int
    body: int
    end: int


def sections(text: str) -> list[Section]:
    matches = list(HEADING.finditer(text))
    return [Section(clean_wikitext(m[2]), len(m[1]), m.start(), m.end(),
                    next((n.start() for n in matches[i + 1:] if len(n[1]) <= len(m[1])), len(text)))
            for i, m in enumerate(matches)]


def season_keys(text: str) -> tuple[str, ...]:
    # 引用中的新闻标题不能给歌曲赋季；行内注释则是维基编辑明确提供的行级证据。
    text = re.sub(r'<ref\b[^>]*(?:/>|>.*?</ref>)', '', text, flags=re.S | re.I)
    values = []
    for m in SEASON.finditer(text):
        value = next(v for v in m.groups() if v)
        if not value.isdigit():
            digits = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6,
                      '七': 7, '八': 8, '九': 9, '十': 10}
            if '十' in value and len(value) > 1:
                a, b = value.split('十', 1)
                value = str(digits.get(a, 1) * 10 + digits.get(b, 0))
            else:
                value = str(digits.get(value, value))
        if value not in values:
            values.append(value)
    return tuple(values)


def season_key(text: str) -> str:
    return next(iter(season_keys(text)), '')


def kind_of(text: str):
    # 明确的单曲用途优先于祖先栏目；不得让“插入曲”继承前面的ED。
    if re.search(r'挿入歌|插入曲|插曲|[劇剧]中歌|\bIN\b', text, re.I):
        return ThemeKind.IN
    if re.search(r'オープニング|片[頭头](?:主[題题])?曲|開場曲|开场曲|\bOP(?:テーマ)?\b', text, re.I):
        return ThemeKind.OP
    if re.search(r'エンディング|片尾(?:主[題题])?曲|結尾曲|结尾曲|\bED(?:テーマ)?\b', text, re.I):
        return ThemeKind.ED
    return None


def _cell_parts(line: str, delimiter: str) -> list[str]:
    # 模板参数和链接显示名里的竖线不是列分隔符。
    out, start, braces, links, i = [], 0, 0, 0, 0
    while i < len(line):
        pair = line[i:i + 2]
        if pair == '{{': braces += 1; i += 2; continue
        if pair == '}}': braces = max(0, braces - 1); i += 2; continue
        if pair == '[[': links += 1; i += 2; continue
        if pair == ']]': links = max(0, links - 1); i += 2; continue
        if not braces and not links and line.startswith(delimiter, i):
            out.append(line[start:i]); i += len(delimiter); start = i; continue
        i += 1
    return [*out, line[start:]]


def _cell(raw: str):
    m = re.match(r'^\s*((?:(?:[\w-]+\s*=\s*(?:"[^"]*"|\x27[^\x27]*\x27|[^\s|]+))\s*)+)\|', raw)
    attrs = m[1] if m else ''
    value = raw[m.end():] if m else raw
    def span(name):
        match = re.search(name + r'\s*=\s*["\x27]?(\d+)', attrs)
        return min(200, max(1, int(match[1]))) if match else 1
    return clean_wikitext(value), span('rowspan'), span('colspan'), raw


def table_rows(table: str):
    """展开合并单元格，返回表头及各逻辑行的显示值／原始证据。"""
    physical, cells = [], []
    for line in table.splitlines()[1:]:
        s = line.strip()
        if s.startswith('|-') or s == '|}':
            if cells: physical.append(cells); cells = []
        elif s.startswith(('!', '|')) and not s.startswith(('|+', '{|')):
            cells.extend(_cell(c) for c in _cell_parts(s[1:], '!!' if s.startswith('!') else '||'))
        elif cells and s:
            value, rs, cs, raw = cells[-1]
            cells[-1] = (value + ' ' + clean_wikitext(s), rs, cs, raw + '\n' + s)
    if cells: physical.append(cells)
    carry, headers, rows = {}, [], []
    for physical_row in physical:
        row = {col: (value, raw) for col, (value, raw, _) in carry.items()}
        following = {col: (v, raw, left - 1) for col, (v, raw, left) in carry.items() if left > 1}
        col = 0
        for value, rs, cs, raw in physical_row:
            while col in row: col += 1
            for offset in range(cs):
                row[col + offset] = (value, raw)
                if rs > 1: following[col + offset] = (value, raw, rs - 1)
            col += cs
        carry = following
        logical = [row.get(i, ('', '')) for i in range(max(row, default=-1) + 1)]
        values = [v for v, _ in logical]
        if any(re.fullmatch(r'曲名|歌名|歌曲', v) for v in values) and any(re.search(r'歌手|演唱|主唱|使用話|使用话', v) for v in values):
            headers = values
            continue
        if headers:
            rows.append(logical)
    return headers, rows


def _artist(block: str, language: str) -> str:
    text = clean_wikitext(block.splitlines()[0]).lstrip(':* ') if block.splitlines() else ''
    if language == 'ja':
        return _artist_from_desc(text)
    m = re.search(r'(?:演唱|主唱|歌手|歌唱|歌)\s*[：:]\s*(.+)', text)
    return re.split(r'[，；]', m[1])[0].strip() if m else ''


def _theme(kind, title, artist, raw, page, location, keys, fmt):
    title = title.strip('「」『』《》" ')
    if not title or len(title) > 180:
        return None
    return WikiTheme(kind=kind, song_title=title, artists=[artist] if artist else [],
                     raw=raw[:4000], page=page, section=location, fmt=fmt,
                     sequence=_seq_of(clean_wikitext(raw)) if fmt == 'list' else None,
                     note=clean_wikitext(raw)[:160], season_keys=tuple(keys))


def _episodes(value: str):
    value = clean_wikitext(value)
    value = re.sub(r'[（(].*?[）)]', '', value)
    out = []
    for part in re.split(r'[・、，,／/]', value):
        match = re.fullmatch(r'\s*第?(\d+)\s*(?:([-—－–~〜])\s*第?(\d+)?)?\s*[話话集]?\s*', part)
        if not match:
            return ()
        out.append((int(match[1]), int(match[3]) if match[3] else (None if match[2] else int(match[1]))))
    return tuple(out)


def parse_segment(body: str, page: str, location: str, language: str, inherited=()):
    out = []
    current_keys = inherited
    current_kind = kind_of(location.split(' / ')[-1])
    kind_stack = {}
    lines = body.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]; stripped = line.strip()
        label = re.fullmatch(r"(?:;+\s*|'''\s*)(第[\d一二三四五六七八九十]+[期季])(?:\s*''')?\s*", stripped)
        if label:
            current_keys = season_keys(label[1]); kind_stack.clear(); i += 1; continue
        if stripped.startswith('{|'):
            end = i + 1
            while end < len(lines) and lines[end].strip() != '|}': end += 1
            table = '\n'.join(lines[i:end + 1]); headers, rows = table_rows(table)
            caption = re.search(r'(?m)^\|\+\s*(.*)', table)
            table_kind = kind_of(clean_wikitext(caption[1])) if caption else current_kind
            if caption and table_kind is None and NON_SONG.search(clean_wikitext(caption[1])):
                i = end + 1; continue
            table_keys = season_keys(caption[1]) if caption else ()
            def index(pattern): return next((n for n, v in enumerate(headers) if re.search(pattern, v)), None)
            title_col, artist_col = index(r'曲名|歌名|歌曲'), index(r'歌手|演唱|主唱|アーティスト')
            type_col, season_col, ep_col = index(r'類型|类型|類別|类别'), index(r'季度|季数|季數|期数|期數'), index(r'使用[話话]|話数|话数|集數|集数')
            for row in rows:
                def value(n): return row[n][0] if n is not None and n < len(row) else ''
                def raw_value(n): return row[n][1] if n is not None and n < len(row) else ''
                if title_col is None: continue
                raw = ' | '.join(dict.fromkeys(r for _, r in row))
                # 只读季数／话数单元格及局部行注释，不让曲名里的“第X季”成为季标。
                own = season_keys(raw_value(season_col) + ' ' + raw_value(ep_col))
                keys = tuple(dict.fromkeys([*(table_keys or current_keys), *own]))
                song = _theme(kind_of(value(type_col)) or table_kind, value(title_col), value(artist_col),
                              raw, page, location, keys, 'table')
                if song:
                    song.episode_ranges = _episodes(value(ep_col))
                    song.season_conflict = bool(own and (table_keys or current_keys)
                                               and not set(own) & set(table_keys or current_keys))
                    out.append(song)
            i = end + 1; continue
        entry = re.match(r'^(\*+|:*;)\s*(.*)$', line)
        if not entry or not entry[1]: i += 1; continue
        prefix, head = entry.groups(); cleaned = clean_wikitext(head)
        depth = len(prefix)
        # 说明用的**歌／::歌不应变成独立歌曲。
        if re.match(r'^(?:歌|演唱|主唱|作詞|作词|作曲|編曲|编曲)\s*[：:]', cleaned): i += 1; continue
        label_kind = kind_of(cleaned)
        if label_kind and re.fullmatch(r'(?:片[頭头尾](?:主[題题])?曲|插入曲|插曲|挿入歌|[劇剧]中歌|オープニング(?:テーマ)?|エンディング(?:テーマ)?|OP|ED|IN)', cleaned, re.I):
            kind_stack = {d: k for d, k in kind_stack.items() if d < depth}
            kind_stack[depth] = label_kind; i += 1; continue
        end = i + 1
        while end < len(lines):
            if re.match(r'^(?:\*+|:*;)\s*', lines[end]) and not re.match(r'^\*{2,}(?:歌|演唱|主唱|作)', lines[end]): break
            if lines[end].lstrip().startswith(('{|', "'''", '==')): break
            end += 1
        block = '\n'.join(lines[i:end]); desc = '\n'.join(lines[i + 1:end])
        # 后续段落常比较旧版OP映像／制作，不能改变首行明确的歌曲用途和季数。
        primary = clean_wikitext(desc.splitlines()[0]) if desc.splitlines() else ''
        primary_kind = kind_of(primary.split('。', 1)[0])
        quoted = re.search(r'[「『《"](.+?)[」』》"]', cleaned)
        title = quoted[1] if quoted else re.sub(r'（[^）]*(?:季度|話|话)[^）]*）\s*$', '', cleaned)
        ancestor_kind = next((kind_stack[d] for d in sorted(kind_stack, reverse=True) if d < depth), current_kind)
        if not (primary_kind or label_kind or ancestor_kind) and NON_SONG.search(clean_wikitext(block)):
            i = end; continue
        own = season_keys(primary)
        keys = tuple(dict.fromkeys([*current_keys, *own]))
        song = _theme(primary_kind or label_kind or ancestor_kind, title,
                      _artist(desc, language), block, page, location, keys, 'list')
        if song: out.append(song)
        if song:
            direct = season_keys(primary.split('。', 1)[0])
            song.season_conflict = bool(direct and current_keys and not set(direct) & set(current_keys))
        i = end
    mappings = [(lo, hi, s.season_keys[0]) for s in out if len(s.season_keys) == 1
                for lo, hi in s.episode_ranges]
    for song in out:
        if song.season_keys or not song.episode_ranges:
            continue
        inferred = set()
        covered = True
        for lo, hi in song.episode_ranges:
            keys = {key for start, stop, key in mappings
                    if start <= lo and (stop is None or (hi is not None and hi <= stop))}
            if len(keys) != 1:
                covered = False
                break
            inferred.update(keys)
        if covered and len(inferred) == 1:
            song.season_keys = tuple(inferred)
    return out


def _animation_allowed(path: list[Section], page: str, text: str, headings: list[Section]):
    if LIVE.search(page): return False
    for part in reversed(path):
        if LIVE.search(part.title): return False
        if ANIMATION.search(part.title): return True
    if ANIMATION.search(page): return True
    # 没有混合媒介的普通作品页维持已有行为；混合页的无归属栏目不猜媒介。
    return not any(LIVE.search(h.title) for h in headings)


def all_songs(text: str, page: str, language: str):
    headings = sections(text)
    songs = []
    for i, heading in enumerate(headings):
        path = [h for h in headings if h.start <= heading.start < h.end]
        if not any(THEME.search(h.title) for h in path) or not _animation_allowed(path, page, text, headings): continue
        if NON_SONG.search(heading.title) and not kind_of(heading.title): continue
        end = headings[i + 1].start if i + 1 < len(headings) else len(text)
        body = text[heading.body:end]
        if len(body) > MAX_SECTION: raise WikiResponseError('歌曲章节超过安全长度，需人工核对', 'section_too_large')
        inherited = next((season_keys(h.title) for h in reversed(path) if season_keys(h.title)), season_keys(page))
        location = ' / '.join(h.title for h in path)
        songs.extend(parse_segment(body, page, location, language, inherited))
    return songs


def scope_songs(text: str, page: str, names: list[str], language: str, work_title: str = ''):
    songs = all_songs(text, page, language)
    wanted = set(k for name in names for k in season_keys(name))
    mapped = set()
    if work_title:
        reference = canon_title(work_title)
        headings = sections(text)
        for h in headings:
            if not season_keys(h.title): continue
            end = next((s.start for s in headings if h.body <= s.start < h.end), h.end)
            lead = text[h.body:min(end, h.body + 1500)]
            if reference and (reference in canon_title(h.title) or any(
                    re.search(r'改[编編]自|原作\s*[：:=]', line) and reference in canon_title(clean_wikitext(line)) for line in lead.splitlines())):
                mapped.update(season_keys(h.title))
        wanted = (wanted & mapped) if wanted and mapped else (wanted or mapped)
    required = bool(wanted or work_title)
    matched = [s for s in songs if s.season_keys and set(s.season_keys) & wanted and not s.season_conflict]
    resolved = not required or (len(wanted) == 1 and bool(matched) and (not work_title or bool(mapped)))
    chosen = songs if not required else [s for s in songs if s in matched or not s.season_keys or s.season_conflict]
    for song in chosen:
        if required and (not song.season_keys or song.season_conflict or not resolved):
            song.scope_review = '季度归属不确定，待人工审核'
    diag = {'scope_mode': 'series' if work_title else ('season' if required else 'all_seasons'),
            'target_seasons': sorted(wanted), 'scope_resolved': resolved,
            'review_required': any(s.scope_review for s in chosen)}
    return chosen, songs, diag


def parse_scoped(text: str, page: str, names: list[str], language: str, work_title: str = ''):
    chosen, _, _ = scope_songs(text, page, names, language, work_title)
    unique = {}
    for song in chosen:
        song.source_language = language
        unique.setdefault((song.kind, song.song_title, tuple(song.artists)), song)
    result = list(unique.values())
    return result, list(dict.fromkeys(s.section for s in result))


def parse_chinese(text: str, page: str, context: str = ''):
    return parse_segment(text, page, context, 'zh')


def scoped_theme_sections(text: str, names: list[str], work_title: str = '', page_title: str = ''):
    # 兼容只需段落的调用方；实际筛季在解析行／歌曲后执行，避免丢失表格行注释。
    headings = sections(text)
    roots = [s for s in headings if THEME.search(s.title)]
    roots = [s for s in roots if not any(p.start < s.start < p.end for p in roots)]
    return [(s.title, text[s.body:s.end]) for s in roots
            if _animation_allowed([h for h in headings if h.start <= s.start < h.end], page_title, text, headings)]
