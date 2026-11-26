"""录制人工示范：你在游戏里正常操作一遍，程序记录每一次点击/按键 + 对应画面。

用法（在 ok-bsg 目录下）：
    .venv\\Scripts\\python.exe tools\\record_demo.py            # 开始录制，按 F12 结束
    .venv\\Scripts\\python.exe tools\\record_demo.py --test      # 只截一张图，验证能抓到游戏窗口

产出：
    demo/session_<时间>/frames/*.png   每一步的画面
    demo/session_<时间>/timeline.json  点击/按键的时间轴（含窗口内相对坐标）
    demo/session_<时间>/report.html    一次看完的缩略图墙，红圈标出点击位置
"""
import argparse
import ctypes
import json
import os
import sys
import threading
import time
from datetime import datetime

import cv2
import numpy as np
from PIL import ImageGrab

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 防止缩放导致坐标偏移
except Exception:
    pass

WINDOW_KEYWORDS = ["篮球少女", "飞跃巅峰"]
PROC_KEYWORDS = ["k3_windows_ob"]


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
            w, h_ = r[2] - r[0], r[3] - r[1]
            if w > 400 and h_ > 300:
                hits.append((hwnd, title, exe, r))
        return True

    win32gui.EnumWindows(cb, None)
    if not hits:
        return None
    hits.sort(key=lambda x: -(x[3][2] - x[3][0]) * (x[3][3] - x[3][1]))
    return hits[0]


def client_rect(hwnd):
    """游戏画面区域（不含标题栏和边框），比 GetWindowRect 更准。"""
    import win32gui
    cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
    sl, st = win32gui.ClientToScreen(hwnd, (0, 0))
    sr, sb = win32gui.ClientToScreen(hwnd, (cr, cb))
    return (sl, st, sr, sb)


def grab(rect):
    l, t, r, b = rect
    img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def is_foreground(hwnd):
    import win32gui
    return win32gui.GetForegroundWindow() == hwnd


class Session:
    def __init__(self, root):
        self.root = root
        self.frames_dir = os.path.join(root, "frames")
        os.makedirs(self.frames_dir, exist_ok=True)
        self.events = []
        self.seq = 0
        self.t0 = time.time()
        self.lock = threading.Lock()
        self.last_hash = None
        self.last_img = None
        self.running = True

    def add_frame(self, img, kind, extra):
        self.seq += 1
        name = f"{self.seq:04d}_{kind}.png"
        cv2.imwrite(os.path.join(self.frames_dir, name), img)
        ev = {"i": self.seq, "t": round(time.time() - self.t0, 2), "kind": kind,
              "img": f"frames/{name}", "w": img.shape[1], "h": img.shape[0]}
        ev.update(extra)
        with self.lock:
            self.events.append(ev)
        self.last_img = img
        return ev

    def changed(self, img):
        if self.last_img is None or self.last_img.shape != img.shape:
            return True
        d = float(np.abs(img.astype("int16") - self.last_img.astype("int16")).mean())
        return d > 2.0


