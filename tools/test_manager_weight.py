"""经理权重的离线单测（2026-11-23 22:14 玩家要求；2026-11-26 改成读配置）。

规则在 config/strategy.yaml → tuning.core_score：
  teammate: 1.0          普通队友权重
  manager_names: [球队经理]
  manager_weight: 2.0    球队经理单独权重（2026-11-26 玩家从 1.5 改回 2.0）

_score_item 是纯函数（不碰 self），用 __new__ 直接调即可，不打游戏。
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
score = lambda mates, cfg=CFG, **kw: s._score_item(
    mates, kw.get("level", ""), kw.get("pct", 0), cfg, kw.get("equips"))

# 期望值全部从配置推算，这样玩家调权重（1.5 → 2.0 → …）不用改测试。
TW = float(CFG.get("teammate", 1.0))
MW = float(CFG.get("manager_weight", TW))

print("== 配置本身 ==")
check("teammate = 1.0", TW, 1.0)
check("manager_weight > teammate（经理更值钱）", MW > TW, True)
check("manager_names 含球队经理", "球队经理" in (CFG.get("manager_names") or []), True)
print(f"     （当前 manager_weight = {MW:g}）")

print("\n== 计分 ==")
check(f"单个球队经理 = {MW:g}（不是 {TW:g}）", score(["球队经理"]), MW)
check(f"经理+1队友 = {MW + TW:g}", score(["球队经理", "真崎"]), MW + TW)
check(f"经理+2队友 = {MW + 2 * TW:g}", score(["球队经理", "真崎", "百目鬼"]), MW + 2 * TW)
check("两个普通队友 = 2.0", score(["真崎", "百目鬼"]), 2 * TW)
check("没有经理时权重不变", score(["真崎"]), TW)

# 器材按档位计（彩 1.5 / 金 1.0 / 普 0.5），期望值从配置读。
TIER = CFG.get("equipment_tier") or {}
EQ = float(CFG.get("equipment", 0.5))
CAI = float(TIER.get("彩", EQ))
NONE = float(TIER.get("无", EQ))
check(f"经理+彩卡 = {MW + CAI:g}", score(["球队经理"], equips=["彩"]), MW + CAI)
check(f"经理+1队友+无档卡 = {MW + TW + NONE:g}",
      score(["球队经理", "真崎"], equips=["无"]), MW + TW + NONE)

print("\n== 失败率打折 ==")
check(f"(经理{MW:g}+队友{TW:g})×0.7 = {(MW + TW) * 0.7:.2f}",
      score(["球队经理", "真崎"], pct=30), (MW + TW) * 0.7)

print("\n== 老配置兼容（没配 manager_names）==")
OLD = {k: v for k, v in CFG.items() if k not in ("manager_names", "manager_weight")}
check("老配置球队经理仍按 1.0", score(["球队经理"], OLD), 1.0)
check("老配置队友仍按 1.0", score(["球队经理", "真崎"], OLD), 2.0)

print(f"\n=== 通过 {PASS} / 失败 {FAIL} ===")
sys.exit(1 if FAIL else 0)
