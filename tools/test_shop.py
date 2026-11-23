# -*- coding: utf-8 -*-
"""商店白名单离线自测（不需要游戏在跑）。

用实机商店截图 OCR 出来的真实布局做样本：
  每行 3 张卡 —— 名字在上（可带 +/++），价格是**纯数字**在名字下方，
  再下面是「剩余:N」；顶部「上新时间 7天后」，底部 tab「比赛/商店/能力详情」。
跑法： ../ok-bsg/.venv/Scripts/python.exe tools/test_shop.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Box:
    def __init__(self, h=30):
        self.height = h


class Stub:
    stamina = 80
    last_equip_count = 0
    shop_has_stamina = None

    def log_info(self, m):
        print(f"    info | {m}")

    def log_warning(self, m):
        print(f"    warn | {m}")

    def _tuning(self):
        import yaml
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "config", "strategy.yaml"), encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("tuning", {}) or {}


def it(t, x, y, h=30):
    return {"t": t, "cx": x, "cy": y, "b": Box(h)}


def shelf(goods, tab=True):
    """goods = [(名字, x, y, 价格)]，价格 None 表示 OCR 没读到。"""
    items = [it("上新时间 7天后", 300, 200)]
    if tab:
        items += [it("比赛", 700, 1030), it("商店", 960, 1030), it("能力详情", 1220, 1030)]
    for name, x, y, price in goods:
        items.append(it(name, x, y))
        if price is not None:
            items.append(it(str(price), x, y + 52))
        items.append(it("剩余:1", x, y + 104))
    return items


CASES = []


def case(title):
    def deco(fn):
        CASES.append((title, fn))
        return fn
    return deco


SHOP = Stub()
SHOP._shop_kind = __import__("importlib").import_module("src.tasks.AutoTask").AutoTask._shop_kind
from src.tasks.AutoTask import AutoTask  # noqa: E402

for m in ("_shop_kind", "_shop_price", "_shop_offers", "_shop_plan"):
    setattr(Stub, m, getattr(AutoTask, m))
Stub.SHOP_CATS = AutoTask.SHOP_CATS
Stub.SHOP_NON_ITEM = AutoTask.SHOP_NON_ITEM


def plan(goods, stamina=80, equip=0):
    Stub.stamina = stamina
    Stub.last_equip_count = equip
    s = Stub()
    cfg = s._tuning().get("shop") or {}
    offers = s._shop_offers(shelf(goods), None)
    o, why = s._shop_plan(offers, cfg)
    return (o["name"] if o else None), why, offers


@case("0 元馈赠必买（哪怕精力满）")
def _():
    name, why, offers = plan([
        ("老板的馈赠+", 420, 420, 0),
        ("特训牛奶", 900, 420, 50),
    ], stamina=130)
    assert name == "老板的馈赠+", (name, offers)
    assert len(offers) == 2, offers


@case("没馈赠时：精力低 → 买 50 元以下的精力饮料")
def _():
    name, _, _ = plan([
        ("怪味饮料+", 420, 420, 40),
        ("特训牛奶", 900, 420, 50),
    ], stamina=30)
    assert name == "怪味饮料+", name


@case("精力够（>=50）→ 不买饮料，改买特训牛奶")
def _():
    name, _, _ = plan([
        ("怪味饮料+", 420, 420, 40),
        ("特训牛奶", 900, 420, 50),
    ], stamina=80)
    assert name == "特训牛奶", name


@case("精力饮料 130 元 > 上限 50 → 跳过（wiki：只买 50 以下）")
def _():
    name, why, _ = plan([
        ("摇摆专属饮料++", 420, 420, 130),
    ], stamina=20)
    assert name is None, (name, why)


@case("器材暴击：上一场没器材 → 不买")
def _():
    name, why, _ = plan([("器材暴击符", 420, 420, 90)], stamina=10, equip=0)
    assert name is None, (name, why)


@case("器材暴击：上一场有 2 件器材 → 买")
def _():
    name, _, _ = plan([("器材暴击符", 420, 420, 90)], stamina=10, equip=2)
    assert name == "器材暴击符", name


@case("价格是纯数字也能读到（不是只认「N元」）")
def _():
    s = Stub()
    items = shelf([("投射秘籍", 420, 420, 60)])
    offers = s._shop_offers(items, None)
    assert offers[0]["price"] == 60, offers


@case("「剩余:N」「上新时间」「tab」不会被当商品")
def _():
    s = Stub()
    offers = s._shop_offers(shelf([("投射秘籍", 420, 420, 60)]), None)
    names = [o["name"] for o in offers]
    assert names == ["投射秘籍"], names


@case("认不出的杂项不买")
def _():
    name, why, offers = plan([("白色杂项", 420, 420, 20)])
    assert name is None and not offers, (name, offers)


@case("属性秘籍 60 元 → 在 80 上限内，能买")
def _():
    name, _, _ = plan([("弹跳秘籍", 420, 420, 60)])
    assert name == "弹跳秘籍", name


@case("价格超出上限的课本不买")
def _():
    name, why, _ = plan([("投射秘籍", 420, 420, 200)])
    assert name is None, (name, why)


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
