"""启动《篮球少女：飞跃巅峰》并一路点到「大厅」。

为什么不用 ok-script 自带的自动启动：游戏必须由 Ejoy 启动器（`D:\\LQSN\\launcher.exe`）
点「启动游戏」才会拉起 `k3_windows_ob.exe`，框架只认游戏进程本身，起不来。
所以这里做框架外的一小段引导：起启动器 → 点启动游戏 → 关掉登录后的公告/宣传弹窗 → 进大厅。
进大厅之后就没它的事了，后面交给 `tools/run_auto.py`（MCP）或 `run_auto.bat`。

用法：
    ../ok-bsg/.venv/Scripts/python.exe tools/start_game.py
    ../ok-bsg/.venv/Scripts/python.exe tools/start_game.py --dry-run     # 只观察不点
    ../ok-bsg/.venv/Scripts/python.exe tools/start_game.py --timeout 300

退出码：0 = 已经在大厅（或游戏已在大厅）；2 = 超时 / 失败。
"""

import argparse
import ctypes
import os
import re
import subprocess
import sys
import time

import cv2
import numpy as np
import psutil
import win32con
import win32gui
import win32process
from PIL import ImageGrab

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import devkit  # noqa: E402  窗口/抓屏/OCR 共用件（tools/devkit.py）

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = r"D:\LQSN\launcher.exe"
LAUNCHER_DIR = r"D:\LQSN"
GAME_EXE = "k3_windows_ob.exe"
LAUNCHER_PROC = "launcher.exe"

# 启动器窗口类（Ejoy SDK 固定）
LAUNCHER_CLASS = "ejoysdk_launcher_class"
GAME_CLASS = "UnityWndClass"

# 大厅锚点：底部导航 + 右上角图标。命中 ≥4 就认定在大厅（2026-11-23 实测画面）
HALL_WORDS = ("任务", "协会", "招募", "剧情", "卡册", "笔记", "器材", "角色", "助教", "图鉴", "成就")
HALL_MIN = 4
# 明确「点一下进游戏」的提示
ENTER_WORDS = ("点击进入", "点击屏幕进入", "点击开始")
# 弹窗上的关闭/确认类按钮
CLOSE_WORDS = ("关闭", "取消", "我知道了", "知道了", "以后再说", "跳过")
# 只该被"关掉"的浮层特征
POPUP_HINTS = ("公告", "礼包", "签到", "限时", "更新", "说明", "礼包码", "首充", "月卡")
# 左上角返回箭头（实测商城页 (0.05,0.09) 是 «，误入子页面时用它退回来）
BACK_WORDS = ("«", "<<", "‹", "返回")
# 启动阶段的静态页：等它自己过，不要点
# 注意别把「适龄提示」写进来 —— 公告弹窗底部也有这四个字，会把公告页误判成忠告页
BOOT_HINTS = ("健康游戏忠告", "抵制不良游戏", "检查更新", "游戏初始化", "当前版本号")
# 弹窗右上角 × 的模板（深色粗体 X + 白描边，OCR 认不出来，只能靠模板）
X_TEMPLATE = os.path.join(ROOT, "assets", "misc", "x_close.png")
X_MIN_SCORE = 0.80


# ────────────────────────── 日志 ──────────────────────────
class Log:
    def __init__(self, path=None):
        self.path = path
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self.fh = open(path, "a", encoding="utf-8")
        else:
            self.fh = None

    def __call__(self, msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        if self.fh:
            self.fh.write(line + "\n")
            self.fh.flush()


# ────────────────────────── 窗口 ──────────────────────────
def _proc_name(pid):
    try:
        return psutil.Process(pid).name().lower()
    except Exception:
        return ""


def find_windows(proc_kw, class_kw=None):
    """返回 [(hwnd, client_rect, title)]，只要可见窗口。"""
    out = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        if win32gui.IsIconic(hwnd):
            return
        cls = win32gui.GetClassName(hwnd)
        if class_kw and class_kw.lower() not in cls.lower():
            return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if proc_kw.lower() not in _proc_name(pid):
            return
        out.append((hwnd, client_rect(hwnd), win32gui.GetWindowText(hwnd)))

    win32gui.EnumWindows(cb, None)
    return out


def client_rect(hwnd):
    """客户区在屏幕上的 (l, t, r, b)，不含标题栏/边框。"""
    l, t = win32gui.ClientToScreen(hwnd, (0, 0))
    w, h = win32gui.GetClientRect(hwnd)[2:]
    return l, t, l + w, t + h


def proc_running(name):
    for p in psutil.process_iter(["name"]):
        if (p.info["name"] or "").lower() == name.lower():
            return p
    return None


def bring_front(hwnd):
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        # 前台锁：按一下 alt 再试（常见规避手法）
        ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)
        ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
    time.sleep(0.3)


