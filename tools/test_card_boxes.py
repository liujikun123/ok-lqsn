# -*- coding: utf-8 -*-
"""图形「选项卡」检测自测：合成图 + 真实截图误报统计。

跑法： ../ok-bsg/.venv/Scripts/python.exe tools/test_card_boxes.py
"""
import glob
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.tasks.AutoTask import AutoTask  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W, H = 1600, 900


class S:
    CARD_ROI = AutoTask.CARD_ROI
    _find_card_boxes = AutoTask._find_card_boxes
    _pick_box_by_pos = AutoTask._pick_box_by_pos
    _split_by_gaps = staticmethod(AutoTask._split_by_gaps)
    _frame_edge_score = staticmethod(AutoTask._frame_edge_score)


def canvas():
    """深色背景 + 一点噪声，模拟游戏里的场景图。"""
    img = np.full((H, W, 3), 40, dtype=np.uint8)
    noise = np.random.randint(0, 12, (H, W, 3), dtype=np.uint8)
    return cv2.add(img, noise)


def card(img, cx, cy, w, h, color=(235, 235, 235)):
    cv2.rectangle(img, (cx - w // 2, cy - h // 2), (cx + w // 2, cy + h // 2), color, -1)
    cv2.rectangle(img, (cx - w // 2, cy - h // 2), (cx + w // 2, cy + h // 2), (90, 90, 90), 4)
    return img


def run(name, img, want_n=None, want_dir=None, want_cx=None):
    boxes = S()._find_card_boxes(img)
    ok = True
    msg = f"{len(boxes)} 个"
    if want_n is not None and len(boxes) != want_n:
        ok = False
    if boxes:
        msg += f"（{'横排' if boxes[0]['horiz'] else '竖排'}）"
        if want_dir and (("横排" if boxes[0]["horiz"] else "竖排") != want_dir):
            ok = False
        if want_cx is not None:
            box, why = S()._pick_box_by_pos(boxes)
            got = box["cx"] / W
            msg += f" 选 {got:.2f}（{why}）"
            if abs(got - want_cx) > 0.06:
                ok = False
    print(f"{'✅' if ok else '❌'} {name}: {msg}")
    return ok


def main():
    ok = True

    # 横排三张等大卡片（事件页常见）
    img = canvas()
    for x in (350, 800, 1250):
        card(img, x, 560, 350, 250)
    ok &= run("横排三张 → 认 3 个、选最右", img, 3, "横排", 1250 / W)

    # 竖排三条（胶囊选项）
    img = canvas()
    for y in (300, 470, 640):
        card(img, 800, y, 760, 130)
    ok &= run("竖排三条 → 认 3 个、选最上", img, 3, "竖排", 800 / W)

    # 横排两张（二选一）
    img = canvas()
    for x in (520, 1080):
        card(img, x, 520, 420, 300)
    ok &= run("横排两张 → 选最右", img, 2, "横排", 1080 / W)

    # 空背景：不能瞎认
    ok &= run("纯背景 → 0 个", canvas(), 0)

    # 零散按钮（大小不一、间距不匀）：不能认成选项
    img = canvas()
    card(img, 300, 800, 160, 60)
    card(img, 900, 640, 90, 90)
    card(img, 1300, 300, 240, 70)
    ok &= run("零散按钮 → 0 个", img, 0)

    # 真实截图误报统计（screenshots/auto 里都是正常游戏页，不该有「选项排」）
    fps = sorted(glob.glob(os.path.join(ROOT, "screenshots", "auto", "*.png")))
    hit = []
    for p in fps:
        f = cv2.imread(p)
        if f is None:
            continue
        if len(S()._find_card_boxes(f)) >= 2:
            hit.append(os.path.basename(p))
    rate = len(hit) / max(len(fps), 1)
    print(f"{'✅' if rate < 0.10 else '❌'} 真实截图误报：{len(hit)}/{len(fps)} = {rate:.1%}"
          + (f"（{', '.join(hit[:4])}…）" if hit else ""))
    ok &= rate < 0.10

    print("\n全部通过" if ok else "\n有失败用例")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
