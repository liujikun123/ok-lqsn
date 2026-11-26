# -*- coding: utf-8 -*-
"""角标识别真实素材回归：新算法（全图搜 + 纵向就近） vs 旧算法（OCR 框 ROI）。

基准（ground truth）= 全图橙色小圆里、落在训练项条目列（x > 0.62w）的那些。
不用人工标注，因为角标是唯一性很强的图形（35x35 橙色圆 + 白色小人）。

旧算法留在这里只作对照，用来证明这次改造确实补上了漏检。
"""
import sys, glob
sys.path.insert(0, '.')
import cv2, numpy as np
from tools.devkit import ocr_rows
from src.tasks.AutoTask import AutoTask

inst = object.__new__(AutoTask)
SKILLS = AutoTask.SKILLS


def truth_blobs(frame):
    """基准：训练项列区域里的橙色小圆。

    纵向必须落在胶囊列（0.15h~0.80h）—— 顶部标题栏也有橙色元素
    （实测 y=38/48），不加纵约束会把基准数虚高 2 个。
    """
    h, w = frame.shape[:2]
    return [b for b in inst._badge_blobs(frame)
            if b[0] > 0.62 * w and 0.15 * h < b[1] < 0.80 * h]


def truth_count(frame):
    return len(truth_blobs(frame))


def old_badge(frame, row):
    """旧实现（2026-11-24 之前）：ROI 绑在 OCR 框上"""
    h, w = frame.shape[:2]
    sx, sy = w / 1920.0, h / 1080.0
    b = row["b"]
    x1 = max(int(b.x + b.width - 40 * sx), 0)
    x2 = min(int(b.x + b.width + 190 * sx), w)
    y1 = max(int(b.y - 110 * sy), 0)
    y2 = min(int(b.y + 25 * sy), h)
    if x2 <= x1 or y2 <= y1:
        return None
    roi = frame[y1:y2, x1:x2]
    bb = roi[:, :, 0].astype(int); gg = roi[:, :, 1].astype(int); rr = roi[:, :, 2].astype(int)
    m = ((rr > 195) & (gg > 105) & (gg < 195) & (bb < 95)).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    for c in cnts:
        bx, by, bw, bh = cv2.boundingRect(c)
        if not (16 * sx <= bw <= 52 * sx and 16 * sy <= bh <= 52 * sy):
            continue
        if not (0.6 < bw / max(bh, 1) < 1.7):
            continue
        if bw * bh < 260 * sx * sy:
            continue
        if best is None or bw * bh > best[0]:
            best = (bw * bh, (x1 + bx + bw / 2, y1 + by + bh / 2))
    return best[1] if best else None


def rows_of(frame):
    w = frame.shape[1]
    rows = []
    for (x1, y1, x2, y2, txt, conf) in ocr_rows(frame):
        t = txt.replace(" ", "")
        if t in SKILLS and (x1 + x2) / 2 / w > 0.70:
            rows.append({"t": t, "cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2,
                         "b": type("B", (), {"x": x1, "y": y1,
                                             "width": x2 - x1, "height": y2 - y1})()})
    rows.sort(key=lambda r: r["cy"])
    return rows


files = sorted(glob.glob("logs/audit/**/*core*.png", recursive=True))
step = max(1, len(files) // 40)
sample = files[::step][:40]
print(f"抽样 {len(sample)} 张 / 共 {len(files)} 张\n")

tot_truth = tot_new = tot_old = 0
bad = []
for p in sample:
    img = cv2.imread(p)
    if img is None:
        continue
    rows = rows_of(img)
    if not rows:
        print(f"[SKIP] {p.split('/')[-1]} 没读到训练项行（可能不是标准版式）")
        continue
    blobs = inst._badge_blobs(img)
    truth = truth_count(img)
    new = sum(1 for r in rows if inst._badge_near(img, r, blobs))
    old = sum(1 for r in rows if old_badge(img, r))
    tot_truth += min(truth, len(rows)); tot_new += new; tot_old += old
    flag = "" if new >= truth else "  <<< 新算法仍漏"
    if new < truth:
        bad.append((p.split('/')[-1], truth, len(rows), old, new))
    print(f"{p.split('/')[-1]:34s} 基准{truth} 行{len(rows)} 旧{old} 新{new}{flag}")

print(f"\n=== 合计：基准 {tot_truth} / 旧算法 {tot_old} / 新算法 {tot_new} ===")
if tot_old:
    print(f"旧算法命中率 {tot_old/tot_truth:.1%} → 新算法 {tot_new/tot_truth:.1%}")
if bad:
    print(f"\n新算法仍有漏的 {len(bad)} 张:")
    for b in bad:
        print("  ", b)
sys.exit(0 if tot_new >= tot_truth * 0.97 else 1)
