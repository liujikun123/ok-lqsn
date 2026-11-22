"""一次性探针：查「专注恢复」弹窗里点「使用」为什么没生效。

通过 MCP 跑：
    python tools/mcp_client.py run_task --file tools/probe_stamina.py

只读 + 最多点一次，不改游戏进度。
"""

import time

from src.tasks.MyBaseTask import MyBaseTask


class Task(MyBaseTask):

    def reset_to_start(self):
        # 只读探针，不动游戏状态
        return True

    def verify_start_state(self):
        return True

    def _dump(self, tag):
        frame = self.next_frame()
        if frame is None:
            self.log_info(f"[{tag}] 取不到画面")
            return None, []
        boxes = self.ocr(frame=frame) or []
        items = [{"t": (b.name or "").strip(), "b": b,
                  "cx": b.x + b.width / 2, "cy": b.y + b.height / 2} for b in boxes]
        self.log_info(f"[{tag}] {frame.shape[1]}x{frame.shape[0]} 共 {len(items)} 段文字")
        for it in items:
            self.log_info(f"[{tag}]   「{it['t']}」@({int(it['cx'])},{int(it['cy'])}) "
                          f"w={int(it['b'].width)} h={int(it['b'].height)}")
        self.screenshot(f"probe_{tag}", frame=frame)
        return frame, items

    def run(self):
        _, items = self._dump("before")

        uses = [it for it in items if it["t"] == "使用"]
        self.log_info(f"找到 {len(uses)} 个「使用」按钮")
        if not uses:
            self.log_info("没有「使用」按钮，弹窗可能已经关了，直接结束")
            return True

        # 取最下面那个（大瓶装 +60）
        target = max(uses, key=lambda x: x["cy"])
        self.log_info(f"目标按钮「{target['t']}」中心 @({target['cx']:.0f},{target['cy']:.0f})")

        # 方案 A：点文字框中心
        self.log_info(f"--- A: click(文字中心 {int(target['cx'])},{int(target['cy'])})")
        self.click(int(target["cx"]), int(target["cy"]))
        self.sleep(1.2)
        self._dump("after_a")

        # 若还没变化，方案 B：点按钮右侧（避开文字，落在黄底上）
        _, items2 = self._dump("check_a")
        has_use = any(it["t"] == "使用" for it in items2)
        self.log_info(f"点击后弹窗还在？{has_use}")

        if has_use:
            bx = int(target["cx"] + target["b"].width * 0.5)
            by = int(target["cy"])
            self.log_info(f"--- B: click(按钮右侧 {bx},{by})")
            self.click(bx, by)
            self.sleep(1.2)
            self._dump("after_b")

        return True
