# -*- coding: utf-8 -*-
"""用真实商店截图验证「货架解析 + 价格 + 白名单」。

    ../ok-bsg/.venv/Scripts/python.exe tools/test_shop_live.py screenshots/auto/xxxx_shop.png

为什么单独做：价格/剩余/购买按钮全部是"看起来对、实机错"的重灾区
（价格读成 None、把库存 1 当价格、购买按钮写的是价格数字不是「购买」）。
这个脚本直接拿一帧真图跑一遍 OCR + 分类，把结果和期望值对照。
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


class Box:
    def __init__(self, x, y, w, h):
        self.x, self.y, self.width, self.height = x, y, w, h


class Stub:
    stamina = 24
    last_equip_count = 1
    shop_has_stamina = None
    SHOP_CATS = None
    SHOP_NON_ITEM = None

    def log_info(self, m):
        print("   info |", m)

    def log_warning(self, m):
        print("   warn |", m)

    def _tuning(self):
        import yaml
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "config", "strategy.yaml"), encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("tuning", {}) or {}


def main(path):
    import cv2
    from onnxocr.onnx_paddleocr import ONNXPaddleOcr
    from src.tasks.AutoTask import AutoTask

    for m in ("_shop_kind", "_shop_price", "_shop_offers", "_shop_plan"):
        setattr(Stub, m, getattr(AutoTask, m))
    Stub.SHOP_CATS = AutoTask.SHOP_CATS
    Stub.SHOP_NON_ITEM = AutoTask.SHOP_NON_ITEM

    frame = cv2.imread(path)
    if frame is None:
        print("读不到图：", path)
        return 2
    h, w = frame.shape[:2]
    ocr = ONNXPaddleOcr(use_angle_cls=False, use_openvino=True)
    res = ocr.ocr(frame) or []
    items = []
    for line in res[0] if res and isinstance(res[0], list) else res:
        try:
            box, (text, score) = line[0], line[1]
        except Exception:
            continue
        text = (text or "").strip()
        if not text:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        items.append({"t": text,
                      "cx": (min(xs) + max(xs)) / 2,
                      "cy": (min(ys) + max(ys)) / 2,
                      "b": Box(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))})

    s = Stub()
    offers = s._shop_offers(items, frame)
    print(f"\n=== 货架（{len(offers)} 件）===")
    for o in offers:
        print(f"  {o['name']:<14} 类别={o['kind']:<8} 价格={o['price']}")

    print("\n=== 精度检查 ===")
    ok = True
    named = {o["name"]: o["price"] for o in offers}
    # 实机截图里的真实价格（人工核对）
    expect = {"运控秘籍++": 130, "特训牛奶": 50, "苦味饮料": 60,
              "特调专属饮料+": 100, "精力饮料": 30, "投射秘籍": 60}
    for n, p in expect.items():
        got = named.get(n, "缺")
        flag = "OK " if got == p else "!! "
        if got != p:
            ok = False
        print(f"  {flag}{n:<14} 期望 {p:<4} 实得 {got}")

    o, why = s._shop_plan(offers, s._tuning().get("shop") or {})
    print(f"\n=== 决策（精力 {Stub.stamina}，器材 {Stub.last_equip_count}）===")
    print(f"  买「{o['name'] if o else '不买'}」 原因={why}")

    print("\n结论：", "价格全部读对" if ok else "价格仍有读错的")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
