# -*- coding: utf-8 -*-
"""离线核对：拿历史商店截图跑一遍 OCR + 商品分类/取价，看「摇摆专属饮料」「器材手册」认不认。

    ../ok-bsg/.venv/Scripts/python.exe tools/check_shop_ocr.py screenshots/auto/114351_shop.png

只读图片，不碰游戏窗口。
"""
import os
import sys

import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.tasks.AutoTask import AutoTask  # noqa: E402


class Box:
    def __init__(self, h, w, x, y):
        self.height = h
        self.width = w
        self.x = x
        self.y = y
        self.name = ""


class Stub:
    SHOP_CATS = AutoTask.SHOP_CATS
    SHOP_NON_ITEM = AutoTask.SHOP_NON_ITEM
    _shop_kind = AutoTask._shop_kind
    _shop_price = AutoTask._shop_price
    _shop_offers = AutoTask._shop_offers

    def log_info(self, m):
        print("  ", m)

    def log_warning(self, m):
        print("  W:", m)


def main():
    paths = sys.argv[1:] or ["screenshots/auto/114351_shop.png",
                             "screenshots/auto/114023_shop.png"]
    from onnxocr.onnx_paddleocr import ONNXPaddleOcr
    ocr = ONNXPaddleOcr(use_angle_cls=False, use_openvino=True)

    for p in paths:
        frame = cv2.imread(p)
        if frame is None:
            print("读不到", p)
            continue
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
            xs = [q[0] for q in box]
            ys = [q[1] for q in box]
            bw, bh = max(xs) - min(xs), max(ys) - min(ys)
            b = Box(bh, bw, min(xs), min(ys))
            b.name = text
            items.append({"b": b, "t": text,
                          "cx": (min(xs) + max(xs)) / 2, "cy": (min(ys) + max(ys)) / 2})

        print(f"\n=== {os.path.basename(p)}  {frame.shape[1]}x{frame.shape[0]}  {len(items)} 段")
        print("  倒计时:", [it["t"] for it in items if "后" in it["t"] or "上新" in it["t"]])
        offers = Stub()._shop_offers(items, frame)
        for o in offers:
            print(f"   {o['name']:<12} → {o['kind']:<8} 价格 {o['price']}")
        if not offers:
            print("   （一件都没认出来）")


if __name__ == "__main__":
    main()
