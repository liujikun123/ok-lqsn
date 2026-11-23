"""把当前页面的可选项清单导出来（支持右侧长列表滚动、拖拽两种下滑方式）。

用在「训练核心（选人）/ 选择队友 / 选择器材 / 选择笔记」这几页 ——
右边是一长串卡片，屏幕只显示一部分，得往下滑才看得到全部。

    # 只抓当前这一屏
    python tools/pick_list.py --tag 选择队友

    # 从顶部往下滚 8 次，把整个列表收进来
    python tools/pick_list.py --tag 选择队友 --scroll 8

    # 列表在右半边（默认滚动点在 0.72, 0.55）
    python tools/pick_list.py --tag 选择器材 --scroll 6 --at 0.72,0.55

    # 滚轮无效的页（只能按住拖）：改用拖拽
    python tools/pick_list.py --tag 选择笔记 --scroll 5 --mode drag

输出（logs/pick_list/）：
    <tag>_<HHMMSS>.md    给人看的清单：每帧一段 + 去重总表
    <tag>_<HHMMSS>.json  给程序吃的原始数据（相对坐标 + 首次出现帧）
    <tag>_<HHMMSS>_fN.png 每帧截图，方便对账

只读操作：除了滚动/拖拽列表，不点任何按钮。
"""

import argparse
import ctypes
import json
import os
import sys
import time

import cv2
import numpy as np
import psutil
import win32gui
import win32process
from PIL import ImageGrab

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "logs", "pick_list")

MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


def game_client():
    res = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            return
        if "UnityWndClass" not in win32gui.GetClassName(hwnd):
            return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            if "k3_windows" not in psutil.Process(pid).name().lower():
                return
        except Exception:
            return
        l, t = win32gui.ClientToScreen(hwnd, (0, 0))
        w, h = win32gui.GetClientRect(hwnd)[2:]
        res.append((hwnd, (l, t, l + w, t + h)))

    win32gui.EnumWindows(cb, None)
    return res


def grab(rect):
    img = ImageGrab.grab(bbox=rect, all_screens=True)
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def save_img(path, img):
    """cv2.imwrite 在 Windows 上写不了中文路径，统一走 imencode + tofile。"""
    ext = os.path.splitext(path)[1] or ".png"
    ok, buf = cv2.imencode(ext, img)
    if ok:
        buf.tofile(path)


def ocr_items(ocr, frame):
    h, w = frame.shape[:2]
    res = ocr.ocr(frame) or []
    lines = res[0] if res and isinstance(res[0], list) else res
    out = []
    for line in lines:
        try:
            box, (text, score) = line[0], line[1]
        except Exception:
            continue
        text = (text or "").strip()
        if not text:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        out.append({
            "t": text,
            "cx": round(cx, 1), "cy": round(cy, 1),
            "rx": round(cx / w, 4), "ry": round(cy / h, 4),
            "w": round(max(xs) - min(xs), 1),
            "h": round(max(ys) - min(ys), 1),
            "score": round(float(score), 3),
        })
    out.sort(key=lambda i: (round(i["cy"] / 12), i["cx"]))
    return out


def scroll_at(rect, rx, ry, notches):
    """在窗口内相对位置滚轮。notches > 0 往下滑。"""
    l, t, r, b = rect
    w, h = r - l, b - t
    x, y = int(l + w * rx), int(t + h * ry)
    ctypes.windll.user32.SetCursorPos(x, y)
    time.sleep(0.15)
    ctypes.windll.user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, -120 * notches, 0)


