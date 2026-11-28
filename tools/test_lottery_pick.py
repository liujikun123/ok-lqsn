# -*- coding: utf-8 -*-
"""抽奖三选一的位置规则自测（离线）：横排选最右 / 竖排选最上。

跑法： ../ok-bsg/.venv/Scripts/python.exe tools/test_lottery_pick.py
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


class Stub:
    _pick_by_pos = AutoTask._pick_by_pos


def T(t, cx, cy, w=120, h=40):
    return {"t": t, "cx": cx, "cy": cy,
            "b": Box(cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2)}


W, H = 1600, 900


def frame():
    import numpy as np
    return np.zeros((H, W, 3), dtype="uint8")


def run(name, items, want):
    it, why = Stub()._pick_by_pos(items, frame()) or (None, "None")
    ok = it is not None and it["t"] == want
    print(f"{'✅' if ok else '❌'} {name}: 选中「{it['t'] if it else None}」"
          f"（{why}）" + ("" if ok else f"  期望「{want}」"))
    return ok


def main():
    ok = True

    # 横排三张卡：每家店名 + 奖品名，同一行里两组文字
    ok &= run("横排三选一 → 最右", [
        T("抽奖啦！", 800, 120, 300, 80),
        T("美度", 380, 430), T("三等奖", 380, 500),
        T("汗屋", 800, 430), T("二等奖", 800, 500),
        T("乐得超市", 1220, 430), T("特等奖", 1220, 500),
    ], "乐得超市")

    # 只有店名一行（奖品名没读出来）
    ok &= run("横排只读到店名 → 最右", [
        T("抽奖啦！", 800, 120, 300, 80),
        T("美度", 380, 460), T("汗屋", 800, 460), T("乐得超市", 1220, 460),
    ], "乐得超市")

    # 竖排三张卡：上中下
    ok &= run("竖排三选一 → 最上", [
        T("抽奖啦！", 800, 110, 300, 70),
        T("美度", 800, 330), T("三等奖", 800, 380),
        T("汗屋", 800, 530), T("二等奖", 800, 580),
        T("乐得超市", 800, 730), T("特等奖", 800, 780),
    ], "美度")

    # 竖排、每张卡只有一行
    ok &= run("竖排单行 → 最上", [
        T("抽奖啦！", 800, 110, 300, 70),
        T("美度", 800, 320), T("汗屋", 800, 520), T("乐得超市", 800, 720),
    ], "美度")

    # 只有两个候选（漏读一个）：不能崩，按跨度判
    ok &= run("只读到两个（横排）→ 最右", [
        T("抽奖啦！", 800, 120, 300, 80),
        T("汗屋", 800, 460), T("乐得超市", 1220, 460),
    ], "乐得超市")

    # 全是噪声 → 返回 None，走兜底点击
    got = Stub()._pick_by_pos([T("抽奖啦！", 800, 120, 300, 80)], frame())
    print(f"{'✅' if got is None else '❌'} 只有标题 → 返回 None（兜底点击）")
    ok &= got is None

    print("\n全部通过" if ok else "\n有失败用例")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