# ────────────────────────── 截图 / OCR ──────────────────────────
class Vision:
    def __init__(self):
        # OCR 引擎和参数统一走 tools/devkit.py（必须 use_npu + use_openvino，
        # 原来这里只传 openvino，和项目实跑用的不是同一个后端）
        self.ocr = devkit.engine()
        self.x_tpl = cv2.imread(X_TEMPLATE) if os.path.exists(X_TEMPLATE) else None

    def grab(self, rect):
        img = ImageGrab.grab(bbox=rect, all_screens=True)
        frame = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        return frame

    def find_x(self, frame):
        """在右上区域找弹窗的 × 按钮，返回 (rx, ry, score) 或 None。

        OCR 认不出这个图形，所以用模板匹配。多尺度是为了兼容窗口被缩放的情况。
        """
        if self.x_tpl is None:
            return None
        h, w = frame.shape[:2]
        region = frame[: int(h * 0.4), int(w * 0.65):]
        best = None
        for s in (0.85, 1.0, 1.15):
            tpl = cv2.resize(self.x_tpl, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
            if tpl.shape[0] >= region.shape[0] or tpl.shape[1] >= region.shape[1]:
                continue
            res = cv2.matchTemplate(region, tpl, cv2.TM_CCOEFF_NORMED)
            _, mx, _, mxl = cv2.minMaxLoc(res)
            if best is None or mx > best[2]:
                cx = mxl[0] + tpl.shape[1] / 2 + int(w * 0.65)
                cy = mxl[1] + tpl.shape[0] / 2
                best = (cx / w, cy / h, float(mx))
        if best and best[2] >= X_MIN_SCORE:
            return best
        return None

    def items(self, frame):
        """OCR 结果 → 带像素框和相对坐标的行（解析逻辑在 devkit.ocr_rows 里只写一份）。"""
        h, w = frame.shape[:2]
        return [
            {"t": t, "x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1,
             "cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2,
             "rx": (x1 + x2) / 2 / w, "ry": (y1 + y2) / 2 / h}
            for x1, y1, x2, y2, t, _conf in devkit.ocr_rows(frame)
        ]


def find(items, words, near=None, radius=None):
    """按文字找元素；words 可以是精确词或多词包含。near=(rx,ry) 时只找附近的。"""
    for word in words:
        for it in items:
            if word in it["t"]:
                if near is not None and radius is not None:
                    if abs(it["rx"] - near[0]) > radius[0] or abs(it["ry"] - near[1]) > radius[1]:
                        continue
                return it
    return None


# ────────────────────────── 点击 ──────────────────────────
class Clicker:
    def __init__(self, dry=False):
        self.dry = dry

    def at(self, rect, rx, ry, why="", log=None):
        l, t, r, b = rect
        x, y = int(l + (r - l) * rx), int(t + (b - t) * ry)
        if log:
            log(f"    点击 ({x},{y}) 相对({rx:.2f},{ry:.2f}) {why}")
        if self.dry:
            return
        ctypes.windll.user32.SetCursorPos(x, y)
        time.sleep(0.05)
        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # left down
        time.sleep(0.06)
        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # left up
        time.sleep(0.05)

    def item(self, rect, it, why="", log=None):
        self.at(rect, it["rx"], it["ry"], f"{why}「{it['t']}」", log=log)

    def key(self, vk, log=None, why=""):
        if log:
            log(f"    按键 {why}")
        if self.dry:
            return
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.05)
        ctypes.windll.user32.keybd_event(vk, 0, 2, 0)


