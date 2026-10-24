"""Every SQL statement must be a literal with bound parameters.

The audit found no injection — this keeps it that way. A future edit that
builds SQL by string formatting fails here instead of shipping, even if the
interpolated value happens to be safe at the time.

Checked by parsing, not grepping, so it can't be fooled by formatting.
"""

import ast
import os

SOURCES = ("app.py", "telegram_bot.py")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# the query is always the first positional argument to these
SQL_METHODS = {"execute", "executemany", "executescript"}


def sql_call_args():
    """(file, lineno, first-arg-node) for every .execute*/ call in the app."""
    for name in SOURCES:
        tree = ast.parse(open(os.path.join(ROOT, name)).read(), filename=name)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in SQL_METHODS
                    and node.args):
                yield name, node.lineno, node.args[0]


def test_there_is_sql_to_check():
    """Guard against the scan silently matching nothing."""
    assert len(list(sql_call_args())) > 30


def test_no_sql_is_built_by_string_formatting():
    offenders = []
    for name, lineno, arg in sql_call_args():
        # an f-string
        if isinstance(arg, ast.JoinedStr):
            offenders.append(f"{name}:{lineno} f-string")
        # "..." + x  or  "..." % x
        elif isinstance(arg, ast.BinOp) and isinstance(arg.op, (ast.Add, ast.Mod)):
            offenders.append(f"{name}:{lineno} {'+' if isinstance(arg.op, ast.Add) else '%'} concat")
        # "...".format(x)
        elif (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute)
                and arg.func.attr == "format"):
            offenders.append(f"{name}:{lineno} .format()")
    assert not offenders, (
        "SQL must be a literal with bound parameters, not built by formatting:\n  "
        + "\n  ".join(offenders))


def test_every_sql_query_is_a_plain_string_literal():
    """Belt and braces: the query argument is always a constant or a name
    holding one (adjacent string literals concatenate to a single Constant)."""
    for name, lineno, arg in sql_call_args():
        assert isinstance(arg, (ast.Constant, ast.Name)), (
            f"{name}:{lineno} passes a {type(arg).__name__} as the query")
        if isinstance(arg, ast.Constant):
            assert isinstance(arg.value, str), f"{name}:{lineno} non-string query"
