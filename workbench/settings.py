"""工作台的「可改配置」层：把界面上的控件和配置文件里的键一一对上。

为什么要这一层，而不是界面里直接 `yaml.safe_load` + `yaml.safe_dump`：

    `config/strategy.yaml` 里有大量注释，记着「为什么定成这个值」——
    比如「玩家 2026-11-23：失败可以赌一把，小于 30 都能去」。
    `safe_dump` 会把注释**全部抹掉**，等于一次点击销毁整份决策记录。

所以这里读用 yaml（保证类型正确），写走**逐行定点替换**：
只把 `key:` 后面那一段值换掉，行内注释、缩进、键顺序、别的段落一个字都不动。
找不到这个键时才在所属段落末尾插一行（并标注是工作台加的）。

另外「一个控件管两个键」的情况必须显式写出来，例如：
    · 循环轮数  → tuning.loop.max_rounds（轮数）+ AutoTask.json 跑完一轮就停（≤1 轮时为 true）
    · 允许喝药  → tuning.stamina.use_item + AutoTask.json 专注恢复（用道具 / 关闭）
    · 空位策略  → pick_slots.on_empty + pick_slots.auto_fill_first
这些「联动」写成 transform 名字，读和写走同一张表，保证界面上看到的 = 文件里存的。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import yaml

# ------------------------------------------------------------------ 值格式化


def fmt_yaml(v: Any) -> str:
    """把 Python 值写成 YAML 里的字面量（只覆盖本项目用到的几种）。"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return f"{v:g}" if isinstance(v, float) else str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(fmt_yaml(x) for x in v) + "]"
    s = str(v)
    # 需要引号的情况：空串、带前后空格、含 : # [ ] { } , 或以引号/特殊符号开头
    if s == "" or s != s.strip() or re.search(r"[:#\[\]{},]", s) or s[0] in "-?&*!|>'\"%@`":
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _split_comment(text: str) -> tuple[str, str]:
    """把 `1  # 循环轮数` 拆成 ('1', '  # 循环轮数')，引号里的 # 不算注释。"""
    quote = ""
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1].isspace()):
            j = i
            while j > 0 and text[j - 1] == " ":
                j -= 1
            return text[:j].rstrip(), text[j:]
    return text.rstrip(), ""


# ------------------------------------------------------------------ YAML 文档


@dataclass
class _KeyLine:
    idx: int          # 在 self.lines 里的下标
    indent: int
    key: str
    parent: int | None  # 父键所在行的下标；顶层为 None


_KEY_RE = re.compile(r"^(\s*)([^\s:#][^:#]*?)\s*:(.*)$")


