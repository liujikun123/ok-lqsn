"""MCP 薄壳：通过 MCP 跑「已注册」的 AutoTask。

ok-script 文档推荐的做法 —— MCP 的 run_task 接受的是一个带 `class Task` 的文件，
所以这里只做转发，真正的逻辑留在 src/tasks/AutoTask.py（别把逻辑抄到这里）。
这样每次验证都走同一份代码，不会出现"验的是临时副本"。

    python tools/mcp_client.py reload --ids src.tasks.AutoTask.AutoTask
    python tools/mcp_client.py run_task --file tools/run_auto.py
"""

from src.tasks.AutoTask import AutoTask
from src.tasks.MyBaseTask import MyBaseTask


class Task(MyBaseTask):

    def reset_to_start(self):
        return self.get_task_by_class(AutoTask).reset_to_start()

    def verify_start_state(self):
        return self.get_task_by_class(AutoTask).verify_start_state()

    def run(self):
        return self.get_task_by_class(AutoTask).run()
