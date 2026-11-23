"""P2 规则可配自测：改配置后行为跟着变，且默认配置与旧行为等价（全离线）。"""
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


class Stub:
    SYMBOL_GAIN = AutoTask.SYMBOL_GAIN
    SKILL_WORDS = AutoTask.SKILL_WORDS
    ATTR_WORDS = AutoTask.ATTR_WORDS

    def __init__(self, tuning=None):
        self._t = tuning or {}
        self.stamina = 30
        self.stamina_max = 130

    def _tuning(self):
        return self._t

    def log_info(self, m): pass
    def log_warning(self, m): pass


for m in ("_event_cfg", "_keywords", "_tier_index", "_gap", "_gain",
          "_reward_key", "_key_value", "_match_overlay"):
    setattr(Stub, m, getattr(AutoTask, m))


def cfg(**kw):
    return kw


print("--- 1. 默认 5 档编号与旧行为等价 ---")
s = Stub(cfg(event={"priority": ["笔记", "技能", "精力", "属性", "零用钱"]}))
check("笔记 tier=4", s._reward_key("笔记继承")[0], 4)
check("技能 tier=3", s._reward_key("技能+1")[0], 3)
check("精力 tier=2", s._reward_key("精力+30")[0], 2)
check("属性 tier=1", s._reward_key("属性+20")[0], 1)
check("零钱 tier=0", s._reward_key("零用钱+80")[0], 0)

print("--- 2. 改 priority 顺序 → tier 跟着换 ---")
s = Stub(cfg(event={"priority": ["精力", "笔记", "技能", "属性", "零用钱"]}))
check("精力升到 tier=4", s._reward_key("精力+30")[0], 4)
check("笔记降到 tier=3", s._reward_key("笔记继承")[0], 3)

print("--- 3. 改关键词 → 新词生效 ---")
s = Stub(cfg(event={"priority": ["笔记", "技能", "精力", "属性", "零用钱"],
                    "keywords": {"笔记": ["手写笔记", "秘籍"]}}))
check("「秘籍」按笔记档", s._reward_key("获得秘籍")[0], 4)

print("--- 4. 精力未溢出截断保留 ---")
s = Stub(cfg(event={"priority": ["笔记", "技能", "精力", "属性", "零用钱"]}))
s.stamina = 125           # 只差 5
s.stamina_max = 130
check("+70 只吃进 5", s._reward_key("精力+70")[1], 5)
check("+30 也只吃进 5", s._reward_key("精力+30")[1], 5)

print("--- 5. 改 symbol_gain → ++ 档位跟着变 ---")
s = Stub(cfg(event={"symbol_gain": {"精力": [11, 99]}}))
check("精力+ = 11", s._gain("精力+", "精力"), 11)
check("精力++ = 99", s._gain("精力++", "精力"), 99)

print("--- 6. 改 score_step → 分差跟着变 ---")
s = Stub(cfg(event={"score_step": 100}))
check("tier4 分数 = 400 + 0", s._key_value((4, 0, 0, 0)), 400)

print("--- 7. 概率奖励拒绝可关 ---")
s = Stub(cfg(event={"reject_probability": True}))
check("默认拒绝概率奖励", s._reward_key("30%概率 笔记")[0], 0)
s = Stub(cfg(event={"reject_probability": False}))
check("关掉后按笔记档", s._reward_key("30%概率 笔记")[0], 4)

print("--- 8. 真实配置文件加载后默认等价 ---")
import yaml
p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "config", "strategy.yaml")
real = yaml.safe_load(io.open(p, encoding="utf-8"))
s = Stub(cfg(**real["tuning"]))
check("读真配置：笔记=4", s._reward_key("笔记继承")[0], 4)
check("读真配置：++还原70", s._gain("精力++", "精力"), 70)
check("读真配置：float 不加分", s._reward_key("零用钱+80")[0], 0)

print(f"\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