def drag_at(rect, rx, y_from, y_to, steps=14, hold=0.02):
    """按住左键从 (rx, y_from) 拖到 (rx, y_to)，y_from > y_to 表示往上拖（内容向下滚）。"""
    l, t, r, b = rect
    w, h = r - l, b - t
    x = int(l + w * rx)
    y0, y1 = int(t + h * y_from), int(t + h * y_to)
    ctypes.windll.user32.SetCursorPos(x, y0)
    time.sleep(0.2)
    ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(hold)
    for i in range(1, steps + 1):
        yy = int(y0 + (y1 - y0) * i / steps)
        ctypes.windll.user32.SetCursorPos(x, yy)
        time.sleep(0.012)
    time.sleep(0.05)
    ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True, help="清单名，如 选择队友 / 选择器材 / 选择笔记 / 训练核心")
    ap.add_argument("--scroll", type=int, default=0, help="下滑次数（0 = 只抓当前屏）")
    ap.add_argument("--notches", type=int, default=3, help="每次滚轮滚动几格（drag 模式下无效）")
    ap.add_argument("--at", default="0.72,0.55", help="滚动/拖拽的落点相对坐标 rx,ry")
    ap.add_argument("--mode", choices=["wheel", "drag"], default="wheel")
    ap.add_argument("--drag", default="0.58,0.25",
                    help="拖拽起止 y 相对坐标（起点>终点=内容向上滚；必须落在列表区域内）")
    ap.add_argument("--settle", type=float, default=0.8, help="每次滚动后等待秒数")
    ap.add_argument("--no-shot", action="store_true", help="不保存每帧截图")
    args = ap.parse_args()

    rx, ry = [float(x) for x in args.at.split(",")]

    ws = game_client()
    if not ws:
        print("找不到游戏窗口（k3_windows_ob.exe / UnityWndClass）")
        return 1
    hwnd, rect = ws[0]
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.4)

    from onnxocr.onnx_paddleocr import ONNXPaddleOcr

    print("加载 OCR ...")
    ocr = ONNXPaddleOcr(use_angle_cls=False, use_openvino=True)

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = time.strftime("%H%M%S")
    base = os.path.join(OUT_DIR, f"{args.tag}_{stamp}")

    frames = []
    for i in range(args.scroll + 1):
        frame = grab(rect)
        items = ocr_items(ocr, frame)
        frames.append(items)
        print(f"[帧 {i + 1}/{args.scroll + 1}] {len(items)} 段")
        if not args.no_shot:
            save_img(f"{base}_f{i + 1}.png", frame)
        if i == args.scroll:
            break
        if args.mode == "wheel":
            scroll_at(rect, rx, ry, args.notches)
        else:
            dy0, dy1 = [float(v) for v in args.drag.split(",")]
            drag_at(rect, rx, dy0, dy1)
        time.sleep(args.settle)

    # 去重总表：同一段文字只留第一次出现的那帧
    seen = {}
    for idx, items in enumerate(frames):
        for it in items:
            key = it["t"]
            if key not in seen:
                seen[key] = dict(it, frame=idx + 1)

    md = [f"# 可选清单 · {args.tag}", ""]
    md.append(f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    md.append(f"- 帧数：{len(frames)}（下滑 {args.scroll} 次，模式 {args.mode}）")
    md.append(f"- 去重后共 {len(seen)} 段文字")
    md.append("")
    md.append("## 逐帧原文（按屏幕从上到下）")
    for idx, items in enumerate(frames):
        md.append("")
        md.append(f"### 帧 {idx + 1}")
        for it in items:
            md.append(f"- `{it['t']}`  @({it['rx']:.3f},{it['ry']:.3f})")
    md.append("")
    md.append("## 去重总表（按首次出现的帧序）")
    md.append("")
    md.append("| # | 文字 | 首见帧 | 相对坐标 |")
    md.append("|---|---|---|---|")
    order = sorted(seen.values(), key=lambda i: (i["frame"], round(i["cy"] / 12), i["cx"]))
    for n, it in enumerate(order, 1):
        md.append(f"| {n} | {it['t']} | {it['frame']} | ({it['rx']:.3f}, {it['ry']:.3f}) |")

    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump({"tag": args.tag, "stamp": stamp, "scroll": args.scroll,
                   "mode": args.mode, "frames": frames,
                   "unique": list(seen.values())}, f, ensure_ascii=False, indent=2)

    print(f"\n清单已导出：{base}.md")
    print(f"原始数据：{base}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
