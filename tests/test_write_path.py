"""`collector/write_path.py` + `desktop_cdp` 写入链路的离线测试。

三类：

1. **纯函数**（不需要客户端、不需要网络）：候选 payload 构造与合法性守卫、
   id 归一化 / 「歌单里有没有这首歌」、缺席判定的三态、dispatch 返回值的多形状判读、
   限速器、写入策略的候选排序。
2. **CDP 往返**：复用 `tests/test_cdp.py` 的假 CDP 服务器 `FakeCDP`，
   以及 `tests/test_desktop_cdp.py` 里的 `_PrefixResponses`（按表达式前缀分派响应）
   和 `_fake_state`（假造的客户端 store）。假服务器 + 可变的 state 就能演完整条
   「dispatch → 回查」的链路，包括**写入成功**与**回查不到**两种走向。
3. **不静默成功**（本里程碑最重要的一类）：回查不到、曲目读不全、窗口隐藏、
   JS 抛错、CDP 断连 —— 每一条都必须**显式失败**，绝不允许返回成功。

不用 `tempfile`（该环境里清理会被权限拒）。不碰真实客户端。

⚠️ 这里测的是「代码路径与判定逻辑」，**不是**「payload 形状对了」。
候选 payload 全是推测（见 write_path.py 的 docstring），真机验证才能把推测变事实。
"""

from __future__ import annotations

import copy
import json
import os
import socket
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_HERE, os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from animusic.cdp import CDPBrowser  # noqa: E402
from animusic.collector import desktop_cdp as dc  # noqa: E402
from animusic.collector import write_path as wp  # noqa: E402
from animusic.models import PlaylistRef  # noqa: E402
from test_cdp import HAS_WS, FakeCDP  # noqa: E402
from test_desktop_cdp import _fake_state, _PrefixResponses  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ═══════════════════════════════════════════════════════════════════ 纯函数


