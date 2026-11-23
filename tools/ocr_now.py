"""对当前游戏画面做一次 OCR，打印每段文字和相对坐标。只读，不点任何按钮。

    ../ok-bsg/.venv/Scripts/python.exe tools/ocr_now.py
    ../ok-bsg/.venv/Scripts/python.exe tools/ocr_now.py --save   # 顺便存一张图到 logs/ocr_now/

用途：加页面规则前先看真实文字（README 铁律 1：先看再猜）。
"""

import argparse
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()

    ws = game_client()
    if not ws:
        print("找不到游戏窗口（k3_windows_ob.exe / UnityWndClass）")
        return 1
    hwnd, rect = ws[0]
    img = ImageGrab.grab(bbox=rect, all_screens=True)
    frame = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    print(f"画面 {frame.shape[1]}x{frame.shape[0]}  rect={rect}")

    if args.save:
        d = os.path.join(ROOT, "logs", "ocr_now")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, time.strftime("%H%M%S") + ".png")
        cv2.imwrite(p, frame)
        print("已存图", p)

    from onnxocr.onnx_paddleocr import ONNXPaddleOcr

    ocr = ONNXPaddleOcr(use_angle_cls=False, use_openvino=True)
    res = ocr.ocr(frame) or []
    h, w = frame.shape[:2]
    n = 0
    for line in res[0] if res and isinstance(res[0], list) else res:
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
        n += 1
        print(f"「{text}」@({cx:.0f},{cy:.0f}) 相对({cx / w:.3f},{cy / h:.3f}) w={max(xs) - min(xs):.0f} h={max(ys) - min(ys):.0f} score={score:.2f}")
    print(f"共 {n} 段")
    return 0


if __name__ == "__main__":
    sys.exit(main())
