"""B: 页面识别视觉兜底 —— 全离线，用合成图像，不动游戏。

验证：① _right_card_count 能数出右侧多行黄色等级牌；② 空图/结果页不误判；
③ _classify 在 OCR 判「未知」时用视觉兜底认出选人页；④ 真结果页不被视觉信号误伤。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import cv2
from src.tasks.AutoTask import AutoTask

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {got!r}, want {want!r}")


class Box:
    height = 30


def make_card_frame(rows=4):
    """合成一张右侧有 N 行黄色等级牌的画面（选人页特征）。"""
    img = np.zeros((1080, 1920, 3), dtype="uint8")
    for i in range(rows):
        cy = int(0.12 * 1080) + i * 150
        # 黄色等级牌 BGR: (30, 200, 220)
        img[cy:cy + 30, int(0.78 * 1920):int(0.86 * 1920)] = (30, 200, 220)
    return img


def items(*texts):
    return [{"t": t, "cx": 100 + i * 80, "cy": 500, "b": Box()} for i, t in enumerate(texts)]


s = AutoTask.__new__(AutoTask)

print("--- 1. _right_card_count 数黄色等级牌 ---")
check("4 行卡 → >=2", s._right_card_count(make_card_frame(4)) >= 2, True)
check("2 行卡 → >=2", s._right_card_count(make_card_frame(2)) >= 2, True)
check("空黑图 → 0", s._right_card_count(np.zeros((1080, 1920, 3), np.uint8)), 0)
check("None → 0", s._right_card_count(None), 0)
# 结果页：只有底部一个大按钮，右侧无多行黄牌
res = np.zeros((1080, 1920, 3), np.uint8)
res[800:860, 700:1200] = (30, 200, 220)   # 底部中央一个按钮
check("结果页底部按钮不算右侧卡", s._right_card_count(res), 0)

print("--- 2. _classify 视觉兜底 ---")
# OCR 读出零星文字不足以判页，但画面右侧有卡 → 选择队友
frame = make_card_frame(4)
page = s._classify(items("第1周", "星期二", "110"), frame)
check("视觉兜底认出选人页", page, "选择队友")

print("--- 3. 真结果页不被视觉误伤 ---")
# 属性面板 OCR + 无右侧卡 → 仍是训练结果
frame2 = np.zeros((1080, 1920, 3), np.uint8)
page2 = s._classify(items("属性加成", "心·技·体", "投射", "弹跳", "运控"), frame2)
check("属性面板仍判训练结果", page2, "训练结果")
# 带 MVP 的比赛结果即使有卡也不认选人页
page3 = s._classify(items("MVP", "第3周"), frame)
check("MVP 结果页不被视觉抢走", page3 != "选择队友", True)

print(f"\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
