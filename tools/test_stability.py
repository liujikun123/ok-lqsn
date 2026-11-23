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
        self.config = {}

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


for m in ("_match_overlay", "_do_unknown", "_print_summary"):
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
    (("野餐中",), "野餐中"),
    (("专业指导", "精力提升了10"), "专业指导"),
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
check("训练次数", "训练 8 次" in joined, True)
check("技能分布", "投射×5" in joined and "意识×3" in joined, True)
check("购买", "精力饮料(30)" in joined, True)
check("赌", "防守@25%" in joined, True)
check("浮层", "比赛结果横幅×7" in joined, True)
check("只打一次", joined.count("本轮运行摘要"), 1)

print(f"\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