def build_report(session, win):
    hwnd, title, exe, rect = win
    rows = []
    for ev in session.events:
        full = os.path.join(session.root, ev["img"])
        img = cv2.imread(full)
        if img is None:
            continue
        thumb = img.copy()
        if "rx" in ev:
            cx, cy = int(ev["rx"] * thumb.shape[1]), int(ev["ry"] * thumb.shape[0])
            cv2.circle(thumb, (cx, cy), 26, (0, 0, 255), 4)
            cv2.circle(thumb, (cx, cy), 4, (0, 0, 255), -1)
        scale = 360 / thumb.shape[1]
        thumb = cv2.resize(thumb, (360, int(thumb.shape[0] * scale)))
        tname = ev["img"].replace("frames/", "thumbs/").replace(".png", ".jpg")
        os.makedirs(os.path.join(session.root, "thumbs"), exist_ok=True)
        cv2.imwrite(os.path.join(session.root, tname), thumb, [cv2.IMWRITE_JPEG_QUALITY, 82])
        detail = ""
        if ev["kind"] == "click":
            detail = f"{ev['btn']} @ ({ev['x']}, {ev['y']}) = 相对 ({ev['rx']:.3f}, {ev['ry']:.3f})"
        elif ev["kind"] == "key":
            detail = f"按键 {ev.get('key')}"
        else:
            detail = "画面变化（无操作）"
        rows.append(f"""<tr><td class=n>{ev['i']}</td><td class=t>{ev['t']}s</td>
<td><a href="{ev['img']}" target=_blank><img src="{tname}" loading=lazy></a></td>
<td class=d><b>{ev['kind']}</b><br>{detail}</td></tr>""")

    html = f"""<!doctype html><meta charset=utf-8><title>示范录制 {os.path.basename(session.root)}</title>
<style>
body{{font:14px/1.6 -apple-system,Segoe UI,Microsoft YaHei;margin:0;background:#111;color:#eee}}
header{{position:sticky;top:0;background:#1b1b1b;padding:14px 20px;border-bottom:1px solid #333;z-index:9}}
h1{{font-size:16px;margin:0 0 4px}} .meta{{color:#999;font-size:12px}}
table{{border-collapse:collapse;width:100%}} td{{border-bottom:1px solid #262626;padding:10px 12px;vertical-align:top}}
td.n{{color:#666;width:44px;text-align:right}} td.t{{color:#888;width:64px;font-variant-numeric:tabular-nums}}
td.d{{color:#bbb;width:340px;font-size:13px}} img{{border-radius:6px;display:block;border:1px solid #333}}
a{{color:#4ea1ff;text-decoration:none}}
</style>
<header><h1>示范录制 · {len(session.events)} 个节点</h1>
<div class=meta>{title}（{exe}）窗口 {rect[2]-rect[0]}x{rect[3]-rect[1]} · 录制于 {datetime.now():%Y-%m-%d %H:%M}</div></header>
<table>{''.join(rows)}</table>"""
    path = os.path.join(session.root, "report.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="只截一张图验证")
    ap.add_argument("--interval", type=float, default=0.8, help="非点击时的读数间隔秒")
    args = ap.parse_args()

    win = find_game_window()
    if not win:
        print("[X] 没找到游戏窗口，请先启动《篮球少女：飞跃巅峰》")
        return 1
    hwnd, title, exe, wrect = win
    try:
        rect = client_rect(hwnd)
    except Exception:
        rect = wrect
    l, t, r, b = rect
    print(f"[+] 窗口: {title} / {exe}  画面区域 {l},{t} 尺寸 {r-l}x{b-t}")

    img = grab(rect)
    if args.test:
        os.makedirs("demo", exist_ok=True)
        cv2.imwrite("demo/_test.png", img)
        fg = "前台" if is_foreground(hwnd) else "不在前台（可能被抓到别的窗口）"
        print(f"[+] 截图成功 {img.shape[1]}x{img.shape[0]} 窗口{fg} -> demo/_test.png")
        return 0

    stamp = datetime.now().strftime("%m%d_%H%M%S")
    root = os.path.join("demo", f"session_{stamp}")
    s = Session(root)

    from pynput import keyboard, mouse

    def on_click(x, y, button, pressed):
        if not pressed or not s.running:
            return True
        if not is_foreground(hwnd):
            return True
        try:
            img = grab(rect)
        except Exception as e:
            print("[!] 抓图失败", e)
            return True
        rx, ry = (x - l) / max(1, (r - l)), (y - t) / max(1, (b - t))
        ev = s.add_frame(img, "click", {"x": x, "y": y, "rx": rx, "ry": ry,
                                        "btn": str(button).replace("Button.", "")})
        print(f"  [{ev['i']:03d}] 点击 ({x},{y}) 相对({rx:.3f},{ry:.3f})")
        return True

    def on_key(key):
        if not s.running:
            return False
        name = getattr(key, "name", None) or str(key).replace("'", "")
        if name == "f12":  # 只用 F12 停，Esc 是游戏里的返回键，不能占用
            s.running = False
            return False
        try:
            img = grab(rect)
            ev = s.add_frame(img, "key", {"key": name})
            print(f"  [{ev['i']:03d}] 按键 {name}")
        except Exception:
            pass
        return True

    def sampler():
        while s.running:
            time.sleep(args.interval)
            if not s.running:
                break
            if not is_foreground(hwnd):
                continue
            try:
                img = grab(rect)
            except Exception:
                continue
            if s.changed(img):
                ev = s.add_frame(img, "screen", {})
                print(f"  [{ev['i']:03d}] 画面变化")

    print("\n>>> 开始录制。现在请你在游戏里把「训练」流程正常操作一遍（阵容/笔记随便你怎么点）。")
    print(">>> 结束请按 F12 或 Esc。\n")
    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    ml = mouse.Listener(on_click=on_click)
    kl = keyboard.Listener(on_press=on_key)
    ml.start()
    kl.start()
    kl.join()
    s.running = False
    ml.stop()
    time.sleep(0.3)
    path = build_report(s, win)
    print(f"\n[+] 录制结束，共 {len(s.events)} 个节点")
    print(f"[+] 报告: {os.path.abspath(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
