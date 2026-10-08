"""文本归一化与番剧/艺人别名表。

对应 DESIGN.md §5.5。

三层能力：
    1. `normalize_text`  —— 全角/括号/feat. 等统一清洗
    2. `romanize`        —— 汉字/假名 → 罗马音（跨书写系统匹配的关键，§6.1）
    3. `AliasTable`      —— 番剧/艺人别名收敛 + 审核后自学习回写
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field

import yaml

from . import constants

# ---------------------------------------------------------------- 清洗

_BRACKETS = re.compile(r"[「」『』【】\[\]（）()《》〈〉]")
# CJK 与常见标点都算噪声。
# 注意：NFKC 会先把全角 ！？ 折成 ASCII，所以这里必须同时写半角形式，
# 否则 "Shake！" 归一后仍带 "!"（实测被这条绊过）。
_PUNCT = re.compile(r"[。、・，．！？!?…‥〜:：;；\"'“”‘’]")
_FEAT = re.compile(r"\b(feat|ft|featuring)\.?\b.*$", re.I)
_DASHES = re.compile(r"[-–—~〜/／]+")
_WS = re.compile(r"\s+")

# 艺人分隔符：网易云一个字段里可能塞多个艺人
_ARTIST_SEP = re.compile(
    r"\s*(?:/|／|&|＆|、|,|，|×|\bwith\b|\bfeat\.?\b|\bft\.?\b|\bvs\.?\b)\s*", re.I
)


def normalize_text(s: str | None) -> str:
    """用于相似度比较的激进归一。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)      # 全角 → 半角
    s = s.lower()
    s = _BRACKETS.sub(" ", s)
    s = _PUNCT.sub(" ", s)
    s = _FEAT.sub("", s)
    s = _DASHES.sub(" ", s)
    return _WS.sub(" ", s).strip()


def split_artists(s: str | None) -> set[str]:
    if not s:
        return set()
    return {normalize_text(p) for p in _ARTIST_SEP.split(s) if normalize_text(p)}


def expand_artists(names: list[str]) -> set[str]:
    out: set[str] = set()
    for n in names or []:
        out |= split_artists(n)
    return out


# ---------------------------------------------------------------- 书写系统

_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
_LATIN = re.compile(r"[a-z]")


def script_of(s: str | None) -> str:
    """cjk / latin / mixed / empty。"""
    n = normalize_text(s)
    if not n:
        return "empty"
    has_cjk = bool(_CJK.search(n))
    has_latin = bool(_LATIN.search(n))
    if has_cjk and not has_latin:
        return "cjk"
    if has_latin and not has_cjk:
        return "latin"
    return "mixed"


def is_cross_script(a: str | None, b: str | None) -> bool:
    """两侧书写系统是否不可直接比较。

    包含 mixed —— 实测「ボールパークでShake！Don't Shake！」是中英混排，
    与纯拉丁的「Ballpark de Shake! Don't Shake!」直接比较只得 0.64，
    走过罗马音后能到 0.8+。
    """
    sa, sb = script_of(a), script_of(b)
    return (sa, sb) in (
        ("cjk", "latin"), ("latin", "cjk"),
        ("mixed", "latin"), ("latin", "mixed"),
        ("mixed", "cjk"), ("cjk", "mixed"),
    )


def title_ratio(a: str | None, b: str | None) -> float:
    """曲名相似度 —— **不能用裸 token_set_ratio**。

    实测动机：token_set_ratio 在「一方是另一方的子集」时直接给 1.0，
    于是 "Cocoon" 与 "Zutto Zutto Zutto ~cocoon Ver.~" 被判完全相同（1.00），
    而它们其实是两首不同的歌。

    修法：加**长度比惩罚** —— 子集情形必然长度悬殊，据此把分数压回去。

    另外**跨书写系统时补一次罗马音比较**（§6.1）：
    AnimeThemes 存「Hanakage」、网易云存「花蔭」，
    字符集不相交导致直接比较恒为 0 —— 结果错误的同名歌曲反而赢。
    这一步刻意放在这里（而不是让调用方自己做），避免有人忘了调用。
    """
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0

    plain = _plain_ratio(na, nb)
    try:
        from rapidfuzz.fuzz import token_set_ratio

        ts = token_set_ratio(na, nb) / 100.0
    except ImportError:
        ts = plain
    la, lb = len(na), len(nb)
    lr = min(la, lb) / max(la, lb, 1)
    if lr < 0.75:
        ts *= lr / 0.75
    best = max(plain, ts)

    if is_cross_script(a, b):
        best = max(best, romanize_compare(a, b))
    return best


