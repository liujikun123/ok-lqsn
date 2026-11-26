"""离线统计「训练项页」页的卡片数量：用卡片下方的青色熟练度条定位"""
import json, os, sys
import numpy as np
from PIL import Image

root = "demo/session_1122_200929"

def count_cards(path):
    im = np.asarray(Image.open(path).convert("RGB")).astype(np.int16)
    h, w = im.shape[:2]
    R, G, B = im[:, :, 0], im[:, :, 1], im[:, :, 2]
    # 青色条：R 低，G/B 高
    cyan = (R < 130) & (G > 140) & (B > 170) & (B - R > 70)
    band = np.zeros_like(cyan)
    band[int(h*0.55):int(h*0.88), int(w*0.20):int(w*0.66)] = True
    m = cyan & band
    cols = m.sum(axis=0)
    thr = max(8, int(h * 0.008))
    runs, start = [], None
    for x in range(w):
        if cols[x] >= thr and start is None:
            start = x
        elif cols[x] < thr and start is not None:
            if x - start > 20:
                runs.append((start, x))
            start = None
    return runs

if __name__ == "__main__":
    for p in sys.argv[1:]:
        print(p, count_cards(p))
