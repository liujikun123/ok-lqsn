"""运行记录自测：AutoTask 写 jsonl → runner 读回聚合（全离线，不碰游戏）。"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.tasks.AutoTask import AutoTask
from workbench import runner as R

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
    """借 AutoTask 的 runlog 方法，喂一份典型状态，不跑主循环。"""
    def __init__(self, cfg, path):
        self._t = cfg
        self._p = path
        self.stamina, self.stamina_max = 120, 130
        self.out_focus, self.out_focus_max = 150, 150
        self.rounds_done = 0
        self.max_rounds = 3
        self.stamina_refills = 1
        self.stat = {"train": {"意识": 2, "投射": 1}}
        self.logs = []

    def _tuning(self):
        return self._t

    def log_info(self, m, notify=False):
        self.logs.append(m)

    def log_warning(self, m, notify=False):
        self.logs.append(m)


for m in ("_runlog_path", "_runlog_event", "_runlog_start", "_runlog_train", "_runlog_end"):
    setattr(Stub, m, getattr(AutoTask, m))

tmpdir = tempfile.mkdtemp(prefix="bsg_runlog_")
path = os.path.join(tmpdir, "runs.jsonl")
cfg = {"runlog": {"enabled": True, "path": path}, "loop": {"max_rounds": 3}}

print("--- 1. 写 start + 3 条选课 + end ---")
s = Stub(cfg, path)
s._runlog_start()
rid = s.run_id
check("生成 run_id", bool(rid), True)
s._runlog_train("意识", 6.0, 0, 5, [1])
s.rounds_done = 1
s.stamina, s.out_focus = 95, 120
s._runlog_train("投射", 7.5, 20, 4, [2,3])
s.rounds_done = 2
s.stamina, s.out_focus = 118, 90
s._runlog_train("意识", 6.0, 0, 5, [1])
s.out_focus = 90
s._runlog_end("正常")
check("run_id 收尾清空", s.run_id, None)

with open(path, encoding="utf-8") as f:
    lines = [json.loads(x) for x in f if x.strip()]
check("写了 5 行", len(lines), 5)
check("首行 start", lines[0]["event"], "start")
check("末行 end", lines[-1]["event"], "end")
check("得分记对了", [x["score"] for x in lines if x["event"] == "train"], [6.0, 7.5, 6.0])
check("轮次记对了", [x["round"] for x in lines if x["event"] == "train"], [1, 2, 3])
check("精力起点取首次选课", lines[-1]["stamina_start"], 120)
check("精力终点取收工", lines[-1]["stamina_end"], 118)
check("专注起点取首次选课", lines[-1]["focus_start"], 150)
check("专注终点", lines[-1]["focus_end"], 90)
check("喝药瓶数", lines[-1]["drinks"], 1)
check("状态正常", lines[-1]["status"], "正常")

print("--- 2. 工作台读回聚合 ---")
runs = R.read_run_log(os.path.dirname(path), path)
check("读到 1 次运行", len(runs), 1)
r = runs[0]
check("run_id 一致", r["id"], rid)
check("明细 3 条", len(r["items"]), 3)
check("汇总训练数", r["trains"], 3)
check("汇总完成轮数", r["rounds_done"], 2)
check("汇总总轮数", r["rounds"], 3)
check("汇总状态", r["status"], "正常")
check("明细得分", [i["score"] for i in r["items"]], [6.0, 7.5, 6.0])

print("--- 3. 断点续跑：沿用 run_id，不新开 ---")
s2 = Stub(cfg, path)
s2.run_id = rid           # 模拟从 .state 恢复
s2._runlog_start()
s2._runlog_end("正常")
with open(path, encoding="utf-8") as f:
    tail = [json.loads(x) for x in f if x.strip()][-2:]
check("先写 resume", tail[0]["event"], "resume")
check("resume 沿用 run_id", tail[0]["run"], rid)
check("再写 end", tail[1]["event"], "end")

print("--- 4. 关掉开关就不写 ---")
path_off = os.path.join(tmpdir, "off.jsonl")
off = {"runlog": {"enabled": False, "path": path_off}, "loop": {}}
s3 = Stub(off, path_off)
check("路径为 None", s3._runlog_path(), None)
s3._runlog_start()
check("没建文件", os.path.exists(path_off), False)

print("--- 5. 坏行不崩，且新运行排前面 ---")
with open(path, "a", encoding="utf-8") as f:
    f.write("{bad json\n")
    f.write(json.dumps({"event": "start", "run": "20261123_999999", "at": "x", "rounds": 1}) + "\n")
runs2 = R.read_run_log(os.path.dirname(path), path)
check("坏行被跳过，读到 2 次", len(runs2), 2)
check("新的在前", runs2[0]["id"], "20261123_999999")

print("--- 6. 文件不存在返回空 ---")
check("空列表", R.read_run_log(tmpdir, os.path.join(tmpdir, "nope.jsonl")), [])

print(f"\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
