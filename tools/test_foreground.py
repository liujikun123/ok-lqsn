"""焦点根治逻辑的离线单测（不打游戏、不依赖真实窗口）。

验证三件事：
  1. _ensure_foreground：游戏在前台 → 什么都不做；不在前台 → 调 _force_visible；
  2. 不在前台时日志做 3 秒节流、但抢前台动作每次都做（向日葵反复切也不刷屏）；
  3. AutoTask.click / send_key 覆盖：调用前一定先走 _ensure_foreground，
     且原框架 click/send_key 的返回值、参数原样透传。

由于守卫里 import win32gui，这里用注入假模块的方式隔离真实 Windows API。
"""
import sys
import types
import time

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(
    __import__("os").path.abspath(__file__))))

from src.tasks.AutoTask import AutoTask

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: 得到 {got!r}，期望 {want!r}")


# ---- 假 win32gui ----
GAME_HWND = 12345
FG_HWND = [GAME_HWND]          # 当前前台窗口，测试里改它
FORCE_CALLS = []


def fake_find_window(cls, title):
    return GAME_HWND


def fake_get_foreground():
    return FG_HWND[0]


def fake_get_window_text(h):
    return "游戏窗口" if h == GAME_HWND else "向日葵远程"


fake = types.ModuleType("win32gui")
fake.FindWindow = fake_find_window
fake.GetForegroundWindow = fake_get_foreground
fake.GetWindowText = fake_get_window_text

_real_win32gui = sys.modules.get("win32gui")
sys.modules["win32gui"] = fake


class GuardStub:
    """只挂守卫相关方法，隔离框架/截图。"""
    _ensure_foreground = AutoTask._ensure_foreground

    def __init__(self):
        self.logs = []
        self._last_fg_log = 0.0

    def log_warning(self, m):
        self.logs.append(("warn", m))

    def _force_visible(self):
        FORCE_CALLS.append(time.time())
        return True


print("== _ensure_foreground 前台判定 ==")
s = GuardStub()

# 1. 游戏在前台 → 不抢、不打日志
FG_HWND[0] = GAME_HWND
FORCE_CALLS.clear()
s.logs.clear()
check("前台时返回 True", s._ensure_foreground(), True)
check("前台时不抢", len(FORCE_CALLS), 0)
check("前台时无警告", len(s.logs), 0)

# 2. 不在前台 → 抢一次 + 打一条警告
FG_HWND[0] = 999
FORCE_CALLS.clear()
s.logs.clear()
s._last_fg_log = 0.0
check("后台时返回抢回结果", s._ensure_foreground(), True)
check("后台时抢一次", len(FORCE_CALLS), 1)
check("后台时打一条警告", len(s.logs), 1)
check("警告里点名前台程序", "向日葵远程" in s.logs[0][1], True)

# 3. 连续多次后台：日志 3 秒节流，但抢每次都做（关键：不能再像旧代码那样漏抢）
#     注意第 2 步刚打过一条日志、_last_fg_log 还是当前时间，3 秒内不再打。
FORCE_CALLS.clear()
s.logs.clear()
for _ in range(5):
    s._ensure_foreground()
check("连续5次后台→抢5次(无节流漏抢)", len(FORCE_CALLS), 5)
check("连续5次后台→日志0条(3秒节流中)", len(s.logs), 0)

# 4. 抢回抛异常也不冒泡到 click（吞掉并记录）
class FailStub(GuardStub):
    def _force_visible(self):
        raise RuntimeError("AttachThreadInput 失败")

s2 = FailStub()
s2._last_fg_log = 0.0
FG_HWND[0] = 999
try:
    r = s2._ensure_foreground()
    check("抢回异常返回False不冒泡", r, False)
    check("抢回异常有日志", any("失败" in m for _, m in s2.logs), True)
except Exception as e:
    check("抢回异常返回False不冒泡", False, f"抛异常 {e}")

print("\n== click / send_key 覆盖：先守前台、再透传 ==")

# 覆盖方法里用了 super()，无参 super 绑定 AutoTask 类，测试桩必须走真实例。
# 做法：__new__ 造实例绕过 __init__，patch 基类 MyBaseTask 的 click/send_key 拦住调用。
from src.tasks.MyBaseTask import MyBaseTask

calls = []
guard_calls = []


def fake_base_click(self, *a, **kw):
    calls.append(("click", a, kw))
    return "click_ret"


def fake_base_key(self, *a, **kw):
    calls.append(("send_key", a, kw))
    return "key_ret"


orig_click, orig_key = MyBaseTask.click, MyBaseTask.send_key
MyBaseTask.click, MyBaseTask.send_key = fake_base_click, fake_base_key
try:
    c = AutoTask.__new__(AutoTask)
    c._ensure_foreground = lambda: guard_calls.append("guard")
    FG_HWND[0] = GAME_HWND
    r1 = c.click(100, 200, name="测试按钮")
    check("click 返回值透传", r1, "click_ret")
    check("click 前先守前台", guard_calls, ["guard"])
    check("click 参数原样透传", calls[-1], ("click", (100, 200), {"name": "测试按钮"}))

    r2 = c.send_key("esc", down_time=0.05)
    check("send_key 返回值透传", r2, "key_ret")
    check("send_key 前也守前台", guard_calls, ["guard", "guard"])
    check("send_key 参数原样透传", calls[-1], ("send_key", ("esc",), {"down_time": 0.05}))
finally:
    MyBaseTask.click, MyBaseTask.send_key = orig_click, orig_key

# 还原
if _real_win32gui is None:
    sys.modules.pop("win32gui", None)
else:
    sys.modules["win32gui"] = _real_win32gui

print(f"\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
