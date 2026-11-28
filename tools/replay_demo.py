"""按录制下来的轨迹重放手势，验证「录下来的拖动，脚本能不能一模一样做出来」。

用法（在 bsg-auto 根目录）：
    # 先看这次录制里都有哪些动作、序号是什么
    ../ok-bsg/.venv/Scripts/python.exe tools/replay_demo.py demo/session_1127_152728 --list

    # 重放第 47 号（那次拖拽）：按原始轨迹点 + 原始间隔走一遍
    ../ok-bsg/.venv/Scripts/python.exe tools/replay_demo.py demo/session_1127_152728 --id 47

    # 只重放点击 / 只重放拖拽
    ../ok-bsg/.venv/Scripts/python.exe tools/replay_demo.py demo/session_1127_152728 --id 47 --speed 1.6

产出 <session>/replay/ 下 before_47.png / after_47.png，直接对比看列表有没有真的滚。

和录制器同一套坐标：归一化 (0~1) × 游戏窗口 client rect，跟 click_relative 完全一致。
"""
import argparse
import ctypes
import json
import os
import sys
import time

import cv2
import numpy as np
from PIL import ImageGrab

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

WINDOW_KEYWORDS = ["篮球少女", "飞跃巅峰"]
PROC_KEYWORDS = ["k3_windows_ob"]

u32 = ctypes.windll.user32
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


def client_rect(hwnd):
    import win32gui
    cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
    sl, st = win32gui.ClientToScreen(hwnd, (0, 0))
    sr, sb = win32gui.ClientToScreen(hwnd, (cr, cb))
    return (sl, st, sr, sb)


def find_game_window():
    import win32gui
    import win32process
    hits = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd) or ""
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            import win32api
            import win32con
            h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            exe = os.path.basename(win32process.GetModuleFileNameEx(h, None) or "")
        except Exception:
            exe = ""
        if any(k in title for k in WINDOW_KEYWORDS) or any(k in exe.lower() for k in PROC_KEYWORDS):
            r = win32gui.GetWindowRect(hwnd)
            if r[2] - r[0] > 400 and r[3] - r[1] > 300:
                hits.append((hwnd, title, exe, r))
        return True

    win32gui.EnumWindows(cb, None)
    if not hits:
        return None
    hits.sort(key=lambda x: -(x[3][2] - x[3][0]) * (x[3][3] - x[3][1]))
    return hits[0]


def grab(rect):
    l, t, r, b = rect
    img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def diff_score(a, b):
    """两张图差多少（0~255 的平均绝对差），用来判断重放有没有让画面真的变化。"""
    if a is None or b is None or a.shape != b.shape:
        return -1.0
    return float(np.abs(a.astype("int16") - b.astype("int16")).mean())