class TestPayloadVariants(unittest.TestCase):
    """候选 payload 的元数据与不变量（**这些不变量与形状无关**，所以能离线钉住）。"""

    def test_metadata_is_complete_and_ordered(self):
        for kind, variants in (("add", wp.ADD_PAYLOAD_VARIANTS),
                               ("create", wp.CREATE_PAYLOAD_VARIANTS)):
            with self.subTest(kind=kind):
                self.assertTrue(variants, f"{kind} 至少要有候选")
                names = [v.name for v in variants]
                self.assertEqual(len(names), len(set(names)), "候选名要唯一（日志/诊断靠它）")
                for i, v in enumerate(variants):
                    self.assertEqual(v.kind, kind)
                    self.assertTrue(v.name.strip())
                    self.assertGreater(v.confidence, 0.0, v.name)
                    self.assertLessEqual(v.confidence, 1.0, v.name)
                    # ⚠️ 这里原来是「只允许 doAction / popMenu 两族」——
                    #    那是个**错的**断言：它把「设计阶段猜的两族」当成了全集。
                    #    真机实测证明建歌单走的是第三族（扁平的
                    #    `async:hostResource/createPlaylist`）。所以改成「必须是已知族之一」，
                    #    并另外钉住「实测那条的 action_type 就是抓到的那个」。
                    self.assertIn(v.action_type, (
                        wp.ACTION_TYPE_DO_ACTION, wp.ACTION_TYPE_POP_MENU,
                        wp.ACTION_TYPE_HOST_CREATE_PLAYLIST))
                    # 每种形状都必须写明推测依据（这是任务的硬要求）
                    self.assertGreaterEqual(len(v.rationale), 30, v.name)
                    self.assertTrue(
                        any(w in v.rationale for w in ("推测", "猜", "对照", "依据", "实测")),
                        f"{v.name} 的 rationale 没有标明依据/推测")
                # confidence 降序：confidence 就是「尝试顺序」的依据
                self.assertEqual([v.confidence for v in variants],
                                 sorted((v.confidence for v in variants), reverse=True),
                                 "候选必须按 confidence 降序排列")
                # 第 0 条**必须**是实测的（顺序即尝试顺序，实测的在最前）
                self.assertTrue(variants[0].measured,
                                f"{kind}: 第 0 条必须是真机实测的候选，实际是 {variants[0].name!r}")

    def test_add_payloads_carry_the_target_song_and_playlist(self):
        """任何非对照候选都必须同时提到目标歌与目标歌单 —— 否则就是"加错地方"的候选。"""
        for i, v in enumerate(wp.ADD_PAYLOAD_VARIANTS):
            payload = wp.build_add_payload("501", "1001", variant=i)
            blob = json.dumps(payload, ensure_ascii=False)
            with self.subTest(variant=i, name=v.name):
                self.assertIn("501", blob, "没提到歌曲 id")
                if v.is_control:
                    self.assertNotIn("1001", blob, "对照候选本来就不该带目标歌单")
                else:
                    self.assertIn("1001", blob, "没提到目标歌单 id")

    def test_exactly_one_control_variant_and_it_is_last(self):
        ctl = [i for i, v in enumerate(wp.ADD_PAYLOAD_VARIANTS) if v.is_control]
        self.assertEqual(len(ctl), 1)
        # 显式要求「全试」才谈得上顺序 —— 默认只试实测那条（见 max_add_variants）
        order = wp.WritePolicy(max_add_variants=None).add_variant_order()
        self.assertEqual(order[-1], ctl[0], "对照候选必须最后试")
        self.assertEqual(set(order), set(range(len(wp.ADD_PAYLOAD_VARIANTS))))

    def test_variant_range_and_type_are_guarded(self):
        n = len(wp.ADD_PAYLOAD_VARIANTS)
        for bad in (n, -1, n + 5):
            with self.assertRaises(wp.WritePathError):
                wp.build_add_payload("501", "1001", variant=bad)
        for bad in ("0", 0.0, True, None):          # type: ignore[arg-type]
            with self.assertRaises(wp.WritePathError):
                wp.build_add_payload("501", "1001", variant=bad)  # type: ignore[arg-type]
        m = len(wp.CREATE_PAYLOAD_VARIANTS)
        with self.assertRaises(wp.WritePathError):
            wp.build_create_playlist_payload("26年10月", variant=m)

    def test_empty_ids_are_refused_not_guessed(self):
        """目标歌单未知时**绝不猜** —— 猜错就是把歌推进别人的歌单。"""
        for empty in ("", "   ", None):
            with self.assertRaises(wp.WritePathError):
                wp.build_add_payload(empty, "1001", variant=0)
            with self.assertRaises(wp.WritePathError):
                wp.build_add_payload("501", empty, variant=0)
        with self.assertRaises(wp.WritePathError):
            wp.build_create_playlist_payload("   ", variant=0)
        # 对照候选本来就不带歌单 → 允许空
        ctl = next(i for i, v in enumerate(wp.ADD_PAYLOAD_VARIANTS) if v.is_control)
        self.assertIsInstance(wp.build_add_payload("501", None, variant=ctl), dict)

    def test_zero_is_a_legal_id_not_an_empty_one(self):
        """`0` 是个合法整数（norm_id 不骗人）—— 守卫只拦「根本没有 id」。
        这种无意义的 id 会在**回查**那一步失败，而不是在这里被静默接受。"""
        self.assertEqual(wp.norm_id(0), "0")
        self.assertIn('"0"', json.dumps(wp.build_add_payload(0, 0, variant=0)))

    def test_int_and_str_ids_are_normalized(self):
        a = wp.build_add_payload(501, 1001, variant=0)
        b = wp.build_add_payload("501", "1001", variant=0)
        self.assertEqual(a, b)
        blob = json.dumps(a)
        self.assertIn('"501"', blob)
        self.assertIn('"1001"', blob)

    def _splice_index(self) -> int:
        return next(i for i, v in enumerate(wp.ADD_PAYLOAD_VARIANTS)
                    if wp.ACTION_SPLICE in v.name)

    def test_measured_favorite_variant_matches_the_recorded_action(self):
        """**第 0 条候选是真机实测抓下来的「加入歌单」动作**（DESIGN.md G.15）。

        必须逐字段复现记录器抓到的原文（n=184，把《虹色の日々Duet ver.》
        加进「26年10月」）：

            {actionId: "favorite", eventType: "click",
             data: {resource, resourceType:"track", from:{scene},
                    to:{resourceIdOfScene}, callback, actionPayload}}

        最要紧的一条：`data.from` **不含任何歌单信息**、目标由
        `to.resourceIdOfScene` 唯一决定 —— 这正是它不依赖「客户端当前视图」、
        能被 headless 调用的原因（对比 splice：发了没效果）。
        """
        a = wp.build_add_payload("3395172821", "18457224255", variant=0)
        self.assertTrue(wp.ADD_PAYLOAD_VARIANTS[0].measured, "第 0 条必须标为实测")
        self.assertEqual(a["type"], wp.ACTION_TYPE_DO_ACTION)
        pl = a["payload"]
        self.assertEqual(pl["actionId"], wp.ACTION_FAVORITE)
        self.assertEqual(pl["eventType"], "click")
        d = pl["data"]
        self.assertEqual(sorted(d), ["actionPayload", "callback", "from",
                                     "resource", "resourceType", "to"])
        self.assertEqual(d["resourceType"], "track")
        self.assertEqual(d["resource"]["id"], "3395172821")
        # ★ 目标由它唯一决定
        self.assertEqual(d["to"], {"resourceIdOfScene": "18457224255"})
        # ★ 来源只有 scene，没有 resourceIdOfScene（所以与当前视图无关）
        self.assertEqual(d["from"], {"scene": wp.SCENE_PLAYLIST})
        self.assertEqual(
            d[wp.SYNTHETIC_ACTION_PAYLOAD_KEY],
            {wp.SYNTHETIC_EVENT_KEY: None,
             wp.ACTION_PAYLOAD_FROM_DOWNLOAD: wp.DEFAULT_FROM_DOWNLOAD})
        self.assertEqual(d["callback"], wp.SYNTHETIC_FUNCTION)

    def test_favorite_variant_omits_parent_data_by_default(self):
        """`parentData` 是「发起时正在看哪个歌单」—— 我们没有这个概念。

        少发一个**不知道语义**的字段，好过编一个：实测里
        `parentData.from.resourceIdOfScene` 与 `to.resourceIdOfScene` 是**不同**的值
        （17977808146 vs 18457224255），说明它不是目标选择器，编一个可能是错的。
        """
        a = wp.build_add_payload("501", "1001", variant=0)
        self.assertNotIn("parentData", a["payload"])

    def test_measured_splice_variant_still_round_trips(self):
        """splice 也是实测抓到的，但用途是**在当前列表里拖动**，不是加入歌单。

        它真机发过一次、**服务端一首没加** —— 所以它降为兜底（confidence 极低），
        形状仍然被钉住，免得以后有人把它当成「加歌」的主力。
        """
        i = self._splice_index()
        v = wp.ADD_PAYLOAD_VARIANTS[i]
        self.assertTrue(v.measured)
        self.assertLess(v.confidence, 0.1, "用错场景的候选不该有高 confidence")
        self.assertIn("splice", v.rationale + v.name)
        a = wp.build_add_payload("3406947013", "17977808146", variant=i)
        pl = a["payload"]
        # 实测：eventType 在 payload **里面**
        self.assertEqual(pl["eventType"], "drop")
        self.assertEqual(pl["actionId"], wp.ACTION_SPLICE)
        d = pl["data"]
        self.assertEqual(sorted(d), ["actionPayload", "callback", "from",
                                     "resource", "resourceType", "to"])
        self.assertEqual(d["resourceType"], "track")
        self.assertEqual(d["resource"]["id"], "3406947013")
        self.assertEqual(d["to"]["resourceIdOfScene"], "17977808146")
        self.assertEqual(d["to"]["insertIndex"], 0)
        self.assertEqual(d["from"]["scene"], wp.SCENE_PLAYLIST)
        self.assertEqual(d["from"]["resourceIdOfScene"], "17977808146")
        # 实测里 actionPayload 就是空对象（不是 click 分支那种 {rawEvent: ...}）
        self.assertEqual(d[wp.SYNTHETIC_ACTION_PAYLOAD_KEY], {})
        # callback 是函数 → 只能是哨兵串，交给 JS 换掉
        self.assertEqual(d["callback"], wp.SYNTHETIC_FUNCTION)

    def test_measured_splice_variant_never_guesses_the_target_playlist(self):
        """目标歌单缺失 → 拒绝构造。**猜错就是把歌推进别人的歌单。**"""
        with self.assertRaises(wp.WritePathError):
            wp.build_add_payload("501", "", variant=0)
        with self.assertRaises(wp.WritePathError):
            wp.build_add_payload("501", None, variant=0)
        si = self._splice_index()
        with self.assertRaises(wp.WritePathError):
            wp.build_add_payload("501", "", variant=si)

    def test_measured_variant_accepts_a_full_song_object(self):
        """实测原文给的是**完整歌曲对象**，所以这条候选必须能吃下它。"""
        song = {"id": 3406947013, "name": "決意の唄", "duration": 230000,
                "artists": [{"id": 1, "name": "A"}], "album": {"id": 2, "name": "B"}}
        a = wp.build_add_payload("3406947013", "17977808146", variant=0, resource=song)
        got = a["payload"]["data"]["resource"]
        self.assertEqual(got["name"], "決意の唄")
        self.assertEqual(got["duration"], 230000)
        # 实测原文里 id 是**字符串**，传进来的数字要被归一成字符串
        self.assertEqual(got["id"], "3406947013")
        # 不能改调用方的对象
        self.assertEqual(song["id"], 3406947013)

    def test_measured_variant_refuses_when_ids_disagree(self):
        """`song_id` 与 `resource.id` 不一致 → 拒绝，**不许二选一**。

        猜错的后果是往用户歌单里塞另一首歌，而回查只能看出「多了一首」，
        看不出「加错了」—— 这是本模块最不能接受的一类错误。
        """
        with self.assertRaises(wp.WritePathError) as ctx:
            wp.build_add_payload("999", "17977808146", variant=0,
                                 resource={"id": 111, "name": "别的歌"})
        self.assertIn("打架", str(ctx.exception))

    def test_measured_variant_falls_back_to_bare_id(self):
        a = wp.build_add_payload("501", "1001", variant=0, resource=None)
        self.assertEqual(a["payload"]["data"]["resource"], {"id": "501"})
        # 空 dict 也一样退化（不是「有 resource 就一定用它」）
        b = wp.build_add_payload("501", "1001", variant=0, resource={})
        self.assertEqual(b["payload"]["data"]["resource"], {"id": "501"})

    def test_do_action_and_pop_menu_shapes_match_the_bundle_snippet(self):
        """§7.3 逆出来的外层形状：doAction 的 eventType 在 payload **里面**，
        popMenu 的 parentData/eventType 在 payload 里和在顶层**各有一份**。

        ⚠️ 这几条是**推测**族，实测量出 splice 之后它们已经被降为兜底。
        这里按名字取，不按序号 —— 免得下次调整顺序时测试静默测到别的东西。
        """
        do_i = next(i for i, v in enumerate(wp.ADD_PAYLOAD_VARIANTS)
                    if v.name.startswith("doAction·data{type,id,playlistId}"))
        do = wp.build_add_payload("501", "1001", variant=do_i)
        self.assertEqual(do["type"], wp.ACTION_TYPE_DO_ACTION)
        self.assertNotIn("eventType", {k: v for k, v in do.items() if k != "payload"})
        pl = do["payload"]
        self.assertEqual(pl["actionId"], wp.ACTION_ADD_TO_PLAYLIST)
        self.assertEqual(pl["eventType"], "click")
        self.assertIsInstance(pl["data"], dict)
        self.assertIsInstance(pl["parentData"], dict)

        pop_i = next(i for i, v in enumerate(wp.ADD_PAYLOAD_VARIANTS)
                     if v.action_type == wp.ACTION_TYPE_POP_MENU)
        pop = wp.build_add_payload("501", "1001", variant=pop_i)
        self.assertEqual(pop["type"], wp.ACTION_TYPE_POP_MENU)
        self.assertEqual(pop["eventType"], "rightMenu")
        self.assertIsInstance(pop["parentData"], dict)
        self.assertEqual(pop["payload"]["parentData"]["id"], "1001")
        # popMenu 是把 data 展开到 payload 顶层（不是塞进 payload.data）
        self.assertNotIn("data", pop["payload"])
        self.assertEqual(pop["payload"]["id"], "501")

    def test_raw_event_is_placeholder_not_a_real_event(self):
        """rawEvent 是 React 合成事件，Python 侧造不出来 → 必须是 None 占位，
        由注入的 JS 换成同形桩。这里钉住「我们没伪造一个假事件对象」。

        走**推测族**那条（click 分支才带 rawEvent）；实测的 splice 分支
        `actionPayload` 本来就是空的，不该被强行塞进一个 rawEvent。
        """
        do_i = next(i for i, v in enumerate(wp.ADD_PAYLOAD_VARIANTS)
                    if v.name.startswith("doAction·data{type,id,playlistId}"))
        data = wp.build_add_payload("501", "1001", variant=do_i)["payload"]["data"]
        self.assertIn(wp.SYNTHETIC_ACTION_PAYLOAD_KEY, data)
        self.assertIsNone(data[wp.SYNTHETIC_ACTION_PAYLOAD_KEY][wp.SYNTHETIC_EVENT_KEY])

    def test_js_sentinel_matches_the_python_constant(self):
        """JS 里的函数哨兵必须就是 Python 那个常量 —— 两份真相会静默漏 patch。"""
        import json as _json

        from animusic.collector import desktop_cdp as _dc

        expr = _dc.js_dispatch(wp.build_add_payload("501", "1001", variant=0))
        self.assertIn(_json.dumps(wp.SYNTHETIC_FUNCTION), expr)
        self.assertNotIn("__DCDP_FN_SENTINEL__", expr, "占位符没被替换")

    def test_create_payloads_carry_the_name(self):
        for i, v in enumerate(wp.CREATE_PAYLOAD_VARIANTS):
            payload = wp.build_create_playlist_payload("26年1月", variant=i)
            with self.subTest(variant=i, name=v.name):
                self.assertIn("26年1月", json.dumps(payload, ensure_ascii=False))
                self.assertEqual(payload["type"], v.action_type)

    def test_describe_payload_variants(self):
        rows = wp.describe_payload_variants()
        self.assertEqual(len(rows),
                         len(wp.ADD_PAYLOAD_VARIANTS) + len(wp.CREATE_PAYLOAD_VARIANTS))
        for r in rows:
            self.assertIn("name", r)
            self.assertIn("confidence", r)
            self.assertIn("rationale", r)
            self.assertIn("payload", r)
            self.assertIsNone(r["build_error"])
        add_rows = wp.describe_payload_variants(song_id=501, playlist_id=1001, kind="add")
        self.assertEqual(len(add_rows), len(wp.ADD_PAYLOAD_VARIANTS))
        self.assertIn("501", json.dumps(add_rows[0]["payload"]))
        self.assertEqual([r["variant"] for r in add_rows],
                         list(range(len(wp.ADD_PAYLOAD_VARIANTS))))
        # 占位符：不给 id 时不该假装有真实 id
        self.assertIn(wp.PLACEHOLDER_SONG_ID, json.dumps(rows[0]["payload"]))

    def test_render_payload_report_mentions_every_variant(self):
        rows = wp.describe_payload_variants()
        text = wp.render_payload_report(rows)
        for r in rows:
            self.assertIn(r["name"], text)
        self.assertIn("推测依据", text)
        self.assertIn("对照候选", text)

    def test_action_to_js_literal_roundtrips_hostile_strings(self):
        """歌单名是用户输入 → 必须当作不可信数据；嵌入 JS 后还得能原样解回来。"""
        action = wp.build_create_playlist_payload(
            '26年1月 "引号" \\反斜杠\n换行\ttab 😀 \u2028\u2029', variant=0)
        lit = wp.action_to_js_literal(action)
        self.assertNotIn("\n", lit)
        self.assertNotIn("\u2028", lit)
        self.assertNotIn("\u2029", lit)
        self.assertEqual(json.loads(lit), action)


