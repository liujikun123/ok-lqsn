"""示范录制：你在游戏里正常操作一遍，程序把每一步的「画面 + 识别到的文字 + 点击位置」全记下来。

和原来那个屏外录制脚本的区别：这个跑在框架里，用的是和正式任务完全相同的截图、
OCR、分辨率（1920x1080），所以录下来的东西可以直接当脚本素材用，不用二次换算。

用法：
    双击 record_demo.bat
    玩完之后按 F12

产物（demo/session_<时间>/）：
    frames/*.png    每一步的画面
    timeline.json   完整时间轴（每步的文字 + 坐标 + 点击）
    summary.md      文字版流程摘要，直接可以看
"""
import hashlib
import json
import os
import threading
import time
from datetime import datetime

import cv2
import numpy as np

from ok.task.task import BaseTask

WINDOW_KEYWORDS = ["篮球少女", "飞跃巅峰"]


def _client_rect():
    """游戏画面区域（不含标题栏），用来把鼠标屏幕坐标换成画面内的相对坐标。"""
    try:
        import win32gui
        hit = []

        def cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd) and any(k in (win32gui.GetWindowText(hwnd) or "") for k in WINDOW_KEYWORDS):
                cl, ct, cr, cb_ = win32gui.GetClientRect(hwnd)
                if cr > 400 and cb_ > 300:
                    sl, st = win32gui.ClientToScreen(hwnd, (0, 0))
                    sr, sb = win32gui.ClientToScreen(hwnd, (cr, cb_))
                    hit.append((sl, st, sr, sb))
            return True

        win32gui.EnumWindows(cb, None)
        return hit[0] if hit else None
    except Exception:
        return None


def _game_visible():
    """游戏窗口是否真的可见（没最小化、尺寸正常）。不可见时绝不能取帧，否则会永久阻塞。"""
    try:
        import win32gui
        state = {"ok": False}

        def cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if not any(k in (win32gui.GetWindowText(hwnd) or "") for k in WINDOW_KEYWORDS):
                return True
            if win32gui.IsIconic(hwnd):
                return True
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            if r - l > 400 and b - t > 300:
                state["ok"] = True
            return True

        win32gui.EnumWindows(cb, None)
        return state["ok"]
    except Exception:
        return True


