"""训练核心页连拍扫查：把「点开技能」前后每一帧都存下来。

为什么要连拍：
    2026-11-23 05:14 实测发现，点技能胶囊后画面会先播一段很短的动作演示 +
    加成浮层（左侧「倍率加成 / 属性加成 / 表情一致 / 额外队友」四张卡），
    1~2 秒后浮层就收回，只剩选中态。StepTask 那种「点完等 0.6 秒取一帧」
    的做法正好落在浮层消失之后，所以之前一直以为这页「没有卡片」。

    这个任务点一次技能就连拍 N 帧，浮层出现/稳定/消失三个阶段都能留证。

产出：screenshots/sweep/<序号>_<连拍号>.png + 日志里的逐帧 OCR 文字
"""
import os
import time

import cv2

from src.tasks.AutoTask import AutoTask


class CoreSweepTask(AutoTask):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "训练核心连拍 / CoreSweep"
        self.description = "逐个点开右侧技能并连拍，记录浮层出现全过程（不点决定）"
        self.default_config.update({
            "连拍帧数": 14,
            "连拍间隔": 0.2,
            "点击后首个延迟": 0.05,
        })

    def _save(self, frame, idx, k):
        os.makedirs("screenshots/sweep", exist_ok=True)
        p = f"screenshots/sweep/{idx:02d}_{k:02d}.png"
        cv2.imwrite(p, frame)
        return p

    def _grab(self):
        """尽量取新帧；切场景瞬间可能没有可用帧，返回 None。"""
        try:
            f = self._frame()
            return f
        except Exception:
            return None

    def run(self):
        frame = self._grab()
        if frame is None:
            self.log_error("取不到画面", notify=True)
            return False

        items = self._texts(frame)
        page = self._classify(items)
        self.log_info(f"页面判定：{page}", notify=True)
        if page != "训练核心":
            self.log_error(f"当前不是训练核心页（判定为「{page}」）：{[i['t'] for i in items][:20]}",
                           notify=True)
            return False

        h, w = frame.shape[:2]
        rows = [it for it in items if it["t"] in self.SKILLS and it["cx"] / w > 0.70]
        rows.sort(key=lambda x: x["cy"])
        if not rows:
            self.log_error("没找到右侧技能胶囊", notify=True)
            return False

        order = {"投射": 0, "弹跳": 1, "运控": 2, "体能": 3, "防守": 4, "意识": 5}
        rows.sort(key=lambda x: order.get(x["t"], 9))
        self.log_info(f"待扫技能：{[r['t'] for r in rows]}")

        n = int(self.config.get("连拍帧数") or 14)
        gap = float(self.config.get("连拍间隔") or 0.2)

        for idx, r in enumerate(rows, 1):
            self.log_info(f"===== 点开「{r['t']}」（{idx}/{len(rows)}）连拍 {n} 帧 =====")
            self._click_item(r, "[sweep]")
            time.sleep(float(self.config.get("点击后首个延迟") or 0.05))
            seen = []
            for k in range(n):
                f = self._grab()
                if f is not None:
                    self._save(f, idx, k)
                    try:
                        its = self._texts(f)
                    except Exception:
                        its = []
                    texts = [i["t"] for i in its]
                    bonus = [t for t in texts if any(w in t for w in
                                                     ("加成", "队友", "表情", "倍率", "一致", "组合", "额外"))]
                    names = self._court_names(its, f)
                    pct = self._read_pct(its)
                    names_only = [t for t in texts if 2 <= len(t) <= 5
                                  and t not in self.SKILLS
                                  and not any(ch in t for ch in "0123456789%/")]
                    self.log_info(f"  帧{k:02d} 文字{len(texts)} 失败率={pct} 场上{names} "
                                  f"加成块{bonus} 短词{names_only[:12]}")
                    for t in texts:
                        if t not in seen:
                            seen.append(t)
                time.sleep(gap)
            self.log_info(f"  「{r['t']}」全部出现过的文字：{seen[:40]}")

        self.log_info("扫查结束：没点「决定」，不会开训", notify=True)
        return True
