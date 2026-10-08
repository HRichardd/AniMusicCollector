"""只产清单、不碰账号的兜底实现。

对应 DESIGN.md §5.8。零风险：不调任何网易云接口，只把
「歌名 - 歌手」写成一个文本文件，由用户自己用网易云客户端的
「导入外部歌单」功能贴进去。

M1 阶段用它来跑通流程并测量匹配准确率 —— 先确认匹配质量，
再决定要不要上自动推送。

## 署名：这是许可证义务，不是礼貌

曲目现在的**主数据源是日文维基百科**的「主題歌」栏（见
`sources/wikipedia.py` 的开头说明），而维基正文是 **CC BY-SA 4.0**：
再分发这些数据 —— 报告、CSV、给别人看的歌单清单都算 —— 必须署名，
并且衍生内容要同样以 CC BY-SA 分发。

所以每一种产物都要能追到出处，而且都要**一眼看得到**：

    · `_match.csv`    → `source` 列（每条曲目是 `wikipedia` 还是 `animethemes`）
    · `_SOURCES.md`   → 署名全文 + 逐来源条数（和 CSV 同目录，旁边就是数据）
    · `_missing.md`   → 页脚再写一遍（这是最常被单独转发出去的一份）

`_import.txt` 刻意**不加**署名行：它是要整段贴进网易云「导入外部歌单」的
输入，多一行不是「歌名 - 歌手」的文本会被当成一首找不到的歌 ——
署名放在同目录的 `_SOURCES.md` 与 `_missing.md` 里，别拿产物格式冒险。
"""

from __future__ import annotations

import csv
import os

from .. import constants
from ..models import Candidate, HealthStatus, PlaylistRef
#: 署名文本**只有一处定义**（`sources/wikipedia.py:ATTRIBUTION`），这里引用而不是
#: 抄一份 —— 抄成两份就会漂移，而漂移掉的署名等于没署名（许可证义务）。
from ..sources.wikipedia import ATTRIBUTION


