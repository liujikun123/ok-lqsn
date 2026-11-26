"""训练项页「器材/支援卡片」识别。

页面事实（重新核对于 2026-11-24，1600x900 实机帧 + 1080p 历史帧）：
- 训练项页右侧是 6 个训练项条目；选中项高亮，上方显示「失败率 N%」。
- 画面中间偏下横排 1~3 张卡片，每张 = 这次训练带的支援单位（器材或人物）。
  每张卡：金/彩发光边框 + 卡面插画 + 右上角小徽章 + 下方熟练度进度条。
- 1600x900 下卡片约 137x138，横排左起 x 约 450 / 645 / 835，纵向 y 约 508~660。
- **卡片会淡入淡出**：同一场训练里点不同技能，有的帧有卡、有的帧没有
  （实测一局 4 个训练项：2 张 / 1 张 / 0 张 / 0 张）。所以必须多帧读数取非空帧，
  单帧读不到不等于「这场没带器材」。

为什么不用框架的 `find_color_rectangles`：
    它假设目标是**纯色矩形块**（按钮那种，内部填充率 ≥0.95），而卡片内部是插画、
    只有一圈发光边框，填充率 0.08~0.5 且随插画变化，直接套用会全部漏检。
    这里需要「颜色掩膜 + 形态学闭运算 + 轮廓」这条链路，属于框架没覆盖的部分。

历史教训（2026-11-24 前）：旧实现用「金色掩膜」找卡，但木地板本身就是橙黄色，
    整片地板都满足阈值，卡片和地板连成一块 639x167 的大连通域被尺寸过滤掉 ——
    实机 263 帧全部检出 0 张，**器材分从来没被算进过排名**（每场白丢 0.5~3 分）。
    现在改用「低蓝金边」∪「高饱和色块」双掩膜，并在全帧 397 帧上复测：
    0 张 189 帧 / 1 张 132 / 2 张 62 / 3 张 14，没有误检到第 4 张。
"""
import cv2
import numpy as np

# 卡片所在横条（画面比例）。
# 三张横排时最右一张右沿到 x≈0.62（1600 宽下 999px），所以右边界放到 0.63；
# 左侧收到 0.17 避开球队大卡与左侧面板，下边界 0.84 避开底部属性条。
ROI = (0.17, 0.40, 0.63, 0.84)

# 尺寸（相对全帧宽/高）。1600x900 实测：宽 137~170、高 138~166。
CARD_W = (0.055, 0.140)
CARD_H = (0.085, 0.240)
CARD_RATIO = (0.70, 1.95)      # 高/宽
MIN_AREA_RATIO = 0.0055        # 面积下限（相对整帧像素数）


def _gold_mask(roi):
    """金边：亮橙黄、蓝通道很低、红蓝差很大。

    地板是橙褐色但蓝通道在 120~150，这里要求 b<105 就能把它们分开。
    """
    b = roi[:, :, 0].astype(int)
    g = roi[:, :, 1].astype(int)
    r = roi[:, :, 2].astype(int)
    return ((r > 215) & (g > 150) & (b < 105) & ((r - b) > 130)).astype(np.uint8) * 255


def _vivid_mask(roi):
    """高饱和色块：彩光/浅金边框的卡，饱和度明显高于木地板。"""
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    return ((hsv[:, :, 1] > 165) & (hsv[:, :, 2] > 120)).astype(np.uint8) * 255


def _kernel(w):
    """形态学核随分辨率走（1600 宽 → 13，1920 宽 → 17）。"""
    k = int(round(w * 0.0085)) | 1
    return np.ones((max(7, k), max(7, k)), np.uint8)


def _iou(a, b):
    ax2, ay2 = a["x"] + a["w"], a["y"] + a["h"]
    bx2, by2 = b["x"] + b["w"], b["y"] + b["h"]
    ix = max(0, min(ax2, bx2) - max(a["x"], b["x"]))
    iy = max(0, min(ay2, by2) - max(a["y"], b["y"]))
    inter = ix * iy
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union else 0.0


def find_cards(frame):
    """返回卡片列表：[{x, y, w, h, cx}]，按 x 从左到右（全帧坐标）。"""
    if frame is None or frame.ndim < 2:
        return []
    h, w = frame.shape[:2]
    x1, y1 = int(w * ROI[0]), int(h * ROI[1])
    x2, y2 = int(w * ROI[2]), int(h * ROI[3])
    roi = frame[y1:y2, x1:x2]
    if roi.size == 0:
        return []

    mask = cv2.bitwise_or(_gold_mask(roi), _vivid_mask(roi))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, _kernel(w))

    min_w, max_w = CARD_W[0] * w, CARD_W[1] * w
    min_h, max_h = CARD_H[0] * h, CARD_H[1] * h
    min_area = MIN_AREA_RATIO * w * h

    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        x, y, ww, hh = cv2.boundingRect(c)
        if ww * hh < min_area:
            continue
        # 尺寸上限是必须的：2026-11-23 在「赛前准备」页误检到 379x434 的球队大卡，
        # 会把奖励算到错误技能上。
        if not (min_w <= ww <= max_w and min_h <= hh <= max_h):
            continue
        if not (CARD_RATIO[0] < hh / max(ww, 1) < CARD_RATIO[1]):
            continue
        card = {"x": x + x1, "y": y + y1, "w": ww, "h": hh, "cx": x + x1 + ww / 2}
        # 双掩膜会给出两个略有偏差的框（金框紧、彩色框带光晕），合并成一个。
        for i, other in enumerate(out):
            if _iou(card, other) > 0.5:
                if ww * hh > other["w"] * other["h"]:
                    out[i] = card          # 留大的那个（含光晕，判颜色更稳）
                break
        else:
            out.append(card)
    out.sort(key=lambda d: d["x"])
    return out


