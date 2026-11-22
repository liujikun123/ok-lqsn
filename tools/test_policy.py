"""离线验证：不碰游戏，只喂假数据，看策略引擎能不能按日期/体力选出不同方案。

    .venv\\Scripts\\python.exe tools\\test_policy.py
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.context import build_context  # noqa: E402
from src.core.policy import Policy  # noqa: E402


DECOYS = [  # 训练页上的干扰项，形态和体力一模一样，必须靠区域排除
    ("218/271", 1364, 693, 156, 48),
    ("108/134", 1704, 693, 152, 48),
]


def home_boxes(stamina, stamina_max=150, gold="270.7万", diamond="1.5万"):
    """按实测坐标构造主界面 OCR 结果：金币/钻石/体力在顶栏，剩余次数在右上角。"""
    return [
        (gold, 910, 30, 118, 41),
        (diamond, 1142, 32, 90, 36),
        (f"{stamina}/{stamina_max}", 1376, 16, 242, 59),
        ("4/4", 1806, 20, 68, 48),
        ("训练", 1720, 640, 120, 60),
        ("启程", 1700, 900, 140, 60),
    ] + DECOYS


CASES = [
    ("周六 体力满", datetime(2026, 11, 28, 10, 0), home_boxes(150)),
    ("周六 体力6成", datetime(2026, 11, 28, 20, 0), home_boxes(90)),
    ("周二 体力见底", datetime(2026, 11, 24, 21, 0), home_boxes(25)),
    ("周一 体力7成", datetime(2026, 11, 23, 9, 0), home_boxes(105)),
    ("周三 体力8成", datetime(2026, 11, 25, 14, 0), home_boxes(120)),
    ("周日 体力9成", datetime(2026, 11, 22, 7, 30), home_boxes(135)),
]


def main():
    policy = Policy()
    print(f"策略文件：{policy.path}")
    print(f"规则 {len(policy.rules)} 条，方案 {len(policy.plans)} 套\n")
    for label, when, texts in CASES:
        ctx, hits, issues = build_context(texts, now=when)
        rid, name, plan, _ = policy.pick(ctx)
        pri = " > ".join(plan.get("training_priority") or [])
        print(f"[{label}] {ctx['date_str']} 周{ctx['weekday']}  体力 {ctx['stamina']}/{ctx['stamina_max']}"
              f" ({ctx['stamina_ratio']:.0%})")
        print(f"    命中规则 {rid} → 方案「{name}」")
        print(f"    训练优先级 {pri} | 保留体力 {plan.get('stamina_reserve')} | 事件倾向 {plan.get('event_tendency')}")
        if issues:
            print(f"    ! {issues}")
        print()

    print("— 表达式安全性检查 —")
    from src.core import policy as P
    for bad in ["__import__('os').system('calc')", "open('x')", "(lambda:1)()"]:
        try:
            P.safe_eval(bad, {"stamina": 1})
            print(f"  [危险] 竟然通过了：{bad}")
        except Exception as e:
            print(f"  [已拦截] {bad[:32]:34s} → {type(e).__name__}")


if __name__ == "__main__":
    main()