class YamlDoc:
    """一份 yaml 文件：读走 yaml.safe_load，写只动目标那一行。"""

    def __init__(self, path: str):
        self.path = path
        self.lines: list[str] = []
        self.data: dict = {}
        self.reload()

    # -- 读
    def reload(self) -> None:
        with open(self.path, encoding="utf-8") as f:
            raw = f.read()
        self.raw = raw
        self.lines = raw.splitlines()
        self.data = yaml.safe_load(raw) or {}
        # 每行注释的**原始**列。写值时按这个列对齐，而不是按当前行 ——
        # 否则「true → false → true」这种来回改，空格会一次次被吃掉，
        # 保存出来的 diff 全是没意义的空白变化。
        self._orig_cols = {}
        for i, line in enumerate(self.lines):
            _, c = _split_comment(line)
            if c:
                # `#` 真正所在的列（c 前面还带着对齐用的空格）
                self._orig_cols[i] = len(line) - len(c) + (len(c) - len(c.lstrip()))
        self.dirty = False

    def get(self, path: str, default: Any = None) -> Any:
        cur: Any = self.data
        for k in path.split("."):
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    def has_path(self, path: str) -> bool:
        sentinel = object()
        return self.get(path, sentinel) is not sentinel

    # -- 定位
    def _scan(self) -> list[_KeyLine]:
        stack: list[tuple[int, int]] = []
        out: list[_KeyLine] = []
        for i, raw in enumerate(self.lines):
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            if raw.lstrip().startswith("-"):
                # 列表项（`- id: exhausted`）不参与键定位：本项目的规则表
                # 全是 tuning.* 下的映射，不会写进列表里。
                continue
            m = _KEY_RE.match(raw)
            if not m:
                continue
            ind = len(m.group(1))
            key = m.group(2).strip().strip("'\"")
            while stack and stack[-1][0] >= ind:
                stack.pop()
            parent = stack[-1][1] if stack else None
            out.append(_KeyLine(i, ind, key, parent))
            stack.append((ind, i))
        return out

    def find_line(self, path: str) -> _KeyLine | None:
        keys = path.split(".")
        nodes = self._scan()
        node: _KeyLine | None = None
        for depth, k in enumerate(keys):
            want_parent = None if depth == 0 else node.idx
            nxt = None
            for n in nodes:
                if n.key == k and n.parent == want_parent:
                    nxt = n
                    break
            if nxt is None:
                return None
            node = nxt
        return node

    def _block_end(self, node: _KeyLine) -> int:
        """该键这段（含子键）最后一行的下一行下标。"""
        end = node.idx + 1
        for i in range(node.idx + 1, len(self.lines)):
            raw = self.lines[i]
            if not raw.strip():
                end = i + 1
                continue
            ind = len(raw) - len(raw.lstrip())
            if ind <= node.indent:
                break
            end = i + 1
        return end

    # -- 写
    def _value_of(self, node: _KeyLine) -> str:
        """某行冒号后面的值（去掉行内注释）。"""
        raw = self.lines[node.idx]
        _, comment = _split_comment(raw)
        body = raw[: len(raw) - len(comment)] if comment else raw
        return body.split(":", 1)[1].strip()

    @staticmethod
    def _with_comment(body: str, raw: str, comment: str, orig_col: int | None = None) -> str:
        """把注释接回新内容后面，尽量保持原来的列对齐。

        原文件里注释是竖着对齐的（`true          # 玩家 2026-11-23：…`），
        直接拍一个空格上去，几十行注释就歪成锯齿，diff 里全是噪音。
        值变短就补空格对齐到原列；值变长（超过原列）就退回一个空格。
        """
        if not comment:
            return body
        col = orig_col
        if col is None:
            col = len(raw) - len(comment) + (len(comment) - len(comment.lstrip()))
        pad = max(1, col - len(body))
        return body + " " * pad + comment.lstrip()

    def _is_flow_map(self, node: _KeyLine) -> bool:
        v = self._value_of(node)
        return v.startswith("{") and v.endswith("}")

    def _flow_set(self, node: _KeyLine, key: str, text: str) -> bool:
        """在 `key: {a: 1, b: 2}` 这种行内映射里改一个值。

        本项目的 `shop.rules` 就是这种写法：一行写完 3~4 个字段，
        后面还跟着一段注释解释为什么这么定价。整行重排会把注释和
        字段顺序都打乱，所以这里只替换「目标字段的值」那一小段。
        """
        raw = self.lines[node.idx]
        _, comment = _split_comment(raw)
        body = raw[: len(raw) - len(comment)] if comment else raw
        head, _, rest = body.partition(":")
        inner_start = body.index("{") + 1
        inner_end = body.rindex("}")
        inner = body[inner_start:inner_end]

        # 逐个字段切（引号内的逗号不算），记下每段值的位置
        spans, depth, quote, start = [], 0, "", 0
        for i, ch in enumerate(inner):
            if quote:
                if ch == quote:
                    quote = ""
            elif ch in "\"'":
                quote = ch
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
            elif ch == "," and depth == 0:
                spans.append((start, i))
                start = i + 1
        spans.append((start, len(inner)))

        for a, b in spans:
            part = inner[a:b]
            if ":" not in part:
                continue
            k, _, v = part.partition(":")
            if k.strip().strip("'\"") != key:
                continue
            new_part = part[: len(k) + 1] + f" {text}"
            new_inner = inner[:a] + new_part + inner[b:]
            new_raw = self._with_comment(
                body[:inner_start] + new_inner + body[inner_end:], raw, comment,
                self._orig_cols.get(node.idx))
            if new_raw == raw:
                return False
            self.lines[node.idx] = new_raw
            self._refresh()
            return True

        # 映射里还没有这个字段 → 追加到末尾
        new_inner = inner.rstrip()
        if new_inner and not new_inner.endswith(","):
            new_inner += f", {key}: {text}"
        else:
            new_inner += f" {key}: {text}" if new_inner else f"{key}: {text}"
        new_raw = self._with_comment(
            body[:inner_start] + new_inner + body[inner_end:], raw, comment,
            self._orig_cols.get(node.idx))
        self.lines[node.idx] = new_raw
        self._refresh()
        return True

    def _refresh(self) -> None:
        self.data = yaml.safe_load("\n".join(self.lines)) or {}
        self.dirty = True

    def set(self, path: str, value: Any) -> bool:
        """把 path 的值改成 value。返回 True 表示文件内容真的变了。"""
        keys = path.split(".")
        text = fmt_yaml(value)

        node = self.find_line(path)
        if node is not None:
            raw = self.lines[node.idx]
            _, comment = _split_comment(raw)
            body = raw[: len(raw) - len(comment)] if comment else raw
            head, _, value_region = body.partition(":")
            # 值前面原有的空白照抄（有的行是 `key:   值` 手动对齐的）
            lead = value_region[: len(value_region) - len(value_region.lstrip())] or " "
            newline = self._with_comment(f"{head}:{lead}{text}", raw, comment,
                                         self._orig_cols.get(node.idx))
            if newline == raw:
                return False
            self.lines[node.idx] = newline
            self._refresh()
            return True

        # 目标不是独立一行：往下找「能定位到的最深祖先」
        parent_path, parent = "", None
        for i in range(len(keys) - 1, 0, -1):
            p = ".".join(keys[:i])
            n = self.find_line(p)
            if n is not None:
                parent_path, parent = p, n
                break
        rest = keys[len(parent_path.split(".")) if parent_path else 0:]

        if parent is not None and len(rest) == 1 and self._is_flow_map(parent):
            return self._flow_set(parent, rest[0], text)
        if parent_path and parent is None:
            raise KeyError(f"{os.path.basename(self.path)} 里没有「{parent_path}」，"
                           f"没法插入 {path}")
        if parent is not None and len(rest) > 1:
            raise KeyError(f"「{parent_path}」是行内映射，没法往里插多级路径 {path}")

        indent = (parent.indent + 2) if parent else 0
        at = self._block_end(parent) if parent else len(self.lines)
        self.lines.insert(at, f"{' ' * indent}{keys[-1]}: {text}  # 工作台新增")
        self._refresh()
        return True

    def save(self, backup_dir: str | None = None, keep: int = 20) -> str | None:
        """写回文件。先备份一份，返回备份路径（没备份则 None）。"""
        if not self.dirty:
            return None
        bak = None
        if backup_dir:
            os.makedirs(backup_dir, exist_ok=True)
            bak = os.path.join(
                backup_dir,
                f"{os.path.splitext(os.path.basename(self.path))[0]}_"
                f"{time.strftime('%Y%m%d_%H%M%S')}{os.path.splitext(self.path)[1]}")
            shutil.copy2(self.path, bak)
            olds = sorted(f for f in os.listdir(backup_dir)
                          if f.startswith(os.path.splitext(os.path.basename(self.path))[0] + "_"))
            for f in olds[:-keep]:
                try:
                    os.remove(os.path.join(backup_dir, f))
                except OSError:
                    pass
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(self.lines) + "\n")
        os.replace(tmp, self.path)
        self.reload()
        return bak


