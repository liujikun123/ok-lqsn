"""离线探测训练项页的「器材卡片」：位置、数量、外框光效。

背景（2026-11-23）：
  玩家指出「场上左边一个人，中间一个器材（鞋子），不是很明显吗」，
  说明器材在**选之前**就画在球场中间，之前「器材读不到」的结论是错的。
  这里先离线量出卡片的稳定位置/尺寸/框色，再回填到 AutoTask._find_equipment。

用法：
  ../ok-bsg/.venv/Scripts/python.exe tools/probe_equip.py            # 扫全部 samples
  ../ok-bsg/.venv/Scripts/python.exe tools/probe_equip.py --viz 01_02  # 出可视化图
"""
import sys
import glob
import os

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_DIRS = [os.path.join(ROOT, "screenshots", "sweep"),
               os.path.join(ROOT, "screenshots", "auto")]


def imread(path):
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), 1)


def find_cards(img):
    """在球场中部找器材卡片，返回 [(x, y, w, h, tier)]。"""
    H, W = img.shape[:2]
    x0, x1 = int(0.24 * W), int(0.55 * W)
    y0, y1 = int(0.45 * H), int(0.82 * H)
    roi = img[y0:y1, x0:x1]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

    # 卡片外框：明亮的黄/橙描边
    m = cv2.inRange(hsv, (14, 120, 165), (40, 255, 255))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    out = []
    min_w, max_w = 0.055 * W, 0.115 * W
    min_h, max_h = 0.115 * H, 0.235 * H
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        if not (min_w <= w <= max_w and min_h <= h <= max_h):
            continue
        if not (0.6 <= w / float(h) <= 1.1):
            continue
        fill = cv2.contourArea(c) / float(w * h)
        if fill < 0.55:
            continue
        tier = _tier(roi[y:y + h, x:x + w])
        out.append((x + x0, y + y0, w, h, tier))
    out.sort(key=lambda d: d[0])
    return out


def _tier(card):
    """按外框光效判档：rainbow（多色）/ gold（金黄）/ normal（白蓝）。"""
    hsv = cv2.cvtColor(card, cv2.COLOR_BGR2HSV)
    H, S, V = hsv[:, :, 0].astype(int), hsv[:, :, 1].astype(int), hsv[:, :, 2].astype(int)
    hh, ww = card.shape[:2]
    t = max(4, int(0.14 * min(hh, ww)))
    ring = np.zeros((hh, ww), bool)
    ring[:t, :] = True
    ring[-t:, :] = True
    ring[:, :t] = True
    ring[:, -t:] = True
    sel = ring & (S > 110) & (V > 150)
    if sel.sum() < 50:
        return "normal"
    hs = H[sel]
    # 色相直方图：>3 个不相邻区间各占 8% 以上 → 彩虹
    hist = np.bincount((hs // 10).clip(0, 17), minlength=18).astype(float)
    hist /= hist.sum()
    bands = (hist > 0.10).sum()
    gold = hist[1:4].sum()          # H 10~39
    if bands >= 3 and gold < 0.65:
        return "rainbow"
    if gold >= 0.55:
        return "gold"
    return "normal"


def main():
    viz = None
    if "--viz" in sys.argv:
        viz = sys.argv[sys.argv.index("--viz") + 1]

    files = []
    for d in SAMPLE_DIRS:
        files += sorted(glob.glob(os.path.join(d, "*.png")))
    if viz:
        files = [f for f in files if viz in os.path.basename(f)]

    stat = {}
    for f in files:
        img = imread(f)
        if img is None:
            continue
        cards = find_cards(img)
        key = (len(cards), tuple(c[4] for c in cards))
        stat[key] = stat.get(key, 0) + 1
        print(f"{os.path.basename(f):28} 器材 {len(cards)} 个  "
              + " | ".join(f"({x},{y},{w}x{h},{t})" for x, y, w, h, t in cards))
        if viz:
            for x, y, w, h, t in cards:
                cv2.rectangle(img, (x, y), (x + w, y + h), (0, 0, 255), 3)
                cv2.putText(img, t, (x, y - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)
            cv2.imencode(".png", img)[1].tofile(
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                             "2026-11-23-07-45-02", f"_viz_{viz}.png"))

    print("\n=== 分布 ===")
    for k, v in sorted(stat.items(), key=lambda d: -d[1]):
        print(f"{v:4d} 张  器材{k[0]}个 {k[1]}")


if __name__ == "__main__":
    main()
