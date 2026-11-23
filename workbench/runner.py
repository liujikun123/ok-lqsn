"""后台进程管理：把 ok / start_game 当成子进程跑，输出实时转到界面。

只依赖 PySide6 的 QProcess，不另开线程 —— 免得 GUI 卡死或被强杀时留下孤儿进程。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

import yaml
from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal


# ---------------------------------------------------------------- 路径与配置

def project_root() -> str:
    """本文件在 <项目>/workbench/runner.py，往上两级就是项目根。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_workbench_cfg(root: str | None = None) -> dict:
    root = root or project_root()
    path = os.path.join(root, "config", "workbench.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def list_rule_files(root: str | None = None) -> list[str]:
    """config 目录下的「规则文件」（带 tuning 段的 yaml）。

    不能把 workbench.yaml / states.yaml 也列进来 —— 那两份不是玩法规则，
    选错会让人以为改了规则其实没生效。
    """
    root = root or project_root()
    d = os.path.join(root, "config")
    if not os.path.isdir(d):
        return []
    out = []
    for n in sorted(os.listdir(d)):
        if not n.endswith((".yaml", ".yml")):
            continue
        try:
            with open(os.path.join(d, n), encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            continue
        if isinstance(data, dict) and ("tuning" in data or "rules" in data):
            out.append(n)
    return out


def summarize_rule(path: str) -> list[tuple[str, str]]:
    """把规则文件压成几行关键信息，给界面上的摘要表用。

    TODO: 规则项变多以后，这里应该改成「按角色分组」展示，
          并且支持在界面上直接改值再回写 yaml。
    """
    try:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        return [("读取失败", str(e))]

    t = cfg.get("tuning") or {}
    core = t.get("core_score") or {}
    stock = t.get("stock") or {}
    loop = t.get("loop") or {}
    slots = t.get("pick_slots") or {}
    trip = t.get("trip") or {}
    shop = t.get("shop") or {}

    rows = [
        ("主训角色", str(core.get("main_char") or "-")),
        ("循环轮次", f"{loop.get('max_rounds', '-')} 轮"),
        ("空槽检查", "开启" if slots.get("check", True) else "关闭"),
        ("空槽处理", str(slots.get("on_empty") or "暂停提示")),
        ("炒股模式", str(stock.get("mode") or "-")),
        ("出行优先", " > ".join(trip.get("priority") or []) or "-"),
        ("出行排除", "、".join(trip.get("exclude") or []) or "-"),
        ("加强训练", str((t.get("enhance") or {}).get("strategy") or "-")),
        ("商店自动买", "是" if shop.get("auto_buy") else "否"),
    ]
    routes = cfg.get("routes") or {}
    if routes:
        filled = [k for k, v in routes.items() if v]
        rows.append(("角色分支", ("已配置 " + "、".join(filled)) if filled else "全部为空（走默认）"))
    return rows


# ---------------------------------------------------------------- 运行记录

def read_run_log(root: str | None = None, path: str | None = None) -> list[dict]:
    """读 logs/runs.jsonl，按 run_id 聚合成一条条运行记录（新的在前）。

    每条返回：
        id, start, end, status, rounds_done, rounds, trains,
        stamina_start/end, focus_start/end, drinks,
        items: [{round, skill, score, fail, mates, equips, stamina, focus, at}, ...]
    坏行跳过（文件被强停写坏的半行不能让整个面板打不开）。
    """
    import json
    root = root or project_root()
    path = path or os.path.join(root, "logs", "runs.jsonl")
    runs: dict[str, dict] = {}
    order: list[str] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    rid = ev.get("run")
                    if not rid:
                        continue
                except Exception:
                    continue
                if rid not in runs:
                    runs[rid] = {"id": rid, "start": "", "end": "", "status": "进行中/中断",
                                 "rounds_done": 0, "rounds": 0, "trains": 0,
                                 "stamina_start": None, "stamina_end": None,
                                 "focus_start": None, "focus_end": None, "drinks": 0,
                                 "items": []}
                    order.append(rid)
                r = runs[rid]
                kind = ev.get("event")
                if kind == "start":
                    r["start"] = ev.get("at") or ""
                    r["rounds"] = int(ev.get("rounds") or 0)
                elif kind == "resume":
                    r["resumed"] = True
                elif kind == "train":
                    r["items"].append({
                        "round": ev.get("round"), "skill": ev.get("skill") or "-",
                        "score": ev.get("score"), "fail": ev.get("fail"),
                        "mates": ev.get("mates"), "equips": ev.get("equips"),
                        "stamina": ev.get("stamina"), "focus": ev.get("focus"),
                        "at": ev.get("at") or "",
                    })
                elif kind == "end":
                    r["end"] = ev.get("at") or ""
                    r["status"] = ev.get("status") or "正常"
                    r["rounds_done"] = int(ev.get("rounds_done") or 0)
                    r["rounds"] = int(ev.get("rounds") or r["rounds"])
                    r["trains"] = int(ev.get("trains") or len(r["items"]))
                    r["stamina_start"] = ev.get("stamina_start")
                    r["stamina_end"] = ev.get("stamina_end")
                    r["focus_start"] = ev.get("focus_start")
                    r["focus_end"] = ev.get("focus_end")
                    r["drinks"] = int(ev.get("drinks") or 0)
    except FileNotFoundError:
        return []
    except Exception:
        pass
    return [runs[rid] for rid in reversed(order)]


# ---------------------------------------------------------------- 环境自检

@dataclass
class CheckItem:
    name: str
    ok: bool
    detail: str


def preflight(root: str | None = None) -> list[CheckItem]:
    """开机自检。任何一项失败都能在界面上看到具体原因，不用猜。"""
    root = root or project_root()
    cfg = load_workbench_cfg(root)
    game = cfg.get("game") or {}
    out: list[CheckItem] = []

    out.append(CheckItem("Python", True, f"{sys.version.split()[0]}  ({sys.executable})"))

    try:
        import PySide6
        out.append(CheckItem("界面库 PySide6", True, PySide6.__version__))
    except Exception as e:
        out.append(CheckItem("界面库 PySide6", False, str(e)))

    try:
        import ok  # noqa: F401
        ver = getattr(ok, "__version__", "")
        out.append(CheckItem("ok 框架", True, ver or "已安装"))
    except Exception as e:
        out.append(CheckItem("ok 框架", False, f"没装：{e}"))

    for key, label in (("exe", "游戏本体"), ("launcher", "游戏启动器")):
        p = game.get(key) or ""
        if not p:
            out.append(CheckItem(label, False, "workbench.yaml 里没配路径"))
        elif os.path.exists(p):
            out.append(CheckItem(label, True, p))
        else:
            out.append(CheckItem(label, False, f"路径不存在：{p}"))

    rule = os.path.join(root, game.get("default_rule") or "config/strategy.yaml")
    out.append(CheckItem("规则文件", os.path.exists(rule),
                         os.path.relpath(rule, root) if os.path.exists(rule) else f"找不到 {rule}"))
    return out


def game_running(root: str | None = None) -> tuple[bool, str]:
    """游戏进程在不在跑。优先用 psutil，没有就退回 tasklist。"""
    cfg = load_workbench_cfg(root)
    name = ((cfg.get("game") or {}).get("process") or "k3_windows_ob.exe").lower()
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            n = (p.info.get("name") or "").lower()
            if n == name:
                return True, f"PID {p.pid}"
    except Exception:
        pass
        # 没有 psutil：用 tasklist 兜底
    try:
        import subprocess
        r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}"],
                           capture_output=True, text=True, timeout=8,
                           errors="ignore")
        if name in (r.stdout or "").lower():
            return True, "运行中"
    except Exception:
        pass
    return False, "未运行"


