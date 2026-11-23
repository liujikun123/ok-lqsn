"""界面勘察辅助：一步步点游戏、截图、看 OCR 实际读到什么。
用法（虚拟环境 python）：
  python tools/survey.py shot                 # 截图到 logs/survey_HHMMSS.png
  python tools/survey.py click 815 494        # 按截图内绝对坐标点击（自动把游戏置前）
  python tools/survey.py clickr 0.42 0.44     # 按截图内比例点击
  python tools/survey.py ocr                  # 对当前画面跑项目同款 OCR，打印文字+坐标
  python tools/survey.py all                  # 截图 + OCR 一起
"""
import sys, os, time, ctypes
import win32gui, win32ui, win32con, win32api

TITLE = "篮球少女：飞跃巅峰"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def hwnd():
    h = win32gui.FindWindow(None, TITLE)
    if not h:
        raise SystemExit("找不到游戏窗口")
    return h


def front():
    h = hwnd()
    try:
        win32gui.ShowWindow(h, win32con.SW_RESTORE)
    except Exception:
        pass
    # Windows 前台限制：ALT 键试探绕过
    try:
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
    except Exception:
        pass
    win32gui.SetForegroundWindow(h)
    time.sleep(0.35)
    return h


def shot(tag=""):
    h = hwnd()
    l, t, r, b = win32gui.GetWindowRect(h)
    w, hh = r - l, b - t
    dc = win32gui.GetWindowDC(h)
    mfc = win32ui.CreateDCFromHandle(dc)
    sav = mfc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc, w, hh)
    sav.SelectObject(bmp)
    ctypes.windll.user32.PrintWindow(h, sav.GetSafeHdc(), 2)
    data = bmp.GetBitmapBits(True)
    from PIL import Image
    img = Image.frombuffer("RGB", (w, hh), data, "raw", "BGRX", 0, 1)
    name = f"survey_{tag or time.strftime('%H%M%S')}.png"
    path = os.path.join(ROOT, "logs", name)
    img.save(path)
    win32gui.ReleaseDC(h, dc)
    print(f"截图 {path} 尺寸 {w}x{hh} 窗口左上 ({l},{t})")
    return img


def click(x, y):
    front()
    h = hwnd()
    l, t, r, b = win32gui.GetWindowRect(h)
    sx, sy = l + int(x), t + int(y)
    win32api.SetCursorPos((sx, sy))
    time.sleep(0.1)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0)
    time.sleep(0.06)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)
    print(f"点击图内 ({x},{y}) = 屏幕 ({sx},{sy})")


_ENGINE = None


def _engine():
    global _ENGINE
    if _ENGINE is None:
        from onnxocr.onnx_paddleocr import ONNXPaddleOcr
        # 必须和项目 src/config.py 的 ocr 配置一致：openvino + npu。
        # 传 False 会走 onnxruntime 后端，而这个 venv 没装 onnxruntime → 初始化报错。
        _ENGINE = ONNXPaddleOcr(use_angle_cls=False, use_npu=True, use_openvino=True)
    return _ENGINE


def ocr(frame=None):
    """用项目同款 onnxocr(ONNXPaddleOcr) 读当前画面，打印文字+坐标。"""
    import numpy as np
    from PIL import Image
    img = frame if frame is not None else shot("ocr_tmp")
    arr = np.array(img.convert("RGB"))[:, :, ::-1]  # PIL RGB -> BGR
    result = _engine().ocr(arr)
    # onnxocr ppocrv5 返回 [ [(box, (text, conf)), ...] ]（外层是单张图 list）
    rows = []
    groups = result[0] if result else []
    for pos, item in (groups or []):
        txt, conf = item if isinstance(item, (tuple, list)) else (item, 1.0)
        xs = [p[0] for p in pos]; ys = [p[1] for p in pos]
        rows.append((min(xs), min(ys), max(xs), max(ys), txt.strip(), conf))
    rows.sort(key=lambda r: (r[1], r[0]))
    print("=== OCR 结果 ===")
    for x1, y1, x2, y2, txt, conf in rows:
        print(f"  ({x1:4.0f},{y1:4.0f})-({x2:4.0f},{y2:4.0f})  {txt!r}  {conf:.2f}")
    return rows


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd == "shot":
        shot(sys.argv[2] if len(sys.argv) > 2 else "")
    elif cmd == "click":
        click(float(sys.argv[2]), float(sys.argv[3]))
    elif cmd == "clickr":
        h = hwnd(); l, t, r, b = win32gui.GetWindowRect(h)
        click((r - l) * float(sys.argv[2]), (b - t) * float(sys.argv[3]))
    elif cmd == "ocr":
        ocr()
    elif cmd == "all":
        shot(); ocr()
    else:
        print(__doc__)
