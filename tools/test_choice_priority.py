# -*- coding: utf-8 -*-
"""竖排三选一的「按内容优先级选」自测（离线）。

玩家 2026-11-28：精力恢复 / 属性上限 / 资金援助 这页要按规则选，不是默认第一个。

跑法： ../ok-bsg/.venv/Scripts/python.exe tools/test_choice_priority.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.tasks.AutoTask import AutoTask  # noqa: E402


class Box:
    def __init__(self, x1, y1, x2, y2):
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2
        self.width = x2 - x1
        self.height = y2 - y1


class Stub:
    _tuning = AutoTask._tuning
    _event_cfg = AutoTask._event_cfg
    _keywords = AutoTask._keywords
    _gain = AutoTask._gain
    _gap = AutoTask._gap
    _reward_key = AutoTask._reward_key
    _key_value = AutoTask._key_value
    _rank_options = AutoTask._rank_options

    SYMBOL_GAIN = AutoTask.SYMBOL_GAIN
    stamina = 109
    stamina_max = 130

    def log_warning(self, msg):
        pass


def T(t, cy):
    """居中竖排的胶囊：x 固定在画面中线。"""
    return {"t": t, "cx": 800, "cy": cy,
            "b": Box(800 - 300, cy - 28, 800 + 300, cy + 28)}


def rank(*texts, stamina=109):
    stub = Stub()
    stub.stamina = stamina
    pills = [T(t, 300 + i * 118) for i, t in enumerate(texts)]
    return [(p["t"], why) for p, why in stub._rank_options(pills)]


def case(name, texts, want, why_kw=None, stamina=109):
    got = rank(*texts, stamina=stamina)
    top = got[0][0]
    ok = top == want and (why_kw is None or why_kw in got[0][1])
    print(f"{'✅' if ok else '❌'} {name}: 选「{top}」（{got[0][1]}）"
          + ("" if ok else f"  期望「{want}」  全序={got}"))
    return ok


ok = True
# ① 无数值也走同一份优先级：精力 > 属性 > 金钱（玩家 2026-11-28 否掉「属性优先」）
ok &= case("精力恢复/属性上限/资金援助 → 精力恢复",
           ["精力恢复", "属性上限", "资金援助"], "精力恢复", "精力优先")
ok &= case("顺序打乱也认（资金/精力/属性）",
           ["资金援助", "精力恢复", "属性上限"], "精力恢复")
ok &= case("精力满了（130/130）→ 补了白补，改选属性",
           ["精力恢复", "属性上限"], "属性上限", stamina=130)
ok &= case("精力 91/130 没满 → 精力恢复（读数一错就整页选错）",
           ["精力恢复", "属性上限", "资金援助"], "精力恢复", "精力优先", stamina=91)
ok &= case("只有精力和资金 → 精力", ["精力恢复", "资金援助"], "精力恢复")
ok &= case("属性写法不同（体能训练）也算属性", ["精力恢复", "体能训练"], "精力恢复")

# ② 写了数值 → 完整优先级；精力按缺口截断（当前 109/130，缺口 21）
ok &= case("精力+70 与 属性+30：精力档更高 → 精力+70",
           ["精力+70", "属性+30"], "精力+70")
ok &= case("精力+70 vs 精力+30属性+25：缺口 21 打平 → 带属性的赢",
           ["精力+70", "精力+30 属性+25"], "精力+30 属性+25")
ok &= case("笔记永远第一", ["属性+50", "笔记+1"], "笔记+1")
ok &= case("概率型奖励不要：30%概率 属性+60 < 精力+30",
           ["30% 概率 属性+60", "精力+30"], "精力+30")

# ③ 一条都对不上 → 位置：竖排最上
ok &= case("认不出内容（玩偶/餐厅/香肠）→ 最上",
           ["等身垂耳兔玩偶", "清水家玻璃旋转餐厅", "就要小香肠"],
           "等身垂耳兔玩偶", "竖排最上")

# ④ 顶栏精力读数（_parse_ab）：OCR 常把 ⚡ 图标读成杂字粘在前面
#    玩家 2026-11-28 实拍：真实 91/130，日志却是 130/130 → 「精力已满」误判 → 选了属性。
#    根因是「3 91/130」去空格成「391/130」，贪婪正则给出 a=391 → 被 a<=b*2 拒掉 → 沿用旧值。
Stub._parse_ab = AutoTask._parse_ab
Stub.CAPS = AutoTask.CAPS
_PARSE_CASES = [
    ("3 91/130", (91, 130)),      # 图标粘字（本次实拍）
    ("91/130", (91, 130)),
    ("391/130", (91, 130)),       # 上面的极端形态：没有空格
    ("130/130", (130, 130)),
    ("202/150", (202, 150)),      # 专注可以溢出
    ("221/150+ü三", (221, 150)),  # 后面粘噪音
    ("381130", (38, 130)),        # 斜杠丢了，按已知上限反推
    ("1216/2533", None),          # 属性条大数，不能当资源
    ("284/1784", None),
    ("4-?", None),
]
for text, want in _PARSE_CASES:
    got = Stub()._parse_ab(text)
    good = got == want
    print(f"{'✅' if good else '❌'} 精力读数 「{text}」→ {got}"
          + ("" if good else f"  期望 {want}"))
    ok &= good

print("\n全部通过 ✅" if ok else "\n有失败 ❌")
sys.exit(0 if ok else 1)
