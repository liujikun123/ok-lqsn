import os

from ok import BaseTask, Icon
from src.tasks.MyBaseTask import MyBaseTask


class ProbeTask(MyBaseTask):
    """验证自动化链路：设备连接 -> 截图 -> 分辨率 -> OCR。

    游戏还没装好、或者换了模拟器分辨率的时候，先跑这个任务确认底层是通的，
    再去写真正的业务任务。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "环境探测 / Probe"
        self.description = "检查设备连接、截一张图、识别分辨率并做一次全屏 OCR，截图保存到 screenshots/probe.png"
        self.icon = Icon.SYNC
        self.default_config.update({
            "Save Screenshot": True,
            "Run OCR": True,
            "OCR Log Lines": 40,
        })
        self.config_description.update({
            "Save Screenshot": "把当前画面保存到 screenshots/probe.png / Save the current frame to screenshots/probe.png",
            "Run OCR": "对整屏做一次 OCR，用于确认文字识别可用 / Run one full-screen OCR pass",
            "OCR Log Lines": "日志里最多打印多少条 OCR 结果 / Max OCR results to log",
        })

    def run(self):
        frame = None
        for i in range(5):
            frame = self.next_frame()
            if frame is not None:
                break
            self.log_info(f"等待画面... ({i + 1}/5)")
            self.sleep(1)

        if frame is None:
            self.log_error("没有取到画面。请检查：模拟器已启动且游戏已进入画面、adb devices 能看到设备、"
                           "或 config.py 里的 windows/adb 目标是否填对。", notify=True)
            return False

        height, width = frame.shape[:2]
        self.log_info(f"截图成功，分辨率 {width}x{height}", notify=True)
        self.info_set("分辨率", f"{width}x{height}")

        if self.config.get("Save Screenshot"):
            folder = self.config.get("screenshots_folder") or "screenshots"
            try:
                import cv2
                os.makedirs(folder, exist_ok=True)
                path = os.path.join(folder, "probe.png")
                cv2.imwrite(path, frame)
                self.log_info(f"截图已保存: {path}")
            except Exception as e:
                self.log_warning(f"保存截图失败: {e}")

        if self.config.get("Run OCR"):
            try:
                boxes = self.ocr(log=True) or []
            except Exception as e:
                self.log_warning(f"OCR 执行失败（不影响截图链路）: {e}")
                boxes = []
            self.log_info(f"OCR 识别到 {len(boxes)} 段文本", notify=True)
            self.info_set("OCR 文本数", len(boxes))
            limit = int(self.config.get("OCR Log Lines") or 40)
            for box in boxes[:limit]:
                self.log_info(f"  [{box.x},{box.y},{box.width}x{box.height}] {box.name}")

        return True
