"""P1 断点续跑自测：存盘→读回→清空，且损坏/过期的档不污染（全离线）。"""
import json
import os
import sys
import tempfile
import time

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
    RESUME_FIELDS = AutoTask.RESUME_FIELDS

    def __init__(self, cfg=None, state_path=None):
        self._t = cfg or {}
        self._p = state_path
        self.logs = []
        # 一套典型的运行中状态
        self.saw_training = True
        self.in_training = False
        self.weekday = 3
        self.item_checked = False
        self.trip_picked = False
        self.trip_probe = False
        self.trip_probe_done = "3-3"
        self.trip_probe_disabled = False
        self.trip_back_tries = 0
        self.shop_day = "3-3"
        self.shop_week = "3"
        self.shop_bought = 2
        self.shop_has_stamina = True
        self.notes_done = False
        self.minigame_tries = 0
        self.stock_idx = 0
        self.stock_tries = 0
        self.stock_picked = False
        self.stamina_refills = 1
        self.stamina_misses = 0
        self.last_equip_count = 2
        self.rounds_done = 1
        self.stat = {"train": {"投射": 3}, "gamble": [], "buy": [], "note": 0,
                     "sample_fail": 2, "stamina_drink": 1}

    def _tuning(self):
        return self._t

    def log_info(self, m, notify=False): self.logs.append(("info", m))
    def log_warning(self, m, notify=False): self.logs.append(("warn", m))


for m in ("_state_path", "_save_state", "_load_state", "_clear_state"):
    setattr(Stub, m, getattr(AutoTask, m))

tmpdir = tempfile.mkdtemp(prefix="bsg_resume_")
state = os.path.join(tmpdir, "last_run.json")
cfg = {"resume": {"enabled": True, "path": state}}

print("--- 1. 存盘 → 新实例读回 ---")
s = Stub(cfg)
s._save_state(123, "每日页")
check("文件已生成", os.path.exists(state), True)
with open(state, encoding="utf-8") as f:
    raw = json.load(f)
check("记了步数", raw["step"], 123)
check("记了页面", raw["page"], "每日页")

s2 = Stub(cfg)
s2._load_state()
check("恢复 saw_training", s2.saw_training, True)
check("恢复 weekday", s2.weekday, 3)
check("恢复 trip_probe_done", s2.trip_probe_done, "3-3")
check("恢复 shop_day", s2.shop_day, "3-3")
check("恢复 shop_bought", s2.shop_bought, 2)
check("恢复 rounds_done", s2.rounds_done, 1)
check("恢复 stat", s2.stat["train"], {"投射": 3})
check("打了恢复日志", any("断点恢复" in m for _, m in s2.logs), True)

print("--- 2. 收工清盘 ---")
s2._clear_state()
check("文件已删", os.path.exists(state), False)
s3 = Stub(cfg)
s3._load_state()
check("清盘后不恢复", s3.saw_training, True)   # 保持默认，没被脏数据覆盖
check("没有恢复日志", any("断点恢复" in m for _, m in s3.logs), False)

print("--- 3. 过期档忽略（>6h）---")
s._save_state(1, "大厅")
with open(state, encoding="utf-8") as f:
    raw = json.load(f)
raw["at"] = time.time() - 7 * 3600
with open(state, "w", encoding="utf-8") as f:
    json.dump(raw, f)
s4 = Stub(cfg)
s4.saw_training = False
s4._load_state()
check("过期档不恢复", s4.saw_training, False)
check("打了太旧日志", any("太旧" in m for _, m in s4.logs), True)

print("--- 4. 损坏档不崩 ---")
with open(state, "w", encoding="utf-8") as f:
    f.write("{bad json")
s5 = Stub(cfg)
s5._load_state()
check("损坏档只警告", any("读取失败" in m for _, m in s5.logs), True)

print("--- 5. 关掉开关就不写盘 ---")
off = {"resume": {"enabled": False, "path": state}}
s6 = Stub(off)
s6._save_state(1, "大厅")
check("path 为 None", s6._state_path(), None)

print(f"\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