class RecordTask(BaseTask):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "示范录制"
        self.description = "录制你手动操作的全过程（画面+文字+点击），按 F12 结束"
        self.default_config = {
            "录制秒数": 0,      # 0 = 一直录到按 F12
            "采样间隔": 0.8,    # 画面自己变化时，最快多久存一张
        }
        self.clicks = []
        self._stop = False
        self._last_hash = None

    # ---------- 内部 ----------
    def _drain_clicks(self):
        """只保留落在游戏画面内的点击，点别的窗口不算。"""
        got = self.clicks[:]
        self.clicks.clear()
        rect = _client_rect()
        out = []
        for c in got:
            if not rect:
                out.append(c)
                continue
            w, h = rect[2] - rect[0], rect[3] - rect[1]
            rx, ry = (c["x"] - rect[0]) / w, (c["y"] - rect[1]) / h
            if 0 <= rx <= 1 and 0 <= ry <= 1:
                c["rx"], c["ry"] = round(rx, 3), round(ry, 3)
                out.append(c)
        return out

    def _grab(self):
        """取一帧新画面。窗口不可见时不调用（next_frame 会永久阻塞），返回 None。"""
        if not _game_visible():
            return None
        try:
            return self.next_frame()
        except Exception:
            return None

    def _texts(self, frame):
        try:
            boxes = self.ocr(frame=frame) or []
        except Exception as e:
            self.log_warning(f"OCR 失败：{e}")
            return []
        out = []
        for b in boxes:
            try:
                out.append([b.name, int(b.x), int(b.y), int(b.width), int(b.height)])
            except Exception:
                continue
        return out

    def _diff(self, a, b):
        if a is None or b is None or a.shape != b.shape:
            return 99.0
        return float(np.abs(a.astype("int16") - b.astype("int16")).mean())

    # ---------- 主流程 ----------
    def run(self):
        stamp = datetime.now().strftime("%m%d_%H%M%S")
        root = os.path.join("demo", f"session_{stamp}")
        frames_dir = os.path.join(root, "frames")
        os.makedirs(frames_dir, exist_ok=True)

        seconds = float(self.config.get("录制秒数") or 0) or float(os.getenv("OK_RECORD_SECONDS", 0) or 0)
        interval = float(self.config.get("采样间隔") or 0.8)
        deadline = time.time() + seconds if seconds > 0 else None

        events = []
        seq = 0
        self._stop = False

        # 全局钩子：只监听，不注入任何操作
        try:
            from pynput import keyboard, mouse

            def on_click(x, y, button, pressed):
                if pressed:
                    self.clicks.append({"x": x, "y": y, "btn": str(button).replace("Button.", ""), "t": time.time()})
                return True

            def on_key(key):
                if getattr(key, "name", "") == "f12":
                    self._stop = True
                    return False
                return True

            mouse.Listener(on_click=on_click).start()
            keyboard.Listener(on_press=on_key).start()
        except Exception as e:
            self.log_warning(f"全局钩子没起来（将只能按秒数结束）：{e}")

        self.log_info("开始录制。请在游戏里正常操作，结束按 F12。")
        prev = None
        last_save = 0.0
        t0 = time.time()
        last_warn = 0.0
        first_ok = False

        while not self._stop:
            if deadline and time.time() > deadline:
                break
            try:
                frame = self._grab()
            except Exception as e:
                self.log_warning(f"取画面失败：{e}")
                time.sleep(0.5)
                continue
            if frame is None:
                now = time.time()
                if now - last_warn > 5:  # 别让用户对着黑屏干等
                    last_warn = now
                    self.log_warning("抓不到游戏画面：窗口被最小化了。请把游戏窗口恢复出来"
                                     "（被其他窗口挡住没关系，最小化不行）")
                time.sleep(0.3)
                continue

            if not first_ok:
                first_ok = True
                self.log_info("画面已连上，正在记录你的操作……")

            digest = hashlib.md5(frame.tobytes()).hexdigest()
            if digest == self._last_hash:
                time.sleep(0.05)   # 同一个画面，别重复存
                continue
            self._last_hash = digest

            now = time.time()
            clicks = self._drain_clicks()
            d = self._diff(prev, frame)
            if not clicks and (d < 2.0 or now - last_save < interval):
                prev = frame
                continue

            seq += 1
            name = f"{seq:03d}.png"
            cv2.imwrite(os.path.join(frames_dir, name), frame)
            texts = self._texts(frame)
            ev = {
                "i": seq,
                "t": round(now - t0, 2),
                "kind": "click" if clicks else "screen",
                "img": f"frames/{name}",
                "texts": texts,
            }
            if clicks:
                c = clicks[0]
                ev["click"] = {"x": c["x"], "y": c["y"], "btn": c["btn"],
                               "rx": c.get("rx"), "ry": c.get("ry")}
            events.append(ev)
            prev = frame
            last_save = now
            self.log_info(f"[{seq:03d}] {ev['kind']} 文字 {len(texts)} 段"
                          + (f" 点击({ev['click'].get('rx')},{ev['click'].get('ry')})" if clicks else ""))

        with open(os.path.join(root, "timeline.json"), "w", encoding="utf-8") as f:
            json.dump({"started": stamp, "size": list(prev.shape[1::-1]) if prev is not None else None,
                       "events": events}, f, ensure_ascii=False, indent=1)

        with open(os.path.join(root, "summary.md"), "w", encoding="utf-8") as f:
            f.write(f"# 示范录制 {stamp}\n\n共 {len(events)} 个节点\n\n")
            for ev in events:
                head = f"## {ev['i']:03d}  t={ev['t']}s  {ev['kind']}"
                if ev.get("click", {}).get("rx") is not None:
                    head += f"  点击相对坐标 ({ev['click']['rx']}, {ev['click']['ry']})"
                f.write(head + "\n\n```\n")
                for t in ev["texts"]:
                    f.write(f"{t[0]}\n")
                f.write("```\n\n")

        self.log_info(f"录制结束，{len(events)} 个节点 → {os.path.abspath(root)}")
        return True
