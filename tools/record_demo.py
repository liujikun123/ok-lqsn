"""录制人工示范：你在游戏里正常操作一遍，程序记录每一次点击 / 拖拽 / 按键 + 对应画面。

**默认不需要键盘**——开始后直接操作，停手若干秒自动收尾（适合手机远程操控）。

用法（在 bsg-auto 根目录）：
    ../ok-bsg/.venv/Scripts/python.exe tools/record_demo.py            # 开始录制，停手 25s 自动结束
    ../ok-bsg/.venv/Scripts/python.exe tools/record_demo.py --auto-stop 40   # 想中间停顿久一点
    ../ok-bsg/.venv/Scripts/python.exe tools/record_demo.py --test     # 只截一张图，验证能抓到游戏窗口
    ../ok-bsg/.venv/Scripts/python.exe tools/record_demo.py --no-video # 不录 mp4，只存关键帧

结束方式（任选其一，都不用键盘）：
    1. 停手不动 → 最后一次操作后 auto-stop 秒（默认 25）自动收尾
    2. 到 --max 总时长（默认 300s）强制收尾
    3. 有键盘的话，F12 立即结束（Esc 是游戏返回键，不占用）

分段：不再需要 F9。相邻两次操作间隔 ≥ 3.5s 自动算作新的一段，时间线里会画分隔线。

产出 demo/session_<时间>/：
    frames/*.png     每次按下、以及抬起 0.35s 后的结果各一张
    timeline.md      人可读时间线（AI 复盘就看这个）
    events.jsonl     原始事件（含拖拽途经点，归一化坐标）
    report.html      缩略图墙，点击打红圈、拖拽画红箭头，分节显示
    screen.mp4       整段画面（--no-video 时不生成）

坐标两种都给：屏幕像素 + 相对游戏窗口的归一化 (0~1)。脚本里点击用相对坐标，
复盘以归一化那列为准。
"""
import argparse
import ctypes
import json
import os
import sys
import threading
import time
from datetime import datetime

import cv2
import numpy as np
from PIL import ImageGrab

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 防止缩放导致坐标偏移
except Exception:
    pass

WINDOW_KEYWORDS = ["篮球少女", "飞跃巅峰"]
PROC_KEYWORDS = ["k3_windows_ob"]

STOP_KEY = "f12"
MARK_KEY = "f9"
AFTER_DELAY = 0.35        # 抬起后多久抓「结果帧」：太早还是旧画面，太晚动画跑完了
SEG_GAP = 3.5             # 相邻操作间隔超过这个秒数 → 时间线里算作新的一段


def find_game_window():
    import win32gui
    import win32process

    hits = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd) or ""
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            import win32api
            import win32con
            h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            exe = os.path.basename(win32process.GetModuleFileNameEx(h, None) or "")
        except Exception:
            exe = ""
        if any(k in title for k in WINDOW_KEYWORDS) or any(k in exe.lower() for k in PROC_KEYWORDS):
            r = win32gui.GetWindowRect(hwnd)
            w, h_ = r[2] - r[0], r[3] - r[1]
            if w > 400 and h_ > 300:
                hits.append((hwnd, title, exe, r))
        return True

    win32gui.EnumWindows(cb, None)
    if not hits:
        return None
    hits.sort(key=lambda x: -(x[3][2] - x[3][0]) * (x[3][3] - x[3][1]))
    return hits[0]


def client_rect(hwnd):
    """游戏画面区域（不含标题栏和边框），比 GetWindowRect 更准。"""
    import win32gui
    cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
    sl, st = win32gui.ClientToScreen(hwnd, (0, 0))
    sr, sb = win32gui.ClientToScreen(hwnd, (cr, cb))
    return (sl, st, sr, sb)


def grab(rect):
    l, t, r, b = rect
    img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def is_foreground(hwnd):
    import win32gui
    return win32gui.GetForegroundWindow() == hwnd


