"""P0 稳定性自测：未知页指纹表 + 收工摘要（全离线，不动游戏）。"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.tasks.AutoTask import AutoTask

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {got!r}, want {want!r}")


class Box:
    height = 30


class Stub:
    def __init__(self):
        self.known_overlay_hits = {}
        self.unknown_texts = []
        self.unknown_count = 0
        self.logs = []
        self.shots = []
        self.rounds_done = 1
        self.max_rounds = 1
        self.stat = {"train": {}, "gamble": [], "buy": [], "note": 0,
                     "sample_fail": 0, "stamina_drink": 0}
        self.stat_total = {"train": {}, "gamble": [], "buy": [], "note": 0,
                           "sample_fail": 0, "stamina_drink": 0}
        self.config = {}
        self.transition_warns = []

    _new_stat = staticmethod(AutoTask.__dict__["_new_stat"].__func__)
    _merge_stat = staticmethod(AutoTask.__dict__["_merge_stat"].__func__)
    _rollover_stat = AutoTask._rollover_stat

    def _tuning(self):
        import yaml
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "config", "strategy.yaml")
        return yaml.safe_load(io.open(p, encoding="utf-8")).get("tuning", {})

    def log_info(self, m): self.logs.append(("info", m))
    def log_warning(self, m): self.logs.append(("warn", m))
    def log_error(self, m, notify=False): self.logs.append(("error", m))
    def _shot(self, f, tag): self.shots.append(tag); return f"shot/{tag}.png"
    def _click_confirm(self, items, frame): return False
    def _click_next_word(self, items, frame): return False
    def _click_item(self, it, label): self.logs.append(("click", label))
    def click_relative(self, x, y): self.logs.append(("click", f"rel {x},{y}"))
    def _event_cards(self, frame, items): return []
    def sleep(self, sec): self.logs.append(("sleep", sec))


for m in ("_match_overlay", "_do_unknown", "_print_summary", "_is_loading_frame",
          "_check_transition"):
    setattr(Stub, m, getattr(AutoTask, m))


def items(*texts):
    return [{"t": t, "cx": 100 + i * 80, "cy": 500, "b": Box()} for i, t in enumerate(texts)]


frame = __import__("numpy").zeros((1080, 1920, 3), dtype="uint8")
s = Stub()

print("--- 1. 已知浮层指纹 ---")
for texts, want in [
    (("NBA",), "比赛结果横幅"),
    (("战胜伊澄综合学园", "战胜伊澄综合学园"), "比赛结果横幅"),
    (("海边合宿结束",), "合宿结算"),
    (("本周总结", "球队评分：", "22743"), "本周总结"),
    (("属性加成", "心·技·体", "投射"), "属性加成"),
    # 14:59 实跑：过渡帧末字被吞，只剩「心·技」+ 六属性，曾漏判成真未知提前收工
    (("心·技", "投射C367/1784", "体能C284/1640", "弹跳A679/+25"), "属性加成"),
    (("野餐中",), "野餐中"),
    (("专业指导", "精力提升了10"), "专业指导"),
    # 15:11 实跑：过场未播完只读到标题，下一两秒自动进每日页
    (("CD收集心愿开始",), "收集心愿过场"),
    (("收集心愿",), "收集心愿过场"),
    # 15:17 实跑：训练结算 Bonus 华丽动画，按钮还没出现，等它播完
    (("Bonus:", "594%", "投射A616/1784", "运控A 612143"), "训练结算动画"),
    (("Bonus：", "体能 +143"), "训练结算动画"),
    # 加强训练页：有「心·技·体」但有「确定」按钮，被 exclude 排除，不算属性浮层
    (("请选择一种加强训练", "心·技·体", "+85", "确定"), None),
    (("心·技·体", "确定", "65%"), None),
    (("完全没见过的画面",), None),
]:
    hit = s._match_overlay(items(*texts))
    check(f"指纹 {texts[0]}", hit[0] if hit else None, want)

print("--- 2. 未知页默认 pause ---")
s = Stub()
r = s._do_unknown(items("完全没见过的画面"), frame)
check("没见过 → 暂停", r, None)
check("有 error 日志", any(x[0] == "error" for x in s.logs), True)
check("存了截图", any("unknown" in x for x in s.shots), True)

print("--- 3. 已知浮层走 next，不算未知 ---")
s = Stub()
r = s._do_unknown(items("NBA"), frame)
check("NBA → 继续推进", r, True)
check("命中被记账", s.known_overlay_hits.get("比赛结果横幅"), 1)
check("没有 unknown_texts", len(s.unknown_texts), 0)

print("--- 3b. 过场横幅走 wait，只等不点、不算未知 ---")
s = Stub()
r = s._do_unknown(items("CD收集心愿开始"), frame)
check("过场 → 继续（等过去）", r, True)
check("命中被记账", s.known_overlay_hits.get("收集心愿过场"), 1)
check("没有点击", [x for x in s.logs if x[0] == "click"], [])
check("有等待日志", any(x[0] == "info" and "等待" in x[1] for x in s.logs), True)
check("没有 unknown_texts", len(s.unknown_texts), 0)

print("--- 3c. 白屏加载帧：等待，不算未知 ---")
import numpy as _np, cv2 as _cv2
# 造一张「顶部有 HUD、主体纯白」的帧
load_frame = _np.full((1080, 1920, 3), 245, dtype="uint8")
_cv2.rectangle(load_frame, (0, 0), (1920, 60), (90, 150, 200), -1)  # 顶部彩色 HUD
normal_frame = _np.random.randint(60, 220, (1080, 1920, 3), dtype="uint8")  # 主体有内容
s = Stub()
check("白屏帧被识别", s._is_loading_frame(load_frame), True)
check("正常页不误判", s._is_loading_frame(normal_frame), False)
s = Stub()
r = s._do_unknown(items("第2周", "星期-", "102/130"), load_frame)
check("白屏帧 → 继续等待", r, True)
check("白屏帧没有点击", [x for x in s.logs if x[0] == "click"], [])
check("白屏帧无 unknown_texts", len(s.unknown_texts), 0)

# B. 纯过场背景：整屏紫色渐变、几乎没文字（15:19 实跑只读到一个「9」）
fade = _np.zeros((1080, 1920, 3), dtype="uint8")
for y in range(1080):  # 淡紫渐变，std 很低
    fade[y, :] = (215, 190, 235)
fade += _np.random.randint(0, 12, fade.shape, dtype="uint8")  # 轻微噪点
check("紫过场帧被识别", s._is_loading_frame(fade, items("9")), True)
check("正常页不误判(带items)", s._is_loading_frame(normal_frame, items("训练")), False)
s = Stub()
r = s._do_unknown(items("9"), fade)
check("紫过场帧 → 继续等待", r, True)
check("紫过场帧无 unknown_texts", len(s.unknown_texts), 0)

# C. LOGO 转场（2026-11-23 17:12 实跑）：训练结果→大厅之间播蓝色剪影动画，
#    OCR 只读出一个「m」。full std≈37（不符合 A/B），但顶部无 HUD、蓝色占比高。
logo = _np.full((1080, 1920, 3), 238, dtype="uint8")  # 米白背景 BGR
# 顶部无 HUD（保持纯色）；中间画大片蓝色剪影 BGR(200,120,30)=蓝色
_cv2.rectangle(logo, (450, 150), (1450, 900), (200, 120, 30), -1)
_blue = ((logo[:, :, 0] > 140) & (logo[:, :, 2] < 130)).mean()
check("LOGO过场蓝色占比足够", round(float(_blue), 3) > 0.15, True)
check("LOGO过场帧被识别", s._is_loading_frame(logo, items("m")), True)
check("正常页不误判为LOGO", s._is_loading_frame(normal_frame, items("训练", "投射")), False)
s = Stub()
r = s._do_unknown(items("m"), logo)
check("LOGO过场 → 继续等待", r, True)
check("LOGO过场无点击", [x for x in s.logs if x[0] == "click"], [])
check("LOGO过场无 unknown_texts", len(s.unknown_texts), 0)

# D. 紫色半透明蒙版（2026-11-23 17:26 实跑）：每日页上方盖紫色转场蒙版，
#    底下真实页还在、OCR 读出十几段文字，B 的「文字≤2段」不满足。
#    整屏偏紫(占比≈68%)、低对比(std≈18)。合成一张：紫色底 + 少量暗块模拟图文。
veil = _np.zeros((1080, 1920, 3), dtype="uint8")
veil[:, :] = (227, 178, 206)   # 偏紫 BGR：B227 G178 R206
_cv2.rectangle(veil, (200, 400), (700, 900), (150, 120, 160), -1)  # 底下隐约的图文（低对比）
vg = _cv2.cvtColor(veil, _cv2.COLOR_BGR2GRAY)
check("紫蒙版低对比", round(float(vg.std()), 1) < 35, True)
many_items = items("第3周", "星期二", "101/130", "商店", "训练", "投射", "体能", "弹跳",
                   "防守", "运控", "意识")  # 文字多，走不到 B 的 ≤2 段
check("紫蒙版帧被识别", s._is_loading_frame(veil, many_items), True)
check("正常页不误判紫蒙版", s._is_loading_frame(normal_frame, many_items), False)
s = Stub()
r = s._do_unknown(many_items, veil)
check("紫蒙版 → 继续等待", r, True)
check("紫蒙版无 unknown_texts", len(s.unknown_texts), 0)

print("--- 3.5 页面转移软约束（只警告不拦截）---")
s = Stub()
# 明确不可能：选人环节退回每日页
r = s._check_transition("选择队友", "每日页", frame=None, step=10)
check("选人→每日页 记为存疑", r, True)
check("记入 transition_warns", s.transition_warns, [("选择队友", "每日页", 10)])
check("有警告日志", any(k == "warn" and "存疑" in m for k, m in s.logs), True)
check("没有点击动作(不拦截)", [x for x in s.logs if x[0] == "click"], [])
# 合法跳转不报
s2 = Stub()
check("每日页→训练核心 正常", s2._check_transition("每日页", "训练核心"), False)
check("选择队友→确认编队 正常", s2._check_transition("选择队友", "确认编队"), False)
check("无 prev 不报", s2._check_transition(None, "每日页"), False)
# 过场帧（未知）不报
check("未知→任何页 不报", s2._check_transition("未知", "每日页"), False)
check("任何页→未知 不报", s2._check_transition("选择队友", "未知"), False)
# 训练核心→大厅 也存疑（不该跳过结算）；但训练核心→训练结果正常
check("训练核心→大厅 存疑", s2._check_transition("训练核心", "大厅"), True)
check("训练核心→训练结果 正常", s2._check_transition("训练核心", "训练结果"), False)

print("--- 4. 收工摘要 ---")
s = Stub()
s.stat["train"] = {"投射": 5, "意识": 3}
s.stat["gamble"] = [("防守", 25)]
s.stat["buy"] = [("stamina", "精力饮料", 30)]
s.stat["note"] = 2
s.known_overlay_hits = {"比赛结果横幅": 7}
s._print_summary()
joined = " ".join(m for _, m in s.logs)
check("摘要标题", "本轮运行摘要" in joined, True)
check("训练次数(标明本轮)", "本轮训练 8 次" in joined, True)
check("累计行", "累计训练" in joined, True)
check("技能分布", "投射×5" in joined and "意识×3" in joined, True)
check("购买", "精力饮料(30)" in joined, True)
check("赌", "防守@25%" in joined, True)
check("浮层", "比赛结果横幅×7" in joined, True)
check("只打一次", joined.count("本轮运行摘要"), 1)

print("--- 5. 每轮 stat 清零（修摘要假数据）---")
s = Stub()
s.stat["train"] = {"投射": 5, "意识": 3}
s.stat["note"] = 2
s._rollover_stat()
check("本轮已清零", s.stat["train"], {})
check("本轮note清零", s.stat["note"], 0)
check("累计并入投射", s.stat_total["train"].get("投射"), 5)
check("累计并入意识", s.stat_total["train"].get("意识"), 3)
check("累计并入note", s.stat_total["note"], 2)
s.stat["train"] = {"弹跳": 1}
s._print_summary()
joined = " ".join(m for _, m in s.logs)
check("摘要本轮只含弹跳", "本轮训练 1 次" in joined and "弹跳×1" in joined, True)
check("摘要累计含投射", "累计训练 8 次" in joined and "投射×5" in joined, True)

print(f"\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
