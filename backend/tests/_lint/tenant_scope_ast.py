"""AST scanner: flags `select(TenantModel)` queries missing a tenant filter.

Used by ``tests/test_tenant_scope_guard.py`` (#122 / zeiterfassung-0fkw)
to catch a future cross-tenant leak *before* it ships: any SQLAlchemy
``select()``/``select_from()`` root query for a model that owns a
``tenant_id`` column must, within the same query-building chain, either

- compare ``Model.tenant_id`` directly (``.where(Model.tenant_id == ...)``,
  any RHS variable name), or
- be built via the ``tenant_scoped()`` / ``get_scoped()`` helper
  (``zeiterfassung.db.scoping``, PR #142), which applies the filter
  internally and therefore shows no literal ``.tenant_id ==`` at the call
  site.

This is a best-effort static heuristic over a single function body, not a
full dataflow/type analysis -- see ``tests/test_tenant_scope_guard.py`` for
the explicit, commented allowlist of the legitimately-global cases it would
otherwise (correctly, from a purely syntactic view) flag.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Violation:
    file: str
    lineno: int
    model: str
    snippet: str


def discover_tenant_models() -> set[str]:
    """ORM model class names with their OWN `tenant_id` column.

    Introspected live from SQLAlchemy mappers (``Base.registry.mappers`` /
    ``mapper.local_table``) rather than hand-maintained, so it self-updates
    as models are added or change -- a model gaining a `tenant_id` column
    is automatically covered by the guard on the next test run.
    """
    import zeiterfassung.models  # noqa: F401 - registers all mappers on Base.metadata
    from zeiterfassung.models.base import Base

    tenant_models: set[str] = set()
    for mapper in Base.registry.mappers:
        table = mapper.local_table
        if table is not None and "tenant_id" in table.columns:
            tenant_models.add(mapper.class_.__name__)
    return tenant_models


def _collect_model_aliases(tree: ast.AST) -> dict[str, str]:
    """Map local import alias -> canonical model class name.

    Several services do local, aliased imports to dodge shadowing
    (``from zeiterfassung.models.user import User as _User``) -- without
    resolving these the scanner would silently miss those call sites.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and "zeiterfassung.models" in node.module
        ):
            for alias in node.names:
                local = alias.asname or alias.name
                aliases[local] = alias.name
    return aliases


def _model_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _model_name(node.value)
    return None


def _resolve_model(
    name: str | None, tenant_models: set[str], aliases: dict[str, str]
) -> str | None:
    if name is None:
        return None
    if name in tenant_models:
        return name
    canon = aliases.get(name)
    if canon in tenant_models:
        return canon
    return None


def _find_query_roots(
    node: ast.AST, tenant_models: set[str], aliases: dict[str, str]
) -> list[tuple[ast.Call, str]]:
    """Find `select(Model)` / `select(...).select_from(Model)` root calls for
    tenant-scoped models anywhere within `node`."""
    found: list[tuple[ast.Call, str]] = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        func = n.func
        if isinstance(func, ast.Name) and func.id == "select" and n.args:
            model = _resolve_model(_model_name(n.args[0]), tenant_models, aliases)
            if model:
                found.append((n, model))
        elif isinstance(func, ast.Attribute) and func.attr == "select_from" and n.args:
            model = _resolve_model(_model_name(n.args[0]), tenant_models, aliases)
            if model:
                found.append((n, model))
    return found


def _chain_base_name(value: ast.AST) -> str | None:
    """Leftmost bare-Name base of a fluent method-call chain, if any.

    `stmt.where(...)` -> 'stmt' (continuation of a tracked variable).
    `select(...)` / `tenant_scoped(...)` -> None (fresh query root; the
    call's own Name is the callee, not an object being chained on).
    """
    node = value
    while isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute):
            node = func.value
        else:
            return None
    if isinstance(node, ast.Name):
        return node.id
    return None


def _own_expr_nodes(stmt: ast.stmt) -> list[ast.AST]:
    """The expression subtree(s) belonging to THIS statement, excluding any
    nested statement blocks (`body`/`orelse`/`finalbody`/handlers) -- those
    are separately visited by `_iter_body_stmts` and must not be
    double-scanned (a compound statement's `ast.walk` otherwise re-walks
    its entire nested body too, double-counting any query inside it).
    """
    if isinstance(stmt, (ast.If, ast.While)):
        return [stmt.test]
    if isinstance(stmt, (ast.For, ast.AsyncFor)):
        return [stmt.iter]
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return [item.context_expr for item in stmt.items]
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Try)):
        # Nested defs are scanned separately as their own top-level function;
        # Try's own "body" is entirely delegated to body/handlers/orelse/finalbody.
        return []
    # Simple statements (Expr, Return, Assert, Raise, Delete, AugAssign,
    # AnnAssign, tuple/attribute-target Assign, ...) -- the whole node IS the
    # expression(s).
    return [stmt]


_FILTER_HELPER_NAMES = {"tenant_scoped", "get_scoped"}


def _is_filtered(expr_node: ast.AST) -> bool:
    """True if `expr_node` visibly applies a tenant filter: either a direct
    `Model.tenant_id` attribute comparison (whatever the RHS variable is
    called -- ``tenant_id``, ``tid``, ``eff_tenant_id``, ...) or a call to
    the ``tenant_scoped()`` / ``get_scoped()`` helper
    (``zeiterfassung.db.scoping``, PR #142), which applies the filter
    internally and therefore never shows a literal ``.tenant_id ==`` at the
    call site.

    Deliberately AST-based (an `ast.Attribute` node with `.attr ==
    "tenant_id"`, or a `Call` to one of the helper names) rather than a
    source-text substring search -- avoids both false negatives (helper
    hides the filter; a short local alias like ``tid`` wouldn't textually
    contain "tenant_id") and false positives (a string literal or comment
    merely mentioning the word "tenant_id" would wrongly look "filtered"
    under a text search).
    """
    for node in ast.walk(expr_node):
        if isinstance(node, ast.Attribute) and node.attr == "tenant_id":
            return True
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name in _FILTER_HELPER_NAMES:
                return True
    return False


