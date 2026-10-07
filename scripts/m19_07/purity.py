"""Isolated evaluation of a whitelisted pure subset of an untrusted source file.

`AGENTS.md` section 9 treats host source as untrusted input, so the pinned native H3 module is
parsed and never imported: importing it would execute `torch`, `comfy` and `torchaudio` inside the
harness process. But a qualification harness that re-types the source's arithmetic into its own
module is not measuring the source at all — it is asserting an answer and then agreeing with itself.

The way out is to compile a narrow, explicitly whitelisted subset of the source's own pure functions
in an isolated namespace. A function qualifies only if every node in its tree is on the allow list:
no imports, no attribute access, no subscripts, no lambdas, no comprehensions, no exception
handling, and calls only to a fixed set of builtins plus its declared siblings.

Loops are the one construct that survives a purity check and can still hang the harness, so every
`while` in an admitted function is rewritten with an injected iteration counter before it is
compiled. The bound is deliberately far above any legitimate value: the point is to convert an
unexpected non-terminating source into a loud failure, not to second-guess the source's arithmetic.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from typing import Any

#: The only callables a whitelisted function may invoke. `round` matters most: the source's audio
#: latent join is a rounding decision, and substituting a different rounding rule here would
#: silently change the qualification result.
SAFE_BUILTINS: Mapping[str, Any] = {
    "abs": abs,
    "int": int,
    "max": max,
    "min": min,
    "round": round,
}

#: Largest exponent an admitted function may raise to. `**` is the one arithmetic operator that
#: turns a short expression into an unbounded computation, and the threat model here is a corrupted
#: or hostile pinned source, so it is bounded for the same reason loops are: to convert a hang
#: into a loud failure. The pinned source uses no exponentiation at all, so the bound costs nothing.
MAX_POW_EXPONENT = 64

#: Iteration ceiling injected into every admitted `while`. The pinned source's frame alignment loop
#: terminates within 17 iterations by construction; six orders of magnitude of headroom keeps the
#: guard from ever being the thing that decides a qualification result.
LOOP_ITERATION_LIMIT = 1_000_000

_GUARD_NAME = "__m19_07_loop_guard__"
_GUARD_ERROR = "__m19_07_loop_error__"

_ALLOWED_NODES: frozenset[type[ast.AST]] = frozenset(
    {
        ast.Add,
        ast.And,
        ast.Assign,
        ast.AugAssign,
        ast.BinOp,
        ast.BoolOp,
        ast.Call,
        ast.Compare,
        ast.Constant,
        ast.Div,
        ast.Eq,
        ast.Expr,
        ast.FloorDiv,
        ast.FunctionDef,
        ast.Gt,
        ast.GtE,
        ast.If,
        ast.IfExp,
        ast.List,
        ast.Load,
        ast.Lt,
        ast.LtE,
        ast.Mod,
        ast.Mult,
        ast.Name,
        ast.Not,
        ast.NotEq,
        ast.Or,
        ast.Pass,
        ast.Pow,
        ast.Return,
        ast.Store,
        ast.Sub,
        ast.Tuple,
        ast.UAdd,
        ast.USub,
        ast.UnaryOp,
        ast.While,
        ast.arg,
        ast.arguments,
        ast.expr_context,
    }
)


def _bound_names(node: ast.AST) -> set[str]:
    """Names the function itself introduces: its parameters and anything it assigns."""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.arg):
            names.add(child.arg)
        elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
            names.add(child.id)
        elif isinstance(child, ast.FunctionDef):
            names.add(child.name)
    return names


class ImpureSourceError(ValueError):
    """A source construct outside the whitelisted pure subset.

    Raised rather than skipped: a function the harness cannot evaluate safely must stop the
    derivation, not fall back to a hard-coded value that would look like a measurement.
    """


class LoopBudgetExceeded(RuntimeError):
    """An admitted function exceeded the injected iteration ceiling."""


def assert_pure(
    node: ast.AST,
    *,
    label: str,
    allowed_calls: frozenset[str] = frozenset(),
    allowed_names: frozenset[str] = frozenset(),
) -> None:
    """Reject anything outside the allow list, naming the first offending construct.

    `allowed_calls` carries the sibling functions admitted in the same batch. The pinned source's
    temporal helper calls its own alignment and latent-extent helpers, and refusing those would
    force the harness to re-implement exactly the arithmetic it exists to measure.

    `allowed_names` closes a gap a reviewer found: restricting only the *target of a call* left bare
    name reads unrestricted, so a function could read `__builtins__` and hold it. Nothing
    exploitable followed, because the execution namespace contains only the five safe builtins,
    but that made the safety property depend on how `compile_pure_functions` happens to build its
    namespace rather than on the checker that claims to enforce it. The checker now enforces it.
    """
    callable_names = set(SAFE_BUILTINS) | set(allowed_calls)
    readable = callable_names | set(allowed_names) | _bound_names(node)
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if not isinstance(func, ast.Name) or func.id not in callable_names:
                rendered = ast.unparse(func) if isinstance(func, ast.expr) else type(func).__name__
                raise ImpureSourceError(f"{label}: call to non-whitelisted target {rendered!r}")
            if child.keywords:
                raise ImpureSourceError(f"{label}: keyword arguments are not evaluated")
            continue
        if isinstance(child, ast.arguments):
            if child.defaults or child.kw_defaults or child.vararg or child.kwarg:
                raise ImpureSourceError(f"{label}: only plain positional parameters are evaluated")
            continue
        if isinstance(child, ast.FunctionDef):
            if child.decorator_list:
                raise ImpureSourceError(f"{label}: decorators are not evaluated")
            continue
        if isinstance(child, ast.BinOp) and isinstance(child.op, ast.Pow):
            right = child.right
            if not isinstance(right, ast.Constant) or not isinstance(right.value, int):
                raise ImpureSourceError(f"{label}: exponent must be an integer literal")
            if abs(right.value) > MAX_POW_EXPONENT:
                raise ImpureSourceError(
                    f"{label}: exponent {right.value} exceeds the {MAX_POW_EXPONENT} bound"
                )
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            if child.id not in readable:
                raise ImpureSourceError(f"{label}: reads undeclared name {child.id!r}")
        if type(child) not in _ALLOWED_NODES:
            raise ImpureSourceError(f"{label}: disallowed construct {type(child).__name__}")


def safe_constant(node: ast.expr, *, known: Mapping[str, Any], label: str) -> Any:
    """Evaluate a module-level constant expression without executing the module.

    `ast.literal_eval` is not enough, because the source expresses derived bounds as arithmetic over
    literals and over constants defined earlier in the same module.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in known:
            raise ImpureSourceError(f"{label}: unknown name {node.id!r}")
        return known[node.id]
    if isinstance(node, ast.UnaryOp) and type(node.op) in {ast.USub, ast.UAdd}:
        operand = safe_constant(node.operand, known=known, label=label)
        return -operand if isinstance(node.op, ast.USub) else +operand
    if isinstance(node, ast.BinOp):
        left = safe_constant(node.left, known=known, label=label)
        right = safe_constant(node.right, known=known, label=label)
        op = type(node.op)
        if op is ast.Add:
            return left + right
        if op is ast.Sub:
            return left - right
        if op is ast.Mult:
            return left * right
        if op is ast.Div:
            return left / right
        if op is ast.FloorDiv:
            return left // right
        if op is ast.Mod:
            return left % right
        raise ImpureSourceError(f"{label}: disallowed operator {op.__name__}")
    raise ImpureSourceError(f"{label}: not a constant expression ({type(node).__name__})")


