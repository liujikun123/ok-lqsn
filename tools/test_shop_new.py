# -*- coding: utf-8 -*-
"""商店「上新」角标识别自测（离线，不需要游戏在跑）。

玩家口径：商店不按周刷新，上新时图标上会挂「NEW」角标。
一屏可能同时有好几个 NEW（活动 / 商城 / 飞跃计划），所以只认**商店正上方**那个。
跑法： ../ok-bsg/.venv/Scripts/python.exe tools/test_shop_new.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from src.tasks.AutoTask import AutoTask  # noqa: E402


class Box:
    def __init__(self, h=30):
        self.height = h


class Stub:
    _shop_new_badge = AutoTask._shop_new_badge

    def log_info(self, m):
        pass


def it(t, x, y, h=30):
    return {"t": t, "cx": x, "cy": y, "b": Box(h)}


# 实机坐标基准（1600x900 帧）：商店按钮 @(1470,195)，角标在它正上方约 53px
SHOP = it("商店", 1470, 195)


def frame_with_green(x1, y1, x2, y2):
    f = np.zeros((900, 1600, 3), dtype=np.uint8)
    f[:] = 40
    f[y1:y2, x1:x2] = (70, 220, 120)   # BGR：亮绿（角标色）
    return f


def badge(items, frame=None):
    return Stub()._shop_new_badge(items, frame, SHOP)


CASES = []


def case(title):
    def deco(fn):
        CASES.append((title, fn))
        return fn
    return deco


@case("商店正上方有 NEW → 上新（ocr）")
def _():
    items = [SHOP, it("NEW", 1495, 118)]
    assert badge(items) == "ocr", badge(items)


@case("OCR 粘成 FNEW 也认")
def _():
    items = [SHOP, it("FNEW", 1495, 118)]
    assert badge(items) == "ocr", badge(items)


@case("OCR 把 NEW 读成 OA / SOA（同位置 2~3 个字母）也认")
def _():
    for t in ("OA", "SOA"):
        items = [SHOP, it(t, 1490, 118)]
        assert badge(items) == "ocr", (t, badge(items))


@case("实机截图里「活动」的 NEW(1247,118) 不算")
def _():
    items = [SHOP, it("NEW", 1247, 118)]
    assert badge(items) is None, badge(items)


@case("实机截图里「商城」的 NEW(1549,118) 不算（离商店 80px）")
def _():
    items = [SHOP, it("NEW", 1549, 118)]
    assert badge(items) is None, badge(items)


@case("商店下方的 NEW 不算")
def _():
    items = [SHOP, it("NEW", 1470, 250)]
    assert badge(items) is None, badge(items)


@case("没有 NEW → 不算")
def _():
    assert badge([SHOP, it("任务", 1247, 195)]) is None


def main():
    ok = fail = 0
    for title, fn in CASES:
        print(f"[CASE] {title}")
        try:
            fn()
            print("    ✅")
            ok += 1
        except AssertionError as e:
            print(f"    ❌ {e}")
            fail += 1
    print(f"\n{ok} passed, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
