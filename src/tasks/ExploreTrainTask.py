import os
import re

from ok import Icon
from src.tasks.MyBaseTask import MyBaseTask


class ExploreTrainTask(MyBaseTask):
    """一次性探索任务：进入训练流程，逐屏截图 + OCR，并把每步结果写进日志。

    目的是把「训练入口 -> 下一步 -> 进入训练」这条链路的真实页面录下来，
    后续 AutoTrainTask 才有依据去写判断条件。
    """

    NEXT_TEXTS = ["启程", "下一步", "开始训练", "确认", "继续", "开始"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "探索训练流程 / Explore Train"
        self.description = "点击训练入口并自动点下一步，逐屏保存截图用于分析"
        self.icon = Icon.SYNC
        self.default_config.update({
            "Max Steps": 8,
            "Step Wait": 2.0,
        })
        self.config_description.update({
            "Max Steps": "最多点几次按钮 / Max clicks before stopping",
            "Step Wait": "每次点击后等待界面稳定的秒数 / Seconds to wait after each click",
        })

    def shot(self, tag):
        import cv2
        folder = "screenshots"
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"explore_{tag}.png")
        cv2.imwrite(path, self.frame)
        self.log_info(f"已保存 {path}")

    def dump_text(self, tag):
        boxes = self.ocr(log=True) or []
        self.log_info(f"[{tag}] 识别 {len(boxes)} 段文本")
        for b in boxes:
            self.log_info(f"  [{b.x},{b.y},{b.width}x{b.height}] {b.name}")
        return boxes

    def find_next(self):
        for text in self.NEXT_TEXTS:
            boxes = self.ocr(match=re.compile("^" + re.escape(text) + "$")) or []
            if boxes:
                box = sorted(boxes, key=lambda b: -b.y)[0]
                return text, box
        return None, None

    def find_train_entry(self):
        entries = self.ocr(match=re.compile("^训练$")) or []
        return sorted(entries, key=lambda b: -b.y)[0] if entries else None

    def back_to_home(self):
        """不在主界面时点左上角返回，最多退 5 层。"""
        for _ in range(5):
            if self.find_train_entry():
                return True
            self.log_info("不在主界面，点左上角返回")
            self.click_relative(0.046, 0.068)
            self.sleep(1.5)
        return bool(self.find_train_entry())

    def run(self):
        frame = self.next_frame()
        if frame is None:
            self.log_error("没有画面，先确认游戏窗口在运行", notify=True)
            return False
        self.log_info(f"当前画面 {frame.shape[1]}x{frame.shape[0]}")
        self.shot("00_home")
        self.dump_text("home")

        if not self.back_to_home():
            self.log_error("退不回主界面，也没找到「训练」按钮", notify=True)
            return False

        # 1. 点训练入口
        entries = self.ocr(match=re.compile("^训练$")) or []
        if not entries:
            entries = self.ocr(match="训练") or []
        if not entries:
            self.log_error("没找到「训练」按钮，可能不在主界面", notify=True)
            return False
        entry = sorted(entries, key=lambda b: -b.y)[0]
        self.log_info(f"点击训练入口 {entry}")
        self.click_box(entry)
        self.sleep(self.config.get("Step Wait") or 2.0)
        self.shot("01_after_entry")
        self.dump_text("after_entry")

        # 2. 一直点下一步
        for i in range(int(self.config.get("Max Steps") or 8)):
            text, box = self.find_next()
            if not box:
                self.log_warning(f"第 {i + 1} 次：没有找到下一步类按钮，停止")
                break
            self.log_info(f"第 {i + 1} 次点击：{text} {box}")
            self.click_box(box)
            self.sleep(self.config.get("Step Wait") or 2.0)
            self.shot(f"{i + 2:02d}_after_next")
            self.dump_text(f"after_next_{i + 1}")

        self.log_info("探索结束，截图已保存在 screenshots/", notify=True)
        return True