class Session:
    def __init__(self, root, rect, fps=6.0, video=True):
        self.root = root
        self.rect = rect
        self.l, self.t, self.r, self.b = rect
        self.w = max(1, self.r - self.l)
        self.h = max(1, self.b - self.t)
        self.frames_dir = os.path.join(root, "frames")
        os.makedirs(self.frames_dir, exist_ok=True)
        self.events = []
        self.seq = 0
        self.t0 = time.time()
        self.lock = threading.Lock()
        self.last_hash_img = None
        self.last_img = None
        self.running = True
        self.pressed = {}          # btn -> 进行中的按下信息
        self.markers = 0
        self.acts = 0              # 有效操作次数，用于「一次都没点就自动结束」的保护
        self.last_act = time.time()  # 最后一次操作的时间戳，watchdog 靠它判断停手
        self.screen_frames = 0
        self.fps = fps
        self.video = video
        self.vw = None
        self.latest = None
        self.video_path = os.path.join(root, "screen.mp4")

    # ── 画面 ────────────────────────────────────────────────────────────────
    def rel(self, x, y):
        return (x - self.l) / self.w, (y - self.t) / self.h

    def add_frame(self, img, kind, extra, arrow=None):
        self.seq += 1
        name = f"{self.seq:04d}_{kind}.png"
        cv2.imwrite(os.path.join(self.frames_dir, name), img)
        ev = {"i": self.seq, "t": round(time.time() - self.t0, 2), "kind": kind,
              "img": f"frames/{name}", "w": img.shape[1], "h": img.shape[0]}
        ev.update(extra)
        if arrow:
            ev["arrow"] = arrow
        with self.lock:
            self.events.append(ev)
        self.last_img = img
        return ev

    def changed(self, img):
        if self.last_hash_img is None or self.last_hash_img.shape != img.shape:
            return True
        d = float(np.abs(img.astype("int16") - self.last_hash_img.astype("int16")).mean())
        return d > 2.0

    # ── 录视频 ──────────────────────────────────────────────────────────────
    def video_loop(self):
        if not self.video:
            return
        interval = 1.0 / self.fps
        while self.running:
            t0 = time.time()
            try:
                img = grab(self.rect)
            except Exception:
                time.sleep(0.5)
                continue
            self.latest = img
            if self.vw is None:
                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                self.vw = cv2.VideoWriter(self.video_path, fourcc, self.fps,
                                          (img.shape[1], img.shape[0]))
            if self.vw.isOpened():
                self.vw.write(img)
            dt = interval - (time.time() - t0)
            if dt > 0:
                time.sleep(dt)

    # ── 事件 ────────────────────────────────────────────────────────────────
    def dump(self, win):
        hwnd, title, exe, _ = win
        with self.lock:
            evs = list(self.events)
        meta = {"start": datetime.fromtimestamp(self.t0).strftime("%Y-%m-%d %H:%M:%S"),
                "duration_s": round(time.time() - self.t0, 1),
                "window": title, "exe": exe, "rect": [self.l, self.t, self.r, self.b],
                "size": [self.w, self.h], "markers": self.markers,
                "events": len(evs),
                "video": os.path.basename(self.video_path)
                if self.vw is not None and os.path.exists(self.video_path) else None}
        with open(os.path.join(self.root, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        with open(os.path.join(self.root, "events.jsonl"), "w", encoding="utf-8") as f:
            for ev in evs:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self.timeline(evs, meta)
        self.report(evs, meta)

    def timeline(self, evs, meta):
        # 抬起 0.35s 后那张「结果帧」并到对应动作那一行，不单列
        afters = {ev.get("of"): ev["img"] for ev in evs if ev["kind"] == "after"}
        lines = [f"# 示范时间线 {meta['start']}",
                 "",
                 f"窗口 {meta['window']}（{meta['exe']}）{self.w}x{self.h} "
                 f"@({self.l},{self.t})｜时长 {meta['duration_s']}s｜{len(evs)} 个节点"
                 + (f"｜视频 `{meta['video']}`" if meta["video"] else ""),
                 "",
                 "坐标都是**相对游戏窗口的归一化值**，和脚本里 click_relative 同一套。",
                 ""]
        prev_act = None
        for ev in evs:
            ts = f"{int(ev['t'] // 60):02d}:{ev['t'] % 60:04.1f}"
            k = ev["kind"]
            # 停顿分段：只拿真实操作（click/drag/key）判断，screen 采样帧不参与
            if k in ("click", "drag", "key") and prev_act is not None:
                gap = ev["t"] - prev_act
                if gap >= SEG_GAP:
                    lines += ["", f"── 停顿 {gap:.1f}s ──", ""]
            if k in ("click", "drag", "key"):
                prev_act = ev["t"]
            if k == "marker":
                lines += ["", f"## [{ts}] ── 标记 {ev.get('mark')} ── `{ev['img']}`", ""]
            elif k in ("click", "down"):
                if k == "down":
                    continue          # 只列 click/drag，down 已合进同一条
                after = afters.get(ev["i"])
                lines.append(f"- [{ts}] 点击 {ev['btn']} ({ev['rx']:.3f},{ev['ry']:.3f}) "
                             f"px({ev['x']},{ev['y']})　按下帧 `{ev['img']}`"
                             + (f" → 结果帧 `{after}`" if after else ""))
            elif k == "drag":
                path = ev.get("path") or []
                step = max(1, len(path) // 6)
                sample = path[::step][:7]
                after = afters.get(ev["i"])
                lines.append(
                    f"- [{ts}] **拖拽** {ev['btn']} ({ev['rx']:.3f},{ev['ry']:.3f}) → "
                    f"({ev['ux']:.3f},{ev['uy']:.3f})　历时 {ev.get('dur', 0):.2f}s　"
                    f"轨迹 {len(path)} 点　按下帧 `{ev['img']}`"
                    + (f" → 结果帧 `{after}`" if after else ""))
                lines.append("    - 途经 " + " ".join(f"({a:.3f},{b:.3f})" for a, b in sample))
            elif k == "after":
                continue          # 已并到动作行
            elif k == "key":
                lines.append(f"- [{ts}] 按键 `{ev.get('key')}`")
            elif k == "screen":
                lines.append(f"- [{ts}] 画面变化（无操作）`{ev['img']}`")
        text = "\n".join(lines) + "\n"
        with open(os.path.join(self.root, "timeline.md"), "w", encoding="utf-8") as f:
            f.write(text)
        return text

    def report(self, evs, meta):
        """缩略图墙：点击点红圈、拖拽画红箭头，按 marker 分节。"""
        thumbs = os.path.join(self.root, "thumbs")
        os.makedirs(thumbs, exist_ok=True)
        rows, section = [], 0
        for ev in evs:
            full = os.path.join(self.root, ev["img"])
            img = cv2.imread(full)
            if img is None:
                continue
            if ev["kind"] == "marker":
                section += 1
                rows.append(f"<tr class=mark><td colspan=4>★ 标记 {ev.get('mark')} · "
                            f"{ev['t']}s</td></tr>")
            thumb = img.copy()
            hh, ww = thumb.shape[:2]
            if ev["kind"] == "click":
                c = (int(ev["rx"] * ww), int(ev["ry"] * hh))
                cv2.circle(thumb, c, 26, (0, 0, 255), 4)
                cv2.circle(thumb, c, 4, (0, 0, 255), -1)
            elif ev["kind"] == "drag" or (ev["kind"] == "after" and ev.get("arrow")):
                a = (int(ev["rx"] * ww), int(ev["ry"] * hh))
                b = (int(ev["ux"] * ww), int(ev["uy"] * hh))
                cv2.arrowedLine(thumb, a, b, (0, 0, 255), 5, tipLength=0.06)
                cv2.circle(thumb, a, 10, (0, 255, 255), 4)
            scale = 360 / ww
            thumb = cv2.resize(thumb, (360, int(hh * scale)))
            tname = os.path.basename(ev["img"]).replace(".png", ".jpg")
            cv2.imwrite(os.path.join(thumbs, tname), thumb, [cv2.IMWRITE_JPEG_QUALITY, 82])

            k = ev["kind"]
            if k == "click":
                detail = f"{ev['btn']} @ ({ev['x']},{ev['y']}) = 相对 ({ev['rx']:.3f},{ev['ry']:.3f})"
            elif k == "drag":
                detail = (f"{ev['btn']} 拖拽 ({ev['rx']:.3f},{ev['ry']:.3f}) → "
                          f"({ev['ux']:.3f},{ev['uy']:.3f})<br>历时 {ev.get('dur', 0):.2f}s，"
                          f"轨迹 {len(ev.get('path') or [])} 点")
            elif k == "key":
                detail = f"按键 {ev.get('key')}"
            elif k == "after":
                detail = f"操作后结果（第 {ev.get('of')} 步）"
            else:
                detail = "画面变化（无操作）"
            anchor = "marker" if k == "marker" else ""
            rows.append(f"""<tr id="{anchor}{ev['i']}"><td class=n>{ev['i']}</td>
<td class=t>{ev['t']}s</td>
<td><a href="{ev['img']}" target=_blank><img src="thumbs/{tname}" loading=lazy></a></td>
<td class=d><b>{k}</b><br>{detail}</td></tr>""")

        html = f"""<!doctype html><meta charset=utf-8><title>示范录制 {os.path.basename(self.root)}</title>
<style>
body{{font:14px/1.6 -apple-system,Segoe UI,Microsoft YaHei;margin:0;background:#111;color:#eee}}
header{{position:sticky;top:0;background:#1b1b1b;padding:14px 20px;border-bottom:1px solid #333;z-index:9}}
h1{{font-size:16px;margin:0 0 4px}} .meta{{color:#999;font-size:12px}}
table{{border-collapse:collapse;width:100%}} td{{border-bottom:1px solid #262626;padding:10px 12px;vertical-align:top}}
td.n{{color:#666;width:44px;text-align:right}} td.t{{color:#888;width:64px;font-variant-numeric:tabular-nums}}
td.d{{color:#bbb;width:340px;font-size:13px}} img{{border-radius:6px;display:block;border:1px solid #333}}
tr.mark td{{background:#1f2a1f;color:#8fd18f;font-weight:600;letter-spacing:.5px}}
a{{color:#4ea1ff;text-decoration:none}}
</style>
<header><h1>示范录制 · {len(evs)} 个节点 / {meta['markers']} 个标记</h1>
<div class=meta>{meta['window']}（{meta['exe']}）窗口 {self.w}x{self.h} · 录制于 {meta['start']} ·
点击=红圈，拖拽=红箭头（起点黄圈）</div></header>
<table>{''.join(rows)}</table>"""
        with open(os.path.join(self.root, "report.html"), "w", encoding="utf-8") as f:
            f.write(html)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="只截一张图验证")
    ap.add_argument("--interval", type=float, default=1.5, help="无操作时的画面变化采样间隔秒")
    ap.add_argument("--fps", type=float, default=6.0, help="mp4 帧率")
    ap.add_argument("--no-video", action="store_true", help="不录 mp4")
    ap.add_argument("--full", action="store_true", help="抓不到游戏窗口时改录整个主屏")
    ap.add_argument("--auto-stop", type=float, default=25.0,
                    help="最后一次操作后多少秒自动收尾（默认 25，手机远程不用按键盘）")
    ap.add_argument("--max", type=float, default=300.0, help="总录制时长上限秒，默认 300")
    ap.add_argument("--max-screen", type=int, default=40,
                    help="无操作画面变化的最多保留张数，超了就不再存（防止噪声刷屏）")
    args = ap.parse_args()

    win = find_game_window()
    if not win:
        if not args.full:
            print("[X] 没找到游戏窗口，请先启动《篮球少女：飞跃巅峰》"
                  "（或加 --full 录整个主屏）")
            return 1
        sw, sh = ImageGrab.grab().size
        win = (None, "整个主屏", "-", (0, 0, sw, sh))
        print("[!] 没抓到游戏窗口 → 按 --full 录主屏，坐标相对整屏")
    hwnd, title, exe, wrect = win
    try:
        rect = client_rect(hwnd)
    except Exception:
        rect = wrect
    l, t, r, b = rect
    print(f"[+] 窗口: {title} / {exe}  画面区域 {l},{t} 尺寸 {r-l}x{b-t}")

    img = grab(rect)
    if args.test:
        os.makedirs("demo", exist_ok=True)
        cv2.imwrite("demo/_test.png", img)
        fg = "前台" if (hwnd is None or is_foreground(hwnd)) else "不在前台（可能抓到别的窗口）"
        print(f"[+] 截图成功 {img.shape[1]}x{img.shape[0]} 窗口{fg} -> demo/_test.png")
        return 0

    stamp = datetime.now().strftime("%m%d_%H%M%S")
    root = os.path.join("demo", f"session_{stamp}")
    s = Session(root, rect, fps=args.fps, video=not args.no_video)

    from pynput import keyboard, mouse

    def on_move(x, y):
        """按下期间的移动 = 拖拽轨迹；没按下时不记（悬停全是噪声）。

        注意 pynput 的约定：回调返回 False 会**停掉整个监听**，所以每条分支都要 return True。
        """
        if not s.running or not s.pressed:
            return True
        btn, p = next(iter(s.pressed.items()))
        rx, ry = s.rel(x, y)
        lx, ly = p["path"][-1] if p["path"] else (p["rx"], p["ry"])
        now = time.time()
        if abs(rx - lx) + abs(ry - ly) >= 0.004 or now - p["last"] > 0.15:
            p["path"].append((round(rx, 4), round(ry, 4)))
            p["last"] = now
        return True

    def on_click(x, y, button, pressed):
        if not s.running:
            return True
        if hwnd is not None and not is_foreground(hwnd):
            return True
        btn = str(button).replace("Button.", "")
        rx, ry = s.rel(x, y)
        try:
            img = grab(rect)
        except Exception as e:
            print("[!] 抓图失败", e)
            return True
        if pressed:
            ev = s.add_frame(img, "down", {
                "x": x, "y": y, "rx": round(rx, 4), "ry": round(ry, 4), "btn": btn})
            s.pressed[btn] = {"rx": rx, "ry": ry, "x": x, "y": y, "t": time.time(),
                              "path": [(round(rx, 4), round(ry, 4))],
                              "last": time.time(), "ev": ev}
            s.acts += 1
            s.last_act = time.time()
            print(f"  ↓ [{ev['i']:03d}] 按下 {btn} ({rx:.3f},{ry:.3f})")
            return True

        p = s.pressed.pop(btn, None)
        if not p:
            return True
        dur = time.time() - p["t"]
        moved = max((abs(a - p["rx"]) + abs(bq - p["ry"]) for a, bq in p["path"]), default=0)
        # 按下时那张图留着当「操作前」，抬起后再抓一张当「结果」
        # 拖动列表滑动往往「慢而短」，所以位移阈值放宽到 0.012（约 23px@1920），
        # 并要求按下时长 > 0.3s，避免把普通点击的手指抖动误判成拖拽。
        kind = "drag" if (dur > 0.30 and moved > 0.012) else "click"
        s.last_act = time.time()
        extra = {"x": p["x"], "y": p["y"], "rx": round(p["rx"], 4), "ry": round(p["ry"], 4),
                 "ux": round(rx, 4), "uy": round(ry, 4), "btn": btn,
                 "dur": round(dur, 2), "path": p["path"]}
        ev = p["ev"]
        ev.update({k: v for k, v in extra.items()})
        ev["kind"] = kind
        with s.lock:
            ev["t"] = round(time.time() - s.t0, 2)
        print(f"  ↑ [{ev['i']:03d}] {kind} {btn} → ({rx:.3f},{ry:.3f}) "
              f"{dur:.2f}s 轨迹 {len(p['path'])} 点")

        def after():
            time.sleep(AFTER_DELAY)
            try:
                im2 = grab(rect)
            except Exception:
                return
            s.add_frame(im2, "after", {"of": ev["i"], "rx": ev["rx"], "ry": ev["ry"],
                                       "ux": extra["ux"], "uy": extra["uy"],
                                       "arrow": kind == "drag"})
        threading.Thread(target=after, daemon=True).start()
        return True

    def on_key(key):
        if not s.running:
            return False
        name = getattr(key, "name", None) or str(key).replace("'", "")
        if name == STOP_KEY:
            s.running = False
            return False
        if name == MARK_KEY:
            s.markers += 1
            try:
                ev = s.add_frame(grab(rect), "marker", {"mark": s.markers})
                print(f"\n  ★ 标记 {s.markers} @{ev['t']}s\n")
            except Exception:
                pass
            return True
        return True

    def sampler():
        while s.running:
            time.sleep(args.interval)
            if not s.running or s.pressed:
                continue
            if hwnd is not None and not is_foreground(hwnd):
                continue
            if s.screen_frames >= args.max_screen:
                continue          # 噪声上限到了就只录视频，不再存帧
            try:
                img = grab(rect)
            except Exception:
                continue
            if s.changed(img):
                s.add_frame(img, "screen", {})
                s.screen_frames += 1
                s.last_hash_img = img

    def watchdog():
        """手机远程按不了键盘，靠「停手」自动收尾。"""
        while s.running:
            time.sleep(0.5)
            now = time.time()
            if s.acts > 0 and now - s.last_act > args.auto_stop:
                print(f"\n[·] 已经 {args.auto_stop:.0f}s 没有操作 → 自动收尾")
                s.running = False
                return
            if now - s.t0 > args.max:
                print(f"\n[·] 到达最长录制 {args.max:.0f}s → 自动收尾")
                s.running = False
                return

    if not args.no_video:
        threading.Thread(target=s.video_loop, daemon=True).start()

    print("\n>>> 开始录制，请直接在游戏里操作（不用按键）。")
    print(f">>> 停手 {args.auto_stop:.0f}s 自动结束；总时长上限 {args.max:.0f}s。")
    print(">>> 段落自动切分：相邻操作间隔 ≥3.5s 就算新的一段。\n")
    threading.Thread(target=sampler, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    ml = mouse.Listener(on_click=on_click, on_move=on_move)
    kl = keyboard.Listener(on_press=on_key)
    ml.start()
    kl.start()
    try:
        while s.running:          # 不 join 键盘监听，才不会只能靠 F12 退出
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n[·] Ctrl-C → 收尾")
    s.running = False
    try:
        ml.stop()
        kl.stop()
    except Exception:
        pass
    time.sleep(0.5)
    if s.vw is not None:
        s.vw.release()
    s.dump(win)
    print(f"\n[+] 录制结束，共 {len(s.events)} 个节点 / {s.markers} 个标记")
    print(f"[+] 报告: {os.path.abspath(os.path.join(root, 'report.html'))}")
    print(f"[+] 时间线: {os.path.abspath(os.path.join(root, 'timeline.md'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
