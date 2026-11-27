"""离线验证玩家的三条终局目标（2026-11-24）。

    目标 ①：每个周六、周日都检查有没有笔记
    目标 ②：周一到周六每天都训练，而且不失败（精力规划）
    目标 ③：最终得分稳定 9000+（在 test_core / 摘要里体现，这里只验前两条）

    .venv\\Scripts\\python.exe tools\\test_goal.py

不碰游戏：直接拿 AutoTask 的 _do_daily 跑假 OCR 结果，看它点了哪个按钮。
配置从**真的** config/strategy.yaml 读（_tuning 未命中缓存时会自己读盘），
所以这个测试同时也在验证配置文件本身写得对。
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.tasks.AutoTask import AutoTask  # noqa: E402

FRAME = np.zeros((900, 1600, 3), dtype="uint8")
ok = True


def check(name, got, want):
    global ok
    good = got == want
    ok = ok and good
    print(f"  [{'OK' if good else 'FAIL'}] {name}: {got}" + ("" if good else f"（期望 {want}）"))
    return good


def make_stub(weekday, stamina=80, buttons=("训练", "出行")):
    """造一个只会点按钮的 AutoTask：记录点到的按钮文字，跑真配置。"""
    s = AutoTask.__new__(AutoTask)
    s.logs = []
    s.clicks = []
    s.log_info = lambda *a, **k: s.logs.append(" ".join(str(x) for x in a))
    s.log_warning = lambda *a, **k: s.logs.append("WARN " + " ".join(str(x) for x in a))
    s.log_error = lambda *a, **k: s.logs.append("ERR " + " ".join(str(x) for x in a))
    s._tuning_cache = None                 # ← 强制读真配置
    s.config = {"训练精力下限": 12}
    s.weekday = weekday
    s.stamina, s.stamina_max = stamina, 130
    s.shop_day = None
    s.shop_week = None
    s.shop_bought = 0
    s.trip_probe = False
    s.trip_probe_done = None
    s.trip_probe_disabled = False
    s.trip_back_tries = 0
    s.notes_done = False
    s.trip_picked = False
    s.choice_idx = 0
    s._read_week = lambda items: (3, weekday)
    s._day_key = lambda items: f"3-{weekday}"
    s._shop_button = lambda items, frame: None
    s._want_shop = lambda items, frame=None, shop=None: False
    s._event_cards = lambda frame, items: []
    s.sleep = lambda x: None
    s._shot = lambda *a, **k: None
    s._alert_popup = lambda *a, **k: None
    s._find = lambda items, words, exact=False: None
    s._click_item = lambda it, why="": s.clicks.append(it["t"])
    s._click_btn = lambda it, why="", frame=None: s.clicks.append(it["t"])
    s.click_relative = lambda *a, **k: s.clicks.append("rel")
    s.click = lambda *a, **k: s.clicks.append("click")

    def _btn_in_area(items, words, frame, exact=True):
        for w in words:
            if w in buttons:
                return {"t": w, "cx": 10, "cy": 10}
        return None

    s._btn_in_area = _btn_in_area
    return s


print("== 0. 真配置读出来是什么 ==")
s0 = make_stub(1)
goal = s0._tuning().get("goal") or {}
trip = s0._tuning().get("trip") or {}
core = s0._tuning().get("core_score") or {}
check("goal.train_days", goal.get("train_days"), [1, 2, 3, 4, 5, 6])
check("goal.notes_days", goal.get("notes_days"), [6, 7])
check("goal.rest_days", goal.get("rest_days"), [7])
check("goal.stamina_hard_min", goal.get("stamina_hard_min"), 1)
check("trip.probe_days", trip.get("probe_days"), [6, 7])
check("core_score.fail_limit（可赌到 30%）", core.get("fail_limit"), 30)
check("core_score.strict_fallback（原方案）", core.get("strict_fallback"), "old")

print("\n== 1. 工作日（周三）不探笔记，直接训练 ==")
s = make_stub(3)
s._do_daily([], FRAME)
check("点了训练", s.clicks[-1], "训练")
check("没点出行（工作日不探笔记）", "出行" in s.clicks, False)
check("日志说明为什么不探", any("不探笔记" in x for x in s.logs), True)

print("\n== 2. 周六：先探笔记，回来再训练（测试里只验第一步是出行）==")
s = make_stub(6)
s._do_daily([], FRAME)
check("第一步点出行探笔记", s.clicks[-1], "出行")
# 2026-11-24 改口径：点出行只是发起，**真进了出行页才记账**（第七周周六就是
# 点了没进去却被当成探完，于是整天没检查笔记）。
check("点完还没记账（等进出行页）", s.trip_probe_done, None)
# 模拟「已真的进过出行页并退回」：手动记账后应转去训练
s.trip_probe_done = "3-6"
s.trip_probe = False
s._do_daily([], FRAME)
check("同一帧再跑一次 → 转去训练", s.clicks[-1], "训练")

print("\n== 3. 周日：不训练，出行去公园补精力 ==")
s = make_stub(7, stamina=120, buttons=("训练", "出行", "休息"))
s._do_daily([], FRAME)
check("周日点的是出行（公园）", s.clicks[-1], "出行")
check("周日没点训练", "训练" in s.clicks, False)

print("\n== 4. 训练日精力见底也照练（hard_min=1，旧门槛 12/40 会挡住）==")
s = make_stub(2, stamina=3)
s._do_daily([], FRAME)
check("精力 3 仍然点训练", s.clicks[-1], "训练")

print("\n== 5. 周日没有出行入口 → 才退到休息 ==")
s = make_stub(7, stamina=40, buttons=("休息",))
s._do_daily([], FRAME)
check("点休息兜底", s.clicks[-1], "休息")

print("\n== 6. 周六探过笔记 + 精力不足 → 先出行（去公园），不是休息 ==")
s = make_stub(6, stamina=1, buttons=("训练", "出行", "休息"))
s.trip_probe_done = "3-6"   # 今天已经探过
s._do_daily([], FRAME)
check("精力 1 但仍点训练（训练日硬门槛 1）", s.clicks[-1], "训练")

print("\n" + ("=== 全部通过 ===" if ok else "=== 有失败项 ==="))
sys.exit(0 if ok else 1)
