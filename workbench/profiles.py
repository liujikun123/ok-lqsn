"""训练方案（profile）管理 —— 把整套局内规则存成一份可导入导出的方案。

设计要点
--------
* 方案 = ``profiles/<名字>.json``，里面 ``raw_yaml`` 存的是 ``config/strategy.yaml``
  **原文的逐字快照**（含全部中文注释）。切换方案时把原文写回，因此注释不会丢，
  AutoTask / 工作台设置面板 / 规则摘要全部沿用现有读取链路，零侵入。
* 当前生效方案记在 ``config/active_profile.json``。
* 导出 = 复制 JSON 到用户指定位置；导入 = 校验后复制回 ``profiles/``。
  所以一个 .json 文件就是一份完整可分享的方案。

玩家 2026-11-23 22:14：「把这些局内配置做成一个方案，加上方案管理和导入导出，
我们现在的方案就叫百目鬼训练」。
"""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime

import yaml

FORMAT = "ok-lqsn-profile"
VERSION = "0.1"
DEFAULT_PROFILE = "百目鬼训练"


def project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def profile_dir(root: str | None = None) -> str:
    return os.path.join(root or project_root(), "profiles")


def active_file(root: str | None = None) -> str:
    return os.path.join(root or project_root(), "config", "active_profile.json")


def strategy_file(root: str | None = None) -> str:
    return os.path.join(root or project_root(), "config", "strategy.yaml")


def _safe(name: str) -> str:
    """文件名安全化：只替换 Windows 非法字符，中文保留。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(name or "")).strip()
    return s or "未命名"


def _path(name: str, root: str | None = None) -> str:
    return os.path.join(profile_dir(root), _safe(name) + ".json")


def ensure_dirs(root: str | None = None) -> None:
    os.makedirs(profile_dir(root), exist_ok=True)
    os.makedirs(os.path.dirname(active_file(root)), exist_ok=True)


def list_profiles(root: str | None = None) -> list[str]:
    """列方案名。优先读 JSON 里的 name 字段，兼容手工改过文件名的情况。"""
    d = profile_dir(root)
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                data = json.load(f)
            out.append(str(data.get("name") or fn[:-5]))
        except Exception:
            out.append(fn[:-5])
    return sorted(set(out))


def active_name(root: str | None = None) -> str | None:
    try:
        with open(active_file(root), encoding="utf-8") as f:
            return str((json.load(f) or {}).get("active") or "") or None
    except Exception:
        return None


def set_active(name: str, root: str | None = None) -> None:
    ensure_dirs(root)
    with open(active_file(root), "w", encoding="utf-8") as f:
        json.dump({"active": name, "updated_at": datetime.now().isoformat(timespec="seconds")},
                  f, ensure_ascii=False, indent=2)


def load_profile(name: str, root: str | None = None) -> dict:
    p = _path(name, root)
    if not os.path.exists(p):
        raise FileNotFoundError(f"找不到方案「{name}」：{p}")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_profile(name: str, raw_yaml: str, description: str = "",
                 root: str | None = None) -> str:
    """存一份方案快照。raw_yaml 是 strategy.yaml 的原文（含注释）。"""
    ensure_dirs(root)
    data = {
        "format": FORMAT,
        "version": VERSION,
        "name": _safe(name),
        "description": description or "",
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "raw_yaml": raw_yaml,
    }
    p = _path(name, root)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return p


def snapshot_current(name: str, description: str = "",
                     root: str | None = None) -> str:
    """把当前 config/strategy.yaml 存成方案（保留注释原文）。"""
    with open(strategy_file(root), encoding="utf-8") as f:
        raw = f.read()
    return save_profile(name, raw, description, root)


def apply_profile(name: str, root: str | None = None) -> str:
    """切换方案：把方案原文写回 strategy.yaml，并记为当前方案。"""
    data = load_profile(name, root)
    raw = data.get("raw_yaml")
    if not raw:
        # 兼容「只有 config 字典」的外部方案文件
        cfg = data.get("config") or {}
        raw = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
    ensure_dirs(root)
    p = strategy_file(root)
    with open(p, "w", encoding="utf-8") as f:
        f.write(raw)
    set_active(data.get("name") or name, root)
    return p


def delete_profile(name: str, root: str | None = None) -> bool:
    p = _path(name, root)
    if not os.path.exists(p):
        return False
    os.remove(p)
    if active_name(root) == name:
        rest = list_profiles(root)
        if rest:
            apply_profile(rest[0], root)
        else:
            try:
                os.remove(active_file(root))
            except OSError:
                pass
    return True


def rename_profile(old: str, new: str, root: str | None = None) -> str:
    data = load_profile(old, root)
    data["name"] = _safe(new)
    data["saved_at"] = datetime.now().isoformat(timespec="seconds")
    new_path = save_profile(new, data.get("raw_yaml", ""),
                            data.get("description", ""), root)
    if _safe(old) != _safe(new):
        os.remove(_path(old, root))
    if active_name(root) == old:
        set_active(data["name"], root)
    return new_path


def export_profile(name: str, dest: str, root: str | None = None) -> str:
    """导出到用户指定路径（单个 .json 即完整方案）。返回实际写入路径。"""
    src = _path(name, root)
    if not os.path.exists(src):
        raise FileNotFoundError(f"找不到方案「{name}」")
    dest = os.path.abspath(dest)
    if os.path.isdir(dest):
        dest = os.path.join(dest, _safe(name) + ".json")
    if not dest.lower().endswith(".json"):
        dest += ".json"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(src, dest)
    return dest


def import_profile(src: str, root: str | None = None) -> str:
    """从 .json 文件导入方案，返回方案名。"""
    with open(src, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not (data.get("raw_yaml") or data.get("config")):
        raise ValueError("不是有效的方案文件：缺少 raw_yaml / config")
    if data.get("format") and data.get("format") != FORMAT:
        raise ValueError(f"方案格式不匹配：{data.get('format')}（期望 {FORMAT}）")
    name = _safe(data.get("name") or os.path.basename(src).replace(".json", ""))
    data["name"] = name
    data.setdefault("version", VERSION)
    ensure_dirs(root)
    with open(_path(name, root), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return name


def describe(name: str, root: str | None = None) -> str:
    """给工作台状态栏显示的一句话说明。"""
    try:
        data = load_profile(name, root)
        raw = data.get("raw_yaml") or ""
        cfg = yaml.safe_load(raw) or {}
        t = cfg.get("tuning") or {}
        core = t.get("core_score") or {}
        return (f"{data.get('description', '')} ｜ "
                f"队友{core.get('teammate')} 经理{core.get('manager_weight')} "
                f"笔记优先{t.get('trip', {}).get('probe_notes_first')} ｜ "
                f"存于 {data.get('saved_at', '?')}")
    except Exception as e:
        return f"（读取方案说明失败：{e}）"
