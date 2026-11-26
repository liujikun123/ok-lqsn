"""训练项页「器材/支援卡片」识别。

⚠️ 这里是**纯图像识别，不走 OCR**（玩家 2026-11-26 点明：「器材上面没有文字」）：
   卡面是插画/图标（球、护腕、人物…），玩家看到的「像个字」其实是图标形状，
   没有任何可读文本。所以只能靠「边框颜色 + 轮廓尺寸」找卡，靠「光晕暖/冷色」判品质。
   读得出：几张、普通/彩 → 读不出：是哪件器材（要认具体器材得做图标模板匹配，暂不做）。

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
# 三张横排时最右一张右沿到 x≈0.52（1600 宽下 831px）；最多可能有 5 张
# （玩家 2026-11-26：「器材是开局先选好五个」），5 张时右沿约 0.63，
# 所以右边界放到 0.66。左侧 0.17 避开球队大卡与左侧面板，下边界 0.84 避开底部属性条。
ROI = (0.17, 0.40, 0.66, 0.84)

# 尺寸（相对全帧宽/高）。1600x900 实测：宽 137~170、高 138~166。
CARD_W = (0.055, 0.140)
CARD_H = (0.085, 0.240)
CARD_RATIO = (0.70, 1.95)      # 高/宽
MIN_AREA_RATIO = 0.0055        # 面积下限（相对整帧像素数）
MAX_CARDS = 5                  # 一局最多 5 张（玩家 2026-11-26：开局先选五个）。
                               # 上限只用来防误检，不再是 3。
# 轮廓致密度 = 轮廓面积 / 外接矩形面积。
# 真实的卡是**实心矩形**：实测 0.88~0.94；带彩虹光晕的彩卡因为光晕把外接框撑大，
# 会掉到 0.69~0.71（不能按 0.88 卡，否则把彩卡全滤掉）。
# 而「+33 / +36」这种火焰伤害数字（会被掩膜当成色块，尺寸也刚好落在卡片区间）
# 只有 0.55~0.62。取 0.66 落在两个空档之间。
# 之前它们被判成「彩卡」，是「彩」虚高的主因。
MIN_EXTENT = 0.66

# ===== 品质三档（普 / 金 / 彩）—— 2026-11-26 玩家给参考图后校准完成 =====
#
# 【玩家第三次纠正，带图】「这个左边就是普通的，右边就是金光。」
#   图里并排两张卡：左边球鞋＝浅奶油黄**细**框、外面什么都没有；
#   右边「希雅＋大星星」＝浓橙金**粗**框、外面还有一圈橙金光晕。
#
# 对着这张图，在电脑端 287 张实机卡上量「bbox 内 1~3px 那一圈的饱和度中位数」，
# 得到**三簇，且中间有空档**（不是之前误以为的单峰）：
#
#     S 120~145   16 张   ← 浅黄细框（球鞋 / 藥瓶 / 部分篮球）
#     S 150~165  146 张   ← 同上，只是卡面底色偏橙把读数抬高一点
#     S 165~208    0 张   ← ★ 空档，阈值就放在这里
#     S 209~210  111 张   ← 浓橙金粗框（希雅星卡）
#
# 之前量不到这个簇，是因为采样点取在「内缩 1.5%~3.0%（实际 2~6px）」，
# 那一圈已经越过框线进了**卡面**（卡面本身也是橙黄，S≈170），
# 于是所有卡读出来都是 170 上下 —— 这才是「单峰」的真正原因。往最外沿挪 1~3px 就分开了。
#
# 交叉验证：同一帧里同时存在三张卡时（如 111156_item1_Jump），
# 球鞋 S=123、篮球 S=164、希雅星卡 S=209 —— 同屏同光照同尺寸，
# 只能是品质差异，不是卡种指纹。且比例 普176 / 金111 / 彩3 = 61% / 39% / 1%，
# 与玩家「普最多、金次之、彩最少」完全一致。
QUALITY_CALIBRATED = True

# 「金」的判据：框线最外沿的饱和度 ≥ 185（落在 165~208 这个空档的正中间）。
GOLD_EDGE_SAT = 185
GOLD_RULE = lambda ring: ring.get("edge_sat", 0) >= GOLD_EDGE_SAT

# 采样位置：**框线最外沿**，按卡片短边取「内缩 0.8%、厚 1.6%」的一圈。
#   131px 的卡 → 内缩 1px、厚 2px，即 bbox 内 1~3px 那一圈（正好压在框线上）。
# 再往里就进卡面了（卡面也是橙黄，会把金的读数压平），见上面那段说明。
QUALITY_RING = (0.008, 0.016)
QUALITY_HUE_BIG = 0.08          # 某个色相桶占总像素 ≥8% 才算「一种颜色」（仅日志用）
# 「彩」的判据：框线上**青/蓝/紫**（H 78~165）像素的占比。
# 用「暖色占比」不行 —— 白色底卡（「藥」字卡）暖色只有 0.71、绿黄色卡 0.91，
# 都会挤到阈值附近。改用「彩虹色相」占比，两边拉得极开（实测，人工核对过）：
#   彩卡   0.77 / 0.78 / 0.82     非彩卡  0.00 ~ 0.06
QUALITY_RAINBOW = 0.30


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
        if cv2.contourArea(c) / max(1, ww * hh) < MIN_EXTENT:
            continue        # 不是实心矩形卡（火焰伤害数字、飘字特效）
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
    if len(out) > MAX_CARDS:
        out = out[:MAX_CARDS]
    return out


def describe(frame):
    """给日志用的一句话：几张卡、各自左侧坐标。"""
    cards = find_cards(frame)
    return len(cards), [int(c["x"]) for c in cards]


def _quality_ring(frame, card):
    """读「品质框」那一圈：bbox 往内缩 QUALITY_RING 的一圈（框线本身）。

    为什么不取框外：卡在室外球场时，框外是蓝地板/绿草地，会把暖色占比拉到 0.00，
    金黄色卡也被判成彩（实测 13 张判彩里 7 张是这么来的）。框内那一圈才是品质色。

    为什么不取更里面：再往里 10% 是**所有卡共用**的亮黄细内框和卡面插画，
    读到那里每张卡都是「高饱和暖色」→ 全判金，这正是 2026-11-26 那次翻车的原因。
    """
    x, y, w, h = int(card["x"]), int(card["y"]), int(card["w"]), int(card["h"])
    short = min(w, h)
    gap = max(1, int(round(short * QUALITY_RING[0])))
    th = max(2, int(round(short * QUALITY_RING[1])))
    x0, y0 = x + gap, y + gap
    x1, y1 = x + w - gap, y + h - gap
    if x1 - x0 < 2 * th + 4 or y1 - y0 < 2 * th + 4:
        return None
    pat = frame[max(0, y0):y1, max(0, x0):x1]
    if pat.size == 0:
        return None
    ring = np.zeros(pat.shape[:2], bool)
    ring[:th, :] = ring[-th:, :] = True
    ring[:, :th] = ring[:, -th:] = True
    hsv = cv2.cvtColor(pat, cv2.COLOR_BGR2HSV)
    hue = hsv[:, :, 0].astype(int)
    sat, val = hsv[:, :, 1].astype(int), hsv[:, :, 2].astype(int)
    # 框线饱和度：**不过滤**（框线本身就有颜色，预筛反而会把淡黄框筛没）。
    # 这是区分「金」和「普」的唯一有效量，详见文件头那段三簇统计。
    edge_sat = float(np.median(sat[ring])) if ring.sum() else 0.0
    sel = ring & (sat > 80) & (val > 90)
    if sel.sum() < 40:
        return None
    hs = hue[sel]
    cnt = {}
    for v in (hs // 11):
        cnt[int(v)] = cnt.get(int(v), 0) + 1
    nbig = sum(1 for v in cnt.values() if v / len(hs) >= QUALITY_HUE_BIG)
    warm = float((((hs >= 8) & (hs <= 48)) | (hs >= 172)).mean())
    rainbow = float(((hs >= 78) & (hs <= 165)).mean())     # 青 / 蓝 / 紫
    return {"nbig": nbig, "warm": round(warm, 3), "rainbow": round(rainbow, 3),
            "edge_sat": round(edge_sat, 1),
            "sat": int(np.median(sat[sel])), "val": int(np.median(val[sel])), "n": int(len(hs))}


def card_quality(frame, card):
    """品质读数：{"q": 普/金/彩/无, "ring": 框线特征, "confident": 是否确证}

    confident=False 时它只是「按默认档暂计」，日志里要能看出来 ——
    不要把它当成「已确认」再去对账。
    """
    ring = None if (frame is None or card is None) else _quality_ring(frame, card)
    if ring is None:
        return {"q": "无", "ring": None, "confident": False}
    # 彩优先：框线上青/蓝/紫像素够多（彩虹光晕）。实测彩 0.77~0.91、非彩 ≤0.06。
    if ring["rainbow"] >= QUALITY_RAINBOW:
        return {"q": "彩", "ring": ring, "confident": True}
    if QUALITY_CALIBRATED and callable(GOLD_RULE):
        try:
            if GOLD_RULE(ring):
                return {"q": "金", "ring": ring, "confident": True}
        except Exception:
            pass
    # 校准后（QUALITY_CALIBRATED=True）剩下的就是确证的普；否则只是暂按普计。
    return {"q": "普", "ring": ring, "confident": bool(QUALITY_CALIBRATED)}


def card_glow(frame, card):
    """卡片品质：彩 / 金 / 普（读不到 → "无"）。

    【2026-11-26 三次纠正的完整结论】

      第一次「除了普通和彩，还有一个金框，这个不稀有的呀」
              → 我改成看饱和度 ≥150，结果 287 张里 274 张判金，把普全吞了。
      第二次「普最多、金次之、彩最少，这个规律」
              → 发现是**两层框**：外圈品质色 + 内圈所有卡共用的亮黄细框，
                旧读法读的是内圈，当然全是暖金色。
      第三次「左边就是普通的，右边就是金光」（附手机截图）
              → 对照图才找到正确的采样点：**框线最外沿 1~3px**。
                之前取 2~6px 已经越过框线进了卡面（卡面也是橙黄 S≈170），
                所以读出来是单峰 —— 单峰是采样错的假象，不是游戏里真没有金。

    当前判据（287 张实机卡实测，中间有空档）：

      彩   框线青/蓝/紫占比 ≥0.30      → 彩虹光晕，实测 3 张（1%）
      金   框线最外沿饱和度 ≥185        → 浓橙金粗框，实测 111 张（39%）
      普   其余（浅奶油黄细框）         → 实测 176 张（61%）

    与玩家手机截图对账：截图左卡（球鞋）→ S≈123 判普；右卡（希雅＋大星星）
    → S≈209 判金，与电脑端同一件器材的读数完全一致。
    """
    return card_quality(frame, card)["q"]
