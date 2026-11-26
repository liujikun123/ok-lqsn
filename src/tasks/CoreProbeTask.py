"""训练项页体检：把带角标的技能挨个点开，存图 + 报告编队。

目的：把「每个训练项选项里有几个队友 / 几个器材 / 什么光效」这件事在真机上一次性看清。
**不点「决定」**，不会真的开训。

玩家 2026-11-22 确认：
- 队友 = 球场上站着的角色（名字标签），主力（希雅）不算队友
- 器材 = 画面中间那排卡片
- 金光 / 彩光 = 卡片外框的光效
- 只检查右上角带橙色小人角标的技能

产出：screenshots/core_probe/<序号>_<技能>.png（整屏）
      screenshots/core_probe/<序号>_<技能>_cards.png（中间卡片区放大 2 倍）
"""
import os
import time

import cv2

from src.core.cards import card_glow, find_cards
from src.tasks.AutoTask import AutoTask


class CoreProbeTask(AutoTask):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "训练项页体检 / CoreProbe"
        self.description = "把带角标的技能逐个点开并截图，报告队友/器材/光效（不点决定）"
        self.default_config.update({
            "保存截图": True,
            "每步等待秒数": 1.3,
            "也测无角标的": False,
        })

    # ---------- 工具 ----------

    def _card_crop(self, frame):
        """截取中间那排卡片所在的横条。"""
        h, w = frame.shape[:2]
        return frame[int(h * 0.44):int(h * 0.80), int(w * 0.18):int(w * 0.66)]

    def _save(self, frame, tag):
        os.makedirs("screenshots/core_probe", exist_ok=True)
        stamp = time.strftime("%H%M%S")
        full = f"screenshots/core_probe/{stamp}_{tag}.png"
        crop = f"screenshots/core_probe/{stamp}_{tag}_cards.png"
        cv2.imwrite(full, frame)
        c = self._card_crop(frame)
        cv2.imwrite(crop, cv2.resize(c, (c.shape[1] * 2, c.shape[0] * 2),
                                     interpolation=cv2.INTER_LANCZOS4))
        return full, crop

    # ---------- 主流程 ----------

    def run(self):
        frame = self._frame()
        if frame is None:
            self.log_error("取不到画面（窗口最小化时 WGC 抓不到）", notify=True)
            return False

        items = self._texts(frame)
        page = self._classify(items)
        self.log_info(f"页面判定：{page}", notify=True)
        if page != "训练项页":
            self.log_error(f"当前不是训练项页（判定为「{page}」）。"
                           f"请先手动进到训练项页再运行。屏幕文字：{[i['t'] for i in items][:25]}",
                           notify=True)
            return False

        h, w = frame.shape[:2]
        rows = [it for it in items if it["t"] in self.SKILLS and it["cx"] / w > 0.70]
        rows.sort(key=lambda x: x["cy"])
        if not rows:
            self.log_error("没找到右侧训练项条目，请把截图发我", notify=True)
            return False

        marked = []
        asg = self._badges_for_rows(frame, rows)
        for i, r in enumerate(rows):
            spot = asg.get(i)
            if spot:
                self.log_info(f"「{r['t']}」有角标 @({int(spot[0])},{int(spot[1])})")
                marked.append(r)
            else:
                self.log_info(f"「{r['t']}」没有角标")
        self.log_info(f"当前场上角色名：{self._court_names(items, frame)}", notify=True)

        targets = rows if self.config.get("也测无角标的") else marked
        if not targets:
            self.log_warning("没有任何技能带角标，无需检查", notify=True)
            return True

        if self.config.get("保存截图"):
            self._save(frame, "00_初始")

        summary = []
        for idx, r in enumerate(targets, 1):
            self.log_info(f"—— 点开「{r['t']}」（{idx}/{len(targets)}）")
            self._click_item(r, "[CoreProbe]")
            self.sleep(float(self.config.get("每步等待秒数") or 1.3))

            f2 = self._frame()
            if f2 is None:
                self.log_warning(f"「{r['t']}」点完后取不到画面，跳过")
                continue
            its = self._texts(f2)
            pct = self._read_pct(its)
            cards = find_cards(f2)
            glows = [card_glow(f2, c) for c in cards]
            names = self._court_names(its, f2)
            mates = [n for n in names if n != self.MAIN_CHAR]
            tag = f"{idx:02d}_{r['t']}"
            if self.config.get("保存截图"):
                self._save(f2, tag)
            self.log_info(f"「{r['t']}」失败率={pct}% 器材{len(cards)}张{glows} "
                          f"场上{names} → 队友{len(mates)}{mates}")
            summary.append((r["t"], pct, len(cards), len(mates), "".join(glows) or "-"))

        self.log_info("===== 汇总 =====")
        for t, p, n, m, g in summary:
            self.log_info(f"  {t:<3} 失败率={p}%  器材{n}张[{g}]  队友{m}人")
        self.log_info("体检结束：没点「决定」，不会开训")
        return True
