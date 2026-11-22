import hashlib
import os
import time

from ok import Icon
from src.tasks.MyBaseTask import MyBaseTask


class StepTask(MyBaseTask):
    """单步操作工具：一次运行只做一个动作，前后各截一张图。

    通过环境变量 OK_STEP 指定动作，避免每步都改配置文件：
      OK_STEP=shot            只截图 + OCR，不动游戏（默认）
      OK_STEP=ocr:启程        按文字找按钮并点击
      OK_STEP=xy:0.88,0.87    按相对坐标点击
    截图保存在 screenshots/step_<时间>_<动作hash>_before.png / _after.png
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "单步操作 / Step"
        self.description = "执行一个动作并前后截图：OK_STEP=shot | ocr:文字 | xy:0.5,0.5"
        self.icon = Icon.SYNC
        self.default_config.update({
            "Wait After": 2.5,
        })

    # ---- 工具 ----

    @staticmethod
    def tag_of(text):
        return hashlib.md5(text.encode("utf-8")).hexdigest()[:6]

    def window_ready(self):
        try:
            hwnd = self.executor.device_manager.capture_method.hwnd_window
            self.log_info(f"窗口状态: hwnd={hwnd.hwnd} pos_valid={hwnd.pos_valid} "
                          f"size={hwnd.window_width}x{hwnd.window_height}")
            return bool(hwnd.pos_valid)
        except Exception as e:
            self.log_warning(f"读不到窗口状态: {e}")
            return True

    def save(self, tag, frame=None):
        import cv2
        frame = frame if frame is not None else self.executor.nullable_frame()
        if frame is None:
            self.log_warning(f"[{tag}] 当前没有可用画面，跳过截图")
            return None
        os.makedirs("screenshots", exist_ok=True)
        path = os.path.join("screenshots", f"step_{tag}.png")
        cv2.imwrite(path, frame)
        self.log_info(f"截图: {path}")
        return path

    def dump(self, tag):
        boxes = self.ocr(log=False) or []
        self.log_info(f"[{tag}] 识别 {len(boxes)} 段文本")
        for b in boxes:
            self.log_info(f"  [{b.x},{b.y},{b.width}x{b.height}] {b.name}")
        return boxes

    # ---- 主流程 ----

    def run(self):
        step = (os.environ.get("OK_STEP") or "shot").strip()
        frame = self.next_frame()
        if frame is None:
            self.log_error("没有画面：请确认游戏窗口没有最小化", notify=True)
            return False

        ts = time.strftime("%H%M%S")
        name = f"{ts}_{self.tag_of(step)}"
        self.log_info(f"=== 步骤 [{step}] 开始，画面 {frame.shape[1]}x{frame.shape[0]} ===")
        before = frame.copy()
        self.save(f"{name}_before")
        self.dump("before")

        if step == "shot":
            self.log_info("仅截图，未操作")
            return True

        if not self.window_ready():
            self.log_error("游戏窗口不可见（被最小化或已关闭），为避免点错，已取消操作。"
                           "请把游戏窗口切到最前面后重试。", notify=True)
            return False

        acted = False
        if step.startswith("ocr:"):
            text = step[4:].strip()
            box = self.find_text_box(text)
            if box is None:
                self.log_error(f"没找到文字「{text}」，未操作", notify=True)
                return False

            bgr = self.mean_bgr(box, frame)
            self.log_info(f"「{text}」区域平均色 BGR={bgr}")

            # 关键规则：下一步必须是黄色才点，灰色说明没选人
            if text == "下一步" and not self.is_yellow(bgr):
                self.log_error("「下一步」按钮是灰色的（未选人/条件不满足），已停止，请处理后重试。",
                               notify=True)
                self.info_set("状态", "下一步灰色，已停止")
                return False

            self.log_info(f"点击「{text}」{box}")
            self.click_box(box)
            acted = True
        elif step.startswith("xy:"):
            parts = step[3:].split(",")
            x, y = float(parts[0]), float(parts[1])
            self.log_info(f"点击相对坐标 ({x}, {y})")
            self.click_relative(x, y)
            acted = True
        else:
            self.log_warning(f"未知步骤「{step}」，只截图")

        if acted:
            # 点击后游戏切场景时会短暂没有画面，轮询等新帧，不要直接取
            after = None
            for _ in range(10):
                self.sleep(0.6)
                after = self.executor.nullable_frame()
                if after is not None:
                    break
            if after is None:
                self.log_warning("点击后一直取不到画面，窗口可能被最小化了")
                return False
            self.save(f"{name}_after", after)
            if after.shape == before.shape:
                import numpy as np
                diff = float(np.abs(after.astype("int16") - before.astype("int16")).mean())
                self.log_info(f"前后画面差异: {diff:.2f}（>0.5 说明界面确实变了）")
                if diff <= 0.5:
                    self.log_warning("界面几乎没有变化，点击可能没生效")
            self.dump("after")
        return True
