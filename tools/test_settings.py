"""工作台配置层的回归测试：`python tools/test_settings.py`

三道关，全部围绕「点了保存之后，文件到底变成什么样」：

  1. **键都在**：SPEC 里每个控件指向的路径必须真的存在于配置文件里。
     对不上就等于界面在改一个没人读的键 —— 最坑的一种「看起来生效了」。
  2. **写→读回去一致**：写一个新值，读出来必须就是这个值。
  3. **改完再改回去，文件必须一字不差**：这条最狠，也最能证明
     「逐行定点替换」没有把注释、缩进、顺序搞坏（yaml.safe_dump 必挂这条）。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from workbench import settings as S  # noqa: E402

passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def alt_value(st: S.Setting, cur):
    """给一个和当前值不同的合法值。"""
    if st.kind == "switch":
        return not bool(cur)
    if st.kind == "int":
        lo, hi = st.minimum or 0, st.maximum or 100
        return lo if cur != lo else min(hi, lo + 1)
    if st.kind == "choice":
        for c in st.choices:
            if c != cur:
                return c
        return cur
    if st.kind == "weekdays":
        return [1, 3] if cur != [1, 3] else [7]
    return cur


def main() -> int:
    docs = S.open_docs(ROOT)
    raw_yaml = open(os.path.join(ROOT, S.DEFAULT_PATHS["yaml"]), encoding="utf-8").read()
    raw_json = open(os.path.join(ROOT, S.DEFAULT_PATHS["json"]), encoding="utf-8").read()
    comments_before = sum(1 for line in raw_yaml.splitlines() if line.lstrip().startswith("#"))

    print("--- 1. 每个控件的键都真的存在 ---")
    for st in S.all_settings():
        for t in st.targets:
            check(f"{st.label} → {t.path}", docs[t.doc].has_path(t.path),
                  "配置里没有这个路径")

    print("\n--- 2. 写进去，读出来是同一个值 ---")
    values = S.snapshot(docs)
    for st in S.all_settings():
        cur = values[st.id]
        new = alt_value(st, cur)
        changes = st.write(docs, new)
        got = st.read(docs)
        if st.kind == "weekdays":
            got = [int(x) for x in (got or [])]
        check(f"{st.label}：{cur!r} → {new!r} 读到 {got!r}", got == new, f"改了 {changes}")

    print("\n--- 3. 逐个改回原值后，与原文逐字节比对 ---")
    for st in S.all_settings():
        cur = values[st.id]
        st.write(docs, alt_value(st, cur))
        st.write(docs, cur)
    new_yaml = "\n".join(docs["yaml"].lines) + "\n"
    check("yaml 与原文件一字不差", new_yaml == raw_yaml,
          f"长度 {len(new_yaml)} vs {len(raw_yaml)}")
    if new_yaml != raw_yaml:
        a, b = new_yaml.splitlines(), raw_yaml.splitlines()
        for i in range(min(len(a), len(b))):
            if a[i] != b[i]:
                print(f"        第 {i + 1} 行：\n          现在 {a[i]!r}\n          原来 {b[i]!r}")
                break
    comments_after = sum(1 for line in new_yaml.splitlines() if line.lstrip().startswith("#"))
    check(f"注释行数没变（{comments_before}）", comments_after == comments_before,
          f"{comments_after}")

    print("\n--- 4. 行内注释与缩进被保留 ---")
    # 挑一行既有行内注释、注释又是竖着对齐的（max_buy_per_run）
    key = "tuning.shop.max_buy_per_run"
    before = docs["yaml"].lines[docs["yaml"].find_line(key).idx]
    docs["yaml"].set(key, 9)
    line = docs["yaml"].lines[docs["yaml"].find_line(key).idx]
    check("行内注释还在", "#" in line and "一次进店最多买几件" in line, line)
    check("缩进没变（4 空格）", line.startswith("    "), repr(line))
    check("值已更新", "9" in line.split("#")[0], line)
    check("注释列没被推歪", line.index("#") == before.index("#"),
          f"{line.index('#')} vs {before.index('#')}")
    docs["yaml"].set(key, values["shop_max_buy"])

    print("\n--- 5. 联动项：一个控件管两个键 ---")
    docs["yaml"].set("tuning.loop.max_rounds", 3)
    docs["json"].set("跑完一轮就停", False)
    idx = S.index_by_id()
    idx["rounds"].write(docs, 3)
    check("3 轮 → 跑完一轮就停 = false", docs["json"].get("跑完一轮就停") is False,
          str(docs["json"].get("跑完一轮就停")))
    idx["rounds"].write(docs, 1)
    check("1 轮 → 跑完一轮就停 = true", docs["json"].get("跑完一轮就停") is True)
    check("yaml 轮数同时是 1", docs["yaml"].get("tuning.loop.max_rounds") == 1)

    idx["stamina_use"].write(docs, False)
    check("关药水 → 专注恢复=关闭", docs["json"].get("专注恢复") == "关闭",
          str(docs["json"].get("专注恢复")))
    check("关药水 → use_item=false", docs["yaml"].get("tuning.stamina.use_item") is False)
    idx["stamina_use"].write(docs, True)

    for policy, want_empty, want_fill in (("弹窗暂停", "暂停提示", False),
                                          ("自动补满", "暂停提示", True),
                                          ("忽略", "继续", False)):
        idx["slots_policy"].write(docs, policy)
        check(f"空位策略「{policy}」",
              docs["yaml"].get("tuning.pick_slots.on_empty") == want_empty
              and docs["yaml"].get("tuning.pick_slots.auto_fill_first") == want_fill,
              f"{docs['yaml'].get('tuning.pick_slots.on_empty')}/"
              f"{docs['yaml'].get('tuning.pick_slots.auto_fill_first')}")
        check(f"空位策略「{policy}」读回来一致", idx["slots_policy"].read(docs) == policy)
    idx["slots_policy"].write(docs, values["slots_policy"])

    print("\n--- 6. 列表写入（不进店的星期）---")
    idx["shop_skip_days"].write(docs, [6, 7])
    check("写成 [6, 7]", docs["yaml"].get("tuning.shop.skip_weekdays") == [6, 7],
          str(docs["yaml"].get("tuning.shop.skip_weekdays")))
    check("读回 [6, 7]", list(idx["shop_skip_days"].read(docs)) == [6, 7])
    idx["shop_skip_days"].write(docs, values["shop_skip_days"])

    print("\n--- 7. 缺失键会自动补一行（并在注释里标明）---")
    docs["yaml"].set("tuning.stamina.some_new_key", 5)
    node = docs["yaml"].find_line("tuning.stamina.some_new_key")
    check("能定位到新加的行", node is not None)
    if node is not None:
        check("补的行带标注", "工作台新增" in docs["yaml"].lines[node.idx])
        check("新键可读", docs["yaml"].get("tuning.stamina.some_new_key") == 5)
    docs["yaml"].lines = [l for l in docs["yaml"].lines if "some_new_key" not in l]
    docs["yaml"].data = __import__("yaml").safe_load("\n".join(docs["yaml"].lines))

    print("\n--- 8. 校验器会指出会白跑一轮的配置 ---")
    bad = dict(values, rounds=3, max_steps=300)
    msgs = S.validate(bad, docs)
    check("轮数多但步数不够 → 报警", any("最大步数" in m for m in msgs), str(msgs))
    bad2 = dict(values, stamina_use=False, train_stamina_min=12)
    check("不开药水又低门槛 → 报警", any("体力药水" in m for m in S.validate(bad2, docs)))
    bad3 = dict(values, slots_check=False)
    check("关掉空位检查 → 报警", any("空位" in m for m in S.validate(bad3, docs)))
    check("正常配置不报错", S.validate(values, docs) == [] or
          all("找不到" not in m for m in S.validate(values, docs)), str(S.validate(values, docs)))

    print("\n--- 9. JSON 那一侧格式保持 ---")
    docs["json"].set("最大步数", 9999)
    check("json 读回 9999", docs["json"].get("最大步数") == 9999)
    docs["json"].set("最大步数", values["max_steps"])
    check("json 键顺序没变",
          list(docs["json"].data.keys())[:3] == ["最大步数", "等待秒数", "扭蛋目标关键词"],
          str(list(docs["json"].data.keys())[:3]))
    check("json 仍是 4 空格缩进（原文长度一致）",
          len(raw_json.splitlines()) == len(
              __import__("json").dumps(docs["json"].data, ensure_ascii=False,
                                      indent=4).splitlines()),
          "行数变化 = 缩进或键顺序被改了")

    print(f"\n{'=' * 46}\n通过 {passed} 项，失败 {failed} 项")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