def game_window(root: str | None = None) -> tuple[bool, str]:
    """游戏窗口在不在（进程有但不一定有窗口，比如还在启动器阶段）。"""
    cfg = load_workbench_cfg(root)
    kw = ((cfg.get("game") or {}).get("window_keyword") or "k3_windows").lower()
    try:
        import psutil
        import win32gui
        import win32process
        hits = []

        def cb(h, _):
            if not win32gui.IsWindowVisible(h):
                return
            try:
                _, pid = win32process.GetWindowThreadProcessId(h)
                if kw in psutil.Process(pid).name().lower():
                    hits.append((h, win32gui.GetWindowText(h)))
            except Exception:
                return

        win32gui.EnumWindows(cb, None)
        if not hits:
            return False, "没找到窗口"
        r = win32gui.GetWindowRect(hits[0][0])
        return True, f"{r[2] - r[0]}x{r[3] - r[1]}"
    except Exception as e:
        return False, f"检测不可用：{e}"


# ---------------------------------------------------------------- 子进程

class Runner(QObject):
    """跑一条命令，输出按行发信号。同一时间只允许一条（避免两个脚本抢鼠标）。"""

    log = Signal(str, str)        # (级别, 文本)  级别: info / warn / error / raw
    state = Signal(str)           # idle / running / stopping
    done = Signal(int, str)       # (退出码, 说明)

    def __init__(self, root: str | None = None, parent=None):
        super().__init__(parent)
        self.root = root or project_root()
        self.proc: QProcess | None = None
        self.label = ""

    # -- 状态
    def busy(self) -> bool:
        return self.proc is not None and self.proc.state() != QProcess.NotRunning

    # -- 启动
    def run(self, args: list[str], label: str, python: str | None = None) -> None:
        if self.busy():
            self.log.emit("warn", f"「{self.label}」还在跑，先等它结束或点停止")
            return
        py = python or sys.executable
        self.label = label
        self.proc = QProcess(self)
        self.proc.setWorkingDirectory(self.root)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")     # 不然中文日志会炸
        env.insert("PYTHONUNBUFFERED", "1")
        self.proc.setProcessEnvironment(env)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._on_out)
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_error)
        self.log.emit("info", f"▶ {label}：{py} {' '.join(args)}")
        self.state.emit("running")
        self.proc.start(py, args)

    def stop(self) -> None:
        if not self.busy():
            return
        self.state.emit("stopping")
        self.log.emit("warn", f"正在停止「{self.label}」…")
        # QProcess.kill 只杀自己那层，ok 有时会再起子进程，所以整棵进程树一起砍
        try:
            pid = int(self.proc.processId())
            import subprocess
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=8)
        except Exception:
            pass
        self.proc.kill()

    # -- 内部
    def _on_out(self) -> None:
        if not self.proc:
            return
        raw = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = line.rstrip()
            if not line:
                continue
            low = line.lower()
            if "error" in low or " traceback" in low or line.startswith("!!"):
                level = "error"
            elif "warning" in low or "warn" in low or "暂停" in line:
                level = "warn"
            elif "onnxocr - info" in low:
                level = "raw"        # OCR 引擎的碎碎念，界面上可以过滤
            else:
                level = "info"
            self.log.emit(level, line)

    def _on_finished(self, code: int, status) -> None:
        self.log.emit("info" if code == 0 else "error",
                      f"■ 「{self.label}」结束，退出码 {code}")
        self.proc = None
        self.state.emit("idle")
        self.done.emit(code, self.label)

    def _on_error(self, err) -> None:
        if err == QProcess.FailedToStart:
            self.log.emit("error", "子进程起不来，检查 Python 解释器路径")
            self.proc = None
            self.state.emit("idle")
