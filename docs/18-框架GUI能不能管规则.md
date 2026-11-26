# 18 · 框架自带 GUI 能不能管这套规则（源码核对，2026-11-26）

玩家问：「框架自带的工作台里有人工训练规则和框图的功能，你现在这些规则能用那个模块管理吗？
想人工调整能做到吗？」

**结论：不能直接用。** 框架那个 GUI（`python -m ok gui`）是**开发调试台**，它的配置页只认
「任务里 `default_config` 声明的扁平键」；我们的规则 90% 不在那儿。下面是源码级核对。

---

## 一、框架 GUI 到底有哪几页（`ok/ui/qt/MainWindow.py` 源码）

| 页 | 源码 | 能干什么 | 对我们的规则 |
|---|---|---|---|
| 任务页 | `tasks/` | 一次性任务 / 定时任务列表、启动、停止 | 只是**启动器**，不含规则 |
| **脚本页** | `tasks/EditTaskTab.py` | 打开 `.py` 源码编辑器（Python 高亮、模板库） | 等于「用 IDE 改代码」，不是规则管理 |
| **模板页（框图）** | `tasks/TemplateTab.py` + `tasks/MarkUpWindow.py` | 给图片画框 → 写 COCO 标注，供模板匹配 | **路径不通**，见第三节 |
| **设置页** | `settings/GlobalConfigTab.py` + `tasks/config_form.py` | 按 `default_config` + `config_description` **自动生成**表单 | 只能改到 15 个任务参数，且我们没写说明 |
| 插件 / MCP 页 | `plugins/PluginTab.py` | 第三方 tab、MCP 连接 | 与规则无关 |

配置表单的能力边界（`config_form.py` 实测）：

- ✅ 扁平键：数字 / 开关 / 下拉 / 按钮（`'type': 'button'` + callback）
- ✅ 条件显示：`'sub_configs': {选项: [要显示的键]}`
- ❌ **嵌套字典不铺控件**：`strategy.yaml` 里 `core_score.teammate` 这种层级结构它不认

---

## 二、我们三类规则，分别能不能进那个 GUI

| 规则类型 | 例子 | 存在哪 | 框架 GUI | 现在怎么改 |
|---|---|---|---|---|
| 任务参数（扁平） | 最大步数、训练精力下限、未知页兜底、读数失败即暂停 | `configs/AutoTask.json` | ✅ 能显示（但 `AutoTask` 没写 `config_description`，**字段说明是空的**） | 工作台「运行设置」19 项 / 直接用记事本改 json |
| 规则参数（嵌套） | `core_score.fail_limit`、`trip.priority`、`shop.rules`、`pick_slots`、`event.priority` | `config/strategy.yaml`（20 组、307 行带注释） | ❌ 完全看不见 | 工作台「运行设置」覆盖 19 项；其余手改 yaml |
| 决策逻辑（硬编码） | 六项列表 `SKILLS`、主训人员识别、意识加权、页面识别 73 个分支、兜底坐标、赌一把判定 | `src/tasks/AutoTask.py` | ❌ 只能改代码（脚本页就是个编辑器） | 改 py |
| 框图 / 锚点 | 页面锚点、按钮模板 | `assets/anchors.json` + `assets/coco_annotations.json` | ⚠️ 半能，见下 | `tools/build_anchors.py` 离线生成 |

---

## 三、框图为什么「画了不生效」

框架模板页的路径是**写死**的：

```python
# ok/ui/qt/tasks/TemplateTab.py
TEMPLATE_FOLDER = 'ok_templates'          # ← 固定 <项目>/ok_templates/
COCO_JSON = 'coco_annotations.json'
def ensure_template_folder():
    return str(CocoTemplateStore(os.path.join(os.getcwd(), TEMPLATE_FOLDER)).folder)
```

而我们：

```python
# src/config.py
'template_matching': {'coco_feature_json': os.path.join('assets', 'coco_annotations.json')}
# src/tasks/AutoTask.py：认页用自己那份 assets/anchors.json（页面→锚点→搜索区域）
```

两份数据在**不同目录、不同用途**：框架页面→`ok_templates/`（现在不存在，打开是空的）；
我们→`assets/`，而且 `anchors.json` 的 `region/proto` 是 `tools/build_anchors.py`
从审计帧离线算出来的，**在 UI 里画的框不会写进 `anchors.json`**，认页一点都不会变。

---

## 四、想人工调整，三条路（按投入排）

### A. 现在就能做（0 改动）

- 参数：工作台「运行设置」→ 19 项 → 「保存到配置」（自动备份到 `config/.backup/`，留 20 份）
- 其余：工作台「规则原文」看内容 → 「打开文件」改 yaml → 回来点「重新读取」
- 注意：`strategy.yaml` 是**启动时读一次**（`_tuning()` 有缓存），改完要重启脚本才生效

### B. 小改（半天内，我可以做）

1. 把 `tuning` 里最常调的 8~10 个键补进「运行设置」（`workbench/settings.py` 的 `SPEC` 加一条 `Setting` 即可，它就是 yaml 编辑器）
2. 给 `AutoTask` 补 `config_description` → 框架 GUI 的设置页字段就有中文说明
3. 框图：把框架的 `ok_templates` 做成指向 `assets` 的目录联接（`mklink /J ok_templates assets`），
   框架模板页就能直接编辑我们的 `coco_annotations.json`
   ⚠️ 但 `build_anchors.py` 重跑会覆盖，需要改成「承认 UI 手画的框」的合并模式

### C. 正经做（推荐，但工作量大）

给框架加**自定义 tab**（`src/config.py` 里 `custom_tabs` 是框架支持的）：

- 一个「规则编辑器」页：读 `strategy.yaml` → 按组折叠成表单（不是自由文本框）→ 保存写回原文并保留注释
- 一个「规则试算」按钮：喂一帧真实截图 / 一帧审计图 → 跑当前打分 → 直接显示六项排名和为什么这么选
  （这才是玩家真正要的「人工训练规则」：不是调数字，而是「我想让他优先练防守」这种语义）

---

## 五、一句话回答

- **能用框架 GUI 管吗？** 不能。它只认扁平任务参数，我们的规则大部分在 yaml 嵌套结构和代码里。
- **想人工调整能做到吗？** 能，两条现成的：工作台「运行设置」19 项 + 「规则原文/打开文件」改 yaml；
  逻辑类规则必须改代码。框图目前只能走 `tools/build_anchors.py`，框架模板页需要做目录联接才看得见。
