"""离线复检：拿 sweep 截图跑一遍 OCR，看「失败率」「球场名字」到底能不能读出来。

    ..\\ok-bsg\\.venv\\Scripts\\python.exe tools\\probe_sweep_frames.py 01_01 05_01

只用于排查，不进主流程。
"""
import os
import sys

import cv2

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SWEEP = os.path.join(ROOT, "screenshots", "sweep")

SKILLS = {"投射", "弹跳", "防守", "体能", "运控", "意识"}


def main(names):
    from onnxocr.onnx_paddleocr import ONNXPaddleOcr
    ocr = ONNXPaddleOcr(use_angle_cls=False, use_openvino=True)

    for n in names:
        p = os.path.join(SWEEP, f"{n}.png")
        frame = cv2.imread(p)
        if frame is None:
            print(f"读不到 {p}")
            continue
        h, w = frame.shape[:2]
        res = ocr.ocr(frame) or []
        lines = res[0] if res and isinstance(res[0], list) else res
        items = []
        for line in lines:
            try:
                box, (text, score) = line[0], line[1]
            except Exception:
                continue
            text = (text or "").strip()
            if not text:
                continue
            xs = [q[0] for q in box]
            ys = [q[1] for q in box]
            items.append({"t": text, "cx": (min(xs) + max(xs)) / 2,
                          "cy": (min(ys) + max(ys)) / 2,
                          "bw": max(xs) - min(xs), "bh": max(ys) - min(ys)})

        print(f"\n===== {n}  ({w}x{h}) 共 {len(items)} 段 =====")
        pct = [it["t"] for it in items if "失败率" in it["t"] or "%" == it["t"].strip()]
        print(f"失败率相关文字: {pct}")
        court = [it for it in items
                 if 0.30 < it["cy"] / h < 0.52 and 0.03 < it["cx"] / w < 0.80
                 and 2 <= len(it["t"]) <= 5 and it["t"] not in SKILLS]
        print(f"球场区候选名: {[(c['t'], round(c['cx'] / w, 2), round(c['cy'] / h, 2)) for c in court]}")
        lv = [it["t"] for it in items if it["t"].upper().startswith(("MAX", "LV", "Lv"))]
        print(f"等级文字: {lv[:8]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["01_01", "05_01"]))
