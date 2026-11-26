# 07 框架与 MCP 调试

> 这份文档回答一件事：**怎么用 ok-script 框架自带的 MCP 接口改代码、跑一轮、看结果**。
>
> 之前几十个 bug 全是「改代码 → 手动开游戏 → 肉眼看 → 猜」这种盲调方式找出来的，
> 慢且不可复现。框架从 2.0.17 起 MCP 是必需依赖，也就是说**这套调试通道本来就是现成的**，
> 只是我们一直没用。现在改为以 MCP 为主通道。

---

## 1. 框架速览（ok-script 3.0.0b3）

这是 LmeSzinc 的 ALAS 系框架，包名 `ok`，装在本项目的兄弟目录虚拟环境里（`../ok-bsg/.venv`）。

```
../ok-bsg/.venv/Lib/site-packages/ok/
├─ core/            app 生命周期、config、MCP 服务（mcp_server.py / mcp_runner.py）
├─ task/            BaseTask、TaskExecutor（取帧/点击/OCR 的底座）
├─ device/          capture_methods/（WGC、BitBlt、BitBlt_RenderFull、DXGI）
├─ ui/              Qt GUI（python -m ok gui）
└─ agent_resources/ 给 AI 用的技能与文档（见 §6）
```

### 1.1 项目侧结构

| 位置 | 作用 |
|---|---|
| `src/config.py` | 入口配置：窗口标题、分辨率、截图方式、交互方式、**任务注册** |
| `src/tasks/*.py` | 任务实现，每个任务是一个继承 `BaseTask` 的类，必须实现 `run()` |
| `configs/<任务名>.json` | **运行时覆盖项**，优先级高于代码里的默认值 |
| `config/strategy.yaml` | 玩家规则（优先级、阈值），由 `src/core/policy.py` 读 |

### 1.2 任务契约

```python
class AutoTask(BaseTask):
    def reset_to_start(self): ...
    def verify_start_state(self) -> bool: ...
    def run(self):
        while ...:
            frame = self.next_frame()      # 取一帧（会阻塞，见 §5.2）
            items = self._texts(frame)     # OCR → [{t, cx, cy, b}, ...]
            page  = self._classify(items)  # 认页面
            self._decide(page, items, frame)
```

三个方法是我们自己加的约定，框架只要求 `run()`：

- `reset_to_start()`：把游戏拉回「大厅」这个已知起点
- `verify_start_state()`：确认真的在大厅，不在就不跑（防止从奇怪的中途状态启动）
- `run()`：主循环

### 1.3 ⚠️ 最大的坑：`configs/*.json` 会盖掉代码默认值

改代码里的 `"最大步数": 1500` 没用，只要 `configs/AutoTask.json` 里还写着 `120`，
跑起来就还是 120 —— 这个坑浪费过一整轮排查（日志里步数死活卡在 120）。

**改任何默认值，两处都要改**：`src/tasks/AutoTask.py` 的 `default_config` + `configs/AutoTask.json`。

---

## 2. 启动 MCP 服务

在 `bsg-auto/` 目录下：

```bash
../ok-bsg/.venv/Scripts/python.exe -m ok mcp --config src.config:config
```

启动后：

- 默认监听 `http://127.0.0.1:8765/mcp`；端口被占用时自动 `+1` 往后试（`--port 0` 让系统分配）
- 连接信息写到 **`logs/mcp/connection.json`**（`url` + `token`），客户端就靠它找服务
- 这个进程是常驻的，**一次启动可以反复跑任务**，不用每轮重启
- 该模式下常规任务调度器是暂停的，只有你显式 `run_task` 才会跑

### 2.1 和 GUI 的关系（容易踩）

```bash
../ok-bsg/.venv/Scripts/python.exe -m ok gui --config src.config:config
```

GUI 起来会**顶掉同一个项目的 console 实例，并关掉那个实例的 MCP 服务**。
所以要开发 / 调试时：**先关 GUI，再起 console + MCP**。两者不要同时开。

### 2.2 启动前的卫生检查

服务起不来或连不上，99% 是**残留进程**：

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Select-Object ProcessId, CommandLine | Format-Table -Wrap
```

多个 `ok mcp` 进程会各自抢端口（8765 → 8766 → …），而 `connection.json` 只记最后一个，
客户端连到旧实例上就会出现「改了代码但行为没变」。**动手前先清干净**：

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
```

