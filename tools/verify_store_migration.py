"""只读备份原库，在独立副本验证迁移；不初始化正式库、不输出曲目内容。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_ROOT / "src"), str(_ROOT / "vendor")]

from animusic.store import Store


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _backup(source: Path, target: Path) -> None:
    # backup API 包含已提交的 WAL，直接复制 state.db 会漏掉这部分数据。
    with closing(_readonly(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


def _tables(conn: sqlite3.Connection) -> dict[str, list[str]]:
    return {
        name: [r[1] for r in conn.execute(f"PRAGMA table_info({_quote(name)})")]
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")
    }


def _snapshot(conn: sqlite3.Connection, columns=None) -> dict:
    result = {}
    for table, names in (columns or _tables(conn)).items():
        rows = conn.execute(
            f"SELECT {','.join(map(_quote, names))} FROM {_quote(table)}").fetchall()
        encoded = sorted(json.dumps(tuple(row), ensure_ascii=False, default=lambda b: {
            "bytes": b.hex()}, separators=(",", ":")) for row in rows)
        result[table] = {
            "rows": len(rows),
            "sha256": hashlib.sha256(json.dumps(encoded, ensure_ascii=False).encode("utf-8")).hexdigest(),
        }
    return result


def verify_migration(source: Path, output: Path) -> dict:
    source, output = source.resolve(), output.resolve()
    if not source.is_file():
        raise ValueError(f"原库不存在：{source}")
    # 不覆盖旧证据；即使输出目录已有文件也不做清理。
    output.mkdir(parents=True, exist_ok=False)
    original, migrated = output / "original.db", output / "migrated.db"
    _backup(source, original)
    _backup(original, migrated)
    with closing(_readonly(original)) as conn:
        columns = _tables(conn)
        before = _snapshot(conn, columns)
        old_fk = set(conn.execute("PRAGMA foreign_key_check").fetchall())
        old_integrity = conn.execute("PRAGMA integrity_check").fetchall()

    # Store 构造会迁移，这里只允许指向刚创建的副本。
    with Store(str(migrated)) as store:
        after = _snapshot(store.conn, columns)
        new_fk = {tuple(r) for r in store.conn.execute("PRAGMA foreign_key_check")}
        integrity = store.conn.execute("PRAGMA integrity_check").fetchall()
        first_full = _snapshot(store.conn)
        schema = store.conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        schema = [tuple(r) for r in schema]
        unresolved = store.conn.execute(
            "SELECT COUNT(*) FROM theme WHERE origin='manual' AND source!='manual' "
            "AND source_song_title IS NULL").fetchone()[0]
        indexes = [dict(r) for r in store.conn.execute("PRAGMA index_list(push_selection)")]
        unique_keys = [tuple(r["name"] for r in store.conn.execute(
            f"PRAGMA index_info({_quote(idx['name'])})"))
            for idx in indexes if idx["unique"] and not idx["partial"]]
        selection_key_ok = ("season_id", "theme_id", "netease_song_id") in unique_keys
        unbound_key_ok = any(i["name"] == "idx_push_selection_unbound" and i["unique"]
                             and i["partial"] for i in indexes)
    with Store(str(migrated)) as store:
        repeat_equal = first_full == _snapshot(store.conn) and schema == [tuple(r) for r in
            store.conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")]

    checks = {
        "original_integrity_ok": old_integrity == [("ok",)],
        "migrated_integrity_ok": [tuple(r) for r in integrity] == [("ok",)],
        "all_original_values_preserved": before == after,
        "no_new_foreign_key_violations": not (new_fk - old_fk),
        "selection_key_ok": selection_key_ok,
        "unbound_selection_key_ok": unbound_key_ok,
        "reopen_is_idempotent": repeat_equal,
    }
    report = {
        "evidence": "② 只有离线测试（正式库只读备份，迁移发生在副本）",
        "source": str(source), "output": str(output),
        "checks": checks, "passed": all(checks.values()),
        "before": before, "after": after,
        "existing_foreign_key_violations": len(old_fk),
        "new_foreign_key_violations": len(new_fk - old_fk),
        "unresolved_manual_identities": unresolved,
    }
    (output / "report.json").write_bytes(
        (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="仅以 SQLite mode=ro 打开的原库")
    parser.add_argument("--out", type=Path, required=True, help="必须尚不存在的验证目录")
    args = parser.parse_args(argv)
    report = verify_migration(args.db, args.out)
    print(json.dumps({"passed": report["passed"], "checks": report["checks"],
                      "report": str(args.out / "report.json")}, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
