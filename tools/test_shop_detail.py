# -*- coding: utf-8 -*-
"""商店详情面板核对自测（离线）：防止点错卡片买错东西。

来由（玩家 2026-11-28）：第一周第一天买了个 100 元的「弹跳秘籍」，只剩 90 块。
实拍 195626_shop.png 里脚本点的是「白神的馈赠」，右侧滑出来的却是「弹跳秘籍+」——
OCR 定位歪了就会买错，光看名字点卡是不保险的。

跑法： ../ok-bsg/.venv/Scripts/python.exe tools/test_shop_detail.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.tasks.AutoTask import AutoTask  # noqa: E402


class Box:
    def __init__(self, h=30):
        self.height = h


class Frame:
    shape = (900, 1600, 3)


def it(t, x, y):
    return {"t": t, "cx": x, "cy": y, "b": Box()}


class Stub:
    _shop_detail_ok = AutoTask._shop_detail_ok
    texts = []
    logs = []

    def _texts(self, frame):
        return self.texts

    def log_warning(self, m):
        self.logs.append(m)


def check(title_name, name):
    Stub.texts = [it(title_name, 1200, 120)] if title_name else []
    Stub.logs = []
    return Stub()._shop_detail_ok(Frame(), name)


CASES = []


def case(title):
    def deco(fn):
        CASES.append((title, fn))
        return fn
    return deco


@case("点开「白神的馈赠」但面板是「弹跳秘籍+」→ 判为点错")
def _():
    assert check("弹跳秘籍+", "白神的馈赠") is False


@case("点开「弹跳秘籍+」且面板就是它 → 放行")
def _():
    assert check("弹跳秘籍+", "弹跳秘籍+") is True


@case("OCR 少了个 + 号（「弹跳秘籍」vs「弹跳秘籍+」）→ 放行")
def _():
    assert check("弹跳秘籍", "弹跳秘籍+") is True


@case("面板标题还没渲染出来（读不到字）→ 放行，不能因为没读到就拦")
def _():
    assert check(None, "白神的馈赠") is True


@case("面板标题带效果文字（「弹跳+40」）也能对上")
def _():
    assert check("弹跳+40", "弹跳秘籍+") is True


@case("名字太短（1 个字）→ 不做判断，放行")
def _():
    assert check("弹跳秘籍+", "+") is True


def main():
    passed = failed = 0
    for title, fn in CASES:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f"[CASE] {title}\n    ❌ {exc}")
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            print(f"[CASE] {title}\n    💥 {type(exc).__name__}: {exc}")
        else:
            passed += 1
            print(f"[CASE] {title}\n    ✅")
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
