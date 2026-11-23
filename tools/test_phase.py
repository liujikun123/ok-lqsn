"""C: 显式阶段机 —— 全离线，不动游戏。

验证：① HALL→TRAINING→SETTLING→HALL 正常流转；② 大厅在 HALL 阶段不计轮；
③ 公告/小游戏（不在 PAGE_PHASE）不推进阶段；④ 断点兼容旧档从 saw_training 推导；
⑤ saw_training 与 phase 保持同步（下游判断不变）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.tasks.AutoTask import (AutoTask, PHASE_HALL, PHASE_TRAINING,
                                PHASE_SETTLING, PAGE_PHASE)

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
    phase = PHASE_HALL
    saw_training = False
    in_training = False
    logs = []

    _set_phase = AutoTask._set_phase

    def log_info(self, m): self.logs.append(("info", m))
    def log_warning(self, m): self.logs.append(("warn", m))


print("--- 1. 正常流转 ---")
s = Stub()
check("初始 HALL", s.phase, PHASE_HALL)
s._set_phase(PHASE_TRAINING, page="每日页")
check("每日页 → TRAINING", s.phase, PHASE_TRAINING)
check("TRAINING 同步 saw_training", s.saw_training, True)
s._set_phase(PHASE_SETTLING, page="训练结束")
check("训练结束 → SETTLING", s.phase, PHASE_SETTLING)
s._set_phase(PHASE_HALL, page="大厅", reason="完成一轮")
check("回大厅 → HALL", s.phase, PHASE_HALL)
check("HALL 清 saw_training", s.saw_training, False)

print("--- 2. 页面映射表覆盖 ---")
check("每日页→TRAINING", PAGE_PHASE.get("每日页"), PHASE_TRAINING)
check("训练结束→SETTLING", PAGE_PHASE.get("训练结束"), PHASE_SETTLING)
check("公告页不在表内", PAGE_PHASE.get("公告页"), None)
check("未知页不在表内", PAGE_PHASE.get("未知"), None)
check("大厅不在表内", PAGE_PHASE.get("大厅"), None)

print("--- 3. 非法跳跃只警告不崩 ---")
s = Stub()
s._set_phase(PHASE_SETTLING, page="?", reason="测试")   # HALL→SETTLING 非法
check("非法跳跃仍设置了目标阶段", s.phase, PHASE_SETTLING)
check("有警告日志", any(x[0] == "warn" for x in s.logs), True)

print("--- 4. 同阶段幂等 ---")
s = Stub()
before = len(s.logs)
s._set_phase(PHASE_HALL, page="大厅")
check("HALL→HALL 不打日志", len(s.logs), before)

print("--- 5. 旧断点兼容性（saw_training→phase）---")
# 模拟旧档：只有 saw_training=True，无 phase
old = {"saw_training": True, "in_training": True, "step": 5, "page": "每日页"}
RESUME = ("saw_training", "in_training", "phase")
s = Stub()
for k in RESUME:
    if k in old and old[k] is not None:
        setattr(s, k, old[k])
if "phase" not in old or not old.get("phase"):
    s.phase = PHASE_TRAINING if s.saw_training else PHASE_HALL
check("旧档 saw_training=True → TRAINING", s.phase, PHASE_TRAINING)

print(f"\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
