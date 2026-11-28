# -*- coding: utf-8 -*-
"""图形识别「选项卡片」：不读文字，只看画面里有几个框、各在哪。

用法：
    python tools/probe_options.py <图片或目录> [--roi x1,y1,x2,y2]

输出：在图上画出检测到的框（绿色=同尺寸一组，橙=其他候选），
并存成 <图名>_optboxes.png，同时打印每个框的归一化中心。
"""
import os
import sys
import glob

import cv2
import numpy as np


def detect(frame, roi=None, min_frac=0.010, max_frac=0.55,
           extent_min=0.55, ar_range=(0.30, 3.5), close=(9, 9), iters=2,
           canny=(40, 120), blur=5):
    """返回 [(x, y, w, h, cx, cy, area)]（原图像素坐标）。"""
    h, w = frame.shape[:2]
    x1, y1 = int(roi[0] * w), int(roi[1] * h)
    x2, y2 = int(roi[2] * w), int(roi[3] * h)
    crop = frame[y1:y2, x1:x2]
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    g = cv2.GaussianBlur(g, (blur, blur), 0)
    edges = cv2.Canny(g, canny[0], canny[1])
    k = cv2.getStructuringElement(cv2.MORPH_RECT, close)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k, iterations=iters)
    cnts, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    rh, rw = g.shape
    out = []
    for c in cnts:
        x, y, bw, bh = cv2.boundingRect(c)
        area = float(bw * bh)
        if area < rw * rh * min_frac or area > rw * rh * max_frac:
            continue
        ar = bw / max(bh, 1)
        if ar < ar_range[0] or ar > ar_range[1]:
            continue
        if cv2.contourArea(c) / max(area, 1.0) < extent_min:
            continue
        out.append((x + x1, y + y1, bw, bh, x + x1 + bw // 2, y + y1 + bh // 2, area))
    out.sort(key=lambda b: -b[6])
    return out


def keep_similar(boxes, n_min=2, tol=0.40):
    """只留「大小相近」的一撮：面积落在中位数 ±tol 内，才像同一组选项卡。"""
    if len(boxes) < n_min:
        return []
    med = np.median([b[6] for b in boxes])
    sim = [b for b in boxes if abs(b[6] - med) / med <= tol]
    return sim if len(sim) >= n_min else []


def layout(boxes):
    if len(boxes) < 2:
        return "单", boxes
    xs = [b[4] for b in boxes]
    ys = [b[5] for b in boxes]
    if max(xs) - min(xs) >= max(ys) - min(ys):
        return "横排", sorted(boxes, key=lambda b: b[4])
    return "竖排", sorted(boxes, key=lambda b: b[5])


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    roi = (0.05, 0.12, 0.95, 0.90)
    if "--roi" in sys.argv:
        roi = tuple(float(v) for v in sys.argv[sys.argv.index("--roi") + 1].split(","))
    src = sys.argv[1]
    paths = [src] if os.path.isfile(src) else sorted(glob.glob(os.path.join(src, "*.png")))
    for p in paths:
        frame = cv2.imread(p)
        if frame is None:
            continue
        h, w = frame.shape[:2]
        allb = detect(frame, roi=roi)
        sim = keep_similar(allb, 2)
        d, ordered = layout(sim)
        print(f"\n[{os.path.basename(p)}] {w}x{h} 候选 {len(allb)}，同尺寸 {len(sim)} → {d}")
        for b in ordered:
            print(f"   ({b[4]/w:.3f},{b[5]/h:.3f})  {b[2]}x{b[3]}  面积{b[6]:.0f}")
        vis = frame.copy()
        for b in allb:
            cv2.rectangle(vis, (b[0], b[1]), (b[0] + b[2], b[1] + b[3]), (0, 200, 255), 2)
        for b in sim:
            cv2.rectangle(vis, (b[0], b[1]), (b[0] + b[2], b[1] + b[3]), (0, 255, 0), 3)
            cv2.circle(vis, (b[4], b[5]), 6, (0, 0, 255), -1)
        out = os.path.splitext(p)[0] + "_optboxes.png"
        cv2.imwrite(out, vis)
        print(f"   → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
