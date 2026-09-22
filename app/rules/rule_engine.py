"""Data-driven rule engine.

A rule's `condition` is JSON (never code):

    {"all": [ {"metric": "projected_min", "op": "<", "value": "safety_stock"}, ... ]}
    {"any": [ ... ]}

`value` may be a literal or the *name of another metric* in the context, so
"projected inventory < safety stock" is expressible without code. There is no eval().
"""
from __future__ import annotations

import ast
import math
import operator
from typing import Any

OPS = {
    "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
    "==": operator.eq, "!=": operator.ne,
    "in": lambda a, b: a in b, "not_in": lambda a, b: a not in b,
}


def _resolve(v: Any, ctx: dict):
    if isinstance(v, str) and v in ctx:
        return ctx[v]
    return v


def eval_condition(cond: dict, ctx: dict) -> tuple[bool, list[dict]]:
    """Returns (matched, evidence[]) where evidence lists each leaf test with actual values."""
    if not cond:
        return False, []
    evidence: list[dict] = []

    def leaf(c) -> bool:
        m, op, want = c["metric"], c["op"], c.get("value")
        if op not in OPS:
            raise ValueError(f"Unsupported operator '{op}'")
        actual = ctx.get(m)
        want_v = _resolve(want, ctx)
        if actual is None or want_v is None:
            evidence.append({"metric": m, "op": op, "expected": want, "actual": actual, "ok": False, "note": "missing"})
            return False
        if isinstance(actual, float) and math.isnan(actual):
            return False
        try:
            ok = bool(OPS[op](actual, want_v))
        except TypeError:
            ok = False
        evidence.append({"metric": m, "op": op, "expected": want_v if want != want_v else want,
                         "actual": actual, "ok": ok})
        return ok

    def node(c) -> bool:
        if "all" in c:
            return all(node(x) for x in c["all"])
        if "any" in c:
            return any(node(x) for x in c["any"])
        return leaf(c)

    matched = node(cond)
    return matched, evidence


def severity_for(rule_severity: dict, ctx: dict) -> str:
    """rule.severity = {"default": "MEDIUM", "when": [{"metric":..,"op":..,"value":..,"severity":"HIGH"}, ...]}
    The highest severity among matching `when` clauses wins."""
    order = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    sev = (rule_severity or {}).get("default", "MEDIUM")
    for w in (rule_severity or {}).get("when", []):
        ok, _ = eval_condition({"metric": w["metric"], "op": w["op"], "value": w.get("value")}, ctx)
        if ok and order.index(w["severity"]) > order.index(sev):
            sev = w["severity"]
    return sev


# ---------------------------------------------------------------------------------------------------------------------
# Safe arithmetic expression evaluator (KPI formulas). Only + - * / ** , unary -, numbers, names, and a tiny
# whitelist of functions. This is what lets administrators edit KPI formulas without any code execution.
# ---------------------------------------------------------------------------------------------------------------------
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod}
_FUNCS = {"min": min, "max": max, "abs": abs, "sqrt": math.sqrt, "log": math.log}


class FormulaError(ValueError):
    pass


def safe_eval(expr: str, names: dict[str, float]) -> float:
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as e:
        raise FormulaError(f"Invalid formula syntax: {e.msg}") from e

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.Name):
            if n.id not in names:
                raise FormulaError(f"Unknown measure '{n.id}'")
            return names[n.id]
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            r = ev(n.right)
            if isinstance(n.op, ast.Div) and r == 0:
                raise ZeroDivisionError
            return _BIN[type(n.op)](ev(n.left), r)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            return -ev(n.operand) if isinstance(n.op, ast.USub) else ev(n.operand)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
            return _FUNCS[n.func.id](*[ev(a) for a in n.args])
        raise FormulaError(f"Disallowed expression element: {type(n).__name__}")

    return float(ev(tree))
