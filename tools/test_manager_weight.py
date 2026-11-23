"""经理权重 1.5 的离线单测（2026-11-23 22:14 玩家要求）。

规则在 config/strategy.yaml → tuning.core_score：
  teammate: 1.0          普通队友权重
  manager_names: [球队经理]
  manager_weight: 1.5    球队经理单独权重

_score_core 是纯函数（不碰 self），用 __new__ 直接调即可，不打游戏。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from src.tasks.AutoTask import AutoTask

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "config", "strategy.yaml"), "r", encoding="utf-8") as f:
    CFG = (yaml.safe_load(f) or {}).get("tuning", {}).get("core_score", {})

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if abs(got - want) < 1e-9:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: 得到 {got!r}，期望 {want!r}")


s = AutoTask.__new__(AutoTask)
score = lambda mates, cfg=CFG, **kw: s._score_core(
    mates, kw.get("level", ""), kw.get("pct", 0), cfg, kw.get("equips"))

print("== 配置本身 ==")
check("teammate = 1.0", float(CFG.get("teammate")), 1.0)
check("manager_weight = 1.5", float(CFG.get("manager_weight")), 1.5)
check("manager_names 含球队经理", "球队经理" in (CFG.get("manager_names") or []), True)

print("\n== 计分 ==")
check("单个球队经理 = 1.5（不是 1.0）", score(["球队经理"]), 1.5)
check("经理+1队友 = 2.5", score(["球队经理", "真崎"]), 2.5)
check("经理+2队友 = 3.5", score(["球队经理", "真崎", "百目鬼"]), 3.5)
check("两个普通队友 = 2.0", score(["真崎", "百目鬼"]), 2.0)
check("没有经理时权重不变", score(["真崎"]), 1.0)
check("经理+器材(彩1.5) = 3.0", score(["球队经理"], equips=["彩"]), 3.0)
check("经理+1队友+普通器材 = 3.0", score(["球队经理", "真崎"], equips=["无"]), 3.0)

print("\n== 失败率打折 ==")
check("(经理1.5+队友1.0)×0.7 = 1.75", score(["球队经理", "真崎"], pct=30), 1.75)

print("\n== 老配置兼容（没配 manager_names）==")
OLD = {k: v for k, v in CFG.items() if k not in ("manager_names", "manager_weight")}
check("老配置球队经理仍按 1.0", score(["球队经理"], OLD), 1.0)
check("老配置队友仍按 1.0", score(["球队经理", "真崎"], OLD), 2.0)

print(f"\n=== 通过 {PASS} / 失败 {FAIL} ===")
sys.exit(1 if FAIL else 0)