class JsonDoc:
    """configs/AutoTask.json —— 任务参数。json 没有注释，整份回写是安全的。"""

    def __init__(self, path: str):
        self.path = path
        self.reload()

    def reload(self) -> None:
        with open(self.path, encoding="utf-8") as f:
            self.data = json.load(f)
        self.dirty = False

    def get(self, path: str, default: Any = None) -> Any:
        cur: Any = self.data
        for k in path.split("."):
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    def has_path(self, path: str) -> bool:
        sentinel = object()
        return self.get(path, sentinel) is not sentinel

    def set(self, path: str, value: Any) -> bool:
        keys = path.split(".")
        cur = self.data
        for k in keys[:-1]:
            nxt = cur.get(k)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[k] = nxt
            cur = nxt
        if cur.get(keys[-1]) == value:
            return False
        cur[keys[-1]] = value
        self.dirty = True
        return True

    def save(self, backup_dir: str | None = None, keep: int = 20) -> str | None:
        if not self.dirty:
            return None
        bak = None
        if backup_dir:
            os.makedirs(backup_dir, exist_ok=True)
            bak = os.path.join(backup_dir, f"AutoTask_{time.strftime('%Y%m%d_%H%M%S')}.json")
            shutil.copy2(self.path, bak)
            olds = sorted(f for f in os.listdir(backup_dir) if f.startswith("AutoTask_"))
            for f in olds[:-keep]:
                try:
                    os.remove(os.path.join(backup_dir, f))
                except OSError:
                    pass
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=4)
            f.write("\n")
        os.replace(tmp, self.path)
        self.reload()
        return bak


