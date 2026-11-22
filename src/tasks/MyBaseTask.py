import re

import numpy as np

from ok import BaseTask


class MyBaseTask(BaseTask):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    # ---- 通用小工具 ----

    def mean_bgr(self, box, frame=None):
        """返回某个 Box 区域的平均颜色 (B, G, R)。"""
        frame = frame if frame is not None else self.executor.nullable_frame()
        if frame is None:
            return None
        x1, y1 = max(int(box.x), 0), max(int(box.y), 0)
        x2 = min(int(box.x + box.width), frame.shape[1])
        y2 = min(int(box.y + box.height), frame.shape[0])
        if x2 <= x1 or y2 <= y1:
            return None
        patch = frame[y1:y2, x1:x2]
        b, g, r = patch.reshape(-1, 3).mean(axis=0)
        return float(b), float(g), float(r)

    @staticmethod
    def is_yellow(bgr):
        """可点击的黄色按钮；灰掉时三个通道接近，r-b 会很小。"""
        if bgr is None:
            return False
        b, g, r = bgr
        return r > 180 and g > 140 and b < 140 and (r - b) > 70

    def find_text_box(self, text, exact=True):
        """按文字找最靠下的一个框，exact=True 时整串匹配。"""
        pattern = re.compile("^" + re.escape(text) + "$") if exact else re.compile(re.escape(text))
        boxes = self.ocr(match=pattern) or []
        if not boxes:
            return None
        return sorted(boxes, key=lambda b: -b.y)[0]
