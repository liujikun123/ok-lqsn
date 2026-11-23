"""器材（编队支援卡）检测回归测试 —— 纯离线，喂历史截图。

    ..\\ok-bsg\\.venv\\Scripts\\python.exe tools\\test_equip.py

覆盖：
  1. 历史「训练核心页」帧（screenshots/sweep/0X_01~13）必须检出 1 张金卡；
  2. 过渡帧（_00）允许 0 张；
  3. 赛前准备页的球队大卡不允许被当成器材（尺寸上限）；
  4. 器材计分接进 _score_core（1 张金 = +1 分）。
"""
import glob
import os
import sys

import cv2
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from src.core.cards import find_cards, card_glow  # noqa: E402
from src.tasks.AutoTask import AutoTask  # noqa: E402


def rd(p):
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), 1)


def check(title, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {title} {detail}")
    return ok


def main():
    allok = True

    print("--- 历史训练核心帧：每帧 1 张金卡 ---")
    files = sorted(glob.glob(os.path.join(ROOT, "screenshots", "sweep", "*.png")))
    core = [f for f in files if not os.path.basename(f).endswith("_00.png")]
    trans = [f for f in files if os.path.basename(f).endswith("_00.png")]
    bad = []
    for f in core:
        im = rd(f)
        cs = find_cards(im)
        if len(cs) != 1 or card_glow(im, cs[0]) != "金":
            bad.append((os.path.basename(f), len(cs)))
    allok &= check(f"{len(core)} 张核心帧全部 1 张金卡", not bad, str(bad[:3]) if bad else "")

    print("--- 过渡帧（点开瞬间）允许 0 张 ---")
    okall = True
    for f in trans:
        cs = find_cards(rd(f))
        if len(cs) > 1:
            okall = False
    allok &= check(f"{len(trans)} 张过渡帧没有误检成多张", okall)

    print("--- 赛前准备页：大卡片不能被当成器材 ---")
    pre = os.path.join(ROOT, "screenshots", "auto", "103207_probe_core.png")
    if os.path.exists(pre):
        im = rd(pre)
        big = [c for c in find_cards(im) if c["w"] > 0.14 * im.shape[1]]
        allok &= check("没有超尺寸卡片混进来", not big, str([(c["w"], c["h"]) for c in big]))

    print("--- 计分接入 ---")
    cfg = {"teammate": 1.0, "level_bonus": {}, "fail_weight": 1.0,
           "equipment": 0.5, "equipment_tier": {"金": 1.0, "彩": 1.5, "无": 0.5}}
    got = AutoTask._score_core(None, ["a"], "LV.1", 0, cfg, ["金"])
    allok &= check("1 队友 + 1 金卡 = 2.0", abs(got - 2.0) < 1e-9, f"got {got}")

    print("\n结果:" + (" 全部通过" if allok else " 有失败项"))
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
