"""器材卡检测方案对比（一次性诊断脚本，2026-11-24）。

目的：现有 `src/core/cards.py:find_cards` 在实机 1600x900 帧上 100% 读不到卡片，
      根因是「金色掩膜」把整片木地板也算了进去（木地板本身就是橙黄色），
      所有卡片和地板连成一块 639x167 的大连通域，被尺寸过滤掉。

这里对比三种不看颜色的方案，把检测框画出来人工核对：
  A 收紧金色（低蓝 + 高饱和）    B Canny 边缘 + 四边形    C 高饱和色块
用法：
  python tools/probe_cards.py            # 跑 8 张抽样帧，输出到 logs/probe_cards/
"""
import glob
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "logs", "probe_cards")
ROI = (0.18, 0.42, 0.58, 0.82)
CARD_W = (0.055, 0.130)      # 相对宽度 88~208（1600 宽）
CARD_H = (0.100, 0.230)      # 相对高度 90~207（900 高）


def _roi(frame):
    h, w = frame.shape[:2]
    x1, y1 = int(w * ROI[0]), int(h * ROI[1])
    x2, y2 = int(w * ROI[2]), int(h * ROI[3])
    return frame[y1:y2, x1:x2], (x1, y1)


def _ok_size(ww, hh, w, h):
    """w/h 是**全帧**尺寸（1600x900），不是 ROI 尺寸 —— ROI 里按比例算会全筛掉。"""
    if not (CARD_W[0] * w <= ww <= CARD_W[1] * w and CARD_H[0] * h <= hh <= CARD_H[1] * h):
        return False
    return 0.75 < hh / max(ww, 1) < 1.9


def scheme_a(roi):
    """收紧的金色：蓝通道低 + 高饱和 + 亮。"""
    b, g, r = roi[:, :, 0].astype(int), roi[:, :, 1].astype(int), roi[:, :, 2].astype(int)
    m = ((r > 215) & (g > 150) & (b < 105) & ((r - b) > 130)).astype(np.uint8) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    return m


def scheme_b(roi):
    """Canny 边缘：卡片金边 vs 地板的强对比。"""
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    e = cv2.Canny(gray, 60, 160)
    return cv2.morphologyEx(e, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))


def scheme_c(roi):
    """高饱和色块：卡片插画/金边饱和度明显高于木地板。"""
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    m = ((hsv[:, :, 1] > 165) & (hsv[:, :, 2] > 120)).astype(np.uint8) * 255
    return cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((13, 13), np.uint8))


def boxes_from_mask(mask, roi_shape):
    h, w = roi_shape[:2]
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        x, y, ww, hh = cv2.boundingRect(c)
        if ww * hh < 6000:
            continue
        if not _ok_size(ww, hh, w, h):
            continue
        fill = cv2.contourArea(c) / float(ww * hh)
        out.append((x, y, ww, hh, round(fill, 2)))
    out.sort(key=lambda d: d[0])
    return out


def main():
    fs = sorted(glob.glob(os.path.join(ROOT, "logs/audit/*/*/*core*.png")))
    if not fs:
        fs = sorted(glob.glob(os.path.join(ROOT, "screenshots/auto/core_*.png")))
    fs = fs[:: max(1, len(fs) // 8)][:8]
    os.makedirs(OUT, exist_ok=True)
    print(f"抽样 {len(fs)} 帧 → {OUT}")
    tot = {"A": 0, "B": 0, "C": 0}
    for p in fs:
        im = cv2.imread(p)
        if im is None:
            continue
        roi, (ox, oy) = _roi(im)
        line = [os.path.basename(p)]
        for tag, fn in (("A", scheme_a), ("B", scheme_b), ("C", scheme_c)):
            bs = boxes_from_mask(fn(roi), im.shape)
            tot[tag] += len(bs)
            line.append(f"{tag}={len(bs)}{[(b[0]+ox, b[1]+oy, b[2], b[3]) for b in bs]}")
            for (x, y, ww, hh, fill) in bs:
                cv2.rectangle(im, (x + ox, y + oy), (x + ox + ww, y + oy + hh), (0, 255, 0), 2)
        print("  ".join(line))
        cv2.imwrite(os.path.join(OUT, os.path.basename(p)), im)
    print("合计检出：", tot)


if __name__ == "__main__":
    main()