# ────────────────────────── 主流程 ──────────────────────────
def ensure_game_process(vis, clk, log, timeout=90):
    """确保 k3_windows_ob.exe 在跑；不在就从 launcher 点「启动游戏」。"""
    if proc_running(GAME_EXE):
        log(f"游戏进程已在运行：{GAME_EXE}")
        return True

    if not proc_running(LAUNCHER_PROC):
        if not os.path.exists(LAUNCHER):
            log(f"!! 找不到启动器 {LAUNCHER}")
            return False
        log(f"启动启动器：{LAUNCHER}")
        subprocess.Popen([LAUNCHER], cwd=LAUNCHER_DIR)
        time.sleep(4)
    else:
        log("启动器已在运行，直接复用")

    # 等启动器窗口
    deadline = time.time() + 40
    win = None
    while time.time() < deadline:
        ws = find_windows("launcher", LAUNCHER_CLASS)
        if ws:
            win = ws[0]
            break
        time.sleep(1)
    if not win:
        log("!! 等不到启动器窗口")
        return False

    hwnd, rect, title = win
    log(f"启动器窗口：{title!r} client={rect}")
    bring_front(hwnd)

    # 点「启动游戏」——启动器已登录时直接就是这张广告页
    enter_words = ("启动游戏", "进入游戏", "开始游戏", "开始", "进入")
    for attempt in range(6):
        frame = vis.grab(rect)
        items = vis.items(frame)
        log(f"  启动器第 {attempt + 1} 次识别：{len(items)} 段  {[i['t'] for i in items][:8]}")
        target = find(items, enter_words)
        if not target:
            # 可能是登录页：优先点显眼的登录按钮
            target = find(items, ("登录", "登 录", "扫码登录", "账号登录", "一键登录"))
        if target:
            bring_front(hwnd)
            clk.item(rect, target, "启动器→", log=log)
        else:
            # 认不出就点右下角大片按钮区（Ejoy 启动器固定右下）
            clk.at(rect, 0.83, 0.88, "启动器兜底右下角按钮", log=log)

        # 等游戏进程出现
        t0 = time.time()
        while time.time() - t0 < 25:
            if proc_running(GAME_EXE):
                log(f"游戏进程已拉起（启动器交互后 {time.time() - t0:.0f}s）")
                return True
            time.sleep(1)
        log("  点了之后游戏进程还没出现，重试")

    return proc_running(GAME_EXE) is not None


