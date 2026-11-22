"""离线验证：拿卡死那一刻的真实截图，测「黄卡片事件识别 + 打分 + 点击坐标」。

不连接游戏、不执行点击，只跑算法：
1. 从图里找成排的黄块（模拟 OCR 拿不到的东西）
2. 用块的位置造出「标题框 + 效果框」的 items
3. 调用 AutoTask 里真实的 _event_cards / _score_option，看选了哪张卡、点在哪

用法：.venv/Scripts/python.exe tools/test_event_cards.py
"""
import sys

import cv2
import numpy as np

sys.path.insert(0, ".")

from src.tasks.AutoTask import AutoTask  # noqa: E402


class Box:
    def __init__(self, x, y, w, h):
        self.x, self.y, self.width, self.height = x, y, w, h


class Stub:
    """只借用 AutoTask 的纯算法，不初始化 ok 框架。"""

    _yellow_ratio = AutoTask._yellow_ratio
    _event_cards = AutoTask._event_cards
    _score_option = AutoTask._score_option

    def __init__(self, stamina=None, stamina_max=None):
        self.config = {}
        self.stamina = stamina
        self.stamina_max = stamina_max
        self.clicks = []

    def log_info(self, m):
        print(f"  [log] {m}")

    def log_warning(self, m):
        print(f"  [warn] {m}")

    def click(self, x, y):
        self.clicks.append((int(x), int(y)))
        print(f"  [点击] ({int(x)},{int(y)})")

    def click_relative(self, x, y):
        self.clicks.append((x, y))
        print(f"  [点击相对] ({x},{y})")


def find_yellow_blocks(frame):
    b = frame[:, :, 0].astype(int)
    g = frame[:, :, 1].astype(int)
    r = frame[:, :, 2].astype(int)
    mask = ((r > 185) & (g > 125) & (b < 150) & ((r - b) > 55)).astype(np.uint8)
    # 只保留下半屏，事件卡片都在这里
    h = frame.shape[0]
    mask[: int(h * 0.60), :] = 0
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    n, labels, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
    blocks = []
    for i in range(1, n):
        x, y, w, hh, area = stats[i]
        if area < frame.shape[0] * frame.shape[1] * 0.01:
            continue
        blocks.append(Box(x, y, w, hh))
    blocks.sort(key=lambda bl: bl.x)
    return blocks


def main():
    path = "screenshots/auto/205455_unknown_33.png"
    frame = cv2.imread(path)
    if frame is None:
        print(f"读不到图：{path}")
        return 1
    H, W = frame.shape[:2]
    print(f"截图 {W}x{H}：{path}\n")

    blocks = find_yellow_blocks(frame)
    print(f"1) 找到 {len(blocks)} 个黄色区块")
    for i, bl in enumerate(blocks, 1):
        cx, cy = bl.x + bl.width / 2, bl.y + bl.height / 2
        print(f"   卡片{i}: x={bl.x} y={bl.y} {bl.width}x{bl.height} 中心({int(cx)},{int(cy)})")

    if len(blocks) < 2:
        print("！！没找到成排黄块，_event_cards 会判定为非事件页")
        return 1

    # 2) 模拟 OCR：每张卡片给「标题 + 效果」两行文字（内容照截图里念）
    titles = ["放弃按摩", "安心按摩", "神秘按摩"]
    effects = ["精力+10", "70%概率 推荐属性+30", "30%概率 推荐属性+60"]
    items = []
    for bl, t, e in zip(blocks, titles, effects):
        items.append({"t": t, "b": Box(bl.x + 20, bl.y + 10, bl.width - 40, bl.height * 0.45),
                      "cx": bl.x + bl.width / 2, "cy": bl.y + bl.height * 0.28})
        items.append({"t": e, "b": Box(bl.x + 20, bl.y + bl.height * 0.55, bl.width - 40, bl.height * 0.35),
                      "cx": bl.x + bl.width / 2, "cy": bl.y + bl.height * 0.75})

    # 3) 精力未满（截图里 60/130）→ 按规则「精力(未溢出) > 属性」，应选加精力的放弃按摩
    print("\n2) 精力 60/130（未满）：")
    stub = Stub(stamina=60, stamina_max=130)
    cards = stub._event_cards(frame, items)
    print(f"   识别到 {len(cards)} 张卡片: {[c['title'] for c in cards]}")
    if not cards:
        print("！！卡片分组失败")
        return 1
    scored = sorted(((stub._score_option(c["text"]), c) for c in cards), key=lambda x: -x[0])
    for s, c in scored:
        print(f"   {c['title']:<6} 得分 {s:>6.1f}  文本「{c['text']}」")
    best = scored[0][1]
    print(f"   => 会点「{best['title']}」@({int(best['cx'])},{int(best['cy'])})")

    # 校验点击点是否落在对应黄块里
    hit = None
    for i, bl in enumerate(blocks, 1):
        if bl.x <= best["cx"] <= bl.x + bl.width and bl.y <= best["cy"] <= bl.y + bl.height:
            hit = i
    print(f"   点击落点检验：{'在卡片%d内 ✅' % hit if hit else '不在任何卡片内 ❌'}")

    # 4) 精力已满 → 应改为选属性
    print("\n3) 精力 130/130（已满）：")
    stub2 = Stub(stamina=130, stamina_max=130)
    scored2 = sorted(((stub2._score_option(c["text"]), c) for c in cards), key=lambda x: -x[0])
    for s, c in scored2:
        print(f"   {c['title']:<6} 得分 {s:>6.1f}")
    print(f"   => 会点「{scored2[0][1]['title']}」")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
