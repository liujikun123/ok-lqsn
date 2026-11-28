# -*- coding: utf-8 -*-
"""商店 0 元商品自测（离线）。

玩家 2026-11-28：「第四周第一天进了商店，但是 0 元的东西没有买」。

0 元那件是本次训练专属的馈赠，白送必须拿；但它的名字常常是
「白神的笔记 / 白神的训练手册」，`_shop_kind()` 认不出类别 → 以前整件被丢掉。

跑法： ../ok-bsg/.venv/Scripts/python.exe tools/test_shop_zero.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.tasks.AutoTask import AutoTask  # noqa: E402


class Box:
    def __init__(self, h):
        self.height = h


def it(t, cx, cy, h=28):
    return {"t": t, "cx": cx, "cy": cy, "b": Box(h)}


def card(name, price, cx, cy=300):
    """一张商品卡：「名字 → 剩余：1 → 价格」，和游戏里的竖向层级一致。"""
    return [it(name, cx, cy), it("剩余：1", cx, cy + 174), it(str(price), cx, cy + 217)]


class Stub:
    SHOP_CATS = AutoTask.SHOP_CATS
    SHOP_NON_ITEM = AutoTask.SHOP_NON_ITEM
    _shop_kind = AutoTask._shop_kind
    _shop_price = AutoTask._shop_price
    _shop_offers = AutoTask._shop_offers
    _shop_plan = AutoTask._shop_plan
    _shop_zero_names = AutoTask._shop_zero_names
    GOLD_KEEP = AutoTask.GOLD_KEEP

    stamina = 130
    gold = 300
    notes_done = False
    shop_weekday = 2
    weekday = 2
    last_equip_count = 2
    logs = []

    def log_info(self, m):
        Stub.logs.append(m)

    def log_warning(self, m):
        Stub.logs.append("W:" + m)


SHOP_CFG = {
    "order": ["free", "milk", "stamina", "sway", "manual", "crit", "book", "stat", "charm"],
    "rules": {"milk": {"max_price": 100, "when_below": 50},
              "stamina": {"max_price": 50, "when_below": 50, "urgent_below": 35}},
    "keep_gold": 120,
    "saturday_stamina_min": 10,
}

CASES = []


def case(title):
    def deco(fn):
        CASES.append((title, fn))
        return fn
    return deco


def build(*cards):
    items = []
    for c in cards:
        items.extend(c)
    return items


@case("0 元 + 分类器认不出（白神的笔记）→ 进入货架且归为 free")
def _():
    offers = Stub()._shop_offers(build(card("白神的笔记", 0, 400)), None)
    assert len(offers) == 1, offers
    assert offers[0]["kind"] == "free", offers
    assert offers[0]["price"] == 0, offers


@case("0 元 + 分类器认得出（老板的馈赠）→ 也是 free")
def _():
    offers = Stub()._shop_offers(build(card("老板的馈赠", 0, 400)), None)
    assert len(offers) == 1 and offers[0]["kind"] == "free", offers


@case("价格文本「①0」不算商品（名字没有 ≥2 个汉字）")
def _():
    items = build(card("白神的笔记", 0, 400)) + [it("①0", 400, 517)]
    offers = Stub()._shop_offers(items, None)
    names = [o["name"] for o in offers]
    assert names == ["白神的笔记"], names


@case("0 元的排在最前面：有付费商品也先买它")
def _():
    items = build(card("弹跳教科书", 100, 400), card("白神的笔记", 0, 800))
    s = Stub()
    offers = s._shop_offers(items, None)
    o, why = s._shop_plan(offers, SHOP_CFG)
    assert o is not None, why
    assert o["name"] == "白神的笔记", (o, why)
    assert why == "free", why


@case("金币只剩 130（留底 120）也照买 0 元，但付费的不买")
def _():
    items = build(card("白神的笔记", 0, 400), card("奇妙弹跳秘籍", 45, 800))
    s = Stub()
    s.gold = 130
    offers = s._shop_offers(items, None)
    o, why = s._shop_plan(offers, SHOP_CFG)
    assert o is not None and o["name"] == "白神的笔记", (o, why)


@case("不是 0 元又认不出类别的（杂项）→ 仍然不买")
def _():
    offers = Stub()._shop_offers(build(card("奇怪的小摆件", 80, 400)), None)
    assert offers == [], offers


def main():
    passed = failed = 0
    for title, fn in CASES:
        Stub.logs = []
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
