# -*- coding: utf-8 -*-
"""周四抽奖「选店铺」页的自测（离线）：右 → 中 → 左 降级链。

玩家 2026-11-28 的规则：
    「这种左中右排列的，如果点了右边点不了确定，或者点右边点确定之后没反应，
      再试一下点中间点确定，然后点左边点确定。」

跑法： ../ok-bsg/.venv/Scripts/python.exe tools/test_shop_pick.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.tasks.AutoTask import AutoTask  # noqa: E402


class Box:
    def __init__(self, x1, y1, x2, y2):
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2
        self.width = x2 - x1
        self.height = y2 - y1


def T(t, cx, cy, w=140, h=44):
    return {"t": t, "cx": cx, "cy": cy,
            "b": Box(cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2)}


PAGE = [
    T("请选择要前往的店铺", 800, 120, 400, 60),
    T("风格过时的杂货铺", 380, 420),
    T("设计时尚的百货商场", 800, 420),
    T("装修奢华的玩偶店", 1220, 420),
    T("0", 380, 520), T("5", 800, 520), T("10", 1220, 520),
    T("确定", 800, 840, 200, 60),
]


class Stub:
    """只替换执行动作的三件套，状态机走真代码。"""
    _do_shop_pick = AutoTask._do_shop_pick

    def __init__(self):
        self.logs = []
        self.clicks = []

    def log_info(self, m):
        self.logs.append(("info", m))

    def log_warning(self, m):
        self.logs.append(("warn", m))

    def _shot(self, *a, **k):
        pass

    def _click_item(self, it, tag=""):
        self.clicks.append(it["t"])

    def _click_corner(self):
        self.clicks.append("右下角")

    def click_relative(self, x, y):
        self.clicks.append(f"elev({x},{y})")

    def _find(self, items, words, exact=False):
        for it in items:
            if it["t"] in words:
                return it
        return None

    def _do_pick_by_boxes(self, frame, tag=""):
        return False

    def _tuning(self):
        return {}


def step(s, label, expect):
    s._do_shop_pick(PAGE, None)
    got = s.clicks[-1] if s.clicks else None
    ok = got == expect
    print(f"{'✅' if ok else '❌'} {label}：点「{got}」"
          + ("" if ok else f"  期望「{expect}」"))
    return ok


def main():
    ok = True
    s = Stub()
    # 1) 第一次进来：选最右
    ok &= step(s, "进入选店铺 → 先选最右", "装修奢华的玩偶店")
    _shop_step = getattr(s, "_shop_step", None)
    print(f"    状态 step={_shop_step}")
    ok &= _shop_step == "confirm"
    # 2) 选完店 → 点确定
    ok &= step(s, "下一步 → 点确定", "确定")
    ok &= getattr(s, "_shop_step", None) == "verify"
    # 3) 还在这页 = 最右没生效 → 换中间
    ok &= step(s, "停在原地 → 降级选中间", "设计时尚的百货商场")
    ok &= getattr(s, "_shop_try", 0) == 1
    # 4) 再点确定
    ok &= step(s, "中间 → 点确定", "确定")
    # 5) 还没走 → 换最左
    ok &= step(s, "又停在原地 → 降级选最左", "风格过时的杂货铺")
    ok &= getattr(s, "_shop_try", 0) == 2
    # 6) 再点确定
    ok &= step(s, "最左 → 点确定", "确定")
    # 7) 三家都不行 → 点右下角退出，不死磕
    ok &= step(s, "三家全失败 → 点右下角退出", "右下角")
    ok &= getattr(s, "_shop_try", 0) == 0

    # 8) 成功一次后回到选店铺（下一周）应从最右重新开始
    s2 = Stub()
    s2._do_shop_pick(PAGE, None)          # 选最右
    s2._do_shop_pick(PAGE, None)          # 点确定
    # 模拟页面跳走了：主循环会复位
    s2._shop_step, s2._shop_try = "pick", 0
    ok &= step(s2, "下一次抽奖重新从最右开始", "装修奢华的玩偶店")

    print("\n" + ("全部通过" if ok else "有失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
