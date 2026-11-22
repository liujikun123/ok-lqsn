"""训练核心页「编队卡片」识别。

页面事实（来自录制 frames/463、464、474、493）：
- 训练核心页右侧是 6 个技能胶囊；选中项会高亮，上方显示「失败率 N%」。
- 画面中间偏下会横排 1~3 张卡片，卡片 = 这次训练带上的支援单位。
  每张卡：金/橙色边框 + 卡面插画 + 右上角小徽章 + 下方「MAX」或青色熟练度条。
- 卡片尺寸约 150x160 像素（1920x1080），等距横排，`493.png` 三张卡 x≈449/640/839。

判定方法：金色边框在球场背景上对比度很高，用颜色掩膜 + 闭运算 + 轮廓，
按面积/长宽比过滤，就能稳定数出张数（离线跑 122 帧：0 张 108、1 张 9、2 张 3、3 张 2）。
"""
import cv2
import numpy as np

# 卡片所在的横条（画面比例）
ROI = (0.15, 0.40, 0.70, 0.85)   # x1, y1, x2, y2


def _gold_mask(roi):
    b = roi[:, :, 0].astype(int)
    g = roi[:, :, 1].astype(int)
    r = roi[:, :, 2].astype(int)
    return ((r > 205) & (g > 155) & (b < 160) & ((r - b) > 45)).astype(np.uint8) * 255


def find_cards(frame):
    """返回卡片列表：[{x, y, w, h, cx}]，按 x 从左到右。"""
    h, w = frame.shape[:2]
    x1, y1 = int(w * ROI[0]), int(h * ROI[1])
    x2, y2 = int(w * ROI[2]), int(h * ROI[3])
    roi = frame[y1:y2, x1:x2]
    if roi.size == 0:
        return []
    mask = _gold_mask(roi)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        x, y, ww, hh = cv2.boundingRect(c)
        if ww * hh < 12000:
            continue
        if not (0.8 < hh / max(ww, 1) < 1.8):
            continue
        out.append({"x": x + x1, "y": y + y1, "w": ww, "h": hh, "cx": x + x1 + ww / 2})
    out.sort(key=lambda d: d["x"])
    return out


def describe(frame):
    """给日志用的一句话：几张卡、各自左侧坐标。"""
    cards = find_cards(frame)
    return len(cards), [int(c["x"]) for c in cards]


def card_glow(frame, card):
    """卡片外框的光效：金色 → "金"；彩色/彩虹 → "彩"；判断不了 → "无"。

    做法：取卡片外框一圈像素，统计「高饱和像素出现了几种色相」。
    单色金框只落在橙黄一个色相区；彩光会在框上同时出现紫/青/粉等多个色相。
    """
    x, y, w, h = int(card["x"]), int(card["y"]), int(card["w"]), int(card["h"])
    patch = frame[y:y + h, x:x + w]
    if patch.size == 0:
        return "无"
    t = max(int(min(w, h) * 0.09), 3)
    ring = np.zeros((patch.shape[0], patch.shape[1]), bool)
    ring[:t, :] = True
    ring[-t:, :] = True
    ring[:, :t] = True
    ring[:, -t:] = True

    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    sel = ring & (sat > 110) & (val > 150)
    if sel.sum() < 50:
        return "无"
    bins = np.bincount((hue[sel] // 30).astype(int), minlength=6)[:6]
    lively = int((bins > sel.sum() * 0.06).sum())
    return "彩" if lively >= 3 else "金"