def describe(frame):
    """给日志用的一句话：几张卡、各自左侧坐标。"""
    cards = find_cards(frame)
    return len(cards), [int(c["x"]) for c in cards]


def _nonwarm_ratio(frame, card, gap, rad, inner):
    """卡片某一圈里「非暖色高亮像素」的占比。0 = 纯橙黄，1 = 全是紫青粉。

    inner=True  → 取卡片框**内侧** gap 像素的一圈（读边框本身）
    inner=False → 取卡片框**外侧** gap~rad 像素的一圈（读框外的光晕）

    外侧那圈会排除画面下方 30%：卡片下面压着蓝色的属性条底板，
    它的高饱和度会把「非暖色」占比整体抬上去，导致所有卡都判成彩。
    """
    x, y, w, h = int(card["x"]), int(card["y"]), int(card["w"]), int(card["h"])
    H, W = frame.shape[:2]
    if inner:
        # 往框里缩 gap 像素，取框线那一圈
        x0, y0 = max(0, x + gap), max(0, y + gap)
        x1, y1 = min(W, x + w - gap), min(H, y + h - gap)
        pat = frame[y0:y1, x0:x1]
        if pat.size == 0:
            return None
        t = max(3, int(min(pat.shape[0], pat.shape[1]) * 0.10))
        ring = np.zeros(pat.shape[:2], bool)
        ring[:t, :] = ring[-t:, :] = True
        ring[:, :t] = ring[:, -t:] = True
    else:
        x0, y0 = max(0, x - rad), max(0, y - rad)
        x1, y1 = min(W, x + w + rad), min(H, y + h + rad)
        pat = frame[y0:y1, x0:x1]
        if pat.size == 0:
            return None
        hh, ww = pat.shape[:2]
        yy, xx = np.mgrid[0:hh, 0:ww]
        cx0, cy0 = x - x0, y - y0
        outside = ~((xx >= cx0 - gap) & (xx < cx0 + w + gap) &
                    (yy >= cy0 - gap) & (yy < cy0 + h + gap))
        ring = outside
        ring[int(hh * 0.70):, :] = False        # 去掉被属性条污染的底部
    if ring.sum() < 60:
        return None
    hsv = cv2.cvtColor(pat, cv2.COLOR_BGR2HSV)
    hue = hsv[:, :, 0].astype(int)
    sat, val = hsv[:, :, 1], hsv[:, :, 2]
    sel = ring & (sat > 95) & (val > 160)
    if sel.sum() < 40:
        return None
    hs = hue[sel]
    warm = ((hs >= 8) & (hs <= 48)) | (hs >= 172)   # 橙黄 + 红
    return float((~warm).mean())


def card_glow(frame, card):
    """卡片品质：彩虹/多色光晕 → "彩"；平涂黄框 → "普"；读不到 → "无"。

    【2026-11-24 玩家纠正】「这些器材本身就是黄底的，你是不是没有和冒金光区分」——
    说得对。旧实现只取卡片框**内侧**那一圈（3.5%）判「有没有彩色」，于是：
      · 任何一张平涂黄框的普通卡，内圈全是橙黄 → 判「金」；
      · 而「金」在计分表里值 1.0，是普通卡（0.5）的两倍。
    结果就是**每一张普通卡都被当金光卡算分**，器材分整体虚高一倍，
    排名也跟着失真（四张普通卡和白拿一张 MAX 卡算出来差不多）。

    现在两头都看：
      内圈（框线本身）+ 外圈（框外光晕），**任一处出现非暖色**才算「彩」。
    实测样本（人工看图标注，共 15 张卡）：
      普通黄框  内圈 0.00 / 外圈 0.00~0.04
      彩虹 MAX  内圈或外圈 0.45~0.67
    阈值 0.30 落在空档中间，两侧都留了很大余量。

    为什么对 095607 那张彩卡要额外看内圈：find_cards 会把光晕一起框进来
    （框比普通卡大 60px），这时外圈已经在光晕之外，只剩地板，只有内圈还是紫粉。

    关于「金」这一档：目前**没有任何一帧实机素材**能证明游戏里存在
    「介于普通黄框和彩虹 MAX 之间的金色光晕」品质。既然没有样本，
    就不硬造判据 —— 一律按「普」（0.5）计，宁可少算也不虚高。
    计分表里 "金": 1.0 保留着，哪天真拍到金光帧，补一条判据即可。
    """
    if frame is None or card is None:
        return "无"
    x, y, w, h = int(card["x"]), int(card["y"]), int(card["w"]), int(card["h"])
    if w <= 0 or h <= 0:
        return "无"
    gap = max(2, int(min(w, h) * 0.03))
    rad = max(10, int(min(w, h) * 0.09))
    vals = [v for v in (_nonwarm_ratio(frame, card, gap, rad, inner=True),
                        _nonwarm_ratio(frame, card, gap, rad, inner=False))
            if v is not None]
    if not vals:
        return "无"                      # 白/灰边框，读不到饱和像素
    return "彩" if max(vals) >= 0.30 else "普"
