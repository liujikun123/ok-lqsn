# -*- coding: utf-8 -*-
"""进店判断自测（离线）：精力低 / 图标「新品上市」/ 周日 / 已处理。

跑法： ../ok-bsg/.venv/Scripts/python.exe tools/test_shop_entry.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.tasks.AutoTask import AutoTask  # noqa: E402


class Box:
    def __init__(self, h=28):
        self.height = h


class Stub:
    _want_shop = AutoTask._want_shop
    _shop_verdict = AutoTask._shop_verdict
    _shop_new_badge = AutoTask._shop_new_badge
    _shop_day_key = AutoTask._shop_day_key

    # 默认状态
    stamina = 100
    shop_day = None
    shop_week = None
    shop_bought_trip = 0
    shop_weekday = None
    weekday = None       # 「上一帧读到的星期几」，_want_shop 在当天读不到时用它兜底
    _shop_verdict_key = None
    logs = []

    # 假的页面读数（用例里改）
    day = "2-3"          # 与真实 _day_key 一致：f"{week}-{weekday}"
    week_no = 2
    wd = 3
    badge = None
    cfg = {"enable": True, "skip": [7], "when_below": 50}

    def config_get(self, k, d=None):
        return self.cfg["enable"] if k == "商店自动购买" else d

    def __getitem__(self, k):          # self.config.get(...) 走这里
        return self.config_get(k)

    class _Cfg(dict):
        def __init__(self, outer):
            super().__init__()
            self.outer = outer

        def get(self, k, d=None):
            return self.outer.config_get(k, d)

    def _tuning(self):
        return {"shop": {"skip_weekdays": self.cfg["skip"],
                         "rules": {"stamina": {"when_below": self.cfg["when_below"]}}}}

    def _day_key(self, items):
        return self.day if (self.week_no and self.wd) else None

    def _read_week(self, items):
        return (self.week_no, self.wd)

    def _shop_button(self, items, frame=None):
        return SHOP

    def log_info(self, m):
        Stub.logs.append(m)

    def log_warning(self, m):
        Stub.logs.append("W:" + m)


def it(t, x, y, h=28):
    return {"t": t, "cx": x, "cy": y, "b": Box(h)}


SHOP = it("商店", 1469, 195)

CASES = []


def case(title):
    def deco(fn):
        CASES.append((title, fn))
        return fn
    return deco


def run(**kw):
    s = Stub()
    s.config = Stub._Cfg(s)
    s.shop_weekday = None
    s._shop_verdict_key = None
    s.badge = None
    s.day = "2-3"
    s.week_no = 2
    s.wd = 3
    s.stamina = 100
    for k, v in kw.items():
        setattr(s, k, v)
    s._shop_new_badge = lambda items, frame=None, shop=None: s.badge
    return s._want_shop([SHOP], None, SHOP), s


@case("精力 <50 → 进店")
def _():
    ok, _s = run(stamina=40)
    assert ok is True, ok


@case("精力 50（等于门槛）且没上新 → 不进")
def _():
    ok, s = run(stamina=50)
    assert ok is False, s.logs
    assert any("新品上市" in m for m in s.logs), s.logs


@case("图标挂「新品上市」→ 进店")
def _():
    ok, s = run(stamina=100, badge="ocr")
    assert ok is True, s.logs


@case("真图偏移：商店@(1469,195) 字高28，角标在其上方 2.8 字高 → 命中")
def _():
    items = [SHOP, it("新品上市", 1469, 195 - 78)]
    assert Stub()._shop_new_badge(items, None, SHOP) == "ocr"
    s = Stub()
    assert s._shop_new_badge([SHOP, it("新品上市", 1500, 195 - 78)], None, SHOP) == "ocr"


@case("OCR 把「新品上市」拆成「新品」「上市」两段 → 也命中")
def _():
    for t in ("新品", "上市", "上币"):
        items = [SHOP, it(t, 1469, 117)]
        assert Stub()._shop_new_badge(items, None, SHOP) == "ocr", t


@case("商店上方是别的文字（「任务」/「活动」）→ 不算上新")
def _():
    s = Stub()
    for t in ("任务", "活动", "商城"):
        assert s._shop_new_badge([SHOP, it(t, 1469, 117)], None, SHOP) is None, t


@case("横向差 4 个字宽（别的图标挂的新品上市）→ 不算")
def _():
    items = [SHOP, it("新品上市", 1469 + 112, 117)]
    assert Stub()._shop_new_badge(items, None, SHOP) is None


@case("商店正下方（不是上方）的「新品上市」→ 不算")
def _():
    items = [SHOP, it("新品上市", 1469, 195 + 78)]
    assert Stub()._shop_new_badge(items, None, SHOP) is None


@case("周日 → 不进（哪怕精力见底）")
def _():
    ok, s = run(wd=7, stamina=10)
    assert ok is False, s.logs
    assert any("星期7" in m for m in Stub.logs), Stub.logs


@case("今天已经处理过 → 不进")
def _():
    ok, s = run(shop_day="2-3", stamina=10)
    assert ok is False, s.logs


@case("读不到星期几 + 图标挂「新品上市」→ 照进（记账降级成 4-?）")
def _():
    # 玩家 2026-11-28：第 4 周星期一这天只读到「第4周」，星期几丢了，
    # 旧代码把「读不到日期」当闸门，整天的上新直接错过。进店只看上新和精力。
    ok, s = run(week_no=4, wd=None, stamina=130, badge="ocr")
    assert ok is True, s.logs
    assert any("4-?" in m for m in s.logs), s.logs


@case("读不到星期几 + 精力 40 → 照进")
def _():
    ok, s = run(week_no=4, wd=None, stamina=40)
    assert ok is True, s.logs


@case("读不到星期几 + 精力够 + 没上新 → 不进，且日志写明降级键")
def _():
    ok, s = run(week_no=4, wd=None, stamina=130)
    assert ok is False, s.logs
    assert any("4-?" in m and "新品上市" in m for m in s.logs), s.logs


@case("连第几周都读不到 → 记账键退成 ?，判断照旧")
def _():
    ok, s = run(week_no=None, wd=None, stamina=130, badge="ocr")
    assert ok is True, s.logs
    ok2, s2 = run(week_no=None, wd=None, stamina=130)
    assert ok2 is False, s2.logs


@case("日期降级后同一天不重复进店（shop_day='4-?'）")
def _():
    ok, s = run(week_no=4, wd=None, stamina=130, badge="ocr", shop_day="4-?")
    assert ok is False, s.logs
    assert any("已经处理过" in m for m in s.logs), s.logs


@case("配置关了商店自动购买 → 不进")
def _():
    ok, s = run(stamina=10)
    s.cfg["enable"] = False
    ok, s = run(stamina=10)
    assert ok is False, s.logs


# --- 玩家 2026-11-28：第 7 周起收尾期，第 1 / 3 天固定进店清钱 ---

@case("第 7 周第 1 天：精力满、没上新 → 也进（收尾期固定进店）")
def _():
    ok, s = run(week_no=7, wd=1, stamina=130)
    assert ok is True, s.logs
    assert any("收尾期" in m for m in s.logs), s.logs


@case("第 7 周第 3 天：精力满、没上新 → 也进")
def _():
    ok, s = run(week_no=7, wd=3, stamina=130)
    assert ok is True, s.logs


@case("第 7 周第 2 天：精力满、没上新 → 不进（不是固定进店日）")
def _():
    ok, s = run(week_no=7, wd=2, stamina=130)
    assert ok is False, s.logs


@case("第 6 周第 1 天：还没到收尾期 → 不进")
def _():
    ok, s = run(week_no=6, wd=1, stamina=130)
    assert ok is False, s.logs


@case("第 7 周第 1 天但今天是周日 → 仍然不进（周日规则优先）")
def _():
    ok, s = run(week_no=7, wd=7, stamina=130)
    assert ok is False, s.logs


@case("第 7 周第 3 天读到星期几丢了 + 精力满 → 赚一次（收尾期不卡日期）")
def _():
    ok, s = run(week_no=7, wd=None, stamina=130)
    assert ok is True, s.logs


def main():
    passed = failed = 0
    for title, fn in CASES:
        Stub.logs = []
        Stub.cfg = {"enable": True, "skip": [7], "when_below": 50}
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
