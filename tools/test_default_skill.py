"""「默认选中项」相关逻辑的离线单测（不打游戏、不依赖真实窗口）。

背景（玩家 2026-11-24 11:55 原话）：
    「进入的时候默认就是投射的，不用点击直接就能采样，如果这时候要去点击它，
      会直接变成选中。」
→ 训练核心页一进来，游戏就默认选中「投射」并把它的编队摊在画面上：
   · 这时候**不需要点**，直接采样就能拿到投射的队友/器材/失败率；
   · 一旦去点它，默认态被改成"手动选中"，游戏就要二次确认 → 直接开训，本次比较作废。

验证：
  1. _selected_skill 能从深蓝高亮胶囊认出当前选中项（浅蓝/橙底两种皮肤、认不出返回 None）；
  2. 默认选中项**第一个采**（在点开任何其它技能之前），且全程不点；
  3. 认不出高亮时，所有带角标的技能都点开采样；
  4. **没角标 = 没人**（玩家 2026-11-24 12:55），不进候选池、不采；
  5. 默认项采样失败只重取帧、不补点；最终选择就是默认项时收尾不点它，直接点「决定」。

【变更记录】
 · 11:25 撤掉「投射不是默认选中项就跳过」的硬规则（实测点胶囊只是展开编队，不会开训，
   旧规则让 W2D4 那局「带经理加成 + 器材」的投射整项掉出候选池）。
 · 11:55 玩家澄清默认态本来就是投射、不点就能采 → 改成**先把默认帧采下来**，
   再逐个点开其它技能。旧的实现虽然也判断了 `name == selected` 就不点，但采样顺序按
   胶囊 cy 从上往下，前面若有技能先被点开，轮到默认项时画面早已不是它的编队。
 · 12:55 玩家纠错：「角标代表有人，没角标的一定没有人！」→ 候选池回到「只含有角标」，
   没角标的项队友恒为 0，采它只是白烧 OCR、还可能把误读的名字当队友抬分。
   （11:xx 那版按"角标只是当天加成"六项全采，是理解错了。）
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

    def __init__(self, frame, badges=True, empty_skill=None, badge_seq=None):
        self.f = frame
        self.items = [dict(r) for r in ROWS]
        self.config = {"保存截图": False}
        self.stat = {"train": {}, "gamble": [], "sample_fail": 0}
        self.stamina, self.stamina_max, self.gold = 84, 130, 120
        self.core_checked = False
        self.badges = badges            # False = 六个技能都没角标
        self.badge_seq = badge_seq or []  # 模拟逐帧变化：第 i 次重取帧后的角标配置
        self.frames = 0                 # 取帧次数（验证"坏帧重取"用）
        self.empty_skill = empty_skill  # 这个技能采不到编队（模拟采样失败）
        self.clicks = []                # (技能名, 原因)
        self.trace = []                 # 事件顺序：("click"/"sample", 技能名)
        self.audits = []                # (tag, 文本)
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

    def _badge_blobs(self, frame):
        """打桩角标：真实分配逻辑（_badges_for_rows）照跑，只替换"画面里有哪些角标"。

        合成角标放在 (cx+100, cy-50)：横向落在胶囊列（cx≈1600 > 0.62*1920），
        纵向比技能行中心高 50px —— 和真实素材实测的 43~45px 一致，在 0.055h(=59px) 内。
        """
        if self.badges is False:
            return []
        if isinstance(self.badges, (set, list, tuple)):
            return [(r["cx"] + 100, r["cy"] - 50) for r in ROWS if r["t"] in self.badges]
        return [(r["cx"] + 100, r["cy"] - 50) for r in ROWS]

    def _click_item(self, r, why=""):
        self.clicks.append((r["t"], why))
        self.trace.append(("click", r["t"]))

    def click(self, x, y):
        self.clicks.append(("坐标点击", f"({x},{y})"))
        self.trace.append(("click", "坐标点击"))

    def _sample_core(self, r, **kw):
        self.trace.append(("sample", r["t"]))
        if r["t"] == self.empty_skill:
            return [], None, None, []
        # 投射给 4 个队友（让它得分最高），其它给 2 个
        n = 4 if r["t"] == "投射" else 2
        return [f"队友{i}" for i in range(n)], 0, "LV.1", []

    def _frame(self, *a, **kw):
        self.frames += 1
        if self.badge_seq and self.frames - 1 < len(self.badge_seq):
            self.badges = self.badge_seq[self.frames - 1]
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


def run_flow(frame, **kw):
    st = CoreStub(frame, **kw)
    st._do_training_core(st.items, st.f)
    return st


def find_audit(st, tag):
    for t, txt in st.audits:
        if t == tag:
            return txt
    return ""


def sample_order(st):
    return [n for k, n in st.trace if k == "sample"]


print("\n== 2. 默认选中=投射：第一个采、全程不点 ==")
st = run_flow(make_frame("投射"))
clicked = [c[0] for c in st.clicks]
check("投射一次都没被点（含收尾）", "投射" in clicked, False)
check("第一个动作就是采投射", st.trace[0], ("sample", "投射"))
check("投射只采一次", sample_order(st).count("投射"), 1)
check("6 个技能全采到", len(sample_order(st)), 6)
check("其它带角标技能照常点开", len([c for c in clicked if c != "坐标点击"]), 5)
check("写出 DECIDE", "DECIDE" in [t for t, _ in st.audits], True)
dec = find_audit(st, "DECIDE")
check("DECIDE 标注默认选中项", "【默认选中项】投射" in dec, True)
check("DECIDE 说明未点击", "先进页面直接采默认帧、全程未点击" in dec, True)
check("DECIDE 里投射参与排名", "投射 4.00分" in dec, True)
check("投射最终被选中", "【最终选择】投射" in dec, True)
core0 = [(t, x) for t, x in st.audits if t.startswith("core0_")]
check("投射证据落在 core0", len(core0), 1)
check("证据标注未点击", "默认选中帧(未点击)" in core0[0][1], True)
check("收尾点了决定", st.decided, ["决定"])
check("记了一条训练", len(st.runlog), 1)

print("\n== 3. 认不出高亮 → 所有带角标的技能都点开采样（不特殊对待投射）==")
st2 = run_flow(make_frame(None))
clicked2 = [c[0] for c in st2.clicks]
check("投射也会被点开", "投射" in clicked2, True)
# 认不出高亮时收尾仍会点一次「最优编队」（合理：无法假定它已选中），这里只数采样点击
sample_clicks2 = [c for c in st2.clicks if not str(c[1]).startswith("[最优编队")]
check("六个技能都采样", len(sample_clicks2), 6)
dec2 = find_audit(st2, "DECIDE")
check("DECIDE 标注未识别", "【默认选中项】未识别" in dec2, True)
check("投射参与排名", "投射 4.00分" in dec2, True)
check("仍然写出 DECIDE", "【最终选择】" in dec2, True)

print("\n== 4. 默认项是弹跳：弹跳第一个采且不点，其余 5 个（含投射）点开 ==")
st3 = run_flow(make_frame("弹跳"))
clicked3 = [c[0] for c in st3.clicks]
check("投射照常点开", "投射" in clicked3, True)
check("弹跳（高亮）也不点击", "弹跳" in clicked3, False)
check("第一个动作就是采弹跳", st3.trace[0], ("sample", "弹跳"))
# 收尾还会点一次「最优编队」，那不是采样点击，排除掉再数
sample_clicks3 = [c for c in st3.clicks if not str(c[1]).startswith("[最优编队")]
check("其余 5 个技能点开", len([c for c in sample_clicks3 if c != "坐标点击"]), 5)
dec3 = find_audit(st3, "DECIDE")
check("DECIDE 标注默认选中弹跳", "【默认选中项】弹跳" in dec3, True)

print("\n== 5. 六项都没角标 = 残帧 → 先重取 3 次，仍没有才决定 ==")
# 玩家 2026-11-24 12:55：「角标代表有人，没角标的一定没有人」→ 没角标的项队友恒为 0。
# 但 2026-11-24 13:00 玩家又追问「不可能一整天都没角标」——对，575 张真实核心帧
# 0 张是全无（最少 1 个，均值 3.5），所以「全无」是残帧信号，要先重取帧。
st4 = run_flow(make_frame("投射"), badges=False)
check("一个采样点击都没有", [c[0] for c in st4.clicks if c[0] != "坐标点击"], [])
check("一次采样都没有", sample_order(st4), [])
check("重取了 3 次帧", st4.frames, 3)
check("兜底直接点决定", st4.decided, ["决定"])

print("\n== 6. 默认项采样失败：只重取帧、不补点 ==")
st5 = run_flow(make_frame("投射"), empty_skill="投射")
check("投射始终没被点", "投射" in [c[0] for c in st5.clicks], False)
check("投射重采了一次（共 2 次采样）", sample_order(st5).count("投射"), 2)
dec5 = find_audit(st5, "DECIDE")
check("DECIDE 说明默认项没采到", "未采到，未参与排名" in dec5, True)
check("仍选出其它技能", "【最终选择】" in dec5, True)

print("\n== 7. 只采有角标的：默认项没角标就是没人，不采也不点 ==")
st6 = run_flow(make_frame("投射"), badges={"弹跳", "意识"})
order = [c[0] for c in st6.clicks if not str(c[1]).startswith("[最优编队")]
check("只点有角标的两项（收尾点最优不算）", order, ["弹跳", "意识"])
check("只采这两项", sample_order(st6), ["弹跳", "意识"])
dec6 = find_audit(st6, "DECIDE")
check("默认项没采到 → 不参与排名", "未采到，未参与排名" in dec6, True)
check("仍然写出最终选择", "【最终选择】" in dec6, True)

print("\n== 8. 残帧重取：第一帧扫不到角标，重取后正常 → 照常比较（不白扔训练）==")
st7 = run_flow(make_frame("投射"), badges=False, badge_seq=[True])
check("重取后照常比较（没被当坏帧放弃）", len(sample_order(st7)), 6)
check("默认项仍第一个采", st7.trace[0], ("sample", "投射"))
check("写出最终选择", "【最终选择】" in find_audit(st7, "DECIDE"), True)

print("\n== 9. 角标一对一分配：相邻两行不共用同一个角标 ==")
s9 = AutoTask.__new__(AutoTask)
f9 = np.full((1080, 1920, 3), 40, dtype="uint8")
rows9 = [{"t": "A", "cx": 1600, "cy": 500}, {"t": "B", "cx": 1600, "cy": 540}]
# 只有一个角标 @(1700,480)：离 A 20px、离 B 40px → 只能给 A，B 不许抢
check("同一个角标只给最近的一行", s9._badges_for_rows(f9, rows9, [(1700, 480)]),
      {0: (1700, 480)})
# 两个角标 @480/@540 → 各配一行
check("两个角标各配一行", len(s9._badges_for_rows(f9, rows9, [(1700, 480), (1700, 540)])), 2)
check("超纵向阈值的角标不分配", s9._badges_for_rows(f9, rows9, [(1700, 900)]), {})
check("左侧（属性条区）的角标不分配", s9._badges_for_rows(f9, rows9, [(200, 490)]), {})

print(f"\n=== 通过 {PASS} / 失败 {FAIL} ===")
sys.exit(1 if FAIL else 0)
