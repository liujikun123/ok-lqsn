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
    gold = 500          # 顶栏金币，留底逻辑需要；500 表示钱够，不影响原选货用例

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
    # 布局按实机 1920x1080 截图逐字量出来的间距：
    #   名字 y → 「剩余：1」y+79 → 价格 y+124（价格在剩余下面约 43px）
    for name, x, y, price in goods:
        items.append(it(name, x, y))
        items.append(it("剩余：1", x, y + 79))
        if price is not None:
            items.append(it(str(price), x, y + 124))
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
Stub.GOLD_KEEP = AutoTask.GOLD_KEEP


import copy


def plan(goods, stamina=80, equip=0, gold=500, enable=None):
    """enable: 要临时打开的白名单类别（如 ['stat','crit']），用于测算法本身。"""
    Stub.stamina = stamina
    Stub.last_equip_count = equip
    Stub.gold = gold
    s = Stub()
    cfg = copy.deepcopy(s._tuning().get("shop") or {})
    for cat in (enable or []):
        (cfg.setdefault("rules", {}).setdefault(cat, {}))["enabled"] = True
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


@case("器材暴击：上一场有 2 件器材 → 买（显式开启该规则时）")
def _():
    name, _, _ = plan([("器材暴击符", 420, 420, 90)], stamina=10, equip=2, enable=["crit"])
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


@case("属性秘籍 60 元 → 显式开启时在 80 上限内，能买")
def _():
    name, _, _ = plan([("弹跳秘籍", 420, 420, 60)], enable=["stat"])
    assert name == "弹跳秘籍", name


@case("价格超出上限的课本不买")
def _():
    name, why, _ = plan([("投射秘籍", 420, 420, 200)], enable=["book"])
    assert name is None, (name, why)


# --- 玩家 2026-11-23 16:25：留 120 给笔记 + 默认不买秘籍类 ---

@case("默认规则下：属性秘籍不买（玩家16:25质问为何买秘籍）")
def _():
    name, why, _ = plan([("弹跳秘籍", 420, 420, 60)], gold=500)
    assert name is None, (name, why, "stat 默认 on:false")


@case("金币 155：买 60 元秘籍会剩 95<120 → 不买（哪怕显式开启 stat）")
def _():
    name, why, _ = plan([("弹跳秘籍", 420, 420, 60)], gold=155, enable=["stat"])
    assert name is None, (name, why)


@case("金币 200：买 60 元秘籍剩 140≥120 → 可以买（显式开启 stat）")
def _():
    name, _, _ = plan([("弹跳秘籍", 420, 420, 60)], gold=200, enable=["stat"])
    assert name == "弹跳秘籍", name


@case("留底不拦 0 元馈赠：金币 0 也白拿")
def _():
    name, _, _ = plan([("老板的馈赠+", 420, 420, 0)], gold=0)
    assert name == "老板的馈赠+", name


@case("金币读不到(None)：付费项不买，0元仍买")
def _():
    name, why, _ = plan([("特训牛奶", 900, 420, 50)], gold=None)
    assert name is None, (name, why)
    s = Stub(); s.gold = None
    cfg = s._tuning().get("shop") or {}
    offers = s._shop_offers(shelf([("老板的馈赠+", 420, 420, 0)]), None)
    o, _ = s._shop_plan(offers, cfg)
    assert o and o["name"] == "老板的馈赠+", offers


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