def _bound_loops(definition: ast.FunctionDef) -> ast.FunctionDef:
    """Inject an iteration ceiling into every `while` in an already-admitted definition.

    The rewrite happens after the purity check, so the injected `raise` never has to be added to the
    allow list. Semantics of the source's own arithmetic are untouched; only non-termination
    changes, from a hang into `LoopBudgetExceeded`.
    """
    loops = [node for node in ast.walk(definition) if isinstance(node, ast.While)]
    if not loops:
        return definition

    for loop in loops:
        guard = ast.AugAssign(
            target=ast.Name(id=_GUARD_NAME, ctx=ast.Store()),
            op=ast.Add(),
            value=ast.Constant(value=1),
        )
        check = ast.If(
            test=ast.Compare(
                left=ast.Name(id=_GUARD_NAME, ctx=ast.Load()),
                ops=[ast.Gt()],
                comparators=[ast.Constant(value=LOOP_ITERATION_LIMIT)],
            ),
            body=[
                ast.Raise(
                    exc=ast.Call(
                        func=ast.Name(id=_GUARD_ERROR, ctx=ast.Load()),
                        args=[ast.Constant(value=definition.name)],
                        keywords=[],
                    ),
                    cause=None,
                )
            ],
            orelse=[],
        )
        loop.body = [guard, check, *loop.body]

    definition.body.insert(
        0,
        ast.Assign(
            targets=[ast.Name(id=_GUARD_NAME, ctx=ast.Store())],
            value=ast.Constant(value=0),
        ),
    )
    return definition


def compile_pure_functions(
    definitions: Mapping[str, ast.FunctionDef],
    *,
    constants: Mapping[str, Any],
) -> dict[str, Callable[..., Any]]:
    """Compile whitelisted definitions into one shared, isolated namespace.

    The functions are compiled together because they call each other. The namespace carries the
    source's own constants and nothing else, so a function that reaches for anything the source did
    not define fails loudly at call time instead of silently resolving to a harness value.
    """
    siblings = frozenset(definitions)
    known = frozenset(constants)
    for name, definition in definitions.items():
        assert_pure(definition, label=name, allowed_calls=siblings, allowed_names=known)

    bounded: list[ast.stmt] = [_bound_loops(definition) for definition in definitions.values()]
    module = ast.Module(body=bounded, type_ignores=[])
    ast.fix_missing_locations(module)

    namespace: dict[str, Any] = dict(constants)
    namespace["__builtins__"] = dict(SAFE_BUILTINS)
    namespace[_GUARD_ERROR] = LoopBudgetExceeded
    exec(compile(module, filename="<pinned-native-source>", mode="exec"), namespace)  # noqa: S102

    compiled = {name: namespace[name] for name in definitions}
    for name, value in compiled.items():
        if not callable(value):
            raise ImpureSourceError(f"{name}: did not compile to a callable")
    return compiled