# ---------------------------------------------------------------- 罗马音

_kks = None
_romaji_cache: dict[str, str] = {}


def romanize(s: str | None) -> str:
    """汉字/假名 → 罗马音。装不上 pykakasi 时返回空串。

    实测动机：AnimeThemes 存「Hanakage」，网易云存「花蔭」，
    字符集不相交导致相似度恒为 0 —— 结果错误的同名歌曲反而赢。

    ⚠️ 已知局限：pykakasi 对**作品名/人名**读音不一定准
    （实测 紅蓮華 → "gurenhana"，正确是 "gurenge"）。
    因此它只作为**额外**信号，不取代原字符串比较。
    """
    global _kks
    if not s:
        return ""
    if s in _romaji_cache:
        return _romaji_cache[s]

    # 纯拉丁输入直接原样归一后返回。
    # 不能交给 pykakasi —— 实测它会把英文原样吐回但行为不稳定，
    # 而跨书写系统比较时**两侧都要非空**，一侧返回空就会让相似度归零。
    if script_of(s) == "latin":
        out = normalize_text(s)
        _romaji_cache[s] = out
        return out

    out = ""
    try:
        if _kks is None:
            from pykakasi import kakasi

            _kks = kakasi()
        out = normalize_text("".join(x["hepburn"] for x in _kks.convert(s)))
    except Exception:
        out = ""
    _romaji_cache[s] = out
    return out


def romanize_available() -> bool:
    try:
        import pykakasi  # noqa: F401

        return True
    except ImportError:
        return False


# ---------------------------------------------------------------- 别名表

