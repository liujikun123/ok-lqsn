# -*- mode: python ; coding: utf-8 -*-
"""ok-lqsn v0.1 release 打包配置（onedir，解压即用）。

用法：在 bsg-auto 目录执行
  .venv\Scripts\python.exe -m PyInstaller --clean --noconfirm tools/ok-lqsn.spec
产物：dist/ok-lqsn/ok-lqsn.exe（整个目录一起分发）
"""
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = []
for folder in ("config", "profiles", "assets", "icons", "i18n", "knowledge", "docs", "cache"):
    src = os.path.join(ROOT, folder)
    if os.path.isdir(src):
        datas.append((src, folder))
for fn in ("README.md", "run_workbench.bat", "requirements.txt"):
    p = os.path.join(ROOT, fn)
    if os.path.exists(p):
        datas.append((p, "."))

# onnxocr 的字典/模型描述文件；ok 框架的 i18n 等资源
datas += collect_data_files("onnxocr")
datas += collect_data_files("ok", include_py_files=False)

hidden = []
for pkg in ("workbench", "workbench.app", "workbench.runner", "workbench.profiles",
            "workbench.settings", "workbench.settings_ui",
            "src", "src.config", "src.tasks", "src.tasks.AutoTask",
            "onnxocr", "onnxocr.onnx_paddleocr", "ok", "ok.cli"):
    hidden += collect_submodules(pkg) if pkg in ("src.tasks", "ok", "onnxocr") else [pkg]

a = Analysis(
    [os.path.join(ROOT, "tools", "entry_workbench.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["matplotlib", "torch", "tensorflow", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ok-lqsn",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,               # GUI 程序，不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, "icons", "app.ico") if os.path.exists(os.path.join(ROOT, "icons", "app.ico")) else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ok-lqsn",
)
