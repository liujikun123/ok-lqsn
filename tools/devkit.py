"""开发期工具共用件：窗口、抓屏、点击、OCR —— 只在这里实现一次。

为什么要有这个文件（2026-11-24 去重）：
    `survey.py`（界面勘察）和 `start_game.py`（启动器）原来各写了一套
    找窗口 / 抓屏 / 点击 / OCR，而且**OCR 参数还不一致**
    （start_game 只传 use_openvino=True，没传 use_npu=True）——
    同一个项目里两套 OCR 出来的框可能不同，勘察结论和实跑对不上。

    框架（ok）自己有 find_hwnd / get_window_bounds / HwndWindow / PynputInteraction，
    但那几样都绑在「活着的 DeviceManager + 任务实例」上，这两个脚本要在**没有任务**、
    甚至没打开游戏的时候跑，所以这里只复用框架里**无副作用**的部分
    （ok.util.window 的窗口查找与几何），其余用最小实现，并集中在这一处。

用法：
    from devkit import game_hwnd, bounds, front, grab, ocr_rows, click
"""
import ctypes
import os
import time

import numpy as np
import win32api
import win32con
import win32gui
import win32ui

TITLE = "篮球少女：飞跃巅峰"
FRAME = (1600, 900)      # 框架实际出帧尺寸（config resize_to）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_ENGINE = None


# ---------------- 窗口 ----------------

def game_hwnd(raise_if_missing=True):
    """找游戏窗口。优先用框架的 find_hwnd（它还会按 exe 名/比例/子窗口处理）。"""
    h = 0
    try:
        from ok.util.window import find_hwnd
        # 注意返回值是 8 元组：(窗口标题, hwnd, exe 全路径, x_off, y_off, w, h, 候选列表)
        res = find_hwnd(TITLE, None, FRAME[0], FRAME[1])
        if res and res[1]:
            h = int(res[1])
    except Exception:
        h = 0
    if not h:
        h = win32gui.FindWindow(None, TITLE) or 0
    if not h and raise_if_missing:
        raise SystemExit(f"找不到游戏窗口「{TITLE}」")
    return h


def client_bounds(hwnd=None):
    """客户区在屏幕上的位置：(left, top, width, height)。"""
    h = hwnd or game_hwnd()
    try:
        from ok.util.window import get_window_bounds
        b = get_window_bounds(h)
        # (client_x, client_y, 窗口宽, 窗口高, 客户区宽, 客户区高, dpi 缩放)
        if b and b[4] > 10 and b[5] > 10:
            return int(b[0]), int(b[1]), int(b[4]), int(b[5])
    except Exception:
        pass
    l, t, r, b = win32gui.GetWindowRect(h)
    return l, t, r - l, b - t


def front(hwnd=None):
    """把游戏窗口提到前台（Windows 有前台锁，先按一下 ALT 试探）。"""
    h = hwnd or game_hwnd()
    try:
        win32gui.ShowWindow(h, win32con.SW_RESTORE)
    except Exception:
        pass
    try:
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
    except Exception:
        pass
    try:
        win32gui.SetForegroundWindow(h)
    except Exception:
        pass
    time.sleep(0.35)
    return h


# ---------------- 抓屏 ----------------

def grab(hwnd=None):
    """PrintWindow 抓**客户区**，返回 BGR ndarray —— 和框架的 frame 同一个坐标系。

    PrintWindow 只能整窗抓（含标题栏），所以抓完再按客户区偏移裁掉边框/标题栏，
    否则存下来的图会带着 30~40px 的标题栏，和框架 1600x900 的帧对不上，
    勘察时量出来的坐标贴到脚本里就会整体偏一截。
    """
    h = hwnd or game_hwnd()
    wl, wt, wr, wb = win32gui.GetWindowRect(h)
    ww, wh = wr - wl, wb - wt
    cl, ct, cw, ch = client_bounds(h)
    dc = win32gui.GetWindowDC(h)
    mfc = win32ui.CreateDCFromHandle(dc)
    sav = mfc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc, ww, wh)
    sav.SelectObject(bmp)
    ctypes.windll.user32.PrintWindow(h, sav.GetSafeHdc(), 2)
    data = bmp.GetBitmapBits(True)
    img = np.frombuffer(data, dtype=np.uint8).reshape((wh, ww, 4))
    try:
        win32gui.ReleaseDC(h, dc)
        win32gui.DeleteObject(bmp.GetHandle())
        sav.DeleteDC()
        mfc.DeleteDC()
    except Exception:
        pass
    ox, oy = max(0, cl - wl), max(0, ct - wt)
    crop = img[oy:oy + ch, ox:ox + cw, :3]          # BGRA -> BGR，只留客户区
    return np.ascontiguousarray(crop)


def save(frame, path):
    """存图（非 ASCII 路径用 PIL 兜底，cv2.imwrite 在中文路径下会静默失败）。"""
    import cv2
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    ok = cv2.imwrite(path, frame)
    if not ok or not os.path.exists(path) or os.path.getsize(path) < 1024:
        from PIL import Image
        Image.fromarray(np.asarray(frame)[:, :, ::-1]).save(path)
    return path


# ---------------- 点击 ----------------

def click(x, y, hwnd=None):
    """按**图内绝对坐标**点一下（脚本内部统一用这个坐标系）。"""
    h = hwnd or front()
    l, t, _, _ = client_bounds(h)
    sx, sy = l + int(x), t + int(y)
    win32api.SetCursorPos((sx, sy))
    time.sleep(0.08)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    return sx, sy


# ---------------- OCR ----------------

def engine():
    """项目同款 OCR 引擎（参数必须和 src/config.py 的 ocr 段一致）。

    ⚠️ use_npu / use_openvino 两个都要 True：这是改过一次的坑 ——
    只传 openvino 会走 onnxruntime 后端，而这个 venv 没装 onnxruntime。
    """
    global _ENGINE
    if _ENGINE is None:
        from onnxocr.onnx_paddleocr import ONNXPaddleOcr
        _ENGINE = ONNXPaddleOcr(use_angle_cls=False, use_npu=True, use_openvino=True)
    return _ENGINE


def ocr_rows(frame=None):
    """对一帧做 OCR，返回 [(x1,y1,x2,y2,text,conf)]，按 y/x 排好序。"""
    arr = grab() if frame is None else frame
    res = engine().ocr(arr)
    rows = []
    for pos, item in ((res[0] if res else []) or []):
        txt, conf = item if isinstance(item, (tuple, list)) else (item, 1.0)
        xs = [p[0] for p in pos]
        ys = [p[1] for p in pos]
        rows.append((min(xs), min(ys), max(xs), max(ys), str(txt).strip(), float(conf)))
    rows.sort(key=lambda r: (r[1], r[0]))
    return rows


def find(rows, words, exact=False):
    """在 ocr_rows 结果里找第一个命中的行（子串或整串）。"""
    if isinstance(words, str):
        words = [words]
    for w in words:
        for r in rows:
            if (r[4] == w) if exact else (w in r[4]):
                return r
    return None


def center(row):
    return (row[0] + row[2]) / 2.0, (row[1] + row[3]) / 2.0
