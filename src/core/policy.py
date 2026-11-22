"""策略引擎：把「当前数值」翻译成一套具体的优先级方案。

设计要点：
- 规则表达式用受限求值（只允许比较/布尔/算术/列表），不调用任何函数，配置文件被改坏也跑不出危险操作。
- 规则从上往下匹配，第一条成立即返回，所以特殊规则写在前面。
- 方案里的字段不写死在代码里，加字段不用改代码。
"""
import ast
import os

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
STRATEGY_PATH = os.path.join(ROOT, "config", "strategy.yaml")
METRICS_PATH = os.path.join(ROOT, "knowledge", "metrics.yaml")


class UnsafeExpression(Exception):
    pass


_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.Name, ast.Load, ast.Constant, ast.List, ast.Tuple,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.IfExp,
)


def safe_eval(expr, ctx):
    """只允许安全的表达式，任何越界写法直接抛错。"""
    expr = str(expr).strip()
    if expr.lower() in ("true", "yes"):
        return True
    if expr.lower() in ("false", "no"):
        return False
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise UnsafeExpression(f"不支持的语法 {type(node).__name__}：{expr}")
    return bool(_ev(tree.body, ctx))


def _ev(node, ctx):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in ctx:
            raise UnsafeExpression(f"未知变量 {node.id}，可用变量见 strategy.yaml 顶部注释")
        return ctx[node.id]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_ev(e, ctx) for e in node.elts]
    if isinstance(node, ast.BoolOp):
        vals = [_ev(v, ctx) for v in node.values]
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    if isinstance(node, ast.UnaryOp):
        v = _ev(node.operand, ctx)
        return (not v) if isinstance(node.op, ast.Not) else (-v)
    if isinstance(node, ast.BinOp):
        a, b = _ev(node.left, ctx), _ev(node.right, ctx)
        op = node.op
        if isinstance(op, ast.Add):
            return a + b
        if isinstance(op, ast.Sub):
            return a - b
        if isinstance(op, ast.Mult):
            return a * b
        if isinstance(op, ast.Div):
            return a / b if b else 0
        if isinstance(op, ast.Mod):
            return a % b if b else 0
    if isinstance(node, ast.Compare):
        left = _ev(node.left, ctx)
        for op, comp in zip(node.ops, node.comparators):
            right = _ev(comp, ctx)
            ok = (
                (isinstance(op, ast.Eq) and left == right)
                or (isinstance(op, ast.NotEq) and left != right)
                or (isinstance(op, ast.Lt) and left < right)
                or (isinstance(op, ast.LtE) and left <= right)
                or (isinstance(op, ast.Gt) and left > right)
                or (isinstance(op, ast.GtE) and left >= right)
                or (isinstance(op, ast.In) and left in right)
                or (isinstance(op, ast.NotIn) and left not in right)
            )
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return _ev(node.body if _ev(node.test, ctx) else node.orelse, ctx)
    raise UnsafeExpression(f"无法求值：{type(node).__name__}")


class Policy:
    def __init__(self, strategy_path=STRATEGY_PATH):
        self.path = strategy_path
        with open(strategy_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        self.rules = data.get("rules") or []
        self.plans = data.get("plans") or {}
        self.safety = data.get("safety") or {}
        missing = [r.get("plan") for r in self.rules if r.get("plan") not in self.plans]
        if missing:
            raise ValueError(f"规则引用了不存在的方案：{missing}")

    def pick(self, ctx):
        """返回 (命中规则id, 方案名, 方案内容, 全部规则的命中情况)。"""
        trace = []
        for rule in self.rules:
            try:
                ok = safe_eval(rule.get("when", "False"), ctx)
            except UnsafeExpression as e:
                trace.append((rule.get("id"), f"表达式错误: {e}"))
                continue
            trace.append((rule.get("id"), "命中" if ok else "未命中"))
            if ok:
                name = rule["plan"]
                return rule.get("id"), name, self.plans[name], trace
        raise RuntimeError("没有任何规则命中，请在 strategy.yaml 末尾加一条 when: True 的兜底规则")

    def describe(self, ctx):
        rid, name, plan, _ = self.pick(ctx)
        lines = [f"命中规则：{rid}  →  方案：{name}"]
        pri = plan.get("training_priority") or []
        lines.append("训练优先级：" + " > ".join(f"{i + 1}.{p}" for i, p in enumerate(pri)))
        for k, v in plan.items():
            if k != "training_priority":
                lines.append(f"  {k}: {v}")
        return "\n".join(lines)


def load_metrics(path=METRICS_PATH):
    with open(path, "r", encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("fields") or {}
