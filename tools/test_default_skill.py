"""「默认选中项不点击」的离线单测（不打游戏、不依赖真实窗口）。

背景（2026-11-23 实跑 14/14 验证）：训练核心页默认选中的是「投射」，
程序再点它一次，游戏当作确认开训，3~4 秒后直接跳训练结果，整次编队比较作废。

验证：
  1. _selected_skill 能从深蓝高亮胶囊认出当前选中项（浅蓝/橙底两种皮肤、认不出返回 None）；
  2. _do_training_core 里默认选中项只读不点、直接采进页面时的原始帧；
  3. 认不出高亮时走保守路径：投射既不点击也不参与排名，别的技能照常点；
  4. 最终选择就是默认选中项时，收尾不再点它，直接点「决定」。
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


# 真实 OCR 坐标（W1D4 171557_core1_Jump.txt）
ROWS = [
    {"t": "投射", "cx": 1649, "cy": 241},
    {"t": "弹跳", "cx": 1545, "cy": 374},
    {"t": "防守", "cx": 1655, "cy": 507},
    {"t": "体能", "cx": 1642, "cy": 639},
    {"t": "运控", "cx": 1616, "cy": 768},
    {"t": "意识", "cx": 1576, "cy": 904},
]
# 胶囊底色：浅蓝 BGR(233,204,124) 亮度187；深蓝选中 BGR(127,127,117) 亮度124
LIGHT_BLUE = (233, 204, 124)
LIGHT_ORANGE = (100, 150, 230)   # 另一套皮肤：橙底 R>B
DARK_BLUE = (127, 127, 117)


def make_frame(selected, light=LIGHT_BLUE):
    f = np.full((1080, 1920, 3), 40, dtype="uint8")
    for r in ROWS:
        x0, x1 = r["cx"] - 180, r["cx"] + 180
        y0, y1 = r["cy"] - 30, r["cy"] + 30
        f[y0:y1, x0:x1] = DARK_BLUE if r["t"] == selected else light
    return f


print("== 1. _selected_skill：从画面认当前高亮项 ==")
s = AutoTask.__new__(AutoTask)
check("投射深蓝 → 投射", s._selected_skill(make_frame("投射"), ROWS), "投射")
check("弹跳深蓝 → 弹跳", s._selected_skill(make_frame("弹跳"), ROWS), "弹跳")
check("意识深蓝 → 意识", s._selected_skill(make_frame("意识"), ROWS), "意识")
check("橙底皮肤也能认", s._selected_skill(make_frame("弹跳", LIGHT_ORANGE), ROWS), "弹跳")
check("无深蓝 → None", s._selected_skill(make_frame(None), ROWS), None)
check("空帧不抛异常", s._selected_skill(np.zeros((0, 0, 3), dtype="uint8"), ROWS), None)


class CoreStub(AutoTask):
    """把取帧/OCR/打分/落盘全部打桩，只留 _do_training_core 的真实控制流。"""

    def __init__(self, frame):
        self.f = frame
        self.items = [dict(r) for r in ROWS]
        self.config = {"保存截图": False}
        self.stat = {"train": {}, "gamble": [], "sample_fail": 0}
        self.stamina, self.stamina_max, self.gold = 84, 130, 120
        self.core_checked = False
        self.clicks = []          # (技能名, 原因)
        self.audits = []          # (tag, 文本)
        self.decided = []
        self.runlog = []

    def log_info(self, *a, **kw):
        pass

    def log_warning(self, *a, **kw):
        pass

    def sleep(self, *a, **kw):
        pass

    def _tuning(self):
        return {"core_score": {}, "audit": {"enabled": False}}

    def _read_week(self, items):
        return 1, 4

    def _badge_near(self, frame, r):
        return (r["cx"] + 100, r["cy"] - 50)   # 六个技能都带角标

    def _click_item(self, r, why=""):
        self.clicks.append((r["t"], why))

    def click(self, x, y):
        self.clicks.append(("坐标点击", f"({x},{y})"))

    def _sample_core(self, r):
        # 投射给 4 个队友（让它得分最高），其它给 2 个
        n = 4 if r["t"] == "投射" else 2
        return [f"队友{i}" for i in range(n)], 0, "LV.1", []

    def _frame(self, *a, **kw):
        return self.f

    def _texts(self, f):
        return self.items

    def _classify(self, items, frame):
        return "训练核心"

    def _shot(self, *a, **kw):
        return None

    def _audit_save(self, week, day, tag, frame, text):
        self.audits.append((tag, text))
        return "stub"

    def _main_char_of_round(self, scored, cfg):
        return None

    def _apply_fail_limit(self, scored, cfg):
        return scored

    def _score_core(self, real, level, pct, cfg, equips):
        return float(len(real))

    def _apply_consciousness_boost(self, scored, cfg):
        return None

    def _force_skill_when_risky(self, scored, cfg):
        return None

    def _runlog_train(self, *a, **kw):
        self.runlog.append(a)

    def _decide(self, items, frame):
        self.decided.append("决定")
        return True


def run_flow(frame):
    st = CoreStub(frame)
    st._do_training_core(st.items, st.f)
    return st


def find_audit(st, tag):
    for t, txt in st.audits:
        if t == tag:
            return txt
    return ""


print("\n== 2. 默认选中=投射：只读不点，直接采默认帧 ==")
st = run_flow(make_frame("投射"))
clicked = [c[0] for c in st.clicks]
check("投射一次都没被点（含收尾）", "投射" in clicked, False)
check("其它带角标技能照常点开", len([c for c in clicked if c != "坐标点击"]), 5)
check("写出 DECIDE", "DECIDE" in [t for t, _ in st.audits], True)
dec = find_audit(st, "DECIDE")
check("DECIDE 标注默认选中项", "【默认选中项】投射" in dec, True)
check("DECIDE 里投射参与排名", "投射 4.00分" in dec, True)
check("投射最终被选中", "【最终选择】投射" in dec, True)
core1 = [(t, x) for t, x in st.audits if t.startswith("core1_")]
check("投射证据落在 core1", len(core1), 1)
check("证据标注未点击", "默认选中帧(未点击)" in core1[0][1], True)
check("收尾点了决定", st.decided, ["决定"])
check("记了一条训练", len(st.runlog), 1)

print("\n== 3. 认不出高亮 → 保守路径 ==")
st2 = run_flow(make_frame(None))
clicked2 = [c[0] for c in st2.clicks]
check("投射不点击", "投射" in clicked2, False)
# 认不出高亮时收尾仍会点一次「最优编队」（合理：无法假定它已选中），这里只数采样点击
sample_clicks2 = [c for c in st2.clicks if not str(c[1]).startswith("[最优编队")]
check("其余 5 个技能照常采样", len(sample_clicks2), 5)
dec2 = find_audit(st2, "DECIDE")
check("DECIDE 标注未识别", "【默认选中项】未识别" in dec2, True)
check("投射不参与排名", "投射:" in dec2 or "投射 4.00分" in dec2, False)
check("仍然写出 DECIDE", "【最终选择】" in dec2, True)

print("\n== 4. 默认项不是投射时：投射整体跳过，别的高亮项只读不点 ==")
st3 = run_flow(make_frame("弹跳"))
clicked3 = [c[0] for c in st3.clicks]
check("投射不点击", "投射" in clicked3, False)
check("弹跳（高亮）也不点击", "弹跳" in clicked3, False)
check("其余 4 个技能点开", len([c for c in clicked3 if c != "坐标点击"]), 4)
dec3 = find_audit(st3, "DECIDE")
check("DECIDE 标注默认选中弹跳", "【默认选中项】弹跳" in dec3, True)

print(f"\n=== 通过 {PASS} / 失败 {FAIL} ===")
sys.exit(1 if FAIL else 0)
