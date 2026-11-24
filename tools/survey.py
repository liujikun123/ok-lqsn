"""界面勘察辅助：一步步点游戏、截图、看 OCR 实际读到什么。

用法（虚拟环境 python，在 bsg-auto 目录下）：
  python tools/survey.py shot                 # 截图到 logs/survey_HHMMSS.png
  python tools/survey.py click 815 494        # 按图内绝对坐标点击（自动把游戏置前）
  python tools/survey.py clickr 0.42 0.44     # 按图内比例点击
  python tools/survey.py ocr                  # 对当前画面跑项目同款 OCR，打印文字+坐标
  python tools/survey.py all                  # 截图 + OCR 一起

2026-11-24 去重：窗口/抓屏/点击/OCR 全部收到 tools/devkit.py，这里只剩「命令行 + 打印」。
原来这份文件自己写了一套 win32 抓屏和 OCR 引擎，和 start_game.py、build_anchors.py
三份实现各有一点不一样（OCR 参数就对不上），勘察结论容易和实跑不一致。
"""
import os
import sys
import time

import devkit


def shot(tag=""):
    frame = devkit.grab()
    name = f"survey_{tag or time.strftime('%H%M%S')}.png"
    path = os.path.join(devkit.ROOT, "logs", name)
    devkit.save(frame, path)
    h, w = frame.shape[:2]
    l, t, _, _ = devkit.client_bounds()
    print(f"截图 {path} 尺寸 {w}x{h} 窗口左上 ({l},{t})")
    return frame


def ocr(frame=None):
    rows = devkit.ocr_rows(frame)
    print("=== OCR 结果 ===")
    for x1, y1, x2, y2, txt, conf in rows:
        print(f"  ({x1:4.0f},{y1:4.0f})-({x2:4.0f},{y2:4.0f})  {txt!r}  {conf:.2f}")
    return rows


def click(x, y):
    devkit.front()
    sx, sy = devkit.click(x, y)
    print(f"点击图内 ({x},{y}) = 屏幕 ({sx},{sy})")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd == "shot":
        shot(sys.argv[2] if len(sys.argv) > 2 else "")
    elif cmd == "click":
        click(float(sys.argv[2]), float(sys.argv[3]))
    elif cmd == "clickr":
        _, _, w, h = devkit.client_bounds()
        click(w * float(sys.argv[2]), h * float(sys.argv[3]))
    elif cmd == "ocr":
        ocr()
    elif cmd == "all":
        ocr(shot())
    else:
        print(__doc__)