def replay_click(rect, ev, speed=1.0):
    l, t, r, b = rect
    w, h = r - l, b - t
    x, y = int(l + w * ev["rx"]), int(t + h * ev["ry"])
    u32.SetCursorPos(x, y)
    time.sleep(0.12 * speed)
    u32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.06 * speed)
    u32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def replay_drag(rect, ev, speed=1.0, jitter=0, reverse=False):
    """按录到的轨迹点重放：每个点的相对位置 + 点与点之间的原始时间间隔。

    speed 只缩放「等待时间」，不改轨迹本身 —— 轨迹是人的手，缩放它会变形。
    reverse=True 时轨迹整体倒放（用来把列表滚回去，验证正反都认）。
    """
    l, t, r, b = rect
    w, h = r - l, b - t
    path = ev.get("path") or []
    if len(path) < 2:
        path = [(ev["rx"], ev["ry"]), (ev["ux"], ev["uy"])]
    ux, uy = ev["ux"], ev["uy"]
    if reverse:
        path = list(reversed(path))
        ux, uy = ev["rx"], ev["ry"]
    # 原始时间戳没存每点间隔，用总时长均分（录到 50+ 个点，均分足够接近原速）
    dur = max(ev.get("dur") or 1.0, 0.2)
    per = dur / max(len(path) - 1, 1)
    x0, y0 = int(l + w * path[0][0]), int(t + h * path[0][1])
    u32.SetCursorPos(x0, y0)
    time.sleep(0.15 * speed)
    u32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(min(per, 0.08))
    for rx, ry in path[1:]:
        jx = jy = 0
        if jitter:
            jx = int(np.random.randint(-jitter, jitter + 1))
            jy = int(np.random.randint(-jitter, jitter + 1))
        u32.SetCursorPos(int(l + w * rx) + jx, int(t + h * ry) + jy)
        time.sleep(max(per * speed, 0.008))
    u32.SetCursorPos(int(l + w * ux), int(t + h * uy))
    time.sleep(0.05)
    u32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session", help="录制会话目录，如 demo/session_1127_152728")
    ap.add_argument("--list", action="store_true", help="只列出动作，不重放")
    ap.add_argument("--id", type=int, action="append", default=[],
                    help="要重放的事件序号（events.jsonl 里的 i），可重复传多次")
    ap.add_argument("--kind", choices=["click", "drag"], help="不指定 --id 时，重放这类动作的全部")
    ap.add_argument("--speed", type=float, default=1.0, help="等待时间缩放，>1 更慢")
    ap.add_argument("--jitter", type=int, default=0, help="轨迹随机抖动像素，模拟真人手抖")
    ap.add_argument("--reverse", action="store_true", help="轨迹倒放（把列表滚回原处）")
    ap.add_argument("--settle", type=float, default=1.2, help="重放后等多久再抓结果帧")
    args = ap.parse_args()

    evs = [json.loads(l) for l in open(os.path.join(args.session, "events.jsonl"), encoding="utf-8")]
    acts = [e for e in evs if e["kind"] in ("click", "drag")]

    if args.list:
        print(f"{len(acts)} 个动作：")
        for e in acts:
            if e["kind"] == "click":
                print(f"  [{e['i']:3d}] click  rel({e['rx']:.3f},{e['ry']:.3f})  t={e['t']:6.2f}s")
            else:
                print(f"  [{e['i']:3d}] drag   rel({e['rx']:.3f},{e['ry']:.3f}) -> "
                      f"({e['ux']:.3f},{e['uy']:.3f})  dy={e['uy'] - e['ry']:+.3f}  "
                      f"{e.get('dur', 0):.2f}s {len(e.get('path') or [])}点")
        return 0

    todo = acts
    if args.id:
        ids = set(args.id)
        todo = [e for e in acts if e["i"] in ids]
    elif args.kind:
        todo = [e for e in acts if e["kind"] == args.kind]
    if not todo:
        print("[X] 没匹配到要重放的动作")
        return 1

    win = find_game_window()
    if not win:
        print("[X] 没找到游戏窗口")
        return 1
    hwnd, title, exe, _ = win
    rect = client_rect(hwnd)
    out = os.path.join(args.session, "replay")
    os.makedirs(out, exist_ok=True)
    print(f"[+] 窗口 {title} 画面 {rect[2] - rect[0]}x{rect[3] - rect[1]} @({rect[0]},{rect[1]})")

    import win32gui
    if win32gui.GetForegroundWindow() != hwnd:
        print("[!] 游戏不在前台，先把窗口激活")
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.6)

    for ev in todo:
        before = grab(rect)
        tag = f"{ev['i']:04d}" + ("_rev" if args.reverse else "")
        p = os.path.join(out, f"before_{tag}.png")
        cv2.imwrite(p, before)
        print(f"  ▶ 重放 [{ev['i']:3d}] {ev['kind']} "
              f"({ev['rx']:.3f},{ev['ry']:.3f})"
              + (f" -> ({ev['ux']:.3f},{ev['uy']:.3f})" if ev["kind"] == "drag" else "")
              + ("  [倒放]" if args.reverse else ""))
        if ev["kind"] == "click":
            replay_click(rect, ev, args.speed)
        else:
            replay_drag(rect, ev, args.speed, args.jitter, args.reverse)
        time.sleep(args.settle)
        after = grab(rect)
        cv2.imwrite(os.path.join(out, f"after_{tag}.png"), after)
        d = diff_score(before, after)
        print(f"    画面差异 {d:.2f}"
              + ("  （有变化 → 手势生效）" if d > 2.0 else "  （几乎没变 → 可能没生效/已到头）"))
    print(f"\n[+] 对比图在 {os.path.abspath(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