# ------------------------------------------------------------------ 联动变换
#
# 一个控件管多个键时，写和读必须共用同一套语义，否则会出现「界面显示 3 轮、
# 文件里还是 1 轮」这种最难查的错。

WRITE: dict[str, Callable[[Any], Any]] = {
    "value": lambda v: v,
    "loop_stop_flag": lambda v: int(v) <= 1,          # 轮数 ≤1 → 跑完一轮就停 = true
    "stamina_mode": lambda v: "用道具" if v else "关闭",
    "slot_empty": lambda v: "继续" if v == "忽略" else "暂停提示",
    "slot_autofill": lambda v: v == "自动补满",
    "nonzero": lambda v: bool(v),
}

READ: dict[str, Callable[[Any], Any]] = {
    "value": lambda v: v,
    "loop_stop_flag": lambda v: v,
    "stamina_mode": lambda v: str(v) != "关闭",
    "slot_empty": lambda v: v,
    "slot_autofill": lambda v: v,
    "nonzero": lambda v: v,
}


def read_slot_policy(docs: dict[str, Any]) -> str:
    if docs["yaml"].get("tuning.pick_slots.on_empty") == "继续":
        return "忽略"
    if docs["yaml"].get("tuning.pick_slots.auto_fill_first"):
        return "自动补满"
    return "弹窗暂停"


@dataclass
class Target:
    doc: str            # "yaml" = strategy.yaml / "json" = configs/AutoTask.json
    path: str
    transform: str = "value"


@dataclass
class Setting:
    id: str
    label: str
    kind: str                     # switch / int / choice / weekdays
    targets: list[Target]
    default: Any = None
    help: str = ""
    minimum: int = 0
    maximum: int = 100
    step: int = 1
    unit: str = ""
    choices: list[str] = field(default_factory=list)
    reader: str = ""              # 复合项自定义读法

    def read(self, docs: dict[str, Any]) -> Any:
        if self.reader == "slot_policy":
            return read_slot_policy(docs)
        t = self.targets[0]
        raw = docs[t.doc].get(t.path, self.default)
        fn = READ.get(t.transform, READ["value"])
        try:
            return fn(raw)
        except Exception:
            return self.default

    def write(self, docs: dict[str, Any], value: Any) -> list[tuple[str, str, Any, Any]]:
        """把值写进文档，返回 [(doc, path, 旧值, 新值)] 供界面显示 diff。"""
        changes = []
        for t in self.targets:
            fn = WRITE.get(t.transform, WRITE["value"])
            new = fn(value)
            doc = docs[t.doc]
            old = doc.get(t.path)
            if doc.set(t.path, new):
                changes.append((t.doc, t.path, old, new))
        return changes


GROUP_RUN = "运行"
GROUP_STAMINA = "精力与药水"
GROUP_TEAM = "编队与暂停"
GROUP_SHOP = "商店"
GROUP_SCORE = "训练打分"