@dataclass
class AliasTable:
    """番剧 / 艺人 / 曲名别名收敛。

    结构：canonical -> [variants]。查询时做双向展开。
    审核确认后的修正会**回写 yaml**（自学习），下次匹配命中率提升。

    为什么需要它（实测依据，附录 G.9）：算法侧做到约 57% 可用率后就卡住了，
    剩余误差的主要来源是**跨书写系统且读音无规律**：

        ずっと真夜中でいいのに。  ↔  Zutomayo        （团体简称）
        シャイトープ              ↔  Shytaupe         （片假名外来语）
        中島健人                  ↔  Kento Nakajima   （pykakasi 读成 taketo）
        梅田サイファー            ↔  Umeda Cypher     （サイファー = cypher）

    这些靠算法永远猜不准，但**用户确认一次就能永久记住**。
    所以别名表不是可选项，是剩余命中率的主要出路。
    """

    anime: dict[str, list[str]] = field(default_factory=dict)
    artist: dict[str, list[str]] = field(default_factory=dict)
    title: dict[str, list[str]] = field(default_factory=dict)
    path: str | None = None
    dirty: bool = False

    KINDS = ("anime", "artist", "title")

    # ---- 加载 / 保存
    @classmethod
    def load(cls, path: str | None = None) -> "AliasTable":
        p = path or os.path.join(constants.config_dir(), "aliases.yaml")
        t = cls(path=p)
        if not os.path.exists(p):
            return t
        try:
            with open(p, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            return t
        for kind in cls.KINDS:
            d = raw.get(kind) or {}
            if isinstance(d, dict):
                setattr(t, kind, {str(k): [str(x) for x in (v or [])] for k, v in d.items()})
        return t

    def save(self, path: str | None = None) -> None:
        p = path or self.path
        if not p:
            return
        os.makedirs(os.path.dirname(p), exist_ok=True)
        payload = {kind: getattr(self, kind) for kind in self.KINDS}
        payload["_comment"] = (
            "别名表 —— 向导里人工确认过的匹配会自动回写到这里。"
            "canonical 是主名称，列表里是等价写法。手工补充也完全可以。"
        )
        with open(p, "w", encoding="utf-8") as f:
            yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=True)
        self.dirty = False

    # ---- 查询
    def variants_of(self, kind: str, name: str) -> list[str]:
        """给一个名字，返回它所有已知的写法（含自身）。"""
        table: dict[str, list[str]] = getattr(self, kind)
        target = normalize_text(name)
        out = {name} if name else set()

        for canonical, variants in table.items():
            group = [canonical, *variants]
            if any(normalize_text(g) == target for g in group):
                out.update(group)
        return sorted(x for x in out if x)

    def expand_names(self, kind: str, names: list[str]) -> list[str]:
        """把一个名字列表扩展成「含所有已知等价写法」的列表。

        用 `normalize_text` 去重，且**保留原始大小写/写法**（展示要用）。
        """
        seen: set[str] = set()
        out: list[str] = []
        for n in names or []:
            if not n:
                continue
            for v in [n, *self.variants_of(kind, n)]:
                key = normalize_text(v)
                if key and key not in seen:
                    seen.add(key)
                    out.append(v)
        return out

    def _group_of(self, kind: str, name: str) -> str | None:
        """返回该名字所属的 canonical（不在表里则 None）。"""
        table: dict[str, list[str]] = getattr(self, kind)
        t = normalize_text(name)
        for canonical, variants in table.items():
            if normalize_text(canonical) == t:
                return canonical
            if any(normalize_text(v) == t for v in variants):
                return canonical
        return None

    def same_group(self, kind: str, a: str, b: str) -> bool:
        """两个名字是否已被认定为同一事物（在别名的同一组里）。

        这是**人工确认过的事实**，不是算法猜的 —— 所以调用方可以据此给满分。
        实测动机：没有这一步时，学到的别名只能把分数从 0.47 抬到 0.79，
        仍然差一点到自动门槛 0.80，用户还得再确认一次，
        「确认一次、以后自动」这个性质就不成立。
        """
        if not a or not b:
            return False
        ga = self._group_of(kind, a)
        if ga is None:
            return False
        gb = self._group_of(kind, b)
        return gb is not None and normalize_text(ga) == normalize_text(gb)

    # ---- 自学习
    def learn(self, kind: str, canonical: str, variant: str) -> bool:
        """记录一条新别名。返回是否有变化。"""
        if not canonical or not variant:
            return False
        if normalize_text(canonical) == normalize_text(variant):
            return False
        table: dict[str, list[str]] = getattr(self, kind)
        cur = table.setdefault(canonical, [])
        if variant not in cur and normalize_text(variant) not in {normalize_text(x) for x in cur}:
            cur.append(variant)
            self.dirty = True
            return True
        return False

    @staticmethod
    def _pick_canonical(a: str, b: str) -> tuple[str, str]:
        """挑哪个当主名称。

        优先保留**原生写法**（含 CJK 的那个）：
        「ずっと真夜中でいいのに。」比「Zutomayo」稳定，
        而且它是 Bangumi/网易云两边都更可能认得的形态。
        """
        sa, sb = script_of(a), script_of(b)
        if sa == "latin" and sb in ("cjk", "mixed"):
            return b, a
        return a, b

    def learn_pair(self, kind: str, a: str, b: str) -> bool:
        """把两个名字建立等价关系 —— 这是自学习的入口。

        要处理三种情况（不处理就会出现重复的等价组，越学越乱）：
            ① 两边都不在表里       → 新建一组
            ② 一边在表里           → 挂到已有的组
            ③ **两边各在不同的组里** → 两组合并
        """
        if not a or not b:
            return False
        if normalize_text(a) == normalize_text(b):
            return False

        ga, gb = self._group_of(kind, a), self._group_of(kind, b)
        if ga and gb:
            if ga == gb:
                return False
            table: dict[str, list[str]] = getattr(self, kind)
            merged = table.pop(gb)
            table[ga].extend(x for x in [gb, *merged] if x)
            self.dirty = True
            return True
        if ga:
            return self.learn(kind, ga, b)
        if gb:
            return self.learn(kind, gb, a)
        canonical, variant = self._pick_canonical(a, b)
        return self.learn(kind, canonical, variant)

    def learn_artists(self, target: list[str], chosen: list[str]) -> int:
        """把「目标艺人」与「网易云上实际署名的艺人」建立对应。

        ⚠️ **绝不能用笛卡尔积**。这是实测踩过的坑：

            目标 [J'Nique Nicole]  ←→  候选 [Kevin Penkin, J'Nique Nicole]
            朴素全配对 → 把「J'Nique Nicole」和「Kevin Penkin」学成同一个人

            目标 [ファイルーズあい] ←→  候选 [内田真礼, 長谷川育美, 相良茉優, ...9 个声优]
            朴素全配对 → 9 个**不同的人**被合并成一组，
            之后所有跟其中任何一个有关的匹配都会乱套，
            而且因为别名是隐式生效的，**完全不会报错**。

        正确做法：
            · 两边各只有一个 → 直接学（无歧义）
            · 否则做**一对一最佳对齐**（按罗马音/折叠相似度贪心配对），
              并设相似度下限 —— 这样「ファイルーズあい ↔ Fairouz Ai」
              能学到，而「ファイルーズあい ↔ 内田真礼」不会。
        """
        ts = self._dedupe_names(target)
        cs = self._dedupe_names(chosen)
        if not ts or not cs:
            return 0

        if len(ts) == 1 and len(cs) == 1:
            # 无歧义：唯一对唯一
            return 1 if self.learn_pair("artist", ts[0], cs[0]) else 0

        pairs = sorted(
            ((artist_romaji_similarity(t, c), t, c) for t in ts for c in cs),
            key=lambda x: -x[0],
        )
        used_t: set[str] = set()
        used_c: set[str] = set()
        n = 0
        for score, t, c in pairs:
            kt, kc = normalize_text(t), normalize_text(c)
            if kt in used_t or kc in used_c:
                continue
            if score < ARTIST_LEARN_MIN_SIM:
                break   # 已按分数降序，后面的只会更低
            used_t.add(kt)
            used_c.add(kc)
            if self.learn_pair("artist", t, c):
                n += 1
        return n

    @staticmethod
    def _dedupe_names(names: list[str] | None) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for n in names or []:
            if not n:
                continue
            k = normalize_text(n)
            if k and k not in seen:
                seen.add(k)
                out.append(n)
        return out

    def learn_title(self, target_title: str, chosen_name: str) -> bool:
        """曲名等价（罗马音标题 ↔ 日文原名）。"""
        if not target_title or not chosen_name:
            return False
        return self.learn_pair("title", target_title, chosen_name)

    def learn_anime(self, names: list[str], cand_alias: str | None) -> bool:
        """从网易云的番剧标注里学番剧名。

        候选的 `alias` 形如「TV动画《鬼灭之刃》片头曲」，
        把《》里的内容和目标番剧名建立对应 —— 这条**价值最高**，
        因为番剧标注是打分权重最高的信号（0.30）。
        """
        if not cand_alias:
            return False
        m = _ALIAS_TITLE.search(cand_alias)
        if not m:
            return False
        hit = m.group(1).strip()
        if not hit:
            return False
        best = None
        for n in names or []:
            if not n:
                continue
            # 优先和「已经很像」的那个建立对应，避免把别名挂错番
            s = title_ratio(n, hit)
            if best is None or s > best[0]:
                best = (s, n)
        if best is None or best[0] < 0.3:
            # 标注里的番剧名和目标对不上 → 很可能是跨系列/别名的另一部
            # 宁可不学，也不要把错的关系写进表里污染后续匹配
            return False
        return self.learn_pair("anime", best[1], hit)

    # ---- 统计
    def stats(self) -> dict[str, int]:
        return {k: sum(1 + len(v) for v in getattr(self, k).values()) for k in self.KINDS}