class TestVariantOrdering(unittest.TestCase):
    def test_default_tries_only_the_measured_variant(self):
        """**默认只打一发**（2026-10 真机实测后改的）。

        原来默认「全试一遍」，前提是「7 条候选里总有一条对」。实测推翻了这个前提：
        真实的 action 是 `{actionId:'splice', ...}`，其余 7 条 payload 里连
        `playlistId` / `songId` 这些字段都不存在 —— 再试就是**明知无效还往账号上打**。
        真机那次失败推送白发了 8 个 action，服务端确认一首没加。
        """
        order = wp.WritePolicy().add_variant_order()
        self.assertEqual(order, [0], "默认只该试实测那条")
        self.assertTrue(wp.ADD_PAYLOAD_VARIANTS[0].measured)

    def test_explicit_none_still_covers_all_variants(self):
        """想看「全试」的旧行为必须**显式**要求 —— 不许悄悄回退。"""
        order = wp.WritePolicy(max_add_variants=None).add_variant_order()
        self.assertEqual(len(order), len(wp.ADD_PAYLOAD_VARIANTS))

    def test_hint_goes_first_without_duplication(self):
        order = wp.WritePolicy(max_add_variants=None).add_variant_order(hint=4)
        self.assertEqual(order[0], 4)
        self.assertEqual(len(order), len(set(order)))
        self.assertEqual(sorted(order), sorted(range(len(wp.ADD_PAYLOAD_VARIANTS))))

    def test_control_hint_is_ignored(self):
        """对照组成功过也不该被当成「已知可用的形状」优先重试。"""
        ctl = next(i for i, v in enumerate(wp.ADD_PAYLOAD_VARIANTS) if v.is_control)
        order = wp.WritePolicy(max_add_variants=None).add_variant_order(hint=ctl)
        self.assertNotEqual(order[0], ctl)

    def test_max_variants_caps_the_attempts(self):
        self.assertEqual(len(wp.WritePolicy(max_add_variants=2).add_variant_order()), 2)
        self.assertEqual(wp.WritePolicy(max_add_variants=0).add_variant_order(), [])
        self.assertEqual(wp.WritePolicy(max_create_variants=1).create_variant_order(), [0])
        self.assertEqual(wp.WritePolicy(max_create_variants=0).create_variant_order(), [])
        self.assertEqual(len(wp.WritePolicy(max_create_variants=99).create_variant_order()),
                         len(wp.CREATE_PAYLOAD_VARIANTS))


