"""「探笔记」新链路的离线单测（不打游戏、不依赖真实窗口）。

背景（2026-11-23 21:01 玩家确认 + 实机验证）：
  笔记不在出行页的地点标签上，而在「出行 → 步行街 → 扭蛋三选一」里出现
  【笔记 / 继承】。旧逻辑只扫地点标签（步行街标签是「随机增益+」，被
  GAIN_KEYS 过滤成空），永远判「没有笔记」，然后 _leave() 按 ESC 又退不出去，
  于是 trip_probe_disabled=True 永久禁用，周六周日连出行都不点了。

实机验证（21:04）：右上角 (0.95, 0.046) 的 ↩ 连点两次
  扭蛋三选一 → 出行页 → 每日页，精力仍 80/130，训练按钮还在。

验证：
  1. _leave_top_right 点的是右上角返回箭头，不是 ESC；
  2. 探笔记时真的点「步行街」，不是扫标签；
  3. 扭蛋三选一无【笔记/继承】→ 两级右上角退回；有 → 选它并记账；
  4. 退回失败不再永久禁用后续天数；
  5. 周六有训练按钮时先探笔记，探完回每日页照常训练。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from src.tasks.AutoTask import AutoTask

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: 得到 {got!r}，期望 {want!r}")


def it(t, cx, cy):
    return {"t": t, "cx": cx, "cy": cy, "x": cx - 20, "y": cy - 10,
            "w": 40, "h": 20, "b": None, "rx": cx / 1920.0, "ry": cy / 1080.0}


FRAME = np.zeros((1080, 1920, 3), dtype="uint8")

# 出行页 OCR（21:03 实机）：步行街标签是「随机增益+」
TRIP_ITEMS = [
    it("第3周", 100, 42), it("星期六", 256, 44), it("80/130", 486, 42),
    it("游戏厅", 887, 488), it("属性+", 912, 560),
    it("公园", 1585, 630), it("精力+", 1617, 699),
    it("餐厅", 310, 778), it("零用钱+", 335, 848),
    it("步行街", 1082, 926), it("随机增益+", 1090, 996),
]
# 扭蛋三选一 OCR（21:04 实机），无笔记
EGG_NO = [
    it("第3周", 100, 42), it("星期六", 256, 43),
    it("请选择1个扭蛋", 989, 196),
    it("弹跳升级包++", 483, 440), it("熟练度暴涨+", 964, 438),
    it("防守升级包++", 1443, 441),
    it("100", 507, 736), it("100", 988, 736), it("100", 1466, 736),
    it("确定", 955, 978),
]
# 有笔记的三选一
EGG_YES = [
    it("第3周", 100, 42), it("星期六", 256, 43),
    it("请选择1个扭蛋", 989, 196),
    it("笔记继承包", 483, 440), it("熟练度暴涨+", 964, 438),
    it("防守升级包++", 1443, 441),
    it("100", 507, 736), it("100", 988, 736), it("100", 1466, 736),
    it("确定", 955, 978),
]


def make_stub():
    s = AutoTask.__new__(AutoTask)
    s.logs = []
    s.clicked = []       # (t, why) 来自 _click_item
    s.coords = []        # (x, y) 来自 self.click
    s.log_info = lambda *a, **k: s.logs.append(" ".join(str(x) for x in a))
    s.log_warning = lambda *a, **k: s.logs.append("WARN " + " ".join(str(x) for x in a))
    s.click = lambda x, y: s.coords.append((int(x), int(y)))
    s._click_item = lambda i, why="": s.clicked.append((i["t"], why))
    s._click_btn = lambda i, why="", frame=None: s.clicked.append((i["t"], why))
    s._audit_save = lambda *a, **k: None
    s._read_week = lambda items: (3, "六")
    s.stat = {"note": 0}
    s.config = {"扭蛋目标关键词": "笔记,继承", "扭蛋最高价": 120,
                "等待秒数": 0.01, "训练精力下限": 12}
    s._tuning = lambda: {"trip": {"probe_notes_first": True}, "audit": {"enabled": False}}
    s._frame = lambda: None
    s._click_confirm = lambda items, frame: True
    s.sleep = lambda x: None
    s._find = lambda items, words, exact=True: None
    s._yellow_ratio = lambda frame, box: 0.0
    s._gap = lambda: 0
    s._excluded_place = lambda t: t == "游戏厅"
    s._place_tag = lambda n, items, frame: ""
    s._reward_key = lambda text: (0, 0, 0, 0)
    s._key_value = lambda key: 0.0
    s._shop_button = lambda items, frame: None
    s._want_shop = lambda items: False
    s._btn_in_area = lambda items, words, frame: None
    s.stamina, s.stamina_max = 80, 130
    s.shop_week = "3"
    s.shop_day = None
    s.trip_picked = False
    s.trip_probe = False
    s.trip_probe_done = None
    s.trip_probe_disabled = False
    s.trip_probe_back = 0
    s.trip_back_tries = 0
    s.notes_done = False
    return s


print("== 1. 右上角返回箭头（不是 ESC）==")
s = make_stub()
s._leave_top_right(TRIP_ITEMS, FRAME, "[测试]")
check("点 (0.95,0.046) → (1824,49)", s.coords, [(1824, 49)])
check("没有按 ESC / _leave", any("ESC" in x for x in s.logs), False)
check("日志带 [探笔记] 标记", any("[探笔记]" in x for x in s.logs), True)

print("\n== 2. 出行页探笔记：真点「步行街」，不扫标签 ==")
s = make_stub()
s.trip_probe = True
s._do_trip(TRIP_ITEMS, FRAME)
check("点了「步行街」", [c[0] for c in s.clicked], ["步行街"])
check("剩余要退两层", s.trip_probe_back, 2)
check("trip_probe 已消费", s.trip_probe, False)
check("日志写明点进步行街", any("点进「步行街」" in x for x in s.logs), True)
check("没有点别的地点", any(c[0] in ("公园", "餐厅", "游戏厅") for c in s.clicked), False)

print("\n== 3. 退第 2 层：出行页再点一次右上角 ==")
s = make_stub()
s.trip_probe_back = 1
s._do_trip(TRIP_ITEMS, FRAME)
check("点右上角一次", len(s.coords), 1)
check("退完归零", s.trip_probe_back, 0)
check("没有点任何地点", s.clicked, [])

print("\n== 4. 扭蛋三选一：无【笔记/继承】→ 退第 1 层 ==")
s = make_stub()
s.trip_probe_back = 2
s._do_egg_select(EGG_NO, FRAME)
check("点右上角一次", len(s.coords), 1)
check("剩 1 层", s.trip_probe_back, 1)
check("没有买任何扭蛋", s.clicked, [])
check("日志写明没有笔记/继承", any("没有「笔记/继承」" in x for x in s.logs), True)
check("日志写明回每日页走训练", any("回每日页走训练" in x for x in s.logs), True)

print("\n== 5. 扭蛋三选一：有【笔记继承】→ 选它并记账 ==")
s = make_stub()
s.trip_probe_back = 2
s._do_egg_select(EGG_YES, FRAME)
check("选了笔记扭蛋", [c[0] for c in s.clicked][:1], ["笔记继承包"])
check("stat.note +1", s.stat["note"], 1)
check("不退回（归零）", s.trip_probe_back, 0)
check("没有点右上角", s.coords, [])
check("日志写明命中", any("命中" in x for x in s.logs), True)

print("\n== 6. 退回失败不再永久禁用后续天数 ==")
s = make_stub()
s.trip_back_tries = 1
s.trip_probe_done = "3-六"
s._do_trip(TRIP_ITEMS, FRAME)
check("trip_probe_disabled 仍为 False", s.trip_probe_disabled, False)
check("日志说明只放弃今天", any("今天放弃探笔记" in x for x in s.logs), True)

print("\n== 7. 周六每日页：先探笔记，第二天记账后照常训练 ==")
DAILY = [it("第3周", 100, 42), it("星期六", 256, 42), it("80/130", 486, 44),
         it("训练", 1726, 718), it("出行", 1708, 930), it("商店", 1762, 234)]


def btn(items, words, frame):
    return next((x for x in items if x["t"] == words[0]), None)


s = make_stub()
s._btn_in_area = btn
s._shop_button = lambda items, frame: None
s._do_daily(DAILY, FRAME)
check("周六先点「出行」探笔记", [c[0] for c in s.clicked][:1], ["出行"])
check("当天已记账", s.trip_probe_done, "3-六")
check("置 trip_probe=True", s.trip_probe, True)

s2 = make_stub()
s2._btn_in_area = btn
s2.trip_probe_done = "3-六"
s2.trip_probe_back = 1          # 刚退回来的状态
s2._do_daily(DAILY, FRAME)
check("回来后清掉退回状态", s2.trip_probe_back, 0)
check("回来后点「训练」", [c[0] for c in s2.clicked][:1], ["训练"])
check("训练日志标注探笔记后", any("探笔记后-训练" in x for x in [c[1] for c in s2.clicked]), True)
check("日志写明已回每日页照常训练", any("已回到每日页" in x for x in s2.logs), True)

print("\n== 8. 周日每日页：没有训练也要先探笔记，再去公园 ==")
# 2026-11-23 22:14 玩家反馈「周日没有检查笔记，直接去公园了」。
# 旧逻辑把探笔记塞在「有训练且精力够」分支里，周日休息无训练 → 直接去公园，
# 整周最后一天从不检查笔记。现在探笔记提到所有行动分支之前。
SUNDAY = [it("第3周", 100, 42), it("星期日", 256, 42), it("8/130", 486, 44),
          it("出行", 1708, 930), it("休息", 1726, 718)]
s3 = make_stub()
s3._btn_in_area = btn
s3._shop_button = lambda items, frame: None
s3.stamina = 8
s3._read_week = lambda items: (3, "日")
s3._do_daily(SUNDAY, FRAME)
check("周日先点「出行」探笔记", [c[0] for c in s3.clicked][:1], ["出行"])
check("周日已记账 3-日", s3.trip_probe_done, "3-日")
check("周日置 trip_probe=True", s3.trip_probe, True)
check("周日没去点休息", all("休息" not in c[0] for c in s3.clicked), True)

s4 = make_stub()
s4._btn_in_area = btn
s4._shop_button = lambda items, frame: None
s4.stamina = 8
s4._read_week = lambda items: (3, "日")
s4.trip_probe_done = "3-日"      # 探完已退回每日页
s4._do_daily(SUNDAY, FRAME)
check("探完回每日页 → 出行去公园", [c[0] for c in s4.clicked][:1], ["出行"])
check("日志写明去公园补精力", any("去公园补精力" in x for x in s4.logs), True)
check("不会重复探笔记", s4.trip_probe, False)

print(f"\n=== 通过 {PASS} / 失败 {FAIL} ===")
sys.exit(1 if FAIL else 0)
