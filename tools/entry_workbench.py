"""PyInstaller 入口：双击 exe 启动工作台。

打包后 __file__ 在 <exe目录>/_internal/tools/ 下，这里把 _internal 加进 sys.path，
让 workbench / src 包和数据目录（config、profiles、cache）都能按项目内的相对路径找到。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

# PyInstaller 冻结后把 sys.executable 所在目录当作项目根的外部壳，
# 资源实际在 _internal 下（BASE 就是 _internal），所以无需再改 cwd。
os.chdir(BASE)

from workbench.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
