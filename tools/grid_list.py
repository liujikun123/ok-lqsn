"""把页面上的「网格列表」逐格编号导出：整图标注 + 每格小图 + 清单。

用在「选择器材」这种只有图标、没有名字的网格页 —— OCR 读不出器材名，
只能靠编号 + 小图让玩家指认（"我要第 2 行第 3 个"）。

    # 基本用法：四列三行，区域用相对坐标框住
    python tools/grid_list.py --tag 4选择器材 --box 0.645,0.178,0.966,0.662 --cols 4 --rows 3

    # 网格能滚的话（先按 --scroll 试，滚完内容会变才说明有效）
    python tools/grid_list.py --tag 4选择器材 --box ... --scroll 3 --at 0.80,0.45

输出（logs/grid_list/）：
    <tag>_<HHMMSS>_marked.png   在原图上画了编号的整图（编号=行优先 1..N）
    <tag>_<HHMMSS>_r1c2.png     每格小图，命名 r行c列
    <tag>_<HHMMSS>.md           清单（编号 / 行列 / 截图 / 格内 OCR 文字）

只读：除了滚动，不点任何按钮。
"""

import argparse
import ctypes
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
OUT_DIR = os.path.join(ROOT, "logs", "grid_list")
MOUSEEVENTF_WHEEL = 0x0800


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
    """cv2.imwrite 在 Windows 上写不了中文路径（会变成 4閫夋嫨 这种乱码名），
    所以统一走 imencode + tofile。"""
    ext = os.path.splitext(path)[1] or ".png"
    ok, buf = cv2.imencode(ext, img)
    if not ok:
        raise IOError(f"编码失败 {path}")
    buf.tofile(path)


def scroll_at(rect, rx, ry, notches):
    l, t, r, b = rect
    x, y = int(l + (r - l) * rx), int(t + (b - t) * ry)
    ctypes.windll.user32.SetCursorPos(x, y)
    time.sleep(0.15)
    ctypes.windll.user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, -120 * notches, 0)


def drag_at(rect, rx, y_from, y_to, steps=20):
    """按住左键拖拽。列表只认拖拽不认滚轮时用这个（器材页实测如此）。"""
    l, t, r, b = rect
    w, h = r - l, b - t
    x = int(l + w * rx)
    y0, y1 = int(t + h * y_from), int(t + h * y_to)
    ctypes.windll.user32.SetCursorPos(x, y0)
    time.sleep(0.25)
    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.15)
    for i in range(1, steps + 1):
        ctypes.windll.user32.SetCursorPos(x, int(y0 + (y1 - y0) * i / steps))
        time.sleep(0.02)
    time.sleep(0.1)
    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--box", required=True, help="网格区域 相对坐标 x0,y0,x1,y1")
    ap.add_argument("--cols", type=int, required=True)
    ap.add_argument("--rows", type=int, required=True)
    ap.add_argument("--scroll", type=int, default=0, help="滚动次数（用于确认网格能不能滚）")
    ap.add_argument("--notches", type=int, default=3)
    ap.add_argument("--at", default="0.80,0.45")
    ap.add_argument("--mode", choices=["wheel", "drag"], default="wheel")
    ap.add_argument("--drag", default="0.58,0.25", help="拖拽起止 y（起点=列表内偏下）")
    args = ap.parse_args()

    x0, y0, x1, y1 = [float(v) for v in args.box.split(",")]
    sr, sy = [float(v) for v in args.at.split(",")]

    ws = game_client()
    if not ws:
        print("找不到游戏窗口")
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

    def ocr_in(img):
        res = ocr.ocr(img) or []
        lines = res[0] if res and isinstance(res[0], list) else res
        out = []
        for line in lines:
            try:
                text = (line[1][0] or "").strip()
            except Exception:
                continue
            if text:
                out.append(text)
        return out

    rounds = []
    for i in range(args.scroll + 1):
        frame = grab(rect)
        h, w = frame.shape[:2]
        bx0, by0, bx1, by1 = int(w * x0), int(h * y0), int(w * x1), int(h * y1)
        cw = (bx1 - bx0) / args.cols
        ch = (by1 - by0) / args.rows

        marked = frame.copy()
        cv2.rectangle(marked, (bx0, by0), (bx1, by1), (0, 0, 255), 2)
        cells = []
        n = 0
        for r in range(args.rows):
            for c in range(args.cols):
                n += 1
                cx0, cy0 = int(bx0 + cw * c), int(by0 + ch * r)
                cx1, cy1 = int(bx0 + cw * (c + 1)), int(by0 + ch * (r + 1))
                cell = frame[cy0:cy1, cx0:cx1]
                if cell.size == 0:
                    continue
                texts = ocr_in(cell)
                cell_path = f"{base}_r{r + 1}c{c + 1}.png"
                save_img(cell_path, cell)
                # 画编号（只用 ASCII，避免 cv2 中文乱码）
                cv2.rectangle(marked, (cx0, cy0), (cx1, cy1), (0, 255, 0), 1)
                label = f"{n}"
                cv2.putText(marked, label, (cx0 + 6, cy0 + 26),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 4, cv2.LINE_AA)
                cv2.putText(marked, label, (cx0 + 6, cy0 + 26),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255), 2, cv2.LINE_AA)
                cells.append({"n": n, "r": r + 1, "c": c + 1,
                              "texts": texts, "png": os.path.basename(cell_path),
                              "rx": round((cx0 + cx1) / 2 / w, 4),
                              "ry": round((cy0 + cy1) / 2 / h, 4)})
        mp = f"{base}_marked_f{i + 1}.png"
        save_img(mp, marked)
        rounds.append({"frame": i + 1, "cells": cells, "marked": os.path.basename(mp)})
        print(f"[帧 {i + 1}/{args.scroll + 1}] {len(cells)} 格 → {os.path.basename(mp)}")
        if i < args.scroll:
            if args.mode == "wheel":
                scroll_at(rect, sr, sy, args.notches)
            else:
                dy0, dy1 = [float(v) for v in args.drag.split(",")]
                drag_at(rect, sr, dy0, dy1)
            time.sleep(0.9)

    md = [f"# 网格清单 · {args.tag}", "",
          f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
          f"- 网格：{args.cols} 列 × {args.rows} 行，区域相对坐标 ({x0},{y0})-({x1},{y1})",
          f"- 滚动 {args.scroll} 次（滚完内容变 = 能滚）", ""]
    for rd in rounds:
        md.append(f"## 帧 {rd['frame']} · 标注图 `{rd['marked']}`")
        md.append("")
        md.append("| 编号 | 行列 | 格内文字 | 相对坐标 | 小图 |")
        md.append("|---|---|---|---|---|")
        for cell in rd["cells"]:
            txt = " / ".join(cell["texts"]) or "—"
            md.append(f"| {cell['n']} | r{cell['r']}c{cell['c']} | {txt} | "
                      f"({cell['rx']:.3f}, {cell['ry']:.3f}) | `{cell['png']}` |")
        md.append("")

    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    print(f"\n清单：{base}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