SPEC: list[tuple[str, list[Setting]]] = [
    (GROUP_RUN, [
        Setting(
            "rounds", "循环轮数", "int",
            [Target("yaml", "tuning.loop.max_rounds"),
             Target("json", "跑完一轮就停", "loop_stop_flag")],
            default=1, minimum=1, maximum=20, unit="轮",
            help="跑完这么多轮才收工。只有 1 轮时，「跑完一轮就停」自动变 true；\n"
                 "填 3 就是连着跑 3 轮（游戏不关、状态不清）。",
        ),
        Setting(
            "max_steps", "最大步数", "int", [Target("json", "最大步数")],
            default=1500, minimum=200, maximum=20000, step=100, unit="步",
            help="一次进程的总步数上限，跨轮累计。实测每轮 150~500 步，\n"
                 "跑多轮时记得给够：轮数 × 500 起。",
        ),
        Setting(
            "unknown_fallback", "未知页面兜底", "choice",
            [Target("json", "未知页面兜底")],
            default="点屏幕中间", choices=["点屏幕中间", "暂停"],
            help="认不出的页面怎么办。「点屏幕中间」可能误点（历史上两次误点都来自这里），\n"
                 "「暂停」更安全但会停下来等你。",
        ),
        Setting(
            "save_shot", "保存截图", "switch", [Target("json", "保存截图")],
            default=False, help="每一步都存一张画面，排查用；平时关掉省磁盘。",
        ),
    ]),
    (GROUP_STAMINA, [
        Setting(
            "stamina_use", "允许使用体力药水", "switch",
            [Target("yaml", "tuning.stamina.use_item"),
             Target("json", "专注恢复", "stamina_mode")],
            default=True,
            help="关掉后：专注不足弹「是否回复」时直接关掉弹窗，绝不喝药。\n"
                 "开着也只喝小的（+30），并且每轮有次数上限。",
        ),
        Setting(
            "stamina_max", "每轮最多喝几瓶", "int",
            [Target("yaml", "tuning.stamina.max_per_round")],
            default=1, minimum=0, maximum=6, unit="瓶",
            help="喝够次数就不再补，避免把存货一次喝光。",
        ),
        Setting(
            "train_stamina_min", "训练精力下限", "int",
            [Target("json", "训练精力下限")],
            default=12, minimum=0, maximum=80, unit="点",
            help="局内精力低于这个数就不训练、改去出行补精力。\n"
                 "实测一次训练只花 15~25 点，设太高会把能练的日子挡成出行（原来 40 就是这样丢的周六）。",
        ),
        Setting(
            "shop_stamina_below", "精力低于多少才买药", "int",
            [Target("yaml", "tuning.shop.rules.stamina.when_below")],
            default=50, minimum=0, maximum=150, unit="点",
            help="商店里只买 50 元以下的精力饮料，且精力低于这个值才买。",
        ),
        Setting(
            "consciousness", "精力低时优先「意识」", "switch",
            [Target("yaml", "tuning.core_score.consciousness.enabled")],
            default=True,
            help="意识训练不耗精力、反而小幅回精力，成功率也最高；\n"
                 "精力见底时把它顶到第一，是「周一到周六天天能练」的关键。",
        ),
    ]),
    (GROUP_TEAM, [
        Setting(
            "slots_check", "检查编队空位", "switch",
            [Target("yaml", "tuning.pick_slots.check")],
            default=True,
            help="在「选择队友 / 笔记选择 / 器材选择」页找金色的空槽（白底+黄十字）。",
        ),
        Setting(
            "slots_policy", "发现空位时", "choice",
            [Target("yaml", "tuning.pick_slots.on_empty", "slot_empty"),
             Target("yaml", "tuning.pick_slots.auto_fill_first", "slot_autofill")],
            default="弹窗暂停", choices=["弹窗暂停", "自动补满", "忽略"],
            reader="slot_policy",
            help="弹窗暂停：绝不自作主张配队，截图 + 置顶弹窗 + 停下等你（推荐）。\n"
                 "自动补满：先点一次「自动选择」，还补不上才停。\n"
                 "忽略：当没看见，照旧点下一步（会带着空位开训）。",
        ),
        Setting(
            "alerts_popup", "停下时弹桌面提示", "switch",
            [Target("yaml", "tuning.alerts.popup")],
            default=True,
            help="脚本因空位/配队没满主动停下时，弹一个置顶小窗告诉你原因。",
        ),
    ]),
    (GROUP_SHOP, [
        Setting(
            "shop_buy", "商店自动购买", "switch",
            [Target("json", "商店自动购买"), Target("yaml", "tuning.shop.auto_buy")],
            default=True,
            help="按白名单买：0 元馈赠 / 特训牛奶 / 精力饮料 / 器材暴击 / 教科书 / 秘籍 / 护符。",
        ),
        Setting(
            "shop_skip_days", "不进店的星期", "weekdays",
            [Target("yaml", "tuning.shop.skip_weekdays")],
            default=[7],
            help="勾上的星期一律不进店。默认勾周日：周日去公园就能回满，再买精力药是溢出浪费。",
        ),
        Setting(
            "shop_max_buy", "一次最多买几件", "int",
            [Target("yaml", "tuning.shop.max_buy_per_run")],
            default=4, minimum=0, maximum=10, unit="件",
            help="一次进店的上限，也是整轮的上限（防止来回刷着买）。",
        ),
    ]),
    (GROUP_SCORE, [
        Setting(
            "fail_limit", "失败率上限", "int",
            [Target("yaml", "tuning.core_score.fail_limit")],
            default=30, minimum=0, maximum=100, unit="%",
            help="超过这个失败率直接踢出排名；这个值以内不排除，交给\n"
                 "「(队友+器材) × (1−失败率)」的期望收益去比 —— 高收益的课值得赌。",
        ),
        Setting(
            "trip_first", "出行先看笔记（笔记 > 训练）", "switch",
            [Target("yaml", "tuning.trip.probe_notes_first")],
            default=True,
            help="每天有训练时，先进出行页看有没有笔记：有就出行，没有就回来训练。\n"
                 "每天只探一次；出行页退不出去会自动永久关掉这条路。",
        ),
    ]),
]