---

## 3. 工具清单（服务端暴露的 9 个）

| 工具 | 参数 | 用途 |
|---|---|---|
| `list_skills` | — | 列出框架自带的 AI 技能 |
| `get_skill` | `skill_id` | 读某个训练项的全文（**开工前先读 `develop`**） |
| `list_tasks` | — | 列出已注册任务及其 `task_id` |
| `reload_tasks` | `task_ids?` | 热重载任务模块，改完代码必做 |
| `run_task` | `file_path` | 按**任务文件路径**跑一次 |
| `get_run_status` | `run_id` | 查 `phase` / `finished_at` / `error` |
| `stop_task` | `run_id` | 停掉某次运行 |
| `screenshot` | — | 抓当前画面，返回落盘路径 |
| `exit_process` | `reason` | 退出整个服务进程 |

每次运行的产物在 **`logs/mcp/<run_id>/run.log`** —— 这就是我们的「黑匣子」，
每步都会打印：步号、页面类型、精力、文字段数，以及每一次点击的坐标和理由。

截图落在 `logs/mcp/observations/*.png`。

---

## 4. 本地客户端：`tools/mcp_client.py`

MCP 是 streamable HTTP + Bearer token，直接 curl 不方便，所以写了个薄客户端：

```bash
PY=../ok-bsg/.venv/Scripts/python.exe

$PY tools/mcp_client.py list_tasks                    # 看任务与 task_id
$PY tools/mcp_client.py screenshot                    # 抓一帧，打印路径
$PY tools/mcp_client.py run_task --file tools/run_auto.py   # 跑一轮
$PY tools/mcp_client.py status --run <run_id>         # 查状态
$PY tools/mcp_client.py stop --run <run_id>           # 停
$PY tools/mcp_client.py reload --ids src.tasks.AutoTask.AutoTask
$PY tools/mcp_client.py skills                        # 列框架技能
$PY tools/mcp_client.py skill --id develop            # 读技能全文
```

### 4.1 两个必知的细节

**① `run_task` 要的是文件路径，且该文件必须定义 `class Task`。**

框架的任务文件契约是（原文见 `agent_resources/docs/mcp.md` §Task file contract）：

> Each file defines a synchronous `class Task(BaseTask)` with these methods:
> `reset_to_start()` / `verify_start_state()` / `run()`

而 `src/tasks/AutoTask.py` 里的类叫 **`AutoTask`**，不是 `Task` —— 直接传它，
runner 找不到约定入口，起不来。所以用 `tools/run_auto.py` 当薄壳转发：

```python
# tools/run_auto.py（只是转发，逻辑一律留在 src/tasks/AutoTask.py）
from src.tasks.AutoTask import AutoTask
from src.tasks.MyBaseTask import MyBaseTask

class Task(MyBaseTask):
    def reset_to_start(self):     return self.get_task_by_class(AutoTask).reset_to_start()
    def verify_start_state(self): return self.get_task_by_class(AutoTask).verify_start_state()
    def run(self):                return self.get_task_by_class(AutoTask).run()
```

这样每轮验证走的都是同一份 `AutoTask` 代码，不会出现「验的是临时副本」。
runner 每次会新建实例 → 调 `after_init()/post_init()` → 准备截图与交互 →
依次执行 reset / verify / run；失败也会调 `on_destroy()`。

**② `reload_tasks` 要完整 `task_id`。**
`AutoTask` 不好使，必须写 `src.tasks.AutoTask.AutoTask`。改完代码不reload，
跑的还是内存里的旧类 —— 表现就是「我明明改了，怎么没生效」。

### 4.2 依赖踩坑

客户端用的是 `mcp` SDK 的 streamable HTTP 传输，版本敏感的命名：

- 老写法 `streamablehttp_client` → 现行 `streamable_http_client`（带下划线）
- HTTP 库用的是 `httpx2`，不是 `httpx`

---

## 5. 标准开发循环