def _iter_body_stmts(stmts: list[ast.stmt]):
    """Yield statements in approximate sequential order, descending into
    control-flow blocks (if/for/while/with/try) but NOT into nested
    function/class bodies (those get their own top-level scan)."""
    for stmt in stmts:
        yield stmt
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for attr in ("body", "orelse", "finalbody"):
            block = getattr(stmt, attr, None)
            if block:
                yield from _iter_body_stmts(block)
        handlers = getattr(stmt, "handlers", None)
        if handlers:
            for handler in handlers:
                yield from _iter_body_stmts(handler.body)


def _scan_function(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    source: str,
    tenant_models: set[str],
    aliases: dict[str, str],
    filename: str,
) -> list[Violation]:
    """Best-effort per-function dataflow: tracks `var = select(Model)...`
    query-builder variables through `var = var.where(...)` reassignment
    chains (the dominant pattern in this codebase, e.g.
    ``stmt = tenant_scoped(select(User), User, tid); stmt =
    stmt.where(...)``) and checks whether a tenant filter was ever applied
    to the chain by the time the variable's last assignment in this
    function is reached.
    """
    violations: list[Violation] = []
    var_state: dict[str, dict] = {}

    def stmt_src(node: ast.AST) -> str:
        return ast.get_source_segment(source, node) or ""

    def make_violation(node: ast.AST, model: str) -> Violation:
        src = stmt_src(node)
        first_line = src.strip().splitlines()[0] if src.strip() else ""
        return Violation(
            file=filename, lineno=getattr(node, "lineno", 0), model=model, snippet=first_line[:120]
        )

    for stmt in _iter_body_stmts(func.body):
        is_simple_assign = (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
        )
        if is_simple_assign:
            value = stmt.value  # type: ignore[attr-defined]
            filtered_here = _is_filtered(value)
            target = stmt.targets[0].id  # type: ignore[union-attr]
            base = _chain_base_name(value)
            if base is not None and base in var_state:
                # Continuation of a tracked query-builder variable, e.g.
                # `stmt = stmt.where(...)` -- OR the filtered-state in, keep
                # tracking under the (possibly new) target name.
                entry = var_state.pop(base)
                entry["filtered"] = entry["filtered"] or filtered_here
                var_state[target] = entry
                continue
            models_in_expr = {m for _, m in _find_query_roots(value, tenant_models, aliases)}
            if models_in_expr:
                if target in var_state:
                    # Reusing the same variable name for an unrelated fresh
                    # query -- finalize the old one before it's lost.
                    old = var_state[target]
                    if not old["filtered"]:
                        violations.append(make_violation(old["node"], old["model"]))
                # Convention in this codebase: one query-root model per assignment.
                model = sorted(models_in_expr)[0]
                var_state[target] = {"model": model, "filtered": filtered_here, "node": stmt}
            elif target in var_state:
                # Reassigned to something unrelated -- finalize.
                old = var_state.pop(target)
                if not old["filtered"]:
                    violations.append(make_violation(old["node"], old["model"]))
            continue

        # Non-simple-assign statement: any FRESH query root found in the
        # statement's OWN expression (excluding nested statement blocks,
        # which get their own iteration) must be filtered within that same
        # expression -- these forms don't participate in the variable-chain
        # tracking above (e.g. `for u in (await db.execute(select(User)...))`,
        # `return await db.execute(select(User).where(...))`).
        for expr_node in _own_expr_nodes(stmt):
            filtered_here = _is_filtered(expr_node)
            for call_node, model in _find_query_roots(expr_node, tenant_models, aliases):
                if not filtered_here:
                    violations.append(make_violation(call_node, model))

    # End of function: any tracked query variable whose chain never applied a
    # tenant filter (regardless of where/whether it's eventually executed).
    for state in var_state.values():
        if not state["filtered"]:
            violations.append(make_violation(state["node"], state["model"]))

    return violations


def scan_source(source: str, filename: str, tenant_models: set[str]) -> list[Violation]:
    """Scan a source string (module or synthetic snippet) for unfiltered
    tenant-model queries. `filename` is only used for reporting."""
    tree = ast.parse(source, filename=filename)
    aliases = _collect_model_aliases(tree)
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            violations.extend(_scan_function(node, source, tenant_models, aliases, filename))
    return violations


def scan_file(
    path: Path, tenant_models: set[str], *, display_name: str | None = None
) -> list[Violation]:
    source = path.read_text(encoding="utf-8")
    return scan_source(source, display_name or str(path), tenant_models)


def scan_paths(
    paths: list[Path], tenant_models: set[str], *, root: Path | None = None
) -> list[Violation]:
    """Scan every `.py` file under each path in `paths`. Violation ``file``
    is reported relative to `root` (falls back to `path`) for stable,
    portable allowlist keys."""
    violations: list[Violation] = []
    for base in paths:
        for py_file in sorted(base.rglob("*.py")):
            display = str(py_file.relative_to(root)) if root else str(py_file)
            violations.extend(scan_file(py_file, tenant_models, display_name=display))
    return violations
