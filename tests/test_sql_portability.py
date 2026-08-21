"""No `conn.execute("... ?", ...)` may reach main.

Deliberately NOT in `test_postgres_dialect.py`: that module skips wholesale
unless a live Postgres is configured, and a guard that only runs when the thing
it guards against is already being exercised protects nothing. This one reads
SOURCE, so it runs on every machine and every CI job.

It exists because the bare placeholders were real. Fifty-one statements across
four modules passed `?` straight to the driver -- a syntax error on Postgres --
including three written the same week, in `state_service`. They were invisible
because nothing had ever pointed the engine at a Postgres server.
"""

from __future__ import annotations


class TestEveryStatementIsPortable:
    """A static guard, so a new `conn.execute("... ?", ...)` cannot reach main
    and only fail once somebody points the engine at Postgres.

    Runs on SQLite too -- it reads source, not a database -- so the protection
    does not depend on the CI job being present."""

    def test_no_bare_placeholder_survives(self):
        import ast
        import pathlib

        offenders = []
        root = pathlib.Path(__file__).resolve().parent.parent / "hivepilot"
        for f in sorted(root.rglob("*.py")):
            src = f.read_text(encoding="utf-8", errors="ignore")
            try:
                tree = ast.parse(src)
            except SyntaxError:  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "execute"
                    and node.args
                ):
                    continue
                arg = node.args[0]
                seg = ast.get_source_segment(src, arg) or ""
                if "?" not in seg:
                    continue
                if isinstance(arg, ast.Call) and (
                    getattr(arg.func, "id", "") == "ph" or getattr(arg.func, "attr", "") == "ph"
                ):
                    continue
                offenders.append(f"{f.relative_to(root.parent)}:{arg.lineno}")

        # SECOND blind spot, and the one that got through: SQL built by
        # concatenation puts the `?` in a VARIABLE, so the literal scan above
        # sees none and reports clean. `unresolved_pr_gate_outcomes` did
        # exactly that -- `conn.execute(sql + " ORDER BY ...", params)` -- and
        # this guard called the file portable while Postgres answered
        # "the query has 0 placeholders but 2 parameters were passed".
        #
        # So: any non-literal SQL passed WITH parameters must go through
        # `ph()`. `db.py` is exempt because the dialect branch lives there --
        # it is the thing being called, not a caller.
        for f in sorted(root.rglob("*.py")):
            if f.name == "db.py":
                continue
            src = f.read_text(encoding="utf-8", errors="ignore")
            try:
                tree = ast.parse(src)
            except SyntaxError:  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "execute"
                    and len(node.args) >= 2
                ):
                    continue
                arg = node.args[0]
                if isinstance(arg, (ast.Constant, ast.JoinedStr)):
                    continue
                if isinstance(arg, ast.Call) and (
                    getattr(arg.func, "id", "") == "ph" or getattr(arg.func, "attr", "") == "ph"
                ):
                    continue
                offenders.append(f"{f.relative_to(root.parent)}:{arg.lineno} (built SQL)")

        assert offenders == [], (
            "these pass `?` straight to the driver, which is a syntax error on "
            f"Postgres -- wrap the SQL in ph(): {offenders}"
        )
