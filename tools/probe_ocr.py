"""只读探针：把当前画面 OCR 出来逐段打印，用来查「数字读不到」这类问题。

通过 MCP 跑：
    ../ok-bsg/.venv/Scripts/python.exe tools/mcp_client.py run_task --file tools/probe_ocr.py

只读，不点任何按钮。
"""

import re

from src.tasks.MyBaseTask import MyBaseTask


class Task(MyBaseTask):

    def reset_to_start(self):
        return True

    def verify_start_state(self):
        return True

    def run(self):
        frame = self.next_frame()
        if frame is None:
            self.log_info("取不到画面")
            return True
        boxes = self.ocr(frame=frame) or []
        h, w = frame.shape[:2]
        self.log_info(f"画面 {w}x{h}，共 {len(boxes)} 段文字")
        for b in boxes:
            t = (b.name or "").strip()
            cx, cy = b.x + b.width / 2, b.y + b.height / 2
            m = re.search(r"^(\d{1,3})\s*/\s*(\d{2,3})\+?$", t.replace(" ", ""))
            flag = f"  <== 匹配 a/b: {m.group(1)}/{m.group(2)}" if m else ""
            self.log_info(f"「{t}」@({int(cx)},{int(cy)}) 相对({cx / w:.2f},{cy / h:.2f}) "
                          f"w={int(b.width)} h={int(b.height)}{flag}")
        self.screenshot("probe_ocr", frame=frame)
        return True