def all_settings() -> list[Setting]:
    return [s for _, items in SPEC for s in items]


def index_by_id() -> dict[str, Setting]:
    return {s.id: s for s in all_settings()}


# ------------------------------------------------------------------ 文档集合

DEFAULT_PATHS = {
    "yaml": os.path.join("config", "strategy.yaml"),
    "json": os.path.join("configs", "AutoTask.json"),
}


def open_docs(root: str, paths: dict[str, str] | None = None) -> dict[str, Any]:
    paths = paths or DEFAULT_PATHS
    return {
        "yaml": YamlDoc(os.path.join(root, paths["yaml"])),
        "json": JsonDoc(os.path.join(root, paths["json"])),
    }


def snapshot(docs: dict[str, Any]) -> dict[str, Any]:
    return {s.id: s.read(docs) for s in all_settings()}


def validate(values: dict[str, Any], docs: dict[str, Any]) -> list[str]:
    """保存/启动前的体检。只报「会让你白跑一轮」的问题，不报鸡毛蒜皮。"""
    out: list[str] = []
    rounds = int(values.get("rounds") or 1)
    steps = int(values.get("max_steps") or 0)
    if rounds > 1 and steps < rounds * 400:
        out.append(f"跑 {rounds} 轮但最大步数只有 {steps}：实测每轮 150~500 步，"
                   f"建议至少改到 {rounds * 500}，否则会在半路停。")
    if not values.get("stamina_use") and int(values.get("train_stamina_min") or 0) < 30:
        out.append("没开「允许使用体力药水」，训练精力下限又很低：局内精力见底时会卡在"
                   "「专注不足」弹窗前反复关窗。建议下限 ≥30 或把药水打开。")
    if not values.get("slots_check"):
        out.append("「检查编队空位」已关：编队没配满也会直接开训，人少了分数直接打折。")
    if values.get("slots_policy") == "忽略":
        out.append("空位策略是「忽略」：带空位开训会白丢队友分，确认这是你要的。")
    if int(values.get("fail_limit") or 0) >= 60:
        out.append(f"失败率上限 {values.get('fail_limit')}% 偏高：一半概率白扔体力。")
    if not values.get("shop_buy"):
        out.append("商店自动购买已关：特训牛奶（降精力消耗）、0 元馈赠都不会拿。")

    # 键是否真的存在（有人手改过 yaml 造成键名漂移时能立刻发现）
    for s in all_settings():
        for t in s.targets:
            if not docs[t.doc].has_path(t.path):
                out.append(f"配置里找不到「{t.path}」（{s.label}）—— 保存时会新增一行，"
                           f"但如果这键是被改名了，新值不会生效。")
    return out


def save_docs(docs: dict[str, Any], root: str) -> list[str]:
    """把改动落盘，返回备份文件列表。"""
    bak_dir = os.path.join(root, "config", ".backup")
    done = []
    for key in ("yaml", "json"):
        bak = docs[key].save(backup_dir=bak_dir)
        if bak:
            done.append(os.path.relpath(bak, root))
    return done
