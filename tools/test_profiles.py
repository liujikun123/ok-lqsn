"""方案管理（profiles）离线单测：全部在临时目录里跑，不碰真实配置。

验证：快照保留注释原文、切换写回、当前方案记账、导入导出往返、重命名/删除、坏文件拒绝。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workbench import profiles as P

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: 得到 {got!r}，期望 {want!r}")


RAW = ("# 这份注释一定要在切换方案后还活着\n"
       "rules: {}\n"
       "tuning:\n"
       "  core_score:\n"
       "    teammate: 1.0\n"
       "    manager_weight: 1.5\n")
RAW2 = ("# 第二套方案\n"
        "rules: {}\n"
        "tuning:\n"
        "  core_score:\n"
        "    teammate: 2.0\n"
        "    manager_weight: 3.0\n")

with tempfile.TemporaryDirectory() as root:
    os.makedirs(os.path.join(root, "config"), exist_ok=True)
    strat = P.strategy_file(root)
    with open(strat, "w", encoding="utf-8") as f:
        f.write(RAW)

    print("== 1. 快照 / 当前方案 ==")
    check("初始无方案", P.list_profiles(root), [])
    p = P.snapshot_current("百目鬼训练", "多人+器材，每周看笔记", root)
    check("方案文件已建", os.path.basename(p), "百目鬼训练.json")
    check("列表含百目鬼训练", P.list_profiles(root), ["百目鬼训练"])
    data = json.load(open(p, encoding="utf-8"))
    check("方案格式", data["format"], "ok-lqsn-profile")
    check("方案版本", data["version"], "0.1")
    check("快照保留注释原文", "# 这份注释一定要在切换方案后还活着" in data["raw_yaml"], True)
    P.set_active("百目鬼训练", root)
    check("当前方案", P.active_name(root), "百目鬼训练")

    print("\n== 2. 切换方案：原文写回，注释不丢 ==")
    P.save_profile("测试方案B", RAW2, "另一套", root)
    P.apply_profile("测试方案B", root)
    back = open(strat, encoding="utf-8").read()
    check("写回目标方案内容", "manager_weight: 3.0" in back, True)
    check("注释跟着写回", "# 第二套方案" in back, True)
    check("当前方案已切", P.active_name(root), "测试方案B")
    P.apply_profile("百目鬼训练", root)
    back = open(strat, encoding="utf-8").read()
    check("切回原方案", "manager_weight: 1.5" in back, True)
    check("原注释还在", "# 这份注释一定要在切换方案后还活着" in back, True)

    print("\n== 3. 导入导出往返 ==")
    out = os.path.join(root, "导出", "百目鬼训练.json")
    got = P.export_profile("百目鬼训练", out, root)
    check("导出路径", got, out)
    check("导出文件存在", os.path.exists(out), True)
    P.delete_profile("百目鬼训练", root)
    check("删除后列表只剩B", P.list_profiles(root), ["测试方案B"])
    name = P.import_profile(out, root)
    check("导入返回方案名", name, "百目鬼训练")
    check("导入后列表恢复", sorted(P.list_profiles(root)), ["测试方案B", "百目鬼训练"])
    check("导入的快照仍带注释",
          "# 这份注释一定要在切换方案后还活着"
          in json.load(open(P._path("百目鬼训练", root), encoding="utf-8"))["raw_yaml"], True)

    print("\n== 4. 重命名 ==")
    P.rename_profile("百目鬼训练", "百目鬼训练-改", root)
    check("重命名后列表", sorted(P.list_profiles(root)), ["测试方案B", "百目鬼训练-改"])
    check("旧文件已删", os.path.exists(P._path("百目鬼训练", root)), False)

    print("\n== 5. 坏文件拒绝导入 ==")
    bad = os.path.join(root, "bad.json")
    with open(bad, "w", encoding="utf-8") as f:
        json.dump({"name": "坏方案", "hello": "world"}, f)
    try:
        P.import_profile(bad, root)
        rejected = False
    except ValueError:
        rejected = True
    check("缺 raw_yaml/config 被拒", rejected, True)
    bad2 = os.path.join(root, "bad2.json")
    with open(bad2, "w", encoding="utf-8") as f:
        json.dump({"format": "other-tool", "raw_yaml": RAW}, f)
    try:
        P.import_profile(bad2, root)
        rejected2 = False
    except ValueError:
        rejected2 = True
    check("格式不符被拒", rejected2, True)

    print("\n== 6. 说明文案 ==")
    desc = P.describe("测试方案B", root)
    check("说明含队友权重", "队友2.0" in desc, True)
    check("说明含经理权重", "经理3.0" in desc, True)

print(f"\n=== 通过 {PASS} / 失败 {FAIL} ===")
sys.exit(1 if FAIL else 0)