class TestContainsSong(unittest.TestCase):
    def test_field_name_variants(self):
        for song in ({"id": 501}, {"songId": "501"}, {"trackId": 501},
                     {"id": "501", "name": "x"}, {"netease_song_id": "501"},
                     {"song_id": 501}, {"s_songId": 501}, {"resourceId": 501}):
            with self.subTest(song=song):
                self.assertTrue(wp.contains_song([song], "501"))
                self.assertTrue(wp.contains_song([song], 501))

    def test_id_type_variants(self):
        self.assertTrue(wp.contains_song([{"id": 501}], "501"))
        self.assertTrue(wp.contains_song([{"id": "501"}], 501))
        self.assertTrue(wp.contains_song([{"id": 501.0}], "501"))
        self.assertTrue(wp.contains_song([{"id": "501.0"}], "501"))
        self.assertTrue(wp.contains_song([{"id": " 501 "}], 501))
        self.assertTrue(wp.contains_song([501], "501"))
        self.assertTrue(wp.contains_song(["501"], 501))

    def test_bool_is_not_an_id(self):
        """True 是 int 的子类 —— 绝不能让它变成 "1" 误命中。"""
        self.assertFalse(wp.contains_song([{"id": True}], "1"))
        self.assertIsNone(wp.norm_id(True))
        self.assertIsNone(wp.norm_id(False))

    def test_nested_objects(self):
        self.assertTrue(wp.contains_song([{"song": {"id": 501}}], 501))
        self.assertTrue(wp.contains_song([{"track": {"songId": "501"}}], "501"))
        self.assertTrue(wp.contains_song([{"data": {"id": 501}}], "501"))
        self.assertTrue(wp.contains_song([{"raw": {"id": 501}}], "501"))

    def test_misses(self):
        self.assertFalse(wp.contains_song([{"id": 501}], "502"))
        self.assertFalse(wp.contains_song([{"id": 501}, {"songId": 502}], "999"))
        self.assertFalse(wp.contains_song([], "501"))
        self.assertFalse(wp.contains_song(None, "501"))
        self.assertFalse(wp.contains_song([{"id": 501}], ""))
        self.assertFalse(wp.contains_song([{"id": 501}], None))
        self.assertFalse(wp.contains_song([{"id": None}], "501"))
        self.assertFalse(wp.contains_song([{"id": {"nested": 1}}], "501"))

    def test_normalized_song_dicts_from_playlist_songs(self):
        """`playlist_songs()` 的产物用的是 netease_song_id。"""
        songs = [{"netease_song_id": "601", "name": "Some OP", "artists": ["Aki"]}]
        self.assertTrue(wp.contains_song(songs, "601"))
        self.assertTrue(wp.contains_song(songs, 601))
        self.assertFalse(wp.contains_song(songs, 602))

    def test_duplicates_and_junk_do_not_break_it(self):
        songs = [{"netease_song_id": "501"}, {"netease_song_id": "501"},
                 {"name": "没有 id"}, None, 0, "", {"id": []}]
        self.assertTrue(wp.contains_song(songs, "501"))
        self.assertFalse(wp.contains_song(songs, "502"))

    def test_iter_song_ids_yields_in_priority_order(self):
        got = list(wp.iter_song_ids({"id": 1, "songId": 2, "trackId": 3}))
        self.assertEqual(got, ["1", "2", "3"])
        self.assertEqual(list(wp.iter_song_ids(" 7 ")), ["7"])


class TestCheckMembership(unittest.TestCase):
    """缺席判定的三态 —— 「能证明不在」与「判不出来」绝不能混。"""

    def test_present(self):
        m = wp.check_membership([{"id": 501}], 501)
        self.assertIs(m.present, True)
        self.assertTrue(m.complete)
        self.assertIn("已经有", m.reason)

    def test_provable_absence_when_all_tracks_are_resolved(self):
        m = wp.check_membership([{"id": 501}, {"id": 502}], 999, declared_count=2)
        self.assertIs(m.present, False)
        self.assertTrue(m.complete)
        self.assertEqual((m.resolved, m.declared), (2, 2))

    def test_empty_playlist_is_provably_absent(self):
        m = wp.check_membership([], 999, declared_count=0, entries=0)
        self.assertIs(m.present, False)
        self.assertIn("空歌单", m.reason)

    def test_incomplete_track_data_is_unknown_not_absent(self):
        """store 里 trackIds 指向的歌曲对象没拉下来 → 解析出 0 首 ≠ 歌单是空的。"""
        m = wp.check_membership([], 999, declared_count=37, entries=37)
        self.assertIsNone(m.present)
        self.assertFalse(m.complete)
        self.assertIn("不能据此断定", m.reason)
        self.assertIn("打开这个歌单", m.reason)

    def test_unknown_count_and_no_entries_is_unknown(self):
        m = wp.check_membership([], 999, declared_count=None, entries=0)
        self.assertIsNone(m.present)
        self.assertIn("分不清", m.reason)

    def test_unknown_count_with_entries_is_absent(self):
        m = wp.check_membership([{"id": 1}, {"id": 2}], 999, declared_count=None, entries=2)
        self.assertIs(m.present, False)
        self.assertTrue(m.complete)

    def test_unknown_count_with_unresolved_entries_is_unknown(self):
        m = wp.check_membership([{"id": 1}], 999, declared_count=None, entries=5)
        self.assertIsNone(m.present)

    def test_empty_song_id_is_unknown(self):
        m = wp.check_membership([{"id": 1}], "")
        self.assertIsNone(m.present)
        self.assertIn("song_id 为空", m.reason)

    def test_declared_bool_is_ignored(self):
        self.assertIsNone(wp.check_membership([], 1, declared_count=True).declared)


