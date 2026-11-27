# -*- coding: utf-8 -*-
"""商店「上新」角标识别自测（离线，不需要游戏在跑）。

玩家口径：商店不按周刷新（局内商店页底部是「上新时间 N天后」倒计时），
上新时**局内每日页**的商店图标会挂角标，OCR 能读到。
位置约束还没用局内真实样本校准，故意留宽；跑一轮看日志再收紧。
跑法： ../ok-bsg/.venv/Scripts/python.exe tools/test_shop_new.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.tasks.AutoTask import AutoTask  # noqa: E402


class Box:
    def __init__(self, h=30):
        self.height = h


class Stub:
    _shop_new_badge = AutoTask._shop_new_badge

    def log_info(self, m):
        pass

    def log_warning(self, m):
        pass

    def _day_key(self, items):
        return "第3周-星期五"       # 校准埋点要读日期；这里给个固定值即可


def it(t, x, y, h=30):
    return {"t": t, "cx": x, "cy": y, "b": Box(h)}


# 商店按钮坐标基准（局内每日页待校准，这里取日志里出现过的 (1469,195)）
SHOP = it("商店", 1469, 195)


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


@case("横向差 3.5 个字宽（别的图标上的 NEW）不算")
def _():
    items = [SHOP, it("NEW", 1469 - 105, 118)]
    assert badge(items) is None, badge(items)


@case("商店下方的 NEW 不算")
def _():
    items = [SHOP, it("NEW", 1469, 250)]
    assert badge(items) is None, badge(items)


@case("没有 NEW → 不算")
def _():
    assert badge([SHOP, it("任务", 1469, 118)]) is None


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