def wait_game_window(timeout=120, log=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        ws = find_windows("k3_windows", GAME_CLASS)
        if ws:
            return ws[0]
        time.sleep(1)
    return None


def hall_score(items):
    text = " ".join(i["t"] for i in items)
    return sum(1 for w in HALL_WORDS if w in text)


def dismiss_popup(vis, clk, hwnd, rect, frame, items, log, shot_dir, step):
    """处理一屏弹窗/引导页。返回 True 表示做了动作。

    铁律（README 三条）：认不出就停，绝不「兜底乱点」。
    2026-11-23 翻车记录：大厅右上角 (0.93,0.11) 是「商城」图标，用固定坐标兜底关浮层
    会直接点进商城。所以这里只在**认出具体按钮**时才点，认不出就按 ESC（可撤销）。
    """
    text = " ".join(i["t"] for i in items)

    # 0) 误入了子页面（商城/活动页）：左上角返回箭头先退出来
    back = [i for i in items if i["rx"] < 0.15 and i["ry"] < 0.15 and any(w == i["t"].strip() for w in BACK_WORDS)]
    if back:
        clk.item(rect, back[0], "子页面返回→", log=log)
        return True

    # 1) 「点击进入」——标题页
    t = find(items, ENTER_WORDS)
    if t:
        clk.item(rect, t, "标题页→", log=log)
        return True

    # 2) 显式的关闭/取消按钮
    t = find(items, CLOSE_WORDS)
    if t:
        clk.item(rect, t, "弹窗→", log=log)
        return True

    # 3) 弹窗右上角的 ×：先看 OCR 认没认出来，再用模板匹配（实测 OCR 认不出图形 ×）
    xs = [i for i in items if i["t"].strip() in ("×", "X", "x", "✕", "╳") and i["rx"] > 0.8 and i["ry"] < 0.25]
    if xs:
        it = sorted(xs, key=lambda i: i["ry"])[0]
        clk.item(rect, it, "右上角关闭→", log=log)
        return True
    hit = vis.find_x(frame)
    if hit:
        rx, ry, sc = hit
        log(f"    模板匹配到关闭按钮 ({rx:.3f},{ry:.3f}) score={sc:.2f}")
        clk.at(rect, rx, ry, "模板×→", log=log)
        return True

    # 4) 有浮层特征但认不出按钮：按 ESC（可撤销，绝不会点进别的页面）
    if any(k in text for k in POPUP_HINTS):
        clk.key(0x1B, log=log, why="ESC 关浮层")
        return True

    # 5) 「点击屏幕继续」类推进
    if "点击屏幕" in text or "点击继续" in text or "点击任意" in text:
        clk.at(rect, 0.5, 0.5, "推进", log=log)
        return True

    return False


def enter_hall(vis, clk, hwnd, rect, log, shot_dir, timeout=240, grace=60):
    deadline = time.time() + timeout
    grace_until = time.time() + grace
    step = 0
    unknown = 0
    while time.time() < deadline:
        step += 1
        bring_front(hwnd)
        frame = vis.grab(rect)
        items = vis.items(frame)
        text = " ".join(i["t"] for i in items)
        score = hall_score(items)
        log(f"[{step:02d}] 文字{len(items):>3}段 大厅词{score}  {[i['t'] for i in items][:10]}")

        if shot_dir:
            cv2.imwrite(os.path.join(shot_dir, f"step{step:02d}.png"), frame)

        # 大厅优先判定：命中就收工，一个按钮都不点（避免点到商城/活动图标）
        if score >= HALL_MIN:
            if "点击进入" in text:
                log("  大厅锚点齐但还在标题页，继续推进")
            else:
                log(f"→ 已在大厅（命中 {score} 个锚点），停止点击")
                return True

        # 加载/过场/健康忠告/logo：等它自己走，不算「认不出」
        # 注意加上「文字少」这个前提：标题页底部也印着版本号，光看关键词会把标题页误判成加载页
        if len(items) < 3 or (len(items) <= 10 and any(k in text for k in BOOT_HINTS)):
            log("  加载/过场/忠告页，等待")
            time.sleep(2.0)
            continue

        if dismiss_popup(vis, clk, hwnd, rect, frame, items, log, shot_dir, step):
            unknown = 0
        else:
            if time.time() < grace_until:
                log("  启动宽限期内认不出，只等不点")
                time.sleep(2.0)
                continue
            unknown += 1
            log(f"  认不出页面（第 {unknown} 次），不点击。文字：{[i['t'] for i in items][:12]}")
            if unknown >= 3:
                log("!! 连续认不出，按铁律停机，不乱点")
                return False
        time.sleep(1.6)

    log("!! 超时，还没进到大厅")
    return False


def run_auto(log):
    """进大厅之后把主流程接上去（等价于 run_auto.bat）。

    走命令行 `-m ok run_task`，不需要 MCP 服务常驻。
    """
    py = os.path.join(os.path.dirname(ROOT), "ok-bsg", ".venv", "Scripts", "python.exe")
    if not os.path.exists(py):
        py = sys.executable
    cmd = [py, "-m", "ok", "run_task", "-c", "src.config:config", "AutoTask"]
    log(f"接着跑主流程：{' '.join(cmd)}")
    p = subprocess.run(cmd, cwd=ROOT)
    log(f"主流程结束，exit={p.returncode}")
    return p.returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只观察，不点击")
    ap.add_argument("--timeout", type=int, default=240, help="进大厅阶段的超时秒数")
    ap.add_argument("--no-shot", action="store_true", help="不保存过程截图")
    ap.add_argument("--auto", action="store_true", help="进大厅后直接跑 AutoTask 开始训练")
    args = ap.parse_args()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    shot_dir = None if args.no_shot else os.path.join(ROOT, "logs", "start_game", stamp)
    if shot_dir:
        os.makedirs(shot_dir, exist_ok=True)
    log = Log(os.path.join(ROOT, "logs", "start_game", "start_game.log"))
    log(f"===== start_game 开始 dry={args.dry_run} =====")

    vis = Vision()
    clk = Clicker(dry=args.dry_run)

    if not ensure_game_process(vis, clk, log):
        log("!! 拉不起游戏进程，退出")
        return 2

    win = wait_game_window(120, log=log)
    if not win:
        log("!! 等不到游戏窗口")
        return 2
    hwnd, rect, title = win
    log(f"游戏窗口：{title!r} client={rect}")

    ok = enter_hall(vis, clk, hwnd, rect, log, shot_dir, timeout=args.timeout)
    log(f"===== 引导结束：{'已在大厅' if ok else '失败'} =====")
    if ok and args.auto:
        return run_auto(log)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