class TestParseActionResult(unittest.TestCase):
    """dispatch 返回值的多形状判读。核心不变量：没有明确成功标记 → 绝不 True。"""

    def test_none_is_undecidable(self):
        r = wp.parse_action_result(None)
        self.assertIsNone(r.ok)
        self.assertIn("回查", r.reason)

    def test_bool_passthrough(self):
        self.assertIs(wp.parse_action_result(True).ok, True)
        self.assertIs(wp.parse_action_result(False).ok, False)

    def test_numeric_codes(self):
        for code in (200, 0, 20000):
            self.assertIs(wp.parse_action_result(code).ok, True, code)
        for code in (400, 403, -1, 301):
            self.assertIs(wp.parse_action_result(code).ok, False, code)

    def test_string_shapes(self):
        self.assertIs(wp.parse_action_result("ok").ok, True)
        self.assertIs(wp.parse_action_result("success").ok, True)
        self.assertIs(wp.parse_action_result("error").ok, False)
        self.assertIs(wp.parse_action_result("failed").ok, False)
        self.assertIs(wp.parse_action_result("200").ok, True)
        self.assertIs(wp.parse_action_result("500").ok, False)
        self.assertIsNone(wp.parse_action_result("").ok)
        self.assertIsNone(wp.parse_action_result("echo:whatever").ok)
        self.assertIs(wp.parse_action_result('{"code":200}').ok, True)
        self.assertIs(wp.parse_action_result('{"code":400}').ok, False)

    def test_dict_shapes(self):
        self.assertIs(wp.parse_action_result({"code": 200}).ok, True)
        self.assertIs(wp.parse_action_result({"code": 404}).ok, False)
        self.assertIs(wp.parse_action_result({"statusCode": 200}).ok, True)
        self.assertIs(wp.parse_action_result({"ok": True}).ok, True)
        self.assertIs(wp.parse_action_result({"ok": False}).ok, False)
        self.assertIs(wp.parse_action_result({"success": True}).ok, True)
        self.assertIs(wp.parse_action_result({"error": "boom"}).ok, False)
        self.assertIs(wp.parse_action_result({"msg": "失败：无权限"}).ok, False)
        self.assertIs(wp.parse_action_result({"message": "ok"}).ok, True)
        self.assertIs(wp.parse_action_result({"result": {"code": 200}}).ok, True)

    def test_redux_state_is_undecidable_not_success(self):
        """附录 B 记的「dispatch 返回新 state」就是这种 —— 不能当成成功。"""
        state = {"user": {}, "playlist": {}, "song": {}, "ui": {}, "search": {}}
        r = wp.parse_action_result(state)
        self.assertIsNone(r.ok)
        self.assertEqual(r.kind, "state")
        self.assertIn("看不出", r.reason)

    def test_envelope_from_injected_js(self):
        ok = wp.parse_action_result({"called": True, "via": "fiber.memoizedProps.dispatch",
                                     "threw": False, "error": None,
                                     "returned": {"code": 200}})
        self.assertIs(ok.ok, True)
        thr = wp.parse_action_result({"called": False, "threw": True,
                                      "error": "TypeError: x is not a function"})
        self.assertIs(thr.ok, False)
        self.assertIn("TypeError", thr.reason)
        nod = wp.parse_action_result({"called": False, "threw": False,
                                      "error": "no-dispatch"})
        self.assertIs(nod.ok, False)
        self.assertIn("no-dispatch", nod.reason)
        # 调用了、但返回值判不出来 → 依然不可判定（不许乐观）
        self.assertIsNone(wp.parse_action_result(
            {"called": True, "threw": False, "error": None, "returned": None}).ok)
        self.assertIsNone(wp.parse_action_result(
            {"called": True, "threw": False, "error": None,
             "returned": {"__dcdp_kind__": "object", "keys": ["a"], "picked": {}}}).ok)

    def test_unknown_shapes_are_never_success(self):
        for raw in ([1, 2, 3], {"a": 1}, {"__dcdp_kind__": "object"},
                    object(), 3.5, {"msg": "不确定的话"}):
            with self.subTest(raw=raw):
                self.assertIsNot(wp.parse_action_result(raw).ok, True)
        # 不是数字码、也不是明确成败的 → 判「不知道」，不是判「失败」
        for raw in ([1, 2, 3], {"a": 1}, object(), 3.5, {"msg": "不确定的话"}):
            with self.subTest(raw=raw):
                self.assertIsNone(wp.parse_action_result(raw).ok)

    def test_never_true_without_an_explicit_marker(self):
        """把「不是成功」的东西都灌进去，只要有任何一个被判成 True 就是灾难。"""
        suspicious = [
            None, "", "   ", [], {}, {"foo": "bar"}, [{"code": 200}],
            {"code": None}, {"code": "abc"}, {"ok": "yes"}, {"success": 1},
            {"error": None}, {"msg": ""}, {"status": None}, 3.5, "pending", "unknown",
        ]
        for raw in suspicious:
            with self.subTest(raw=raw):
                self.assertIsNot(wp.parse_action_result(raw).ok, True)

    def test_summary_is_readable(self):
        s = wp.parse_action_result({"code": 200}).summary()
        self.assertIn("自报成功", s)
        self.assertIn("code", s)


class _FakeClock:
    """假时钟：sleep 会推进它，于是限速可以离线断言。"""

    def __init__(self, t: float = 1000.0):
        self.t = t
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


class TestRateLimiter(unittest.TestCase):
    """§7.4：写操作必须限速（默认 1 req/s）。"""

    def test_default_is_the_project_constant(self):
        from animusic import constants

        self.assertEqual(wp.WritePolicy().rate_rps, constants.WRITE_RATE_RPS)
        self.assertEqual(wp.RateLimiter().rps, constants.WRITE_RATE_RPS)
        self.assertEqual(wp.RateLimiter().interval, 1.0)

    def test_one_rps_waits_one_second_between_writes(self):
        c = _FakeClock()
        rl = wp.RateLimiter(rps=1.0, clock=c.now, sleep=c.sleep)
        self.assertEqual(rl.wait(), 0.0)          # 第一次不等
        self.assertAlmostEqual(rl.wait(), 1.0)
        self.assertAlmostEqual(rl.wait(), 1.0)
        self.assertEqual(c.slept, [1.0, 1.0])
        self.assertAlmostEqual(c.t, 1002.0)

    def test_two_rps(self):
        c = _FakeClock()
        rl = wp.RateLimiter(rps=2.0, clock=c.now, sleep=c.sleep)
        rl.wait()
        self.assertAlmostEqual(rl.wait(), 0.5)
        self.assertEqual(rl.interval, 0.5)

    def test_zero_or_negative_means_unlimited(self):
        for rps in (0.0, -1.0):
            c = _FakeClock()
            rl = wp.RateLimiter(rps=rps, clock=c.now, sleep=c.sleep)
            self.assertEqual([rl.wait() for _ in range(4)], [0.0] * 4)
            self.assertEqual(c.slept, [])
            self.assertEqual(rl.interval, 0.0)

    def test_reset(self):
        c = _FakeClock()
        rl = wp.RateLimiter(rps=1.0, clock=c.now, sleep=c.sleep)
        rl.wait()
        rl.reset()
        self.assertEqual(rl.wait(), 0.0)


# ═════════════════════════════════════════════════════════════════════ 假 CDP