class ExportCollector:
    """产物：reports/{season}_import.txt 与 .csv"""

    def __init__(self, season_key: str = ""):
        self.season_key = season_key
        self.rows: list[dict] = []
        self.playlists: dict[str, PlaylistRef] = {}

    # ---------------------------------------------------------------- SearchCollector

    def health(self) -> HealthStatus:
        return HealthStatus(True, "纯导出模式（不访问网易云）")

    def search(self, keyword: str, limit: int = 10) -> list[Candidate]:
        """导出模式没有搜索能力 —— 返回空，让上层把该条标为缺失。"""
        return []

    # ---------------------------------------------------------------- PlaylistCollector

    def list_playlists(self) -> list[PlaylistRef]:
        return list(self.playlists.values())

    def ensure_playlist(self, name: str) -> PlaylistRef:
        ref = self.playlists.get(name) or PlaylistRef(name=name)
        self.playlists[name] = ref
        return ref

    def add_to_playlist(self, song_id: str, playlist: PlaylistRef) -> bool:
        """只是记录，不做任何网络写操作。"""
        return True

    # ---------------------------------------------------------------- 导出

    def reset(self) -> None:
        self.rows = []

    def record(self, **row) -> None:
        self.rows.append(row)

    def export(self, out_dir: str | None = None) -> dict[str, str]:
        d = out_dir or constants.REPORT_DIR
        os.makedirs(d, exist_ok=True)
        stem = os.path.join(d, f"{self.season_key or 'season'}_match")

        csv_path = stem + ".csv"
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "anime", "kind", "seq", "song_title", "artists",
                    "chosen_name", "chosen_artists", "netease_song_id",
                    "duration", "score", "status", "query_used", "note",
                    # 出处必须跟着数据走：没有这一列，把 CSV 转给别人就
                    # 等于把署名一起丢了（`Theme.source`，不是猜出来的）。
                    "source",
                ],
                extrasaction="ignore",
            )
            w.writeheader()
            w.writerows(self.rows)

        # 供网易云「导入外部歌单」用的纯文本
        txt_path = stem + "_import.txt"
        with open(txt_path, "w", encoding="utf-8", newline="") as f:
            for r in self.rows:
                if r.get("status") in ("auto", "confirmed", "pending_review"):
                    name = r.get("chosen_name") or ""
                    artist = r.get("chosen_artists") or ""
                    if name:
                        f.write(f"{name} - {artist}\n")

        # 缺失清单
        miss_path = stem + "_missing.md"
        with open(miss_path, "w", encoding="utf-8", newline="") as f:
            f.write(f"# {self.season_key} 缺失清单\n\n")
            groups = {
                "missing_source": "数据源未收录（下次重跑可能自动补上）",
                "missing_netease": "网易云未上架 / 无版权（等上架或人工处理）",
                "missing_match": "匹配不上（需要人工：调阈值或补别名）",
                "low": "置信度过低（需要人工确认）",
            }
            for key, title in groups.items():
                items = [r for r in self.rows if r.get("status") == key]
                if not items:
                    continue
                f.write(f"## {title}（{len(items)}）\n\n")
                for r in items:
                    f.write(
                        f"- **{r.get('anime')}** {r.get('kind')}"
                        f"{r.get('seq') or ''} — {r.get('song_title')}"
                        f" / {'、'.join(r.get('artists') or [])}"
                        f"　得分 {r.get('score')}\n"
                    )
                f.write("\n")
            # 页脚再写一遍署名：这份文件最常被单独转发出去，
            # 而「数据本身」离了署名就是侵权 —— 不能只在别的文件里写。
            f.write(self._sources_footer(stem))

        # ---- 来源与许可（署名义务的落点）
        sources_path = stem + "_SOURCES.md"
        with open(sources_path, "w", encoding="utf-8", newline="") as f:
            f.write(self._sources_doc(stem))

        return {
            "csv": csv_path,
            "import": txt_path,
            "missing": miss_path,
            "sources": sources_path,
        }

    # ---------------------------------------------------------------- 署名

    def source_counts(self) -> dict[str, int]:
        """逐来源条数 —— `Theme.source` 的原始取值（`wikipedia` / `animethemes` …）。"""
        counts: dict[str, int] = {}
        for r in self.rows:
            key = str(r.get("source") or "未知")
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _sources_doc(self, stem: str) -> str:
        counts = self.source_counts()
        listed = " · ".join(f"{k} {v} 条" for k, v in sorted(counts.items())) or "（本次没有曲目）"
        base = os.path.basename(stem)
        return (
            f"# {self.season_key} 数据来源与许可\n\n"
            f"> {ATTRIBUTION}\n\n"
            "维基百科（日文）是本报告 OP/ED 曲目的**主数据源**，其正文以\n"
            "**CC BY-SA 4.0** 授权。**转发、再分发本报告（含 CSV / 歌单清单）时\n"
            "必须保留上面这行署名**，并以同样的许可分发衍生内容。\n\n"
            "## CSV 的 `source` 列是什么意思\n\n"
            "| `source` | 含义 | 许可 / 义务 |\n"
            "|---|---|---|\n"
            "| `wikipedia` | 曲目取自日文维基百科「主題歌」栏（**主源**） | CC BY-SA 4.0，**必须署名** |\n"
            "| `animethemes` | 维基没有对应词条时回落到 AnimeThemes | 见 animethemes.moe 的条款 |\n"
            "| `manual` | 人工补录 | 无第三方义务 |\n"
            "| 其它 | 见 `theme.source` 的原始值 | 以该来源的条款为准 |\n\n"
            f"本次计数：{listed}\n\n"
            "## 这次导出的文件\n\n"
            f"- `{base}.csv` —— 全量匹配报告，带 `source` 列\n"
            f"- `{base}_import.txt` —— 供网易云「导入外部歌单」的纯文本"
            "（刻意不加署名行：多一行会被当成一首找不到的歌）\n"
            f"- `{base}_missing.md` —— 缺失清单（页脚同样带署名）\n"
            "- 本文件 —— 来源与许可说明\n"
        )

    def _sources_footer(self, stem: str) -> str:
        return (
            "\n---\n\n"
            f"**{ATTRIBUTION}**\n\n"
            "曲目主数据源是日文维基百科（CC BY-SA 4.0）—— 再分发本清单时必须保留"
            "上面这行署名。逐条出处见 CSV 的 `source` 列，详见 `"
            f"{os.path.basename(stem)}_SOURCES.md`。\n"
        )