_ALIAS_TITLE = re.compile(r"《([^》]+)》")

# 多对多艺人对齐时的相似度下限。
# 定 0.75 的依据：实测同一个人的跨书写系统相似度普遍 ≥ 0.75
# （奥井雅美↔Masami Okui 0.95、女王蜂↔Ziyoou-vachi 0.75、花冷え。↔HANABIE. 1.00），
# 而不同人的分数远低于此。真正的 1:1 情形不走这个阈值，所以
# 「シャイトープ↔Shytaupe 只有 0.50」这种仍然能学到。
ARTIST_LEARN_MIN_SIM = 0.75


# ---------------------------------------------------------------- 番剧名匹配

def name_similarity(a: str | None, b: str | None) -> float:
    """番剧名相似度 0~1。跨书写系统时补罗马音比较。"""
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    base = _ratio(na, nb)
    if is_cross_script(a, b):
        base = max(base, _ratio(romanize(a), romanize(b)))
    return base


def _ratio(a: str, b: str) -> float:
    """词序不敏感的相似度（用于原始字符串）。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    try:
        from rapidfuzz.fuzz import token_set_ratio

        return token_set_ratio(a, b) / 100.0
    except ImportError:
        from difflib import SequenceMatcher

        return SequenceMatcher(None, a, b).ratio()


def _plain_ratio(a: str, b: str) -> float:
    """普通字符相似度（用于罗马音，不能用 token 版）。

    实测动机：pykakasi 把「真昼の月」切成 mahiru/no/tsuki，
    若用空串拼接得到 "mahirunotsuki"，与 "mahiru no tsuki" 做
    **token** 比较时会因为词边界不同只得到 0.79。
    所以罗马音一律**去掉空格后做普通字符比较**。
    """
    if not a or not b:
        return 0.0
    a2, b2 = a.replace(" ", ""), b.replace(" ", "")
    if a2 == b2:
        return 1.0
    try:
        from rapidfuzz.fuzz import ratio

        return ratio(a2, b2) / 100.0
    except ImportError:
        from difflib import SequenceMatcher

        return SequenceMatcher(None, a2, b2).ratio()


def romanize_compare(a: str | None, b: str | None) -> float:
    """两个名字的罗马音相似度（去空格、普通字符比较）。"""
    return _plain_ratio(romanize(a), romanize(b))


# 罗马音方案的常见差异（训令式 ↔ 平文式、长音写法）
_ROMAJI_FOLD = [
    ("ou", "o"), ("oo", "o"), ("uu", "u"), ("aa", "a"), ("ee", "e"),
    ("ii", "i"),
    ("zi", "ji"), ("si", "shi"), ("ti", "chi"), ("tu", "tsu"),
    ("hu", "fu"), ("sya", "sha"), ("syu", "shu"), ("syo", "sho"),
    ("zya", "ja"), ("zyu", "ju"), ("zyo", "jo"),
]


def fold_romaji(s: str) -> str:
    """把不同罗马音方案归一到同一写法。"""
    t = (s or "").lower().replace(" ", "").replace("-", "").replace("　", "")
    for a, b in _ROMAJI_FOLD:
        t = t.replace(a, b)
    return t


def artist_romaji_similarity(a: str | None, b: str | None) -> float:
    """艺人名的跨书写系统相似度。

    **必须词序不敏感** —— 实测：
        中島健人  ↔ Kento Nakajima
        奥井雅美  ↔ Masami Okui
    日文姓名是「姓 名」，罗马音常写成「名 姓」，
    词序敏感的比较会全部落空（这两组当时都被判 人0.00）。

    另外还要吸收罗马音方案差异（zi/ji、ou/o、uu/u 等），
    例如 女王蜂 → jooubachi ↔ Ziyoou-vachi。
    """
    ra, rb = romanize(a), romanize(b)
    if not ra and not rb:
        return 0.0
    # 名字本身就是拉丁的（如 WANIMA），直接用原串
    la, lb = normalize_text(a), normalize_text(b)
    fa, fb = fold_romaji(ra or la), fold_romaji(rb or lb)
    if not fa or not fb:
        return 0.0
    if fa == fb:
        return 1.0

    best = _plain_ratio(fa, fb)          # 折叠后整体比较
    try:
        from rapidfuzz.fuzz import token_set_ratio

        # 词序不敏感：解决「姓 名」vs「名 姓」
        best = max(best, token_set_ratio(ra or la, rb or lb) / 100.0)
        best = max(best, token_set_ratio(fa, fb) / 100.0)
    except ImportError:
        pass

    # **旋转检测**：pykakasi 会把「中島健人」整块拼成 "nakajimakento"，
    # 而罗马音是 "kentonakajima" —— 两者词序相反但都被拼成了单个 token，
    # 于是词序不敏感的比较也失效。这种情况本质是字符串旋转关系。
    if len(fa) == len(fb) and len(fa) >= 6 and fb in (fa + fa):
        best = max(best, 0.95)
    return best
