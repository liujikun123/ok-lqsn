"""空位「+」检测的离线自测（合成图）。

为什么要有这个：实跑这几局从来没报过空槽，也没留下选人页截图，
所以「检测到底跑没跑 / 参数对不对」一直没证据。这里用合成图把三种典型情况钉住：

  1. 空地槽（金框 + 白底 + 黄十字）           → 必须检出
  2. 已选中的卡（整块金黄，没有白底十字）     → 不能检出
  3. 灰色虚线框（没有金框）                   → 检出不了（已知限制，日志里会写原因）

真图校准靠 `screenshots/auto/*_slots_*.png`（每局进选人页自动存一张）。

用法：../ok-bsg/.venv/Scripts/python.exe tools/test_slots.py
"""

import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.tasks.AutoTask import AutoTask  # noqa: E402

W, H = 1920, 1080
GOLD = (60, 180, 245)      # BGR 金黄
WHITE = (250, 250, 250)
GREY = (190, 190, 190)


class Stub:
    def log_info(self, msg):
        print(f"    info | {msg}")

    def log_warning(self, msg):
        print(f"    warn | {msg}")


Stub._find_empty_slots = AutoTask._find_empty_slots


def canvas():
    # 深色底，模拟游戏背景
    img = np.zeros((H, W, 3), np.uint8)
    img[:] = (48, 40, 36)
    return img


def draw_slot(img, x, y, size=170, frame_color=GOLD, plus=True):
    """画一个槽位：粗边框 + 白底 + 黄色十字。"""
    t = 10
    cv2.rectangle(img, (x, y), (x + size, y + size), frame_color, t)
    cv2.rectangle(img, (x + t, y + t), (x + size - t, y + size - t), WHITE, -1)
    if plus:
        c = size // 2
        arm = int(size * 0.18)
        thick = max(6, size // 16)
        cv2.line(img, (x + c - arm, y + c), (x + c + arm, y + c), GOLD, thick)
        cv2.line(img, (x + c, y + c - arm), (x + c, y + c + arm), GOLD, thick)


def draw_filled_card(img, x, y, size=170):
    """已选中的卡：整块金黄，没有白底，也没有十字。"""
    cv2.rectangle(img, (x, y), (x + size, y + size), GOLD, -1)
    cv2.rectangle(img, (x + 14, y + 14), (x + size - 14, y + size - 14), (40, 120, 210), -1)


# 用线上真配置，避免「代码改了、测试还在用旧参数」这种假通过
try:
    import yaml

    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "config", "strategy.yaml"), encoding="utf-8") as fp:
        CFG = (yaml.safe_load(fp)["tuning"]["pick_slots"]).copy()
    print(f"读配置 pick_slots: region={CFG.get('region')} "
          f"min_height={CFG.get('min_height')} max_height={CFG.get('max_height')}")
    print()
except Exception as e:  # pragma: no cover
    print(f"读配置失败（{e}），用内置默认值")
    CFG = {
        "region": [0.02, 0.42, 0.68, 0.78],
        "min_height": 0.06,
        "max_height": 0.24,
        "min_white": 0.55,
    }

ok = True


def check(name, img, want, cfg=None):
    global ok
    s = Stub()
    got = s._find_empty_slots(img, cfg or CFG, debug=True)
    good = len(got) == want
    ok &= good
    print(f"{'PASS' if good else 'FAIL'} | {name}：检出 {len(got)} 个（期望 {want}）\n")
    return got


def main():
    print("--- 1. 单个空地槽 ---")
    img = canvas()
    draw_slot(img, 300, 520)
    check("金框+白底+黄十字", img, 1)

    print("--- 2. 三个已选 + 一个空 ---")
    img = canvas()
    for i in range(3):
        draw_filled_card(img, 220 + i * 200, 520)
    draw_slot(img, 820, 520)
    check("只有最后一格是空的", img, 1)

    print("--- 3. 全部选满（整块金黄）---")
    img = canvas()
    for i in range(4):
        draw_filled_card(img, 220 + i * 200, 520)
    check("已选满", img, 0)

    print("--- 4. 灰框空位（没有金框，已知检不出的情况）---")
    img = canvas()
    draw_slot(img, 300, 520, frame_color=GREY)
    check("灰框（预期 0，日志会写拒绝原因）", img, 0)

    print("--- 5. 偏小的槽位（门槛已放宽到 65px，70px 应该能检出）---")
    img = canvas()
    draw_slot(img, 300, 520, size=70)
    check("70px 小格（预期 1）", img, 1)

    print("--- 5.1 太小的图形（不像按钮，应该忽略）---")
    img = canvas()
    draw_slot(img, 300, 520, size=40)
    check("40px 小图（预期 0）", img, 0)

    print("--- 6. 槽位在 ROI 外面 ---")
    img = canvas()
    draw_slot(img, 1500, 200)
    check("ROI 外（预期 0）", img, 0)

    print("=" * 46)
    print("全部通过" if ok else "有不符合预期的项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