class _WriteResponses(_PrefixResponses):
    """按表达式前缀分派响应；额外支持：

    · `state` / `state_provider` —— 让 store 的「当前状态」能随测试推进而变化
      （真实客户端就是这样：dispatch 之后 store 才更新，且可能晚一拍）
    · 值为 callable 时就调用它 —— 用来模拟「dispatch 的副作用」
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.state: dict = {}
        self.state_provider = None          # None → 用 self.state

    def __getitem__(self, key: str):
        m = self._match(key)
        if m is None:
            raise KeyError(key)
        if m == dc.JS_MARKERS["state"]:
            st = self.state_provider() if self.state_provider is not None else self.state
            return json.dumps(st, ensure_ascii=False)
        val = dict.__getitem__(self, m)
        return val(key) if callable(val) else val


def _health(hidden: bool = False) -> str:
    return json.dumps({
        "hasStore": True, "hidden": hidden, "title": "网易云音乐",
        "url": "orpheus://orpheus/pub/app.html",
        "topKeys": ["user", "playlist", "song"],
        "user": {"userId": 1000001, "nickname": "HRichardY", "vipType": 11},
        "error": None, "diag": {"via": "ancestor", "depth": 4},
    }, ensure_ascii=False)


def _dispatch_ok(**returned) -> str:
    return json.dumps({
        "called": True, "via": "fiber.memoizedProps.dispatch", "where": "dom",
        "depth": 4, "threw": False, "error": None, "patchedRawEvent": True,
        "returned": returned or {"code": 200},
    }, ensure_ascii=False)


def _add_song_to_state(state: dict, song_id: str, playlist_index: int = 0) -> None:
    """模拟客户端真的把歌加进去了：曲目条目 + 歌曲对象 + trackCount 都更新。"""
    pl = state["playlist"]["myPlaylist"][playlist_index]
    pl.setdefault("trackIds", []).append({"id": int(song_id) if song_id.isdigit() else song_id})
    pl["trackCount"] = int(pl.get("trackCount") or 0) + 1
    state.setdefault("song", {}).setdefault("songs", {})[str(song_id)] = {
        "id": int(song_id) if song_id.isdigit() else song_id,
        "name": f"Song {song_id}", "ar": [{"id": 1, "name": "A"}],
        "al": {"id": 1, "name": "Al"}, "dt": 200000, "fee": 0,
    }


@unittest.skipUnless(HAS_WS, "需要 websockets")
class TestDesktopWritesAgainstFakeCDP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeCDP().start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()

    def setUp(self):
        self.state = _fake_state()
        self.fake.tabs.clear()
        self.fake.eval_log.clear()
        self.fake.expect_exception = False
        resp = _WriteResponses({
            "document.readyState": "complete",
            "document.hidden": False,
            dc.JS_MARKERS["health"]: _health(),
            dc.JS_MARKERS["state"]: "{}",           # 值被 __getitem__ 覆盖
        })
        resp.state = self.state
        self.responses = resp
        self.fake.responses = resp
        CDPBrowser(self.fake.http_port).open_tab("orpheus://orpheus/pub/app.html")

    # ------------------------------------------------------------ 工具

    def _collector(self, **kw) -> dc.DesktopCDPCollector:
        kw.setdefault("write_policy", wp.WritePolicy(
            rate_rps=0.0, confirm_timeout=0.0, confirm_poll=0.01))
        # 单测不联网：曲目的 web API 路径关掉（它有自己的测试文件）。
        # 关掉后 _web_membership 返回 None → _membership 原样返回 store 的结论，
        # 也就是「加 web API 之前」的行为，这些断言不需要改语义。
        kw.setdefault("use_web_playlist", False)
        return dc.DesktopCDPCollector(self.fake.http_port, timeout=5.0, **kw)

    def _dispatches(self) -> list[str]:
        return [e for e in self.fake.eval_log if e.startswith(dc.JS_MARKERS["dispatch"])]

    def _probes(self) -> list[str]:
        return [e for e in self.fake.eval_log if e.startswith(dc.JS_MARKERS["write_probe"])]

    def _ref(self, name: str = "26年10月", pid: str = "1001") -> PlaylistRef:
        return PlaylistRef(name=name, netease_id=pid)

    # ------------------------------------------------------------ ensure_playlist：复用

    def test_ensure_playlist_reuses_existing_and_never_dispatches(self):
        c = self._collector()
        ref = c.ensure_playlist("26年10月")
        self.assertEqual(ref.netease_id, "1001")
        self.assertEqual(ref.name, "26年10月")
        self.assertEqual(self._dispatches(), [], "命中已有歌单时不该发任何创建请求")
        self.assertIn("复用", c.last_write_note)
        self.assertIsNone(c.last_write_error)

    def test_ensure_playlist_reuses_via_fuzzy_name(self):
        c = self._collector()
        for alias in ("26.10", "2026年10月", "26年10月份", "26Q4"):
            with self.subTest(alias=alias):
                self.assertEqual(c.ensure_playlist(alias).netease_id, "1001")
        self.assertEqual(self._dispatches(), [])

    def test_ensure_playlist_refuses_empty_name(self):
        with self.assertRaises(dc.DesktopCDPError):
            self._collector().ensure_playlist("   ")
        with self.assertRaises(dc.DesktopCDPError):
            self._collector().ensure_playlist(None)  # type: ignore[arg-type]

    # ------------------------------------------------------------ ensure_playlist：创建

    def test_ensure_playlist_creates_then_confirms_by_relisting(self):
        def on_dispatch(expr):
            self.state["playlist"]["myPlaylist"].append({
                "id": 2001, "name": "26年1月", "trackCount": 0,
                "creator": {"userId": 1000001}})
            return _dispatch_ok()

        self.responses[dc.JS_MARKERS["dispatch"]] = on_dispatch
        c = self._collector()
        ref = c.ensure_playlist("26年1月")
        self.assertEqual(ref.netease_id, "2001")
        self.assertEqual(c.last_create_variant, 0)
        self.assertEqual(len(self._dispatches()), 1)
        self.assertIn("创建成功", c.last_write_note)
        self.assertIsNone(c.last_write_error)

    def test_ensure_playlist_raises_when_the_playlist_never_appears(self):
        """**核心哨兵**：dispatch 明明"成功"了，但回查看不到歌单 → 必须抛错。"""
        self.responses[dc.JS_MARKERS["dispatch"]] = _dispatch_ok()
        c = self._collector()
        with self.assertRaises(dc.DesktopCDPError) as ctx:
            c.ensure_playlist("26年7月")
        msg = str(ctx.exception)
        self.assertIn("26年7月", msg)
        self.assertIn("回查", msg)
        self.assertIn("创建歌单", msg)
        # 试了全部（默认 2 条）创建候选，且每次都回查过
        self.assertEqual(len(self._dispatches()), wp.WritePolicy().max_create_variants)
        self.assertIsNotNone(c.last_write_error)
        self.assertIsNone(c.last_create_variant)

    def test_ensure_playlist_reports_the_side_effect_instead_of_hiding_it(self):
        """客户端真的建了个歌单、但名字对不上 —— 必须报出来，不能装作什么都没发生。"""
        def on_dispatch(expr):
            self.state["playlist"]["myPlaylist"].append({
                "id": 3001, "name": "我自己起的名字", "trackCount": 0,
                "creator": {"userId": 1000001}})
            return _dispatch_ok()

        self.responses[dc.JS_MARKERS["dispatch"]] = on_dispatch
        c = self._collector()
        with self.assertRaises(dc.DesktopCDPError) as ctx:
            c.ensure_playlist("26年7月")
        self.assertIn("我自己起的名字", str(ctx.exception))
        self.assertIn("别重复创建", str(ctx.exception))

    def test_ensure_playlist_does_not_trust_dispatch_error_text_to_stop_early(self):
        """dispatch 报错也要走完「回查」—— 万一动作其实生效了呢（不许猜，只许查）。"""
        self.responses[dc.JS_MARKERS["dispatch"]] = json.dumps(
            {"called": False, "threw": True, "error": "TypeError: boom"})
        c = self._collector()
        with self.assertRaises(dc.DesktopCDPError) as ctx:
            c.ensure_playlist("26年7月")
        self.assertIn("TypeError", str(ctx.exception))

    def test_ensure_playlist_fails_when_window_is_hidden(self):
        self.responses[dc.JS_MARKERS["health"]] = _health(hidden=True)
        c = self._collector()
        with self.assertRaises(dc.DesktopCDPError) as ctx:
            c.ensure_playlist("26年7月")
        self.assertIn("隐藏", str(ctx.exception))
        self.assertEqual(self._dispatches(), [])

    # ------------------------------------------------------------ add_to_playlist：幂等

    def test_add_to_playlist_is_idempotent_and_skips_dispatch(self):
        c = self._collector()
        self.assertTrue(c.add_to_playlist("501", self._ref()))
        self.assertEqual(self._dispatches(), [], "已在歌单里就该直接跳过，不发写入请求")
        self.assertIn("已经在", c.last_write_note)
        self.assertIsNone(c.last_write_error)

    def test_add_to_playlist_idempotent_for_int_ids(self):
        c = self._collector()
        self.assertTrue(c.add_to_playlist(501, self._ref()))
        self.assertTrue(c.add_to_playlist("502", self._ref()))
        self.assertTrue(c.add_to_playlist(502, self._ref()))     # 重复调用仍然只跳过
        self.assertEqual(self._dispatches(), [])

    # ------------------------------------------------------------ add_to_playlist：写入

    def test_add_to_playlist_dispatches_and_confirms(self):
        def on_dispatch(expr):
            _add_song_to_state(self.state, "999")
            return _dispatch_ok()

        self.responses[dc.JS_MARKERS["dispatch"]] = on_dispatch
        c = self._collector()
        self.assertTrue(c.add_to_playlist("999", self._ref()))
        self.assertEqual(c.last_add_variant, 0, "第一条候选就该成功")
        disp = self._dispatches()
        self.assertEqual(len(disp), 1)
        # 第一发必须就是**实测那条**（favorite），不许再盲试
        self.assertIn(wp.ACTION_FAVORITE, disp[0])
        self.assertIn('"999"', disp[0])
        self.assertIn("成功", c.last_write_note)
        self.assertIsNone(c.last_write_error)

    def test_add_to_playlist_waits_for_the_store_to_refresh(self):
        """真实客户端不会在 dispatch 返回时就把 store 更新好 —— 必须轮询回查。"""
        old = copy.deepcopy(self.state)

        def on_dispatch(expr):
            _add_song_to_state(self.state, "999")
            return _dispatch_ok()

        self.responses[dc.JS_MARKERS["dispatch"]] = on_dispatch
        reads = {"n": 0}

        def provider():
            reads["n"] += 1
            return old if reads["n"] <= 2 else self.state     # 预检 + 第一次回查都是旧的

        self.responses.state_provider = provider
        c = self._collector(write_policy=wp.WritePolicy(
            rate_rps=0.0, confirm_timeout=1.0, confirm_poll=0.02))
        self.assertTrue(c.add_to_playlist("999", self._ref()))
        self.assertGreaterEqual(reads["n"], 3, "应该轮询到了第二次回查")

    def test_add_to_playlist_returns_false_when_recheck_never_shows_it(self):
        """**核心哨兵**：dispatch 返回 code=200，但回查看不到歌 → 必须 False。"""
        self.responses[dc.JS_MARKERS["dispatch"]] = _dispatch_ok()
        c = self._collector()
        self.assertFalse(c.add_to_playlist("999", self._ref()))
        self.assertIsNotNone(c.last_write_error)
        self.assertIn("回查", c.last_write_error)
        self.assertIn("999", c.last_write_error)
        # 默认只打实测那一发 —— 证据见 max_add_variants 的注释
        self.assertEqual(len(self._dispatches()), 1)

    def test_add_to_playlist_retries_every_variant_when_explicitly_asked(self):
        """显式要求全试时才逐条试完（含对照候选），且每次都回查。"""
        self.responses[dc.JS_MARKERS["dispatch"]] = _dispatch_ok()
        c = self._collector(write_policy=wp.WritePolicy(
            rate_rps=0.0, confirm_timeout=0.0, confirm_poll=0.01,
            max_add_variants=None))
        self.assertFalse(c.add_to_playlist("999", self._ref()))
        self.assertIn("回查", c.last_write_error or "")
        self.assertEqual(len(self._dispatches()), len(wp.ADD_PAYLOAD_VARIANTS))

    def test_add_to_playlist_pinned_variant_only_tries_that_one(self):
        def on_dispatch(expr):
            _add_song_to_state(self.state, "999")
            return _dispatch_ok()

        self.responses[dc.JS_MARKERS["dispatch"]] = on_dispatch
        c = self._collector(add_variant=3)
        self.assertTrue(c.add_to_playlist("999", self._ref()))
        self.assertEqual(c.last_add_variant, 3)
        self.assertEqual(len(self._dispatches()), 1)

    def test_add_to_playlist_hint_is_reused_for_the_next_song(self):
        """某条候选成功过 → 下一首先试它（否则每首都要盲试 6 次 × 1 秒）。"""
        winner = 4

        def on_dispatch(expr):
            _add_song_to_state(self.state, "999")
            return _dispatch_ok()

        self.responses[dc.JS_MARKERS["dispatch"]] = on_dispatch
        c = self._collector(add_variant=winner)
        self.assertTrue(c.add_to_playlist("999", self._ref()))
        self.assertEqual(c.last_add_variant, winner)
        self.assertEqual(c._add_variant_hint, winner)

        # 下一首（换一首已经不在歌单里的歌）应该直接用 hint，不再从头盲试
        self.state["song"]["songs"]["998"] = {"id": 998, "name": "S2", "ar": [], "dt": 1}
        self.fake.eval_log.clear()

        def on_dispatch2(expr):
            _add_song_to_state(self.state, "998")
            return _dispatch_ok()

        self.responses[dc.JS_MARKERS["dispatch"]] = on_dispatch2
        self.assertTrue(c.add_to_playlist("998", self._ref()))
        self.assertEqual(c.last_add_variant, winner)
        self.assertEqual(len(self._dispatches()), 1, "有 hint 时不该再盲试前面的候选")

    def test_add_to_playlist_returns_false_when_dispatch_throws_js(self):
        self.responses[dc.JS_MARKERS["dispatch"]] = json.dumps(
            {"called": False, "threw": True, "error": "TypeError: x is not a function"})
        c = self._collector()
        self.assertFalse(c.add_to_playlist("999", self._ref()))
        self.assertIn("TypeError", c.last_write_error)
        self.assertIn("回查", c.last_write_error)

    def test_add_to_playlist_returns_false_when_dispatch_expression_is_not_json(self):
        """假服务器对未知表达式回 "echo:..." —— 解析不了就必须失败（不能乐观）。"""
        c = self._collector()
        self.assertFalse(c.add_to_playlist("999", self._ref()))
        self.assertTrue(self._dispatches(), "应该真的发过 dispatch")
        self.assertIsNotNone(c.last_write_error)

    # ------------------------------------------------------------ add_to_playlist：拒绝写入

    def test_add_to_playlist_refuses_without_playlist_id(self):
        c = self._collector()
        self.assertFalse(c.add_to_playlist("999", PlaylistRef(name="26年10月")))
        self.assertEqual(self._dispatches(), [])
        self.assertIn("netease_id", c.last_write_error)

    def test_add_to_playlist_refuses_empty_song_id_and_bad_type(self):
        c = self._collector()
        for bad in ("", "   ", None):
            with self.subTest(bad=bad):
                self.assertFalse(c.add_to_playlist(bad, self._ref()))
                self.assertIn("song_id", c.last_write_error or "")
        self.assertFalse(c.add_to_playlist("999", "26年10月"))  # type: ignore[arg-type]
        self.assertIn("PlaylistRef", c.last_write_error or "")
        self.assertFalse(c.add_to_playlist("999", None))        # type: ignore[arg-type]
        self.assertEqual(self._dispatches(), [])

    def test_add_to_playlist_refuses_when_track_data_is_incomplete(self):
        """**核心哨兵**：判不出来「在不在歌单里」时，连 dispatch 都不许发。"""
        c = self._collector()
        # 1003：trackCount=1 但 store 里没有任何曲目条目 → check_membership 返回 None
        self.assertFalse(c.add_to_playlist("999", self._ref("26年4月", "1003")))
        self.assertEqual(self._dispatches(), [])
        self.assertIn("无法确认", c.last_write_error)
        self.assertIn("打开这个歌单", c.last_write_error)

    def test_add_to_playlist_refuses_when_playlist_is_not_in_store(self):
        c = self._collector()
        self.assertFalse(c.add_to_playlist("999", self._ref("26年7月", "7777")))
        self.assertEqual(self._dispatches(), [])
        self.assertIn("找不到歌单", c.last_write_error)

    def test_add_to_playlist_returns_false_when_window_is_hidden(self):
        self.responses[dc.JS_MARKERS["health"]] = _health(hidden=True)
        c = self._collector()
        self.assertFalse(c.add_to_playlist("999", self._ref()))
        self.assertIn("隐藏", c.last_write_error)
        self.assertEqual(self._dispatches(), [])

    def test_add_to_playlist_returns_false_when_the_client_is_gone(self):
        """端口没监听（客户端没开）→ 明确失败，绝不静默成功。"""
        c = dc.DesktopCDPCollector(_free_port(), timeout=2.0)
        self.assertFalse(c.add_to_playlist("999", self._ref()))
        self.assertIsNotNone(c.last_write_error)
        self.assertEqual(self._dispatches(), [])

    def test_add_to_playlist_returns_false_when_js_always_throws(self):
        self.fake.expect_exception = True
        c = self._collector()
        self.assertFalse(c.add_to_playlist("999", self._ref()))
        self.assertIsNotNone(c.last_write_error)
        self.assertEqual(self._dispatches(), [])

    def test_add_to_playlist_with_zero_variants_fails_loudly(self):
        c = self._collector(write_policy=wp.WritePolicy(
            rate_rps=0.0, confirm_timeout=0.0, max_add_variants=0))
        self.assertFalse(c.add_to_playlist("999", self._ref()))
        self.assertIn("max_add_variants", c.last_write_error)

    # ------------------------------------------------------------ 限速

    def test_every_dispatch_goes_through_the_rate_limiter(self):
        class _CountingLimiter:
            def __init__(self):
                self.calls = 0
                self.slept = 0.0

            def wait(self):
                self.calls += 1
                self.slept += 0.25
                return 0.25

        lim = _CountingLimiter()
        c = self._collector(rate_limiter=lim)
        self.assertFalse(c.add_to_playlist("999", self._ref()))
        self.assertEqual(lim.calls, len(self._dispatches()),
                         "每次会改状态的 dispatch 都必须先过限速器")
        self.assertGreater(lim.calls, 0)
        self.assertAlmostEqual(c.last_write_wait, 0.25)

    def test_default_rate_limiter_is_one_rps(self):
        """默认构造（不传 policy）必须用 constants.WRITE_RATE_RPS 限速。"""
        from animusic import constants

        c = dc.DesktopCDPCollector(self.fake.http_port, timeout=5.0)
        self.assertEqual(c.write_policy.rate_rps, constants.WRITE_RATE_RPS)
        self.assertEqual(c.rate_limiter.rps, constants.WRITE_RATE_RPS)
        self.assertEqual(c.rate_limiter.interval, 1.0)

    # ------------------------------------------------------------ 探针

    def test_write_probe_returns_offline_variants_and_live_facts(self):
        live = {"ok": True, "dispatch": {"via": "fiber.memoizedProps.dispatch"},
                "menuWrappers": [{"component": "MenuWrapper",
                                  "data": {"type": "object", "keys": ["id", "type"]}}],
                "songRows": [], "globals": {}, "errors": []}
        self.responses[dc.JS_MARKERS["write_probe"]] = json.dumps(live, ensure_ascii=False)
        c = self._collector()
        report = c.probe_write_path(song_id="501", playlist_id="1001")
        self.assertIsNone(report["live_error"])
        self.assertEqual(report["live"], live)
        self.assertEqual(c.last_write_probe, live)
        self.assertEqual(len(report["offline"]),
                         len(wp.ADD_PAYLOAD_VARIANTS) + len(wp.CREATE_PAYLOAD_VARIANTS))
        self.assertIn("501", json.dumps(report["offline"], ensure_ascii=False))
        self.assertEqual(len(self._probes()), 1)

    def test_write_probe_reports_the_error_but_still_returns_offline(self):
        report = dc.DesktopCDPCollector(_free_port(), timeout=2.0).probe_write_path()
        self.assertIsNotNone(report["live_error"])
        self.assertIn("未监听", report["live_error"])
        self.assertTrue(report["offline"])

    def test_probe_js_is_never_confused_with_a_write(self):
        """探针只读：跑完探针不该产生任何 dispatch。"""
        self.responses[dc.JS_MARKERS["write_probe"]] = json.dumps({"ok": False})
        c = self._collector()
        c.probe_write_path()
        self.assertEqual(self._dispatches(), [])

    # ------------------------------------------------------------ 诊断信息

    def test_diagnostics_are_populated_for_the_gui(self):
        def on_dispatch(expr):
            _add_song_to_state(self.state, "999")
            return _dispatch_ok()

        self.responses[dc.JS_MARKERS["dispatch"]] = on_dispatch
        c = self._collector()
        self.assertTrue(c.add_to_playlist("999", self._ref()))
        diag = c.last_dispatch_diag
        self.assertEqual(diag["action_type"], wp.ACTION_TYPE_DO_ACTION)
        self.assertIn("via", diag)
        self.assertIn("payload_keys", diag)

    def test_failure_leaves_the_collector_in_a_diagnosable_state(self):
        self.responses[dc.JS_MARKERS["dispatch"]] = _dispatch_ok()
        c = self._collector()
        self.assertFalse(c.add_to_playlist("999", self._ref()))
        self.assertIsNotNone(c.last_dispatch_diag)
        self.assertEqual(c.last_dispatch_diag["threw"], False)
        self.assertIsNone(c.last_add_variant)
        self.assertIn("回查", c.last_write_error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