```
① 先读文档：get_skill("develop") + 本目录 docs/
② 抓现场：screenshot → 看当前是什么页
③ 改代码：src/tasks/AutoTask.py（页面判定 → src/core；规则 → config/strategy.yaml）
④ 编译：python -m py_compile src/tasks/AutoTask.py
⑤ 热重载：reload --ids src.tasks.AutoTask.AutoTask
⑥ 跑一轮：run_task --file tools/run_auto.py
⑦ 读黑匣子：logs/mcp/<run_id>/run.log（重点看「页面：」「点击「」这两类行）
⑧ 有问题回到 ③
```

一条经验：**先有观测，再改代码。** 觉得某个页面认错了，先 grep 出那一步的
`未知页面（第 N 次），点屏幕中间推进。文字: [...]` —— 数组里就是那一屏的全部 OCR 文本，
按它写页面指纹，比凭想象准得多。

### 5.1 判断「这轮算跑通」的标准

- `grep -c ERROR run.log` = 0
- 最后几行出现 `★ 一轮训练已跑完，回到大厅 —— 收工`
- 页面序列覆盖到 `第7周` 并回到 `大厅`

### 5.2 截图方式的坑（显示器关着时）

`src/config.py` 的 `'capture_method'` 列表决定优先级：

| 方式 | 特点 |
|---|---|
| `WGC`（Windows Graphics Capture） | 快，但**显示器断电 / DPMS 休眠后会永久阻塞**，`next_frame()` 一去不回，连 `screenshot` 工具都超时 |
| `BitBlt_RenderFull`（PrintWindow） | 慢一点，但显示器关着也能抓到完整画面 |
| `BitBlt` | 更慢，兼容性兜底 |

本机结论：**优先级设成 `['BitBlt_RenderFull', 'BitBlt', 'WGC']`**。
显示器关着跑通宵靠的就是这个。另外：

- 游戏窗口**不能最小化**（最小化时 WGC 返回 −32000 矩形）
- 点击是真实鼠标事件（Pynput），所以窗口必须在**前台**，挡在后面不行
- `next_frame()` 会阻塞，所以 `_frame()` 里包了一层**子线程 + 超时**，超时就丢帧并
  尝试把窗口拉回前台，避免整个主循环卡死

---

## 6. 框架自带的 AI 技能（`agent_resources`）

位置：`../ok-bsg/.venv/Lib/site-packages/ok/agent_resources/`

```
docs/mcp.md               MCP 接入总说明
docs/agent-setup.md       从零装环境
docs/feature-crops.md     图像特征/模板裁剪
examples/mcp_observe.py   官方给的 MCP 观测示例（可对照改 tools/mcp_client.py）
```

技能通过 `list_skills` / `get_skill` 取，关键几个：

| 技能 | 什么时候读 |
|---|---|
| `develop` | **开工第一件事**。框架推荐的开发方式、API 发现方法 |
| `generate-templates` | 要做按钮/角标的图像匹配时 |
| `write-test` | 想给某个识别写回归测试时 |
| `reload-tasks` | 热重载细节 |
| `setup-project` | 新项目初始化 |

框架对「按钮和小组件」的官方建议是：**模板匹配 / 颜色 / 几何形状，别用 OCR**。
这一条对我们后面要修「训练项读数」和「技能角标」很关键（见 `06-下一步.md` 的 P0-2）。

---

## 7. 速查表

```bash
# 起服务（保持一个，别叠）
cd bsg-auto
../ok-bsg/.venv/Scripts/python.exe -m ok mcp --config src.config:config

# 改完代码
../ok-bsg/.venv/Scripts/python.exe -m py_compile src/tasks/AutoTask.py
../ok-bsg/.venv/Scripts/python.exe tools/mcp_client.py reload --ids src.tasks.AutoTask.AutoTask

# 跑 + 看
../ok-bsg/.venv/Scripts/python.exe tools/mcp_client.py run_task --file tools/run_auto.py
grep -E "页面：|点击「|ERROR" logs/mcp/<run_id>/run.log | tail -40

# 收工
../ok-bsg/.venv/Scripts/python.exe tools/mcp_client.py stop --run <run_id>
```

---

## 8. 待补

- `get_run_status` 只返回 phase，没有「跑到第几周／第几步」的汇总；现在是靠 grep 日志。
  可以加一个 `tools/run_report.py` 把 run.log 汇总成一张表（周数、训练次数、评分分布）。
- 目前所有迭代都是「整轮跑」。改页面判定时更该用单步任务（`StepTask`），
  一步一截图，比整轮跑快得多。
