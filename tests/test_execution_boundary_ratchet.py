from __future__ import annotations

import ast
import hashlib
import json
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest


DIRECT_EXECUTION_BASELINE_SHA256 = "2936e0a41c44dad2f2e9f6b33ecfd8a8324ce67c398aa189bd172c3311b5a034"
DIRECT_EXECUTION_BASELINE_COUNTS = {
    "run_tcl": 82,
    "subprocess.run": 8,
    "subprocess.Popen": 1,
}
RUN_TCL_ALLOWED_FILES = {
    "src/vivado_agent_mcp/tools.py",
    "src/vivado_agent_mcp/vivado/session.py",
}
SUBPROCESS_ALLOWED_FILES = {
    "src/vivado_agent_mcp/release_identity.py",
    "src/vivado_agent_mcp/vivado/bootstrap.py",
    "src/vivado_agent_mcp/vivado/env.py",
    "src/vivado_agent_mcp/vivado/runtime_cache.py",
    "src/vivado_agent_mcp/vivado/session.py",
}
SUBPROCESS_CALL_NAMES = {
    "run",
    "Popen",
    "call",
    "check_call",
    "check_output",
    "getoutput",
    "getstatusoutput",
}
OS_PROCESS_CALL_NAMES = {"fork", "forkpty", "system", "popen", "startfile"}
ASYNCIO_EVENT_LOOP_PROCESS_CALL_NAMES = {
    "_make_subprocess_transport",
    "subprocess_exec",
    "subprocess_shell",
}
DYNAMIC_ATTRIBUTE_CALLABLE_KIND = "dynamic_attribute_callable"
PTY_PROCESS_CALL_NAMES = {"fork", "spawn"}
PLATFORM_PROCESS_MODULES = frozenset({"os", "posix", "nt"})
VALUE_FLOW_METHOD_NAMES = {
    "__getitem__",
    "__iter__",
    "copy",
    "get",
    "items",
    "keys",
    "pop",
    "popitem",
    "setdefault",
    "values",
}
DEFAULT_VARS_REFERENCES = frozenset(
    {"vars", "builtins.vars", "__builtins__.vars"}
)


def _direct_execution_records(paths: list[Path] | None = None) -> Counter[tuple[str, str, str]]:
    records: Counter[tuple[str, str, str]] = Counter()
    source_paths = sorted(paths) if paths is not None else sorted(Path("src").rglob("*.py"))
    for path in source_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative_path = path.as_posix()
        annotations_are_deferred = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "__future__"
            and any(alias.name == "annotations" for alias in node.names)
            for node in tree.body
        )
        function_stack: list[str] = []
        callable_alias_stack: list[dict[str, str]] = [{}]
        callable_shadow_stack: list[set[str]] = [set()]
        module_alias_stack: list[dict[str, str]] = [{}]
        module_shadow_stack: list[set[str]] = [set()]
        builtin_name_shadow_stack: list[set[str]] = [set()]
        builtin_getattr_alias_stack: list[set[str]] = [set()]
        builtin_getattr_shadow_stack: list[set[str]] = [set()]
        global_name_stack: list[set[str]] = [set()]
        nonlocal_name_stack: list[set[str]] = [set()]
        resolver_alias_stack: list[set[str]] = [set()]
        resolver_shadow_stack: list[set[str]] = [set()]
        importlib_alias_stack: list[set[str]] = [set()]
        importlib_shadow_stack: list[set[str]] = [set()]
        builtins_alias_stack: list[set[str]] = [set()]
        builtins_shadow_stack: list[set[str]] = [set()]
        vars_alias_stack: list[set[str]] = [set()]
        vars_shadow_stack: list[set[str]] = [set()]
        sys_alias_stack: list[set[str]] = [set()]
        sys_shadow_stack: list[set[str]] = [set()]
        module_registry_alias_stack: list[set[str]] = [set()]
        module_registry_shadow_stack: list[set[str]] = [set()]
        operator_alias_stack: list[set[str]] = [set()]
        operator_shadow_stack: list[set[str]] = [set()]
        operator_transform_alias_stack: list[dict[str, str]] = [{}]
        operator_transform_shadow_stack: list[set[str]] = [set()]
        functools_alias_stack: list[set[str]] = [set()]
        functools_shadow_stack: list[set[str]] = [set()]
        functools_partial_alias_stack: list[set[str]] = [set()]
        functools_partial_shadow_stack: list[set[str]] = [set()]
        qualified_vars_alias_stack: list[set[str]] = [set()]
        qualified_vars_exact_shadow_stack: list[set[str]] = [set()]
        qualified_vars_prefix_shadow_stack: list[set[str]] = [set()]
        receiver_dependent_vars_stack: list[dict[str, set[str]]] = [{}]
        alias_scope_kind_stack = ["module"]
        class_vars_export_stack: list[set[str]] = []
        class_builtin_getattr_export_stack: list[set[str]] = []
        class_scope_depth_stack: list[int] = []
        class_method_stack: list[
            list[ast.FunctionDef | ast.AsyncFunctionDef]
        ] = []
        class_reference_stack: list[str] = []
        recorded_sites: set[tuple[str, int, int, str]] = set()
        module_aliases = {
            "subprocess": {"subprocess"},
            "os": {"os"},
            "asyncio": {"asyncio", "asyncio.subprocess"},
            "asyncio_runner": set(),
            "asyncio_task": set(),
            "pty": {"pty"},
            "posix": {"posix"},
            "nt": {"nt"},
        }
        function_aliases: dict[str, str] = {}
        resolver_aliases = {"__import__"}
        importlib_aliases = {"importlib"}
        builtins_aliases = {"builtins", "__builtins__"}
        vars_aliases = {"vars"}
        sys_aliases = {"sys"}
        module_registry_aliases: set[str] = set()
        operator_aliases = {"operator"}
        operator_transform_aliases: dict[str, str] = {}
        functools_aliases = {"functools"}
        functools_cached_property_aliases: set[str] = set()
        functools_partial_aliases: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "importlib" or (
                        alias.name.startswith("importlib.") and alias.asname is None
                    ):
                        importlib_aliases.add(alias.asname or "importlib")
                    if alias.name == "builtins":
                        builtins_aliases.add(alias.asname or "builtins")
                    if alias.name == "sys":
                        sys_aliases.add(alias.asname or "sys")
                    if alias.name == "operator":
                        operator_aliases.add(alias.asname or "operator")
                    if alias.name == "functools":
                        functools_aliases.add(alias.asname or "functools")
                    module_kind = _imported_process_module_kind(alias.name)
                    if module_kind:
                        module_aliases[module_kind].add(alias.asname or alias.name)
                        if alias.asname is None:
                            module_aliases[module_kind].add(module_kind)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if node.module == "importlib" and alias.name == "import_module":
                        resolver_aliases.add(alias.asname or alias.name)
                    if node.module == "builtins" and alias.name == "__import__":
                        resolver_aliases.add(alias.asname or alias.name)
                    if node.module == "builtins" and alias.name == "vars":
                        vars_aliases.add(alias.asname or alias.name)
                    if node.module == "sys" and alias.name == "modules":
                        module_registry_aliases.add(alias.asname or alias.name)
                    if (
                        node.module == "operator"
                        and alias.name
                        in {"attrgetter", "methodcaller", "getitem", "itemgetter"}
                    ):
                        operator_transform_aliases[alias.asname or alias.name] = (
                            alias.name
                        )
                    if (
                        node.level == 0
                        and node.module == "functools"
                        and alias.name == "cached_property"
                    ):
                        functools_cached_property_aliases.add(
                            alias.asname or alias.name
                        )
                    if (
                        node.level == 0
                        and node.module == "functools"
                        and alias.name == "partial"
                    ):
                        functools_partial_aliases.add(alias.asname or alias.name)
                    if node.module == "builtins" and alias.name in {"eval", "exec"}:
                        function_aliases[alias.asname or alias.name] = (
                            f"dynamic_code.{alias.name}"
                        )
                    if node.module == "asyncio" and alias.name in {
                        "events",
                        "subprocess",
                    }:
                        module_aliases["asyncio"].add(alias.asname or alias.name)
                        continue
                    module_kind = _imported_process_module_kind(node.module)
                    if module_kind:
                        function_aliases[alias.asname or alias.name] = (
                            f"{module_kind}.{alias.name}"
                        )

        parent_nodes: dict[ast.AST, ast.AST] = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }

        def qualified_scope_reference(node: ast.AST) -> str:
            names: list[str] = []
            current: ast.AST | None = node
            while current is not None:
                if isinstance(
                    current,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                ):
                    names.append(current.name)
                current = parent_nodes.get(current)
            return ".".join(reversed(names))

        def callback_reference_candidates(
            reference: str,
            context: ast.AST | None,
        ) -> tuple[str, ...]:
            candidates: list[str] = []
            enclosing_function_seen = False
            current = parent_nodes.get(context) if context is not None else None
            while current is not None:
                if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    candidates.append(
                        f"{qualified_scope_reference(current)}.{reference}"
                    )
                    enclosing_function_seen = True
                elif isinstance(current, ast.ClassDef) and not enclosing_function_seen:
                    candidates.append(
                        f"{qualified_scope_reference(current)}.{reference}"
                    )
                current = parent_nodes.get(current)
            candidates.append(reference)
            return tuple(dict.fromkeys(candidates))

        def callback_binding_reference(reference: str, context: ast.AST) -> str:
            if "." in reference:
                return reference
            current = parent_nodes.get(context)
            while current is not None:
                if isinstance(
                    current,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                ):
                    return f"{qualified_scope_reference(current)}.{reference}"
                current = parent_nodes.get(current)
            return reference

        def invoked_inline_lambda_parameter_positions(
            value: ast.AST | None,
            operator_call_resolver: Callable[[ast.AST | None], bool],
        ) -> set[int]:
            if not isinstance(value, ast.Lambda):
                return set()
            positional_parameters = [
                *value.args.posonlyargs,
                *value.args.args,
            ]
            aliases: dict[str, set[int]] = {
                parameter.arg: {index}
                for index, parameter in enumerate(positional_parameters)
            }

            def referenced_positions(candidate: ast.AST | None) -> set[int]:
                if isinstance(candidate, ast.Name):
                    return set(aliases.get(candidate.id, set()))
                if isinstance(candidate, ast.Attribute):
                    reference = _dotted_name(candidate)
                    direct = aliases.get(reference or "", set())
                    if direct:
                        return set(direct)
                    if candidate.attr == "__call__":
                        return referenced_positions(candidate.value)
                    return set()
                if isinstance(candidate, ast.NamedExpr):
                    return referenced_positions(candidate.value)
                if isinstance(candidate, ast.IfExp):
                    return referenced_positions(
                        candidate.body
                    ) | referenced_positions(candidate.orelse)
                if isinstance(candidate, ast.BoolOp):
                    return set().union(
                        *(referenced_positions(item) for item in candidate.values)
                    )
                if isinstance(candidate, (ast.List, ast.Tuple, ast.Set)):
                    return set().union(
                        *(referenced_positions(item) for item in candidate.elts)
                    )
                if isinstance(candidate, ast.Dict):
                    return set().union(
                        *(
                            referenced_positions(item)
                            for item in (*candidate.keys, *candidate.values)
                            if item is not None
                        )
                    )
                if isinstance(candidate, (ast.Await, ast.Yield, ast.YieldFrom)):
                    return referenced_positions(candidate.value)
                if isinstance(candidate, ast.Subscript):
                    return referenced_positions(candidate.value)
                return set()

            changed = True
            while changed:
                changed = False
                for candidate in ast.walk(value.body):
                    if not isinstance(candidate, ast.NamedExpr):
                        continue
                    target_reference = _dotted_name(candidate.target)
                    if target_reference is None:
                        continue
                    positions = referenced_positions(candidate.value)
                    previous = aliases.setdefault(target_reference, set())
                    new_positions = positions - previous
                    if new_positions:
                        previous.update(new_positions)
                        changed = True

            invoked_positions: set[int] = set()
            pending = [value.body]
            while pending:
                candidate = pending.pop()
                if isinstance(candidate, ast.Lambda):
                    continue
                if isinstance(candidate, ast.Call):
                    if operator_call_resolver(candidate.func) and candidate.args:
                        invoked_positions.update(
                            referenced_positions(candidate.args[0])
                        )
                    invoked_reference = _dotted_name(candidate.func)
                    if (
                        isinstance(candidate.func, ast.Attribute)
                        and candidate.func.attr == "__call__"
                    ):
                        invoked_reference = _dotted_name(candidate.func.value)
                    if invoked_reference is not None:
                        invoked_positions.update(
                            aliases.get(invoked_reference, set())
                        )
                pending.extend(ast.iter_child_nodes(candidate))
            return invoked_positions

        callback_parameter_positions: dict[str, set[int]] = {}
        callback_parameter_names: dict[str, set[str]] = {}
        callback_projected_keyword_names: dict[str, set[str]] = {}
        event_loop_receiver_parameter_positions: dict[
            str,
            dict[int, set[str]],
        ] = {}
        event_loop_receiver_parameter_names: dict[
            str,
            dict[str, set[str]],
        ] = {}
        helper_parameter_positions: dict[str, dict[str, int]] = {}
        helper_call_argument_sources: dict[
            str,
            list[
                tuple[
                    ast.Call,
                    tuple[set[str], ...],
                    dict[str, set[str]],
                    set[str],
                    set[str],
                ]
            ],
        ] = {}
        helper_returned_calls: dict[str, set[ast.Call]] = {}
        returned_parameter_positions: dict[str, set[int]] = {}
        returned_parameter_names: dict[str, set[str]] = {}
        event_loop_property_return_values: dict[str, tuple[ast.AST, ...]] = {}
        callback_receiver_kinds: dict[str, str] = {}
        helper_definitions: list[
            tuple[ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda, str]
        ] = [
            (definition, qualified_scope_reference(definition))
            for definition in ast.walk(tree)
            if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for binding in ast.walk(tree):
            targets: list[ast.AST] = []
            value: ast.AST | None = None
            if isinstance(binding, ast.Assign):
                targets = list(binding.targets)
                value = binding.value
            elif isinstance(binding, ast.AnnAssign):
                targets = [binding.target]
                value = binding.value
            elif isinstance(binding, ast.NamedExpr):
                targets = [binding.target]
                value = binding.value
            if not isinstance(value, ast.Lambda):
                continue
            for target in targets:
                reference = _dotted_name(target)
                if reference is not None:
                    helper_definitions.append(
                        (
                            value,
                            callback_binding_reference(reference, binding),
                        )
                    )

        def bound_identifiers(target: ast.AST) -> set[str]:
            if isinstance(target, ast.Name):
                return {target.id}
            if isinstance(target, (ast.Tuple, ast.List)):
                return set().union(
                    *(bound_identifiers(item) for item in target.elts)
                )
            if isinstance(target, ast.Starred):
                return bound_identifiers(target.value)
            return set()

        def callback_executor_scope_info(
            scope: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
        ) -> tuple[
            set[str],
            set[str],
            set[str],
            set[str],
            set[str],
            set[str],
            set[str],
            set[str],
            set[str],
            set[str],
        ]:
            bound: set[str] = set()
            executor_names: set[str] = set()
            map_names: set[str] = set()
            itertools_names: set[str] = set()
            functools_names: set[str] = set()
            builtins_names: set[str] = set()
            dict_names: set[str] = set()
            operator_call_names: set[str] = set()
            operator_names: set[str] = set()
            secondary_executor_names: set[str] = set()
            if isinstance(scope, ast.Module):
                pending: list[ast.AST] = list(scope.body)
            elif isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                pending = list(scope.body)
                bound.update(
                    parameter.arg
                    for parameter in (
                        *scope.args.posonlyargs,
                        *scope.args.args,
                        *scope.args.kwonlyargs,
                    )
                )
                if scope.args.vararg is not None:
                    bound.add(scope.args.vararg.arg)
                if scope.args.kwarg is not None:
                    bound.add(scope.args.kwarg.arg)
            else:
                pending = [scope.body]
                bound.update(
                    parameter.arg
                    for parameter in (
                        *scope.args.posonlyargs,
                        *scope.args.args,
                        *scope.args.kwonlyargs,
                    )
                )
                if scope.args.vararg is not None:
                    bound.add(scope.args.vararg.arg)
                if scope.args.kwarg is not None:
                    bound.add(scope.args.kwarg.arg)

            while pending:
                candidate = pending.pop()
                if isinstance(
                    candidate,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                ):
                    bound.add(candidate.name)
                    continue
                if isinstance(candidate, ast.Lambda):
                    continue
                if isinstance(
                    candidate,
                    (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp),
                ):
                    continue
                if isinstance(candidate, ast.Import):
                    for alias in candidate.names:
                        bound_name = alias.asname or alias.name.partition(".")[0]
                        bound.add(bound_name)
                        if alias.name == "itertools":
                            itertools_names.add(bound_name)
                        elif alias.name == "functools":
                            functools_names.add(bound_name)
                        elif alias.name == "builtins":
                            builtins_names.add(bound_name)
                        elif alias.name == "operator":
                            operator_names.add(bound_name)
                    continue
                if isinstance(candidate, ast.ImportFrom):
                    for alias in candidate.names:
                        if alias.name == "*":
                            if candidate.module == "itertools":
                                executor_names.update(
                                    {
                                        "dropwhile",
                                        "filterfalse",
                                        "starmap",
                                        "takewhile",
                                    }
                                )
                                secondary_executor_names.update(
                                    {"accumulate", "groupby"}
                                )
                            elif candidate.module == "functools":
                                executor_names.add("reduce")
                            elif candidate.module == "builtins":
                                executor_names.update({"filter", "map"})
                                map_names.add("map")
                                dict_names.add("dict")
                            elif candidate.module == "operator":
                                operator_call_names.add("call")
                            continue
                        bound_name = alias.asname or alias.name
                        bound.add(bound_name)
                        if (
                            candidate.module == "itertools"
                            and alias.name
                            in {
                                "dropwhile",
                                "filterfalse",
                                "starmap",
                                "takewhile",
                            }
                        ) or (
                            candidate.module == "functools"
                            and alias.name == "reduce"
                        ) or (
                            candidate.module == "builtins"
                            and alias.name in {"filter", "map"}
                        ):
                            executor_names.add(bound_name)
                            if candidate.module == "builtins" and alias.name == "map":
                                map_names.add(bound_name)
                        if candidate.module == "operator" and alias.name == "call":
                            operator_call_names.add(bound_name)
                        if (
                            candidate.module == "itertools"
                            and alias.name in {"accumulate", "groupby"}
                        ):
                            secondary_executor_names.add(bound_name)
                        if candidate.module == "builtins" and alias.name == "dict":
                            dict_names.add(bound_name)
                    continue
                if isinstance(candidate, ast.Assign):
                    for target in candidate.targets:
                        bound.update(bound_identifiers(target))
                    pending.append(candidate.value)
                    continue
                if isinstance(candidate, ast.AnnAssign):
                    bound.update(bound_identifiers(candidate.target))
                    if candidate.value is not None:
                        pending.append(candidate.value)
                    continue
                if isinstance(candidate, ast.NamedExpr):
                    bound.update(bound_identifiers(candidate.target))
                    pending.append(candidate.value)
                    continue
                if isinstance(candidate, (ast.For, ast.AsyncFor)):
                    bound.update(bound_identifiers(candidate.target))
                    pending.extend([candidate.iter, *candidate.body, *candidate.orelse])
                    continue
                pending.extend(ast.iter_child_nodes(candidate))
            return (
                bound,
                executor_names,
                map_names,
                itertools_names,
                functools_names,
                builtins_names,
                dict_names,
                operator_call_names,
                operator_names,
                secondary_executor_names,
            )

        def callback_executor_scope_chain(
            definition: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
        ) -> tuple[
            ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
            ...,
        ]:
            enclosing: list[
                ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
            ] = []
            current = parent_nodes.get(definition)
            while current is not None:
                if isinstance(
                    current,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda),
                ):
                    enclosing.append(current)
                current = parent_nodes.get(current)
            return (tree, *reversed(enclosing), definition)

        for definition, contract_reference in helper_definitions:
            positional_parameters = [
                *definition.args.posonlyargs,
                *definition.args.args,
            ]
            parameter_positions = {
                parameter.arg: index
                for index, parameter in enumerate(positional_parameters)
            }
            if definition.args.vararg is not None:
                parameter_positions[definition.args.vararg.arg] = len(
                    positional_parameters
                )
            parameter_names = {
                *parameter_positions,
                *(parameter.arg for parameter in definition.args.kwonlyargs),
                *(
                    (definition.args.kwarg.arg,)
                    if definition.args.kwarg is not None
                    else ()
                ),
            }
            helper_parameter_positions[contract_reference] = dict(
                parameter_positions
            )
            scoped_executor_names: set[str] = set()
            scoped_map_names: set[str] = set()
            scoped_itertools_names: set[str] = set()
            scoped_functools_names: set[str] = set()
            scoped_builtins_names: set[str] = {"builtins", "__builtins__"}
            scoped_dict_names: set[str] = set()
            scoped_operator_call_names: set[str] = set()
            scoped_operator_names: set[str] = set()
            scoped_secondary_executor_names: set[str] = set()
            shadowed_builtins: set[str] = set()
            for scope in callback_executor_scope_chain(definition):
                (
                    bound_names,
                    imported_executor_names,
                    imported_map_names,
                    imported_itertools_names,
                    imported_functools_names,
                    imported_builtins_names,
                    imported_dict_names,
                    imported_operator_call_names,
                    imported_operator_names,
                    imported_secondary_executor_names,
                ) = callback_executor_scope_info(scope)
                for name in bound_names:
                    scoped_executor_names.discard(name)
                    scoped_map_names.discard(name)
                    scoped_itertools_names.discard(name)
                    scoped_functools_names.discard(name)
                    scoped_builtins_names.discard(name)
                    scoped_dict_names.discard(name)
                    scoped_operator_call_names.discard(name)
                    scoped_operator_names.discard(name)
                    scoped_secondary_executor_names.discard(name)
                    if name in {"dict", "filter", "map"}:
                        shadowed_builtins.add(name)
                scoped_executor_names.update(imported_executor_names)
                scoped_map_names.update(imported_map_names)
                scoped_itertools_names.update(imported_itertools_names)
                scoped_functools_names.update(imported_functools_names)
                scoped_builtins_names.update(imported_builtins_names)
                scoped_dict_names.update(imported_dict_names)
                scoped_operator_call_names.update(imported_operator_call_names)
                scoped_operator_names.update(imported_operator_names)
                scoped_secondary_executor_names.update(
                    imported_secondary_executor_names
                )
                shadowed_builtins.difference_update(
                    (imported_executor_names & {"filter", "map"})
                    | imported_dict_names
                )

            def current_positional_callback_executor_reference(
                value: ast.AST | None,
            ) -> bool:
                reference = _dotted_name(value)
                if reference in scoped_executor_names:
                    return True
                if (
                    isinstance(value, ast.Name)
                    and value.id in {"filter", "map"}
                    and value.id not in shadowed_builtins
                ):
                    return True
                if not isinstance(value, ast.Attribute):
                    return False
                owner = _dotted_name(value.value)
                if owner in scoped_builtins_names and value.attr in {"filter", "map"}:
                    return True
                if owner in scoped_functools_names and value.attr == "reduce":
                    return True
                return owner in scoped_itertools_names and value.attr in {
                    "dropwhile",
                    "filterfalse",
                    "starmap",
                    "takewhile",
                }

            def current_builtin_map_reference(value: ast.AST | None) -> bool:
                reference = _dotted_name(value)
                if reference in scoped_map_names:
                    return True
                if (
                    isinstance(value, ast.Name)
                    and value.id == "map"
                    and "map" not in shadowed_builtins
                ):
                    return True
                return (
                    isinstance(value, ast.Attribute)
                    and value.attr == "map"
                    and _dotted_name(value.value) in scoped_builtins_names
                )

            def current_builtin_dict_reference(value: ast.AST | None) -> bool:
                reference = _dotted_name(value)
                if reference in scoped_dict_names:
                    return True
                if (
                    isinstance(value, ast.Name)
                    and value.id == "dict"
                    and "dict" not in shadowed_builtins
                ):
                    return True
                return (
                    isinstance(value, ast.Attribute)
                    and value.attr == "dict"
                    and _dotted_name(value.value) in scoped_builtins_names
                )

            def current_operator_call_reference(value: ast.AST | None) -> bool:
                reference = _dotted_name(value)
                if reference in scoped_operator_call_names:
                    return True
                return (
                    isinstance(value, ast.Attribute)
                    and value.attr == "call"
                    and _dotted_name(value.value) in scoped_operator_names
                )

            def current_secondary_callback_arguments(
                call: ast.Call,
            ) -> tuple[ast.AST, ...]:
                reference = _dotted_name(call.func)
                secondary_executor = reference in scoped_secondary_executor_names
                if isinstance(call.func, ast.Attribute):
                    secondary_executor = secondary_executor or (
                        _dotted_name(call.func.value) in scoped_itertools_names
                        and call.func.attr in {"accumulate", "groupby"}
                    )
                if not secondary_executor:
                    return ()
                arguments: list[ast.AST] = []
                if len(call.args) > 1:
                    arguments.append(call.args[1])

                callback_keywords = ("func", "key")

                def merge_keyword_candidates(
                    base: dict[str, tuple[ast.AST, ...]],
                    overlay: dict[str, tuple[ast.AST, ...]],
                    overlay_definite: frozenset[str],
                ) -> dict[str, tuple[ast.AST, ...]]:
                    merged = dict(base)
                    for keyword_name in callback_keywords:
                        if keyword_name in overlay_definite:
                            merged[keyword_name] = overlay[keyword_name]
                        else:
                            merged[keyword_name] = (
                                *merged[keyword_name],
                                *overlay[keyword_name],
                            )
                    return merged

                def unpacked_keyword_candidates(
                    value: ast.AST,
                ) -> tuple[
                    dict[str, tuple[ast.AST, ...]],
                    frozenset[str],
                ]:
                    if isinstance(value, ast.NamedExpr):
                        return unpacked_keyword_candidates(value.value)
                    if isinstance(value, ast.IfExp):
                        body, body_definite = unpacked_keyword_candidates(value.body)
                        orelse, orelse_definite = unpacked_keyword_candidates(
                            value.orelse
                        )
                        return (
                            {
                                keyword_name: (
                                    *body[keyword_name],
                                    *orelse[keyword_name],
                                )
                                for keyword_name in callback_keywords
                            },
                            body_definite & orelse_definite,
                        )
                    if isinstance(value, ast.BoolOp):
                        branches = [
                            unpacked_keyword_candidates(candidate)
                            for candidate in value.values
                        ]
                        definite = set(callback_keywords)
                        for _, branch_definite in branches:
                            definite.intersection_update(branch_definite)
                        return (
                            {
                                keyword_name: tuple(
                                    item
                                    for branch, _ in branches
                                    for item in branch[keyword_name]
                                )
                                for keyword_name in callback_keywords
                            },
                            frozenset(definite),
                        )
                    if isinstance(value, ast.BinOp) and isinstance(
                        value.op,
                        ast.BitOr,
                    ):
                        left, left_definite = unpacked_keyword_candidates(value.left)
                        right, right_definite = unpacked_keyword_candidates(value.right)
                        return (
                            merge_keyword_candidates(left, right, right_definite),
                            left_definite | right_definite,
                        )
                    if isinstance(value, ast.Dict):
                        matched = {
                            keyword_name: () for keyword_name in callback_keywords
                        }
                        definite: set[str] = set()
                        for key, item in zip(
                            value.keys,
                            value.values,
                            strict=True,
                        ):
                            if key is None:
                                overlay, overlay_definite = (
                                    unpacked_keyword_candidates(item)
                                )
                                matched = merge_keyword_candidates(
                                    matched,
                                    overlay,
                                    overlay_definite,
                                )
                                definite.update(overlay_definite)
                                continue
                            static_key = _static_string_value(key)
                            if static_key in callback_keywords:
                                matched[static_key] = (item,)
                                definite.add(static_key)
                            elif static_key is None:
                                for keyword_name in callback_keywords:
                                    matched[keyword_name] = (
                                        *matched[keyword_name],
                                        item,
                                    )
                        return matched, frozenset(definite)
                    if (
                        isinstance(value, ast.Call)
                        and current_builtin_dict_reference(value.func)
                    ):
                        matched = {
                            keyword_name: () for keyword_name in callback_keywords
                        }
                        definite: set[str] = set()
                        for argument in value.args:
                            overlay, overlay_definite = (
                                unpacked_keyword_candidates(argument)
                            )
                            matched = merge_keyword_candidates(
                                matched,
                                overlay,
                                overlay_definite,
                            )
                            definite.update(overlay_definite)
                        for keyword in value.keywords:
                            if keyword.arg in callback_keywords:
                                matched[keyword.arg] = (keyword.value,)
                                definite.add(keyword.arg)
                            elif keyword.arg is None:
                                overlay, overlay_definite = (
                                    unpacked_keyword_candidates(keyword.value)
                                )
                                matched = merge_keyword_candidates(
                                    matched,
                                    overlay,
                                    overlay_definite,
                                )
                                definite.update(overlay_definite)
                        return matched, frozenset(definite)
                    return (
                        {
                            keyword_name: (value,)
                            for keyword_name in callback_keywords
                        },
                        frozenset(),
                    )

                def unpacked_keyword_values(
                    value: ast.AST,
                ) -> tuple[ast.AST, ...]:
                    candidates, _ = unpacked_keyword_candidates(value)
                    return tuple(
                        item
                        for keyword_name in callback_keywords
                        for item in candidates[keyword_name]
                    )

                for keyword in call.keywords:
                    if keyword.arg in {"func", "key"}:
                        arguments.append(keyword.value)
                    elif keyword.arg is None:
                        arguments.extend(unpacked_keyword_values(keyword.value))
                return tuple(arguments)
            definition_body = (
                list(definition.body)
                if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef))
                else []
            )
            definition_expression = (
                definition.body if isinstance(definition, ast.Lambda) else None
            )
            pending = [
                *definition_body,
                *((definition_expression,) if definition_expression is not None else ()),
            ]
            body_nodes: list[ast.AST] = []
            while pending:
                candidate = pending.pop()
                if isinstance(
                    candidate,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
                ):
                    continue
                body_nodes.append(candidate)
                pending.extend(ast.iter_child_nodes(candidate))
            alias_sources = {
                name: {name}
                for name in parameter_names
            }
            captured_closure_sources: dict[str, set[str]] = {}

            def captured_invoked_parameters(
                nested: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
                environment: dict[str, set[str]],
            ) -> set[str]:
                nested_parameter_names = {
                    *(parameter.arg for parameter in nested.args.posonlyargs),
                    *(parameter.arg for parameter in nested.args.args),
                    *(parameter.arg for parameter in nested.args.kwonlyargs),
                    *((nested.args.vararg.arg,) if nested.args.vararg is not None else ()),
                    *((nested.args.kwarg.arg,) if nested.args.kwarg is not None else ()),
                }
                nested_body = (
                    list(nested.body)
                    if isinstance(nested, (ast.FunctionDef, ast.AsyncFunctionDef))
                    else [nested.body]
                )
                nested_environment = {
                    name: set(sources)
                    for name, sources in environment.items()
                    if name not in nested_parameter_names
                }

                def nested_referenced_sources(
                    value: ast.AST | None,
                    sources_by_name: dict[str, set[str]],
                ) -> set[str]:
                    if isinstance(value, ast.Name):
                        return set(sources_by_name.get(value.id, set()))
                    if isinstance(value, ast.Attribute):
                        reference = _dotted_name(value)
                        direct = sources_by_name.get(reference or "", set())
                        if direct:
                            return set(direct)
                        if value.attr == "__call__":
                            return nested_referenced_sources(
                                value.value,
                                sources_by_name,
                            )
                        return set()
                    if isinstance(value, ast.NamedExpr):
                        return nested_referenced_sources(
                            value.value,
                            sources_by_name,
                        )
                    if isinstance(value, ast.IfExp):
                        return nested_referenced_sources(
                            value.body,
                            sources_by_name,
                        ) | nested_referenced_sources(
                            value.orelse,
                            sources_by_name,
                        )
                    if isinstance(value, ast.BoolOp):
                        return set().union(
                            *(
                                nested_referenced_sources(item, sources_by_name)
                                for item in value.values
                            )
                        )
                    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
                        return set().union(
                            *(
                                nested_referenced_sources(item, sources_by_name)
                                for item in value.elts
                            )
                        )
                    if isinstance(value, ast.Dict):
                        return set().union(
                            *(
                                nested_referenced_sources(item, sources_by_name)
                                for item in (*value.keys, *value.values)
                                if item is not None
                            )
                        )
                    if isinstance(value, (ast.Await, ast.Yield, ast.YieldFrom)):
                        return nested_referenced_sources(
                            value.value,
                            sources_by_name,
                        )
                    if isinstance(value, ast.Subscript):
                        return nested_referenced_sources(
                            value.value,
                            sources_by_name,
                        )
                    if isinstance(value, ast.Call):
                        return nested_referenced_sources(
                            value.func,
                            sources_by_name,
                        )
                    if isinstance(value, ast.Lambda):
                        return captured_invoked_parameters(value, sources_by_name)
                    return set()

                def directly_nested_in_current_callable(candidate: ast.AST) -> bool:
                    enclosing = parent_nodes.get(candidate)
                    while enclosing is not None and not isinstance(
                        enclosing,
                        (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda),
                    ):
                        enclosing = parent_nodes.get(enclosing)
                    return enclosing is nested

                direct_nested_definitions = [
                    candidate
                    for statement in nested_body
                    for candidate in ast.walk(statement)
                    if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and directly_nested_in_current_callable(candidate)
                ]
                for child in sorted(
                    direct_nested_definitions,
                    key=lambda definition: getattr(definition, "lineno", -1),
                ):
                    child_sources = captured_invoked_parameters(
                        child,
                        nested_environment,
                    )
                    if child_sources:
                        nested_environment[child.name] = child_sources

                def nested_sources_before(point: ast.AST) -> dict[str, set[str]]:
                    sources_by_name = {
                        name: set(sources)
                        for name, sources in nested_environment.items()
                    }
                    for statement in nested_body:
                        if getattr(statement, "lineno", -1) >= getattr(
                            point,
                            "lineno",
                            -1,
                        ):
                            break
                        targets: list[ast.AST] = []
                        value: ast.AST | None = None
                        if isinstance(statement, ast.Assign):
                            targets = list(statement.targets)
                            value = statement.value
                        elif isinstance(statement, ast.AnnAssign):
                            targets = [statement.target]
                            value = statement.value
                        if value is None:
                            continue
                        sources = nested_referenced_sources(
                            value,
                            sources_by_name,
                        )
                        for target in targets:
                            target_reference = _dotted_name(target)
                            if target_reference is not None:
                                sources_by_name[target_reference] = set(sources)
                    return sources_by_name

                pending_nested = list(nested_body)
                captured: set[str] = set()
                while pending_nested:
                    candidate = pending_nested.pop()
                    if isinstance(
                        candidate,
                        (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
                    ):
                        continue
                    if isinstance(candidate, ast.Call):
                        sources_at_call = nested_sources_before(candidate)
                        if (
                            current_operator_call_reference(candidate.func)
                            and candidate.args
                        ):
                            captured.update(
                                nested_referenced_sources(
                                    candidate.args[0],
                                    sources_at_call,
                                )
                            )
                        for callback_argument in current_secondary_callback_arguments(
                            candidate
                        ):
                            captured.update(
                                nested_referenced_sources(
                                    callback_argument,
                                    sources_at_call,
                                )
                            )
                        if (
                            current_positional_callback_executor_reference(
                                candidate.func
                            )
                            and not (
                                isinstance(candidate.func, ast.Name)
                                and candidate.func.id in nested_parameter_names
                            )
                            and candidate.args
                        ):
                            captured.update(
                                nested_referenced_sources(
                                    candidate.args[0],
                                    sources_at_call,
                                )
                            )
                        if (
                            current_builtin_map_reference(candidate.func)
                            and not (
                                isinstance(candidate.func, ast.Name)
                                and candidate.func.id in nested_parameter_names
                            )
                            and candidate.args
                        ):
                            for position in invoked_inline_lambda_parameter_positions(
                                candidate.args[0],
                                current_operator_call_reference,
                            ):
                                iterable_index = position + 1
                                if iterable_index < len(candidate.args):
                                    captured.update(
                                        nested_referenced_sources(
                                            candidate.args[iterable_index],
                                            sources_at_call,
                                        )
                                    )
                        invoked_reference = _dotted_name(candidate.func)
                        if (
                            isinstance(candidate.func, ast.Attribute)
                            and candidate.func.attr == "__call__"
                        ):
                            invoked_reference = _dotted_name(candidate.func.value)
                        if (
                            invoked_reference is not None
                            and invoked_reference not in nested_parameter_names
                        ):
                            captured.update(
                                sources_at_call.get(invoked_reference, set())
                            )
                    if isinstance(candidate, (ast.Return, ast.Yield, ast.YieldFrom)):
                        captured.update(
                            nested_referenced_sources(
                                candidate.value,
                                nested_sources_before(candidate),
                            )
                        )
                    pending_nested.extend(ast.iter_child_nodes(candidate))
                return captured

            def referenced_parameters(
                value: ast.AST | None,
                environment: dict[str, set[str]] | None = None,
            ) -> set[str]:
                sources_by_name = alias_sources if environment is None else environment
                if isinstance(value, ast.Name):
                    return set(sources_by_name.get(value.id, set()))
                if isinstance(value, ast.Attribute):
                    reference = _dotted_name(value)
                    direct = sources_by_name.get(reference or "", set())
                    if direct:
                        return set(direct)
                    if value.attr == "__call__":
                        return referenced_parameters(value.value, sources_by_name)
                    return set()
                if isinstance(value, ast.NamedExpr):
                    return referenced_parameters(value.value, sources_by_name)
                if isinstance(value, ast.IfExp):
                    return referenced_parameters(
                        value.body,
                        sources_by_name,
                    ) | referenced_parameters(
                        value.orelse,
                        sources_by_name,
                    )
                if isinstance(value, ast.BoolOp):
                    return set().union(
                        *(
                            referenced_parameters(item, sources_by_name)
                            for item in value.values
                        )
                    )
                if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
                    return set().union(
                        *(
                            referenced_parameters(item, sources_by_name)
                            for item in value.elts
                        )
                    )
                if isinstance(value, ast.Dict):
                    return set().union(
                        *(
                            referenced_parameters(item, sources_by_name)
                            for item in (*value.keys, *value.values)
                            if item is not None
                        )
                    )
                if isinstance(value, (ast.Await, ast.Yield, ast.YieldFrom)):
                    return referenced_parameters(value.value, sources_by_name)
                if isinstance(value, ast.Subscript):
                    return referenced_parameters(value.value, sources_by_name)
                if isinstance(value, ast.Call):
                    return referenced_parameters(value.func, sources_by_name)
                if isinstance(value, ast.Lambda):
                    return captured_invoked_parameters(value, sources_by_name)
                return set()

            def unconditional_sources_before(
                point: ast.AST,
            ) -> dict[str, set[str]]:
                environment = {
                    name: {name}
                    for name in parameter_names
                }
                environment.update(
                    {
                        name: set(sources)
                        for name, sources in captured_closure_sources.items()
                    }
                )
                for statement in definition_body:
                    if getattr(statement, "lineno", -1) >= getattr(point, "lineno", -1):
                        break
                    targets: list[ast.AST] = []
                    value: ast.AST | None = None
                    if isinstance(statement, ast.Assign):
                        targets = list(statement.targets)
                        value = statement.value
                    elif isinstance(statement, ast.AnnAssign):
                        targets = [statement.target]
                        value = statement.value
                    if value is None:
                        continue
                    sources = referenced_parameters(value, environment)
                    for target in targets:
                        target_reference = _dotted_name(target)
                        if target_reference is not None:
                            environment[target_reference] = set(sources)
                return environment

            for nested in (
                candidate
                for statement in definition_body
                for candidate in ast.walk(statement)
                if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef))
            ):
                enclosing = parent_nodes.get(nested)
                while enclosing is not None and not isinstance(
                    enclosing,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda),
                ):
                    enclosing = parent_nodes.get(enclosing)
                if enclosing is not definition:
                    continue
                captured = captured_invoked_parameters(
                    nested,
                    unconditional_sources_before(nested),
                )
                if captured:
                    captured_closure_sources[nested.name] = set(captured)
                    alias_sources[nested.name] = set(captured)

            changed = True
            while changed:
                changed = False
                for candidate in body_nodes:
                    target: ast.AST | None = None
                    value: ast.AST | None = None
                    if isinstance(candidate, ast.Assign):
                        if len(candidate.targets) == 1:
                            target = candidate.targets[0]
                            value = candidate.value
                    elif isinstance(candidate, ast.AnnAssign):
                        target = candidate.target
                        value = candidate.value
                    elif isinstance(candidate, ast.NamedExpr):
                        target = candidate.target
                        value = candidate.value
                    target_reference = _dotted_name(target)
                    if target_reference is None:
                        continue
                    sources = referenced_parameters(value)
                    if not sources:
                        continue
                    previous = alias_sources.setdefault(target_reference, set())
                    new_sources = sources - previous
                    if new_sources:
                        previous.update(new_sources)
                        changed = True

            invoked_parameters: set[str] = set()
            invoked_projected_keywords: set[str] = set()
            event_loop_receiver_parameters: dict[str, set[str]] = {}
            for candidate in body_nodes:
                if not isinstance(candidate, ast.Call):
                    continue
                sources_at_call = unconditional_sources_before(candidate)
                positional_sources: list[set[str]] = []
                starred_sources: set[str] = set()
                for argument in candidate.args:
                    source = referenced_parameters(
                        argument.value
                        if isinstance(argument, ast.Starred)
                        else argument,
                        sources_at_call,
                    )
                    positional_sources.append(source)
                    if isinstance(argument, ast.Starred):
                        starred_sources.update(source)
                keyword_sources: dict[str, set[str]] = {}
                unpacked_keyword_sources: set[str] = set()
                for keyword in candidate.keywords:
                    source = referenced_parameters(
                        keyword.value,
                        sources_at_call,
                    )
                    if keyword.arg is None:
                        unpacked_keyword_sources.update(source)
                    else:
                        keyword_sources.setdefault(keyword.arg, set()).update(
                            source
                        )
                helper_call_argument_sources.setdefault(
                    contract_reference,
                    [],
                ).append(
                    (
                        candidate,
                        tuple(positional_sources),
                        keyword_sources,
                        starred_sources,
                        unpacked_keyword_sources,
                    )
                )
                if (
                    isinstance(candidate.func, ast.Attribute)
                    and candidate.func.attr
                    in ASYNCIO_EVENT_LOOP_PROCESS_CALL_NAMES
                ):
                    receiver_parameters = referenced_parameters(
                        candidate.func.value,
                        sources_at_call,
                    )
                    for parameter_name in receiver_parameters:
                        event_loop_receiver_parameters.setdefault(
                            parameter_name,
                            set(),
                        ).add(f"asyncio.{candidate.func.attr}")
                if current_operator_call_reference(candidate.func) and candidate.args:
                    invoked_parameters.update(
                        referenced_parameters(
                            candidate.args[0],
                            sources_at_call,
                        )
                    )
                for callback_argument in current_secondary_callback_arguments(
                    candidate
                ):
                    invoked_parameters.update(
                        referenced_parameters(
                            callback_argument,
                            sources_at_call,
                        )
                    )
                if (
                    current_positional_callback_executor_reference(candidate.func)
                    and not (
                        isinstance(candidate.func, ast.Name)
                        and candidate.func.id in parameter_names
                    )
                    and candidate.args
                ):
                    invoked_parameters.update(
                        referenced_parameters(
                            candidate.args[0],
                            sources_at_call,
                        )
                    )
                if (
                    current_builtin_map_reference(candidate.func)
                    and not (
                        isinstance(candidate.func, ast.Name)
                        and candidate.func.id in parameter_names
                    )
                    and candidate.args
                ):
                    for position in invoked_inline_lambda_parameter_positions(
                        candidate.args[0],
                        current_operator_call_reference,
                    ):
                        iterable_index = position + 1
                        if iterable_index < len(candidate.args):
                            invoked_parameters.update(
                                referenced_parameters(
                                    candidate.args[iterable_index],
                                    sources_at_call,
                                )
                            )
                invoked_reference = _dotted_name(candidate.func)
                if (
                    isinstance(candidate.func, ast.Attribute)
                    and candidate.func.attr == "__call__"
                ):
                    invoked_reference = _dotted_name(candidate.func.value)
                elif (
                    isinstance(candidate.func, ast.Subscript)
                ):
                    invoked_reference = _dotted_name(candidate.func.value)
                    if (
                        definition.args.kwarg is not None
                        and invoked_reference == definition.args.kwarg.arg
                    ):
                        projected_keyword = _static_string_value(
                            candidate.func.slice
                        )
                        if projected_keyword is not None:
                            invoked_projected_keywords.add(projected_keyword)
                if invoked_reference is not None:
                    invoked_parameters.update(
                        sources_at_call.get(
                            invoked_reference,
                            alias_sources.get(invoked_reference, set()),
                        )
                    )
            returned_parameters: set[str] = set()
            for candidate in body_nodes:
                if not isinstance(candidate, (ast.Return, ast.Yield, ast.YieldFrom)):
                    continue
                value = candidate.value
                if value is None:
                    continue
                sources_at_return = unconditional_sources_before(candidate)
                returned_parameters.update(
                    referenced_parameters(value, sources_at_return)
                )
                helper_returned_calls.setdefault(
                    contract_reference,
                    set(),
                ).update(
                    nested
                    for nested in ast.walk(value)
                    if isinstance(nested, ast.Call)
                )
            if definition_expression is not None:
                returned_parameters.update(
                    referenced_parameters(
                        definition_expression,
                        unconditional_sources_before(definition_expression),
                    )
                )
            enclosing_scope = parent_nodes.get(definition)
            while enclosing_scope is not None and not isinstance(
                enclosing_scope,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ):
                enclosing_scope = parent_nodes.get(enclosing_scope)
            if isinstance(definition, ast.Lambda):
                callback_receiver_kinds[contract_reference] = "none"
            elif isinstance(enclosing_scope, ast.ClassDef):
                decorator_names = {
                    _dotted_name(decorator)
                    for decorator in definition.decorator_list
                }
                if any(
                    name == "staticmethod" or name.endswith(".staticmethod")
                    for name in decorator_names
                    if name is not None
                ):
                    callback_receiver_kinds[contract_reference] = "none"
                elif any(
                    name == "classmethod" or name.endswith(".classmethod")
                    for name in decorator_names
                    if name is not None
                ):
                    callback_receiver_kinds[contract_reference] = "class"
                else:
                    callback_receiver_kinds[contract_reference] = "instance"
            else:
                callback_receiver_kinds[contract_reference] = "none"
            if (
                not invoked_parameters
                and not invoked_projected_keywords
                and not event_loop_receiver_parameters
                and not returned_parameters
            ):
                continue
            if invoked_parameters:
                callback_parameter_names.setdefault(contract_reference, set()).update(
                    invoked_parameters
                )
                callback_parameter_positions.setdefault(contract_reference, set()).update(
                    parameter_positions[name]
                    for name in invoked_parameters
                    if name in parameter_positions
                )
            if invoked_projected_keywords:
                callback_projected_keyword_names.setdefault(
                    contract_reference,
                    set(),
                ).update(invoked_projected_keywords)
            if event_loop_receiver_parameters:
                event_loop_receiver_parameter_names[contract_reference] = {
                    name: set(kinds)
                    for name, kinds in event_loop_receiver_parameters.items()
                }
                event_loop_receiver_parameter_positions[contract_reference] = {
                    parameter_positions[name]: set(kinds)
                    for name, kinds in event_loop_receiver_parameters.items()
                    if name in parameter_positions
                }
            if returned_parameters:
                returned_parameter_names.setdefault(contract_reference, set()).update(
                    returned_parameters
                )
                returned_parameter_positions.setdefault(contract_reference, set()).update(
                    parameter_positions[name]
                    for name in returned_parameters
                    if name in parameter_positions
                )

        callback_contract_aliases: dict[str, set[str]] = {
            contract_reference: {contract_reference}
            for _, contract_reference in helper_definitions
        }
        returned_contract_aliases: dict[str, set[str]] = {
            contract_reference: {contract_reference}
            for _, contract_reference in helper_definitions
        }
        known_class_references = {
            qualified_scope_reference(definition)
            for definition in ast.walk(tree)
            if isinstance(definition, ast.ClassDef)
        }
        contract_owner_references = {
            owner
            for contract in (
                *(contract for _, contract in helper_definitions),
                *returned_parameter_names,
            )
            if (owner := contract.rpartition(".")[0]) in known_class_references
        }
        constructor_aliases: dict[str, set[str]] = {
            owner: {owner} for owner in contract_owner_references
        }
        factory_instance_aliases: dict[str, set[str]] = {}
        constructed_instance_aliases: dict[str, set[str]] = {}

        def constructor_contract_owners(
            value: ast.AST | None,
            *,
            context: ast.AST | None = None,
        ) -> set[str]:
            reference = _dotted_name(value)
            if reference is None:
                return set()
            owners: set[str] = set()
            for candidate in callback_reference_candidates(reference, context):
                owners.update(constructor_aliases.get(candidate, set()))
            return owners

        def factory_contract_owners(
            value: ast.AST | None,
            *,
            context: ast.AST | None = None,
        ) -> set[str]:
            reference = _dotted_name(value)
            if reference is None:
                return set()
            owners: set[str] = set()
            for candidate in callback_reference_candidates(reference, context):
                owners.update(factory_instance_aliases.get(candidate, set()))
            return owners

        def constructed_instance_references(
            value: ast.AST | None,
            *,
            context: ast.AST | None = None,
        ) -> set[str]:
            if isinstance(value, ast.NamedExpr):
                return constructed_instance_references(value.value, context=context)
            if isinstance(value, ast.IfExp):
                return constructed_instance_references(
                    value.body,
                    context=context,
                ) | constructed_instance_references(value.orelse, context=context)
            if isinstance(value, ast.BoolOp):
                return set().union(
                    *(
                        constructed_instance_references(item, context=context)
                        for item in value.values
                    )
                )
            if isinstance(value, ast.Call):
                return constructor_contract_owners(
                    value.func,
                    context=context,
                ) | factory_contract_owners(value.func, context=context)
            reference = _dotted_name(value)
            if reference is None:
                return set()
            owners: set[str] = set()
            for candidate in callback_reference_candidates(reference, context):
                owners.update(constructed_instance_aliases.get(candidate, set()))
            return owners

        def local_return_values(
            definition: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
        ) -> tuple[ast.AST, ...]:
            if isinstance(definition, ast.Lambda):
                return (definition.body,)
            values: list[ast.AST] = []
            for candidate in ast.walk(definition):
                if not isinstance(candidate, (ast.Return, ast.Yield, ast.YieldFrom)):
                    continue
                if candidate.value is None:
                    continue
                enclosing = parent_nodes.get(candidate)
                while enclosing is not None and not isinstance(
                    enclosing,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda),
                ):
                    enclosing = parent_nodes.get(enclosing)
                if enclosing is definition:
                    values.append(candidate.value)
            return tuple(values)

        for definition, contract_reference in helper_definitions:
            if isinstance(definition, ast.Lambda):
                continue
            owner = contract_reference.rpartition(".")[0]
            if owner not in known_class_references:
                continue
            decorator_names = [
                _dotted_name(decorator) for decorator in definition.decorator_list
            ]
            if any(
                (
                    name == "property"
                    or name.endswith(".property")
                    or name in functools_cached_property_aliases
                    or (
                        isinstance(decorator, ast.Attribute)
                        and decorator.attr == "cached_property"
                        and _dotted_name(decorator.value) in functools_aliases
                    )
                )
                for decorator, name in zip(
                    definition.decorator_list,
                    decorator_names,
                    strict=True,
                )
                if name is not None
            ):
                event_loop_property_return_values[contract_reference] = (
                    local_return_values(definition)
                )

        changed = True
        while changed:
            changed = False
            for definition, contract_reference in helper_definitions:
                owners: set[str] = set()
                for value in local_return_values(definition):
                    owners.update(
                        constructed_instance_references(
                            value,
                            context=value,
                        )
                    )
                if owners:
                    previous = factory_instance_aliases.setdefault(
                        contract_reference,
                        set(),
                    )
                    new_owners = owners - previous
                    if new_owners:
                        previous.update(new_owners)
                        changed = True
            for candidate in ast.walk(tree):
                targets: list[ast.AST] = []
                value: ast.AST | None = None
                if isinstance(candidate, ast.Assign):
                    targets = list(candidate.targets)
                    value = candidate.value
                elif isinstance(candidate, ast.AnnAssign):
                    targets = [candidate.target]
                    value = candidate.value
                elif isinstance(candidate, ast.NamedExpr):
                    targets = [candidate.target]
                    value = candidate.value
                constructor_owners = constructor_contract_owners(
                    value,
                    context=candidate,
                )
                factory_owners = factory_contract_owners(
                    value,
                    context=candidate,
                )
                instance_owners = constructed_instance_references(
                    value,
                    context=candidate,
                )
                if (
                    not constructor_owners
                    and not factory_owners
                    and not instance_owners
                ):
                    continue
                for target in targets:
                    target_reference = _dotted_name(target)
                    if target_reference is None:
                        continue
                    target_reference = callback_binding_reference(
                        target_reference,
                        candidate,
                    )
                    for aliases, owners in (
                        (constructor_aliases, constructor_owners),
                        (factory_instance_aliases, factory_owners),
                        (constructed_instance_aliases, instance_owners),
                    ):
                        if not owners:
                            continue
                        previous = aliases.setdefault(target_reference, set())
                        new_owners = owners - previous
                        if new_owners:
                            previous.update(new_owners)
                            changed = True

        def event_loop_property_contract_references(
            value: ast.AST | None,
            *,
            context: ast.AST | None = None,
        ) -> set[str]:
            if not isinstance(value, ast.Attribute):
                return set()
            contracts: set[str] = set()
            reference = _dotted_name(value)
            if reference is not None:
                contracts.update(
                    candidate
                    for candidate in callback_reference_candidates(reference, context)
                    if candidate in event_loop_property_return_values
                )
            for owner in constructed_instance_references(
                value.value,
                context=context,
            ):
                contract = f"{owner}.{value.attr}"
                if contract in event_loop_property_return_values:
                    contracts.add(contract)
            return contracts

        def callback_contract_references(
            value: ast.AST | None,
            *,
            context: ast.AST | None = None,
        ) -> set[str]:
            reference = _dotted_name(value)
            if reference is None and isinstance(value, ast.Call):
                constructor = _dotted_name(value.func)
                if constructor is not None:
                    reference = f"{constructor}.__call__"
            if (
                reference is None
                and isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Call)
            ):
                constructor = _dotted_name(value.value.func)
                if constructor is not None:
                    reference = f"{constructor}.{value.attr}"
            if reference is not None:
                for candidate in callback_reference_candidates(reference, context):
                    direct = callback_contract_aliases.get(candidate, set())
                    if direct:
                        return set(direct)
                if isinstance(value, ast.Attribute):
                    contracts: set[str] = set()
                    for owner in constructed_instance_references(
                        value.value,
                        context=context,
                    ):
                        contracts.update(
                            callback_contract_aliases.get(
                                f"{owner}.{value.attr}",
                                set(),
                            )
                        )
                    if contracts:
                        return contracts
                return set()
            if isinstance(value, ast.NamedExpr):
                return callback_contract_references(value.value, context=context)
            if isinstance(value, ast.IfExp):
                return callback_contract_references(
                    value.body,
                    context=context,
                ) | callback_contract_references(value.orelse, context=context)
            if isinstance(value, ast.BoolOp):
                return set().union(
                    *(
                        callback_contract_references(item, context=context)
                        for item in value.values
                    )
                )
            return set()

        def returned_contract_references(
            value: ast.AST | None,
            *,
            context: ast.AST | None = None,
        ) -> set[str]:
            reference = _dotted_name(value)
            if reference is None and isinstance(value, ast.Call):
                constructor = _dotted_name(value.func)
                if constructor is not None:
                    reference = f"{constructor}.__call__"
            if (
                reference is None
                and isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Call)
            ):
                constructor = _dotted_name(value.value.func)
                if constructor is not None:
                    reference = f"{constructor}.{value.attr}"
            if reference is not None:
                for candidate in callback_reference_candidates(reference, context):
                    direct = returned_contract_aliases.get(candidate, set())
                    if direct:
                        return set(direct)
                if isinstance(value, ast.Attribute):
                    contracts: set[str] = set()
                    for owner in constructed_instance_references(
                        value.value,
                        context=context,
                    ):
                        contracts.update(
                            returned_contract_aliases.get(
                                f"{owner}.{value.attr}",
                                set(),
                            )
                        )
                    if contracts:
                        return contracts
                return set()
            if isinstance(value, ast.NamedExpr):
                return returned_contract_references(value.value, context=context)
            if isinstance(value, ast.IfExp):
                return returned_contract_references(
                    value.body,
                    context=context,
                ) | returned_contract_references(value.orelse, context=context)
            if isinstance(value, ast.BoolOp):
                return set().union(
                    *(
                        returned_contract_references(item, context=context)
                        for item in value.values
                    )
                )
            return set()

        def callback_contract_consumes_receiver(
            value: ast.AST | None,
            contract: str,
        ) -> bool:
            receiver_kind = callback_receiver_kinds.get(contract, "none")
            if receiver_kind == "none":
                return False
            owner, separator, method_name = contract.rpartition(".")
            if not separator:
                return False

            if isinstance(value, ast.Call) and method_name == "__call__":
                receiver_reference = _dotted_name(value.func)
                return receiver_reference is not None and (
                    owner == receiver_reference
                    or owner.endswith(f".{receiver_reference}")
                )
            if not isinstance(value, ast.Attribute) or value.attr != method_name:
                return False

            receiver = value.value
            if isinstance(receiver, ast.Call):
                receiver_reference = _dotted_name(receiver.func)
                receiver_is_instance = True
            else:
                receiver_reference = _dotted_name(receiver)
                receiver_is_instance = False
            if owner in constructed_instance_references(
                receiver,
                context=value,
            ):
                return True
            if receiver_reference is None or not (
                owner == receiver_reference
                or owner.endswith(f".{receiver_reference}")
            ):
                return False
            return receiver_is_instance or receiver_kind == "class"

        def reviewed_handler_registry_escape(value: ast.AST | None) -> bool:
            if (
                relative_path != "src/vivado_agent_mcp/tools.py"
                or not function_stack
                or function_stack[-1] != "_handlers"
                or not isinstance(value, ast.DictComp)
                or len(value.generators) != 1
            ):
                return False
            generator = value.generators[0]
            if (
                generator.is_async
                or generator.ifs
                or not isinstance(generator.target, ast.Name)
                or not isinstance(generator.iter, ast.Call)
                or _dotted_name(generator.iter.func) != "registry_tool_names"
                or generator.iter.args
                or generator.iter.keywords
                or not isinstance(value.key, ast.Name)
                or value.key.id != generator.target.id
                or not isinstance(value.value, ast.Call)
                or _dotted_name(value.value.func) != "getattr"
                or len(value.value.args) != 2
                or value.value.keywords
                or not isinstance(value.value.args[0], ast.Name)
                or value.value.args[0].id != "self"
                or not isinstance(value.value.args[1], ast.Call)
            ):
                return False
            handler_lookup = value.value.args[1]
            return (
                _dotted_name(handler_lookup.func) == "handler_name"
                and len(handler_lookup.args) == 1
                and not handler_lookup.keywords
                and isinstance(handler_lookup.args[0], ast.Name)
                and handler_lookup.args[0].id == generator.target.id
            )

        changed = True
        while changed:
            changed = False
            for candidate in ast.walk(tree):
                targets: list[ast.AST] = []
                value: ast.AST | None = None
                if isinstance(candidate, ast.Assign):
                    targets = list(candidate.targets)
                    value = candidate.value
                elif isinstance(candidate, ast.AnnAssign):
                    targets = [candidate.target]
                    value = candidate.value
                elif isinstance(candidate, ast.NamedExpr):
                    targets = [candidate.target]
                    value = candidate.value
                callback_contracts = callback_contract_references(
                    value,
                    context=candidate,
                )
                returned_contracts = returned_contract_references(
                    value,
                    context=candidate,
                )
                if not callback_contracts and not returned_contracts:
                    continue
                for target in targets:
                    target_reference = _dotted_name(target)
                    if target_reference is None:
                        continue
                    target_reference = callback_binding_reference(
                        target_reference,
                        candidate,
                    )
                    for aliases, contracts in (
                        (callback_contract_aliases, callback_contracts),
                        (returned_contract_aliases, returned_contracts),
                    ):
                        if not contracts:
                            continue
                        previous = aliases.setdefault(target_reference, set())
                        new_contracts = contracts - previous
                        if new_contracts:
                            previous.update(new_contracts)
                            changed = True

        changed = True
        while changed:
            changed = False
            for caller_contract, calls in helper_call_argument_sources.items():
                returned_calls = helper_returned_calls.get(caller_contract, set())
                if not returned_calls:
                    continue
                caller_names = returned_parameter_names.setdefault(
                    caller_contract,
                    set(),
                )
                for (
                    call,
                    positional_sources,
                    keyword_sources,
                    starred_sources,
                    unpacked_keyword_sources,
                ) in calls:
                    if call not in returned_calls:
                        continue
                    for callee_contract in returned_contract_references(
                        call.func,
                        context=call,
                    ):
                        consumes_receiver = callback_contract_consumes_receiver(
                            call.func,
                            callee_contract,
                        )
                        for position in returned_parameter_positions.get(
                            callee_contract,
                            set(),
                        ):
                            argument_position = (
                                position - 1
                                if consumes_receiver and position > 0
                                else position
                            )
                            sources = set(starred_sources)
                            if argument_position < len(positional_sources):
                                sources.update(
                                    positional_sources[argument_position]
                                )
                            new_names = sources - caller_names
                            if new_names:
                                caller_names.update(new_names)
                                changed = True
                        callee_names = returned_parameter_names.get(
                            callee_contract,
                            set(),
                        )
                        for parameter_name in callee_names:
                            sources = set(unpacked_keyword_sources)
                            sources.update(
                                keyword_sources.get(parameter_name, set())
                            )
                            new_names = sources - caller_names
                            if new_names:
                                caller_names.update(new_names)
                                changed = True
                caller_positions = helper_parameter_positions.get(
                    caller_contract,
                    {},
                )
                if caller_names:
                    returned_parameter_positions[caller_contract] = {
                        caller_positions[name]
                        for name in caller_names
                        if name in caller_positions
                    }

        changed = True
        while changed:
            changed = False
            for caller_contract, calls in helper_call_argument_sources.items():
                caller_name_kinds = event_loop_receiver_parameter_names.setdefault(
                    caller_contract,
                    {},
                )
                for (
                    call,
                    positional_sources,
                    keyword_sources,
                    starred_sources,
                    unpacked_keyword_sources,
                ) in calls:
                    for callee_contract in callback_contract_references(
                        call.func,
                        context=call,
                    ):
                        consumes_receiver = callback_contract_consumes_receiver(
                            call.func,
                            callee_contract,
                        )
                        callee_position_kinds = (
                            event_loop_receiver_parameter_positions.get(
                                callee_contract,
                                {},
                            )
                        )
                        for position, kinds in callee_position_kinds.items():
                            argument_position = (
                                position - 1
                                if consumes_receiver and position > 0
                                else position
                            )
                            sources = set(starred_sources)
                            if argument_position < len(positional_sources):
                                sources.update(
                                    positional_sources[argument_position]
                                )
                            for source in sources:
                                observed = caller_name_kinds.setdefault(
                                    source,
                                    set(),
                                )
                                new_kinds = kinds - observed
                                if new_kinds:
                                    observed.update(new_kinds)
                                    changed = True
                        for parameter_name, kinds in (
                            event_loop_receiver_parameter_names.get(
                                callee_contract,
                                {},
                            ).items()
                        ):
                            sources = set(unpacked_keyword_sources)
                            sources.update(
                                keyword_sources.get(parameter_name, set())
                            )
                            for source in sources:
                                observed = caller_name_kinds.setdefault(
                                    source,
                                    set(),
                                )
                                new_kinds = kinds - observed
                                if new_kinds:
                                    observed.update(new_kinds)
                                    changed = True
                caller_positions = helper_parameter_positions.get(
                    caller_contract,
                    {},
                )
                if caller_name_kinds:
                    event_loop_receiver_parameter_positions[caller_contract] = {
                        caller_positions[name]: set(kinds)
                        for name, kinds in caller_name_kinds.items()
                        if name in caller_positions
                    }

        class Visitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self._annotation_call_only_depth = 0
                self._conditional_depth = 0
                self._event_loop_property_evaluation_stack: set[str] = set()
                self._literal_code_depth = 0

            @staticmethod
            def _bare_name_scope_is_visible(index: int, total_scopes: int) -> bool:
                return (
                    alias_scope_kind_stack[index] != "class"
                    or index == total_scopes - 1
                )

            def _callable_aliases(self) -> dict[str, str]:
                combined = dict(function_aliases)
                for index, (scope, shadowed) in enumerate(
                    zip(
                        callable_alias_stack,
                        callable_shadow_stack,
                        strict=True,
                    )
                ):
                    if not self._bare_name_scope_is_visible(
                        index,
                        len(callable_alias_stack),
                    ):
                        continue
                    for name in shadowed:
                        combined.pop(name, None)
                    combined.update(scope)
                return combined

            def _module_aliases(self) -> dict[str, set[str]]:
                combined = {
                    module: set(aliases)
                    for module, aliases in module_aliases.items()
                }
                visible = {
                    alias: module
                    for module, aliases in combined.items()
                    for alias in aliases
                }
                for index, (scope, shadowed) in enumerate(
                    zip(
                        module_alias_stack,
                        module_shadow_stack,
                        strict=True,
                    )
                ):
                    if not self._bare_name_scope_is_visible(
                        index,
                        len(module_alias_stack),
                    ):
                        continue
                    for name in shadowed:
                        for reference in tuple(visible):
                            observed_module = visible[reference]
                            if reference == name or (
                                reference.startswith(f"{name}.")
                                and scope.get(name) != observed_module
                            ):
                                visible.pop(reference, None)
                    visible.update(scope)
                return {
                    module: {
                        alias
                        for alias, observed_module in visible.items()
                        if observed_module == module
                    }
                    for module in combined
                }

            def _visible_lexical_aliases(
                self,
                base: set[str],
                scopes: list[set[str]],
                shadows: list[set[str]],
            ) -> set[str]:
                visible = set(base)
                for index, (scope, shadowed) in enumerate(
                    zip(scopes, shadows, strict=True)
                ):
                    if not self._bare_name_scope_is_visible(index, len(scopes)):
                        continue
                    visible.difference_update(shadowed)
                    visible.update(scope)
                return visible

            def _resolver_aliases(self) -> set[str]:
                return self._visible_lexical_aliases(
                    resolver_aliases,
                    resolver_alias_stack,
                    resolver_shadow_stack,
                )

            def _importlib_aliases(self) -> set[str]:
                return self._visible_lexical_aliases(
                    importlib_aliases,
                    importlib_alias_stack,
                    importlib_shadow_stack,
                )

            def _builtins_aliases(self) -> set[str]:
                return self._visible_lexical_aliases(
                    builtins_aliases,
                    builtins_alias_stack,
                    builtins_shadow_stack,
                )

            def _builtin_receiver_available(self, name: str) -> bool:
                return not any(
                    name in shadowed
                    for index, shadowed in enumerate(builtin_name_shadow_stack)
                    if self._bare_name_scope_is_visible(
                        index,
                        len(builtin_name_shadow_stack),
                    )
                )

            def _builtin_getattr_aliases(self) -> set[str]:
                visible = self._visible_lexical_aliases(
                    set(),
                    builtin_getattr_alias_stack,
                    builtin_getattr_shadow_stack,
                )
                if self._in_class_body() and class_builtin_getattr_export_stack:
                    visible.update(class_builtin_getattr_export_stack[-1])
                return visible

            def _builtin_getattr_reference(self, node: ast.AST | None) -> bool:
                if isinstance(node, ast.NamedExpr):
                    return self._builtin_getattr_reference(node.value)
                if isinstance(node, ast.IfExp):
                    return self._builtin_getattr_reference(
                        node.body
                    ) or self._builtin_getattr_reference(node.orelse)
                if isinstance(node, ast.BoolOp):
                    return any(
                        self._builtin_getattr_reference(candidate)
                        for candidate in node.values
                    )
                lookup_source: ast.AST | None = None
                lookup_key: ast.AST | None = None
                if isinstance(node, ast.Subscript):
                    lookup_source = node.value
                    lookup_key = node.slice
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr
                    in {"__getitem__", "get", "pop", "setdefault"}
                    and node.args
                    and not node.keywords
                ):
                    lookup_source = node.func.value
                    lookup_key = node.args[0]
                if (
                    lookup_source is not None
                    and lookup_key is not None
                    and _static_string_value(lookup_key) == "getattr"
                    and self._builtins_dict_reference(lookup_source)
                ):
                    return True
                if isinstance(node, ast.Name):
                    return (
                        (
                            node.id == "getattr"
                            and self._builtin_receiver_available("getattr")
                        )
                        or node.id in self._builtin_getattr_aliases()
                    )
                reference = _dotted_name(node)
                return (
                    reference in self._builtin_getattr_aliases()
                    or (
                        isinstance(node, ast.Attribute)
                        and node.attr == "getattr"
                        and _dotted_name(node.value) in self._builtins_aliases()
                    )
                )

            def _builtin_getattr_descendants(
                self,
                reference: str | None,
            ) -> set[str]:
                if reference is None:
                    return set()
                prefix = f"{reference}."
                return {
                    candidate[len(reference) :]
                    for candidate in self._builtin_getattr_aliases()
                    if candidate.startswith(prefix)
                }

            def _qualified_vars_aliases(self) -> set[str]:
                visible: set[str] = set()
                for index, (aliases, exact_shadows, prefix_shadows) in enumerate(
                    zip(
                        qualified_vars_alias_stack,
                        qualified_vars_exact_shadow_stack,
                        qualified_vars_prefix_shadow_stack,
                        strict=True,
                    )
                ):
                    if not self._bare_name_scope_is_visible(
                        index,
                        len(qualified_vars_alias_stack),
                    ):
                        continue
                    visible.difference_update(exact_shadows)
                    visible = {
                        reference
                        for reference in visible
                        if not any(
                            reference.startswith(f"{prefix}.")
                            for prefix in prefix_shadows
                        )
                    }
                    visible.update(aliases)
                if self._in_class_body() and class_vars_export_stack:
                    visible.update(class_vars_export_stack[-1])
                return visible

            def _vars_aliases(self) -> set[str]:
                visible = self._visible_lexical_aliases(
                    vars_aliases,
                    vars_alias_stack,
                    vars_shadow_stack,
                )
                return (
                    visible
                    | {f"{owner}.vars" for owner in self._builtins_aliases()}
                    | self._qualified_vars_aliases()
                )

            def _sys_aliases(self) -> set[str]:
                return self._visible_lexical_aliases(
                    sys_aliases,
                    sys_alias_stack,
                    sys_shadow_stack,
                )

            def _module_registry_aliases(self) -> set[str]:
                visible = self._visible_lexical_aliases(
                    module_registry_aliases,
                    module_registry_alias_stack,
                    module_registry_shadow_stack,
                )
                return visible | {
                    f"{owner}.modules" for owner in self._sys_aliases()
                }

            def _operator_aliases(self) -> set[str]:
                return self._visible_lexical_aliases(
                    operator_aliases,
                    operator_alias_stack,
                    operator_shadow_stack,
                )

            def _operator_transform_aliases(self) -> dict[str, str]:
                visible = dict(operator_transform_aliases)
                for index, (scope, shadowed) in enumerate(
                    zip(
                        operator_transform_alias_stack,
                        operator_transform_shadow_stack,
                        strict=True,
                    )
                ):
                    if not self._bare_name_scope_is_visible(
                        index,
                        len(operator_transform_alias_stack),
                    ):
                        continue
                    for name in shadowed:
                        visible.pop(name, None)
                    visible.update(scope)
                return visible

            def _operator_transform_reference_kind(
                self,
                node: ast.AST | None,
            ) -> str | None:
                if isinstance(node, ast.Name):
                    return self._operator_transform_aliases().get(node.id)
                if isinstance(node, ast.Attribute):
                    owner = _dotted_name(node.value)
                    if (
                        owner in self._operator_aliases()
                        and node.attr
                        in {"attrgetter", "methodcaller", "getitem", "itemgetter"}
                    ):
                        return node.attr
                return None

            def _functools_aliases(self) -> set[str]:
                return self._visible_lexical_aliases(
                    functools_aliases,
                    functools_alias_stack,
                    functools_shadow_stack,
                )

            def _functools_partial_aliases(self) -> set[str]:
                visible = self._visible_lexical_aliases(
                    functools_partial_aliases,
                    functools_partial_alias_stack,
                    functools_partial_shadow_stack,
                )
                return visible | {
                    f"{owner}.partial" for owner in self._functools_aliases()
                }

            def _functools_partial_reference(
                self,
                node: ast.AST | None,
            ) -> bool:
                reference = _dotted_name(node)
                return (
                    reference is not None
                    and reference in self._functools_partial_aliases()
                )

            def _operator_process_transform_kind(
                self,
                node: ast.Call,
            ) -> str | None:
                def module_dict_callable_kind(
                    source: ast.AST,
                    key_node: ast.AST,
                ) -> str | None:
                    module_kinds = _static_module_dict_kinds(
                        source,
                        self._module_aliases(),
                        self._vars_aliases(),
                    )
                    key = _static_string_value(key_node)
                    if key is None:
                        return None
                    candidates = (
                        f"{module_kind}.{key}"
                        for module_kind in sorted(module_kinds)
                    )
                    return next(
                        (
                            candidate
                            for candidate in candidates
                            if _approved_process_call_kind(candidate)
                        ),
                        None,
                    )

                direct_transform_kind = self._operator_transform_reference_kind(
                    node.func
                )
                if (
                    direct_transform_kind == "getitem"
                    and len(node.args) == 2
                    and not node.keywords
                ):
                    return module_dict_callable_kind(
                        node.args[0],
                        node.args[1],
                    )
                if (
                    not isinstance(node.func, ast.Call)
                    or len(node.args) != 1
                    or node.keywords
                ):
                    return None
                transform = node.func
                transform_kind = self._operator_transform_reference_kind(
                    transform.func
                )
                if transform_kind is None or not transform.args:
                    return None
                if transform_kind == "itemgetter" and (
                    len(transform.args) != 1 or transform.keywords
                ):
                    return None
                if transform_kind == "attrgetter" and (
                    len(transform.args) != 1 or transform.keywords
                ):
                    return None
                attribute = _static_string_value(transform.args[0])
                if attribute == "run_tcl":
                    return "run_tcl"
                if (
                    attribute in ASYNCIO_EVENT_LOOP_PROCESS_CALL_NAMES
                    and _event_loop_receiver(
                        node.args[0],
                        self._module_aliases(),
                        function_aliases,
                    )
                ):
                    return f"asyncio.{attribute}"
                if attribute is None:
                    receiver_module_kind = self._observed_module_reference_kind(
                        node.args[0], self._module_aliases()
                    )
                    return (
                        "asyncio.*"
                        if transform_kind in {"attrgetter", "methodcaller"}
                        and receiver_module_kind is None
                        else None
                    )
                if transform_kind == "itemgetter":
                    return module_dict_callable_kind(
                        node.args[0],
                        transform.args[0],
                    )
                module_kind = self._observed_module_reference_kind(
                    node.args[0], self._module_aliases()
                )
                if module_kind is None:
                    return None
                candidate = f"{module_kind}.{attribute}"
                return candidate if _approved_process_call_kind(candidate) else None

            def _sys_reference(self, node: ast.AST | None) -> bool:
                if isinstance(node, ast.NamedExpr):
                    return self._sys_reference(node.value)
                if isinstance(node, ast.IfExp):
                    return self._sys_reference(node.body) or self._sys_reference(
                        node.orelse
                    )
                if isinstance(node, ast.BoolOp):
                    return any(self._sys_reference(candidate) for candidate in node.values)
                return _dotted_name(node) in self._sys_aliases()

            def _sys_dict_reference(self, node: ast.AST | None) -> bool:
                if isinstance(node, ast.NamedExpr):
                    return self._sys_dict_reference(node.value)
                if isinstance(node, ast.Attribute) and node.attr == "__dict__":
                    return self._sys_reference(node.value)
                if (
                    isinstance(node, ast.Call)
                    and self._builtin_getattr_reference(node.func)
                    and len(node.args) == 2
                    and not node.keywords
                    and _static_string_value(node.args[1]) == "__dict__"
                ):
                    return self._sys_reference(node.args[0])
                if (
                    isinstance(node, ast.Call)
                    and _known_vars_reference_name(
                        node.func,
                        self._vars_aliases(),
                    )
                    is not None
                    and len(node.args) == 1
                    and not node.keywords
                ):
                    return self._sys_reference(node.args[0])
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "copy"
                    and not node.args
                    and not node.keywords
                ):
                    return self._sys_dict_reference(node.func.value)
                return False

            def _builtins_reference(self, node: ast.AST | None) -> bool:
                if isinstance(node, ast.NamedExpr):
                    return self._builtins_reference(node.value)
                if isinstance(node, ast.IfExp):
                    return self._builtins_reference(
                        node.body
                    ) or self._builtins_reference(node.orelse)
                if isinstance(node, ast.BoolOp):
                    return any(
                        self._builtins_reference(candidate)
                        for candidate in node.values
                    )
                return _dotted_name(node) in self._builtins_aliases()

            def _builtins_dict_reference(self, node: ast.AST | None) -> bool:
                if isinstance(node, ast.NamedExpr):
                    return self._builtins_dict_reference(node.value)
                if isinstance(node, ast.Dict):
                    return any(
                        key is None and self._builtins_dict_reference(value)
                        for key, value in zip(node.keys, node.values, strict=True)
                    )
                if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
                    return self._builtins_dict_reference(
                        node.left
                    ) or self._builtins_dict_reference(node.right)
                if isinstance(node, ast.Attribute) and node.attr == "__dict__":
                    return self._builtins_reference(node.value)
                if not isinstance(node, ast.Call):
                    return False
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "copy"
                    and not node.args
                    and not node.keywords
                ):
                    return self._builtins_dict_reference(node.func.value)
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "dict"
                    and len(node.args) == 1
                ):
                    return self._builtins_dict_reference(node.args[0])
                if (
                    self._builtin_getattr_reference(node.func)
                    and len(node.args) == 2
                    and not node.keywords
                    and _static_string_value(node.args[1]) == "__dict__"
                ):
                    return self._builtins_reference(node.args[0])
                if (
                    _known_vars_reference_name(
                        node.func,
                        self._vars_aliases(),
                    )
                    is not None
                    and len(node.args) == 1
                    and not node.keywords
                ):
                    return self._builtins_reference(node.args[0])
                return False

            def _code_execution_reference_kind(
                self,
                node: ast.AST | None,
            ) -> str | None:
                if isinstance(node, ast.Name):
                    aliased = self._callable_aliases().get(node.id)
                    if aliased in {"dynamic_code.eval", "dynamic_code.exec"}:
                        return aliased.removeprefix("dynamic_code.")
                    if node.id in {"eval", "exec"} and self._builtin_receiver_available(
                        node.id
                    ):
                        return node.id
                    return None
                if isinstance(node, ast.Attribute) and node.attr in {"eval", "exec"}:
                    if _dotted_name(node.value) in self._builtins_aliases():
                        return node.attr
                owner_node, attribute = _static_attribute_lookup(
                    node,
                    builtin_getattr_available=self._builtin_receiver_available(
                        "getattr"
                    ),
                    builtin_object_available=self._builtin_receiver_available(
                        "object"
                    ),
                    builtin_getattr_owners=self._builtins_aliases(),
                    builtin_getattr_aliases=self._builtin_getattr_aliases(),
                    vars_references=self._vars_aliases(),
                )
                if (
                    attribute in {"eval", "exec"}
                    and self._builtins_reference(owner_node)
                ):
                    return attribute

                lookup_source: ast.AST | None = None
                lookup_key: ast.AST | None = None
                if isinstance(node, ast.Subscript):
                    lookup_source = node.value
                    lookup_key = node.slice
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr
                    in {"__getitem__", "get", "pop", "setdefault"}
                    and node.args
                ):
                    lookup_source = node.func.value
                    lookup_key = node.args[0]
                elif (
                    isinstance(node, ast.Call)
                    and self._operator_transform_reference_kind(node.func)
                    == "getitem"
                    and len(node.args) == 2
                    and not node.keywords
                ):
                    lookup_source = node.args[0]
                    lookup_key = node.args[1]
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Call)
                    and self._operator_transform_reference_kind(node.func.func)
                    == "itemgetter"
                    and len(node.func.args) == 1
                    and not node.func.keywords
                    and len(node.args) == 1
                    and not node.keywords
                ):
                    lookup_source = node.args[0]
                    lookup_key = node.func.args[0]
                key = _static_string_value(lookup_key)
                if key in {"eval", "exec"} and (
                    self._builtins_dict_reference(lookup_source)
                    or self._dynamic_reference_kind(lookup_source) == "builtins"
                ):
                    return key
                return None

            def _record_literal_code_execution(self, node: ast.Call) -> None:
                execution_kind = self._code_execution_reference_kind(node.func)
                if execution_kind is None:
                    return
                self._record(node, f"dynamic_code.{execution_kind}")
                if self._literal_code_depth >= 4:
                    return
                source_node = node.args[0] if node.args else next(
                    (
                        keyword.value
                        for keyword in node.keywords
                        if keyword.arg in {"source", "object"}
                    ),
                    None,
                )
                source = _static_string_value(source_node)
                if source is None:
                    return
                try:
                    if len(source.encode("utf-8", errors="strict")) > 65_536:
                        return
                    embedded = ast.parse(
                        source,
                        mode="eval" if execution_kind == "eval" else "exec",
                    )
                except (SyntaxError, UnicodeEncodeError, ValueError):
                    return
                if sum(1 for _ in ast.walk(embedded)) > 4_096:
                    return
                self._literal_code_depth += 1
                try:
                    self.visit(embedded)
                finally:
                    self._literal_code_depth -= 1

            def _module_registry_reference(self, node: ast.AST | None) -> bool:
                if isinstance(node, ast.NamedExpr):
                    return self._module_registry_reference(node.value)
                if isinstance(node, ast.IfExp):
                    return self._module_registry_reference(
                        node.body
                    ) or self._module_registry_reference(node.orelse)
                if isinstance(node, ast.BoolOp):
                    return any(
                        self._module_registry_reference(candidate)
                        for candidate in node.values
                    )
                if _dotted_name(node) in self._module_registry_aliases():
                    return True
                source: ast.AST | None = None
                key_node: ast.AST | None = None
                if isinstance(node, ast.Subscript):
                    source = node.value
                    key_node = node.slice
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr
                    in {"__getitem__", "get", "pop", "setdefault"}
                    and node.args
                ):
                    source = node.func.value
                    key_node = node.args[0]
                if (
                    source is not None
                    and _static_string_value(key_node) == "modules"
                    and self._sys_dict_reference(source)
                ):
                    return True
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "copy"
                    and not node.args
                    and not node.keywords
                ):
                    return self._module_registry_reference(node.func.value)
                return False

            def _registered_process_module_kind(
                self,
                node: ast.AST | None,
            ) -> str | None:
                if isinstance(node, ast.NamedExpr):
                    return self._registered_process_module_kind(node.value)
                if isinstance(node, ast.IfExp):
                    return next(
                        (
                            kind
                            for candidate in (node.body, node.orelse)
                            if (
                                kind := self._registered_process_module_kind(
                                    candidate
                                )
                            )
                        ),
                        None,
                    )
                if isinstance(node, ast.BoolOp):
                    return next(
                        (
                            kind
                            for candidate in node.values
                            if (
                                kind := self._registered_process_module_kind(
                                    candidate
                                )
                            )
                        ),
                        None,
                    )
                registry: ast.AST | None = None
                key_node: ast.AST | None = None
                if isinstance(node, ast.Subscript):
                    registry = node.value
                    key_node = node.slice
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr
                    in {"__getitem__", "get", "pop", "setdefault"}
                    and node.args
                ):
                    registry = node.func.value
                    key_node = node.args[0]
                if registry is None or not self._module_registry_reference(registry):
                    return None
                module_name = _static_string_value(key_node)
                if module_name in {"asyncio", "asyncio.subprocess"}:
                    return "asyncio"
                return (
                    module_name
                    if module_name in {"subprocess", "os"}
                    else None
                )

            def _observed_module_reference_kind(
                self,
                node: ast.AST | None,
                module_aliases: dict[str, set[str]],
            ) -> str | None:
                return self._registered_process_module_kind(
                    node
                ) or _module_reference_kind(node, module_aliases)

            def _observed_module_or_event_loop_reference_kind(
                self,
                node: ast.AST | None,
                module_aliases: dict[str, set[str]],
                callable_aliases: dict[str, str],
            ) -> str | None:
                module_kind = self._observed_module_reference_kind(
                    node,
                    module_aliases,
                )
                if module_kind is not None:
                    return module_kind
                if _asyncio_runner_receiver(
                    node,
                    module_aliases,
                    callable_aliases,
                ):
                    return "asyncio_runner"
                if _asyncio_task_receiver(
                    node,
                    module_aliases,
                    callable_aliases,
                ):
                    return "asyncio_task"
                return (
                    "asyncio"
                    if _event_loop_receiver(
                        node,
                        module_aliases,
                        {**function_aliases, **callable_aliases},
                    )
                    or self._returned_event_loop_receiver(node)
                    else None
                )

            def _registered_process_callable_kind(
                self,
                node: ast.AST | None,
            ) -> str | None:
                if isinstance(node, ast.NamedExpr):
                    return self._registered_process_callable_kind(node.value)
                if isinstance(node, ast.IfExp):
                    return next(
                        (
                            kind
                            for candidate in (node.body, node.orelse)
                            if (
                                kind := self._registered_process_callable_kind(
                                    candidate
                                )
                            )
                        ),
                        None,
                    )
                if isinstance(node, ast.BoolOp):
                    return next(
                        (
                            kind
                            for candidate in node.values
                            if (
                                kind := self._registered_process_callable_kind(
                                    candidate
                                )
                            )
                        ),
                        None,
                    )
                module_kind: str | None = None
                callable_name: str | None = None
                if isinstance(node, ast.Attribute):
                    module_kind = self._registered_process_module_kind(node.value)
                    callable_name = node.attr
                else:
                    owner_node, callable_name = _static_attribute_lookup(
                        node,
                        builtin_getattr_available=self._builtin_receiver_available(
                            "getattr"
                        ),
                        builtin_object_available=self._builtin_receiver_available(
                            "object"
                        ),
                        builtin_getattr_owners=self._builtins_aliases(),
                        builtin_getattr_aliases=self._builtin_getattr_aliases(),
                        vars_references=self._vars_aliases(),
                    )
                    module_kind = self._registered_process_module_kind(owner_node)
                if module_kind is None or callable_name is None:
                    return None
                candidate = f"{module_kind}.{callable_name}"
                return candidate if _approved_process_call_kind(candidate) else None

            def _observed_callable_reference_kind(
                self,
                node: ast.AST | None,
                module_aliases: dict[str, set[str]],
                callable_aliases: dict[str, str],
            ) -> str | None:
                observed = self._registered_process_callable_kind(
                    node
                ) or _callable_reference_kind(
                    node,
                    module_aliases,
                    {},
                    callable_aliases,
                    self._vars_aliases(),
                    builtin_getattr_available=self._builtin_receiver_available(
                        "getattr"
                    ),
                    builtin_object_available=self._builtin_receiver_available(
                        "object"
                    ),
                    builtin_getattr_owners=self._builtins_aliases(),
                    builtin_getattr_aliases=self._builtin_getattr_aliases(),
                )
                if observed is not None:
                    return observed
                return None

            def _returned_contract_arguments(
                self,
                node: ast.Call,
            ) -> tuple[ast.AST, ...]:
                contracts = returned_contract_references(node.func, context=node)
                if not contracts:
                    return ()
                returned_positions: set[int] = set()
                for contract in contracts:
                    positions = returned_parameter_positions.get(contract, set())
                    if callback_contract_consumes_receiver(node.func, contract):
                        returned_positions.update(
                            position - 1 for position in positions if position > 0
                        )
                    else:
                        returned_positions.update(positions)
                returned_names = set().union(
                    *(
                        returned_parameter_names.get(contract, set())
                        for contract in contracts
                    )
                )
                candidates = [
                    argument.value
                    if isinstance(argument, ast.Starred)
                    else argument
                    for index, argument in enumerate(node.args)
                    if index in returned_positions
                    or (
                        isinstance(argument, ast.Starred)
                        and bool(returned_positions)
                    )
                ]
                candidates.extend(
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg in returned_names
                    or (keyword.arg is None and bool(returned_names))
                )
                return tuple(candidates)

            def _returned_event_loop_receiver(
                self,
                node: ast.AST | None,
            ) -> bool:
                if _event_loop_receiver(
                    node,
                    self._module_aliases(),
                    {**function_aliases, **self._callable_aliases()},
                ):
                    return True
                for contract in event_loop_property_contract_references(
                    node,
                    context=node,
                ):
                    if contract in self._event_loop_property_evaluation_stack:
                        continue
                    self._event_loop_property_evaluation_stack.add(contract)
                    try:
                        if any(
                            self._returned_event_loop_receiver(value)
                            for value in event_loop_property_return_values[contract]
                        ):
                            return True
                    finally:
                        self._event_loop_property_evaluation_stack.remove(contract)
                if isinstance(node, ast.NamedExpr):
                    return self._returned_event_loop_receiver(node.value)
                if isinstance(node, ast.IfExp):
                    return self._returned_event_loop_receiver(
                        node.body
                    ) or self._returned_event_loop_receiver(node.orelse)
                if isinstance(node, ast.BoolOp):
                    return any(
                        self._returned_event_loop_receiver(candidate)
                        for candidate in node.values
                    )
                if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                    return any(
                        self._returned_event_loop_receiver(candidate)
                        for candidate in node.elts
                    )
                if isinstance(node, ast.Dict):
                    return any(
                        self._returned_event_loop_receiver(candidate)
                        for candidate in (*node.keys, *node.values)
                        if candidate is not None
                    )
                if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
                    return self._returned_event_loop_receiver(node.elt)
                if isinstance(node, ast.DictComp):
                    return self._returned_event_loop_receiver(
                        node.key
                    ) or self._returned_event_loop_receiver(node.value)
                if isinstance(node, (ast.Await, ast.Yield, ast.YieldFrom)):
                    return self._returned_event_loop_receiver(node.value)
                if isinstance(node, ast.Subscript):
                    return self._returned_event_loop_receiver(node.value)
                if isinstance(node, ast.Call) and node.args and not node.keywords:
                    builtin_name: str | None = None
                    if isinstance(node.func, ast.Name) and (
                        node.func.id in {"iter", "next"}
                        and self._builtin_receiver_available(node.func.id)
                    ):
                        builtin_name = node.func.id
                    elif (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr in {"iter", "next"}
                        and _dotted_name(node.func.value) in self._builtins_aliases()
                    ):
                        builtin_name = node.func.attr
                    if (
                        builtin_name == "next"
                        or (builtin_name == "iter" and len(node.args) == 1)
                    ):
                        return self._returned_event_loop_receiver(node.args[0])
                return isinstance(node, ast.Call) and any(
                    self._returned_event_loop_receiver(candidate)
                    for candidate in self._returned_contract_arguments(node)
                )

            def _returned_event_loop_process_call_kind(
                self,
                node: ast.Call,
            ) -> str | None:
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr
                    in ASYNCIO_EVENT_LOOP_PROCESS_CALL_NAMES
                    and self._returned_event_loop_receiver(node.func.value)
                ):
                    return f"asyncio.{node.func.attr}"
                return None

            def _returned_callable_kind(
                self,
                node: ast.AST | None,
            ) -> str | None:
                if isinstance(node, ast.NamedExpr):
                    return self._returned_callable_kind(node.value)
                if isinstance(node, ast.IfExp):
                    return self._returned_callable_kind(
                        node.body
                    ) or self._returned_callable_kind(node.orelse)
                if isinstance(node, ast.BoolOp):
                    return next(
                        (
                            kind
                            for candidate in node.values
                            if (kind := self._returned_callable_kind(candidate))
                        ),
                        None,
                    )
                if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                    return next(
                        (
                            kind
                            for candidate in node.elts
                            if (kind := self._returned_callable_kind(candidate))
                        ),
                        None,
                    )
                if isinstance(node, ast.Dict):
                    return next(
                        (
                            kind
                            for candidate in (*node.keys, *node.values)
                            if candidate is not None
                            and (kind := self._returned_callable_kind(candidate))
                        ),
                        None,
                    )
                if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
                    return self._returned_callable_kind(node.elt)
                if isinstance(node, ast.DictComp):
                    return self._returned_callable_kind(
                        node.key
                    ) or self._returned_callable_kind(node.value)
                if isinstance(node, (ast.Await, ast.Yield, ast.YieldFrom)):
                    return self._returned_callable_kind(node.value)
                if isinstance(node, ast.Subscript):
                    return self._returned_callable_kind(node.value)
                if not isinstance(node, ast.Call):
                    return None
                for candidate in self._returned_contract_arguments(node):
                    call_kind = self._registered_process_callable_kind(
                        candidate
                    ) or _callable_reference_kind(
                        candidate,
                        self._module_aliases(),
                        {},
                        self._callable_aliases(),
                        self._vars_aliases(),
                        builtin_getattr_available=self._builtin_receiver_available(
                            "getattr"
                        ),
                        builtin_object_available=self._builtin_receiver_available(
                            "object"
                        ),
                        builtin_getattr_owners=self._builtins_aliases(),
                        builtin_getattr_aliases=self._builtin_getattr_aliases(),
                    ) or self._dynamic_attribute_callable_kind(candidate)
                    if call_kind is not None:
                        return call_kind
                return None

            def _dynamic_attribute_callable_kind(
                self,
                node: ast.AST | None,
            ) -> str | None:
                if isinstance(node, ast.NamedExpr):
                    return self._dynamic_attribute_callable_kind(node.value)
                if isinstance(node, ast.IfExp):
                    return next(
                        (
                            kind
                            for candidate in (node.body, node.orelse)
                            if (
                                kind := self._dynamic_attribute_callable_kind(
                                    candidate
                                )
                            )
                        ),
                        None,
                    )
                if isinstance(node, ast.BoolOp):
                    return next(
                        (
                            kind
                            for candidate in node.values
                            if (
                                kind := self._dynamic_attribute_callable_kind(
                                    candidate
                                )
                            )
                        ),
                        None,
                    )
                if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                    return next(
                        (
                            kind
                            for candidate in node.elts
                            if (
                                kind := self._dynamic_attribute_callable_kind(
                                    candidate
                                )
                            )
                        ),
                        None,
                    )
                if isinstance(node, ast.Dict):
                    return next(
                        (
                            kind
                            for candidate in node.values
                            if (
                                kind := self._dynamic_attribute_callable_kind(
                                    candidate
                                )
                            )
                        ),
                        None,
                    )
                if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
                    return self._dynamic_attribute_callable_kind(node.elt)
                if isinstance(node, ast.DictComp):
                    return self._dynamic_attribute_callable_kind(
                        node.key
                    ) or self._dynamic_attribute_callable_kind(node.value)
                if (
                    isinstance(node, ast.Call)
                    and self._functools_partial_reference(node.func)
                    and node.args
                ):
                    wrapped_kind = self._observed_callable_reference_kind(
                        node.args[0],
                        self._module_aliases(),
                        self._callable_aliases(),
                    ) or self._dynamic_attribute_callable_kind(node.args[0])
                    if wrapped_kind == DYNAMIC_ATTRIBUTE_CALLABLE_KIND:
                        return wrapped_kind
                if (
                    isinstance(node, ast.Call)
                    and self._operator_transform_reference_kind(node.func)
                    in {"attrgetter", "methodcaller"}
                    and node.args
                ):
                    attribute = _static_string_value(node.args[0])
                    if (
                        attribute is None
                        or attribute in ASYNCIO_EVENT_LOOP_PROCESS_CALL_NAMES
                    ):
                        return DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                owner_node, attribute = _static_attribute_lookup(
                    node,
                    builtin_getattr_available=self._builtin_receiver_available(
                        "getattr"
                    ),
                    builtin_object_available=self._builtin_receiver_available(
                        "object"
                    ),
                    builtin_getattr_owners=self._builtins_aliases(),
                    builtin_getattr_aliases=self._builtin_getattr_aliases(),
                    vars_references=self._vars_aliases(),
                )
                return (
                    DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                    if owner_node is not None and attribute == "*"
                    else None
                )

            def _qualified_vars_descendants(
                self,
                reference: str | None,
            ) -> set[str]:
                if reference is None:
                    return set()
                prefix = f"{reference}."
                return {
                    candidate[len(reference) :]
                    for candidate in self._qualified_vars_aliases()
                    if candidate.startswith(prefix)
                }

            def _dynamic_callable_descendants(
                self,
                reference: str | None,
            ) -> set[str]:
                if reference is None:
                    return set()
                prefix = f"{reference}."
                return {
                    candidate[len(reference) :]
                    for candidate, kind in self._callable_aliases().items()
                    if kind == DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                    and candidate.startswith(prefix)
                }

            @staticmethod
            def _dynamic_callable_source_reference(
                node: ast.AST | None,
            ) -> str | None:
                reference = _dotted_name(node)
                if reference is not None:
                    return reference
                if isinstance(node, ast.Call):
                    return _dotted_name(node.func)
                return None

            def _qualified_vars_source_reference(
                self,
                node: ast.AST | None,
            ) -> str | None:
                reference = _dotted_name(node)
                if reference is not None:
                    return reference
                if isinstance(node, ast.Call):
                    constructor = _dotted_name(node.func)
                    if self._qualified_vars_descendants(constructor):
                        return constructor
                return None

            def _bind_qualified_vars_target(
                self,
                reference: str,
                *,
                vars_alias: bool,
            ) -> None:
                preserve_aliases = (
                    {
                        candidate
                        for candidate in self._qualified_vars_aliases()
                        if candidate == reference
                        or candidate.startswith(f"{reference}.")
                    }
                    if self._conditional_depth > 0
                    else set()
                )
                qualified_vars_exact_shadow_stack[-1].add(reference)
                qualified_vars_prefix_shadow_stack[-1].add(reference)
                qualified_vars_alias_stack[-1].difference_update(
                    {
                        candidate
                        for candidate in qualified_vars_alias_stack[-1]
                        if candidate == reference
                        or candidate.startswith(f"{reference}.")
                    }
                )
                if self._in_class_body() and class_vars_export_stack:
                    if self._conditional_depth <= 0:
                        class_vars_export_stack[-1].difference_update(
                            {
                                candidate
                                for candidate in class_vars_export_stack[-1]
                                if candidate == reference
                                or candidate.startswith(f"{reference}.")
                            }
                        )
                    if vars_alias:
                        class_vars_export_stack[-1].add(reference)
                if vars_alias:
                    preserve_aliases.add(reference)
                qualified_vars_alias_stack[-1].update(preserve_aliases)

            def _bind_qualified_builtin_getattr_target(
                self,
                node: ast.AST,
                reference: str,
                *,
                builtin_getattr_alias: bool,
            ) -> None:
                preserve_alias = (
                    self._conditional_depth > 0
                    and reference in self._builtin_getattr_aliases()
                )
                builtin_getattr_shadow_stack[-1].add(reference)
                builtin_getattr_alias_stack[-1].difference_update(
                    {
                        candidate
                        for candidate in builtin_getattr_alias_stack[-1]
                        if candidate == reference
                        or candidate.startswith(f"{reference}.")
                    }
                )
                if preserve_alias or builtin_getattr_alias:
                    builtin_getattr_alias_stack[-1].add(reference)
                if builtin_getattr_alias:
                    self._record(node, "builtin.getattr.attribute_export")

            def _bind_qualified_dynamic_callable_target(
                self,
                reference: str,
                *,
                call_kind: str | None,
                descendant_suffixes: set[str],
            ) -> None:
                visible_bindings = {
                    candidate
                    for candidate, kind in self._callable_aliases().items()
                    if kind == DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                    and (
                        candidate == reference
                        or candidate.startswith(f"{reference}.")
                    )
                }
                preserve_bindings = (
                    visible_bindings if self._conditional_depth > 0 else set()
                )
                callable_shadow_stack[-1].update(visible_bindings)
                for candidate in tuple(callable_alias_stack[-1]):
                    if candidate == reference or candidate.startswith(
                        f"{reference}."
                    ):
                        callable_alias_stack[-1].pop(candidate, None)
                callable_alias_stack[-1].update(
                    {
                        candidate: DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                        for candidate in preserve_bindings
                    }
                )
                if call_kind == DYNAMIC_ATTRIBUTE_CALLABLE_KIND:
                    callable_alias_stack[-1][reference] = (
                        DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                    )
                callable_alias_stack[-1].update(
                    {
                        f"{reference}{suffix}": DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                        for suffix in descendant_suffixes
                    }
                )
                if ".__call__" in descendant_suffixes:
                    callable_alias_stack[-1][reference] = (
                        DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                    )

            def _shadow_name(
                self,
                name: str,
                *,
                preserve_conditional_vars: bool = True,
            ) -> None:
                visible_builtin_getattr_bindings = {
                    reference
                    for reference in self._builtin_getattr_aliases()
                    if reference == name or reference.startswith(f"{name}.")
                }
                preserve_builtin_getattr_aliases = (
                    visible_builtin_getattr_bindings
                    if self._conditional_depth > 0
                    and visible_builtin_getattr_bindings
                    and (
                        bool(
                            visible_builtin_getattr_bindings
                            & builtin_getattr_alias_stack[-1]
                        )
                        or alias_scope_kind_stack[-1] == "module"
                        or name in global_name_stack[-1]
                    )
                    else set()
                )
                preserve_vars_alias = (
                    preserve_conditional_vars
                    and self._conditional_depth > 0
                    and name in self._vars_aliases()
                )
                preserve_sys_alias = (
                    preserve_conditional_vars
                    and self._conditional_depth > 0
                    and name in self._sys_aliases()
                )
                preserve_registry_alias = (
                    preserve_conditional_vars
                    and self._conditional_depth > 0
                    and name in self._module_registry_aliases()
                )
                preserve_operator_alias = (
                    preserve_conditional_vars
                    and self._conditional_depth > 0
                    and name in self._operator_aliases()
                )
                preserve_functools_alias = (
                    preserve_conditional_vars
                    and self._conditional_depth > 0
                    and name in self._functools_aliases()
                )
                preserve_functools_partial_alias = (
                    preserve_conditional_vars
                    and self._conditional_depth > 0
                    and name in self._functools_partial_aliases()
                )
                preserve_operator_transform = (
                    self._operator_transform_aliases().get(name)
                    if preserve_conditional_vars and self._conditional_depth > 0
                    else None
                )
                visible_dynamic_callable_bindings = {
                    reference
                    for reference, kind in self._callable_aliases().items()
                    if kind == DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                    and (
                        reference == name or reference.startswith(f"{name}.")
                    )
                }
                preserve_dynamic_callable_bindings = (
                    visible_dynamic_callable_bindings
                    if preserve_conditional_vars and self._conditional_depth > 0
                    else set()
                )
                preserve_qualified_aliases = (
                    {
                        reference
                        for reference in self._qualified_vars_aliases()
                        if reference == name or reference.startswith(f"{name}.")
                    }
                    if preserve_conditional_vars and self._conditional_depth > 0
                    else set()
                )
                dependent_aliases = set().union(
                    *(
                        dependencies.get(name, set())
                        for dependencies in receiver_dependent_vars_stack
                    )
                )
                if preserve_conditional_vars and self._conditional_depth > 0:
                    preserve_qualified_aliases.update(
                        dependent_aliases & self._qualified_vars_aliases()
                    )
                callable_shadow_stack[-1].add(name)
                callable_shadow_stack[-1].update(
                    visible_dynamic_callable_bindings
                )
                for reference in tuple(callable_alias_stack[-1]):
                    if reference == name or reference.startswith(f"{name}."):
                        callable_alias_stack[-1].pop(reference, None)
                module_shadow_stack[-1].add(name)
                module_alias_stack[-1].pop(name, None)
                conditional_global_or_module_binding = (
                    self._conditional_depth > 0
                    and (
                        alias_scope_kind_stack[-1] == "module"
                        or name in global_name_stack[-1]
                    )
                )
                if not conditional_global_or_module_binding:
                    builtin_name_shadow_stack[-1].add(name)
                builtin_getattr_shadow_stack[-1].add(name)
                builtin_getattr_shadow_stack[-1].update(
                    visible_builtin_getattr_bindings
                )
                builtin_getattr_alias_stack[-1].difference_update(
                    visible_builtin_getattr_bindings
                )
                resolver_shadow_stack[-1].add(name)
                resolver_alias_stack[-1].discard(name)
                importlib_shadow_stack[-1].add(name)
                importlib_alias_stack[-1].discard(name)
                builtins_shadow_stack[-1].add(name)
                builtins_alias_stack[-1].discard(name)
                vars_shadow_stack[-1].add(name)
                vars_alias_stack[-1].discard(name)
                sys_shadow_stack[-1].add(name)
                sys_alias_stack[-1].discard(name)
                module_registry_shadow_stack[-1].add(name)
                module_registry_alias_stack[-1].discard(name)
                operator_shadow_stack[-1].add(name)
                operator_alias_stack[-1].discard(name)
                operator_transform_shadow_stack[-1].add(name)
                operator_transform_alias_stack[-1].pop(name, None)
                functools_shadow_stack[-1].add(name)
                functools_alias_stack[-1].discard(name)
                functools_partial_shadow_stack[-1].add(name)
                functools_partial_alias_stack[-1].discard(name)
                qualified_vars_exact_shadow_stack[-1].add(name)
                qualified_vars_exact_shadow_stack[-1].update(dependent_aliases)
                qualified_vars_prefix_shadow_stack[-1].add(name)
                qualified_vars_alias_stack[-1].difference_update(
                    {
                        reference
                        for reference in qualified_vars_alias_stack[-1]
                        if reference == name or reference.startswith(f"{name}.")
                    }
                )
                qualified_vars_alias_stack[-1].difference_update(
                    dependent_aliases
                )
                if preserve_vars_alias:
                    vars_alias_stack[-1].add(name)
                builtin_getattr_alias_stack[-1].update(
                    preserve_builtin_getattr_aliases
                )
                if preserve_sys_alias:
                    sys_alias_stack[-1].add(name)
                if preserve_registry_alias:
                    module_registry_alias_stack[-1].add(name)
                if preserve_operator_alias:
                    operator_alias_stack[-1].add(name)
                if preserve_functools_alias:
                    functools_alias_stack[-1].add(name)
                if preserve_functools_partial_alias:
                    functools_partial_alias_stack[-1].add(name)
                if preserve_operator_transform is not None:
                    operator_transform_alias_stack[-1][name] = (
                        preserve_operator_transform
                    )
                callable_alias_stack[-1].update(
                    {
                        reference: DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                        for reference in preserve_dynamic_callable_bindings
                    }
                )
                qualified_vars_alias_stack[-1].update(
                    preserve_qualified_aliases
                )

            def _delete_name(self, name: str) -> None:
                current_index = len(alias_scope_kind_stack) - 1
                visible_dynamic_callable_bindings = {
                    reference
                    for reference, kind in self._callable_aliases().items()
                    if kind == DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                    and (
                        reference == name or reference.startswith(f"{name}.")
                    )
                }
                preserve_dynamic_callable_bindings = (
                    visible_dynamic_callable_bindings
                    if self._conditional_depth > 0
                    else set()
                )
                if name in global_name_stack[-1]:
                    target_indexes = {0, current_index}
                elif name in nonlocal_name_stack[-1] and current_index > 0:
                    target_indexes = {current_index - 1, current_index}
                else:
                    target_indexes = {current_index}
                restore_class_lookup = alias_scope_kind_stack[-1] == "class"
                restore_builtin_getattr = name == "getattr" and (
                    alias_scope_kind_stack[-1] in {"module", "class"}
                    or name in global_name_stack[-1]
                )
                for index in target_indexes:
                    for reference in tuple(callable_alias_stack[index]):
                        if reference == name or reference.startswith(f"{name}."):
                            callable_alias_stack[index].pop(reference, None)
                    module_alias_stack[index].pop(name, None)
                    operator_transform_alias_stack[index].pop(name, None)
                    for aliases in (
                        resolver_alias_stack,
                        importlib_alias_stack,
                        builtins_alias_stack,
                        vars_alias_stack,
                        sys_alias_stack,
                        module_registry_alias_stack,
                        operator_alias_stack,
                        functools_alias_stack,
                        functools_partial_alias_stack,
                    ):
                        aliases[index].discard(name)
                    builtin_getattr_alias_stack[index].difference_update(
                        {
                            reference
                            for reference in builtin_getattr_alias_stack[index]
                            if reference == name
                            or reference.startswith(f"{name}.")
                        }
                    )
                    if restore_class_lookup:
                        for shadows in (
                            callable_shadow_stack,
                            module_shadow_stack,
                            resolver_shadow_stack,
                            importlib_shadow_stack,
                            builtins_shadow_stack,
                            vars_shadow_stack,
                            sys_shadow_stack,
                            module_registry_shadow_stack,
                            operator_shadow_stack,
                            operator_transform_shadow_stack,
                            functools_shadow_stack,
                            functools_partial_shadow_stack,
                        ):
                            shadows[index].discard(name)
                    if restore_builtin_getattr:
                        builtin_name_shadow_stack[index].discard(name)
                        builtin_getattr_shadow_stack[index].discard(name)
                    else:
                        builtin_name_shadow_stack[index].add(name)
                        builtin_getattr_shadow_stack[index].add(name)
                    callable_alias_stack[index].update(
                        {
                            reference: DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                            for reference in preserve_dynamic_callable_bindings
                        }
                    )
                if self._in_class_body() and class_builtin_getattr_export_stack:
                    class_builtin_getattr_export_stack[-1].difference_update(
                        {
                            reference
                            for reference in class_builtin_getattr_export_stack[-1]
                            if reference == name
                            or reference.startswith(f"{name}.")
                        }
                    )

            def _bind_aliases(
                self,
                name: str,
                *,
                module_kind: str | None,
                call_kind: str | None,
                dynamic_kind: str | None = None,
                vars_alias: bool = False,
                sys_alias: bool = False,
                module_registry_alias: bool = False,
                builtin_getattr_alias: bool = False,
                operator_alias: bool = False,
                operator_transform_kind: str | None = None,
                functools_alias: bool = False,
                functools_partial_alias: bool = False,
            ) -> None:
                self._shadow_name(name)
                if builtin_getattr_alias:
                    builtin_getattr_alias_stack[-1].add(name)
                if module_kind:
                    module_alias_stack[-1][name] = module_kind
                if call_kind:
                    callable_alias_stack[-1][name] = call_kind
                if dynamic_kind == "resolver":
                    resolver_alias_stack[-1].add(name)
                elif dynamic_kind == "importlib":
                    importlib_alias_stack[-1].add(name)
                elif dynamic_kind == "builtins":
                    builtins_alias_stack[-1].add(name)
                if vars_alias:
                    vars_alias_stack[-1].add(name)
                if sys_alias:
                    sys_alias_stack[-1].add(name)
                if module_registry_alias:
                    module_registry_alias_stack[-1].add(name)
                if operator_alias:
                    operator_alias_stack[-1].add(name)
                if functools_alias:
                    functools_alias_stack[-1].add(name)
                if functools_partial_alias:
                    functools_partial_alias_stack[-1].add(name)
                if operator_transform_kind is not None:
                    operator_transform_alias_stack[-1][name] = (
                        operator_transform_kind
                    )

            def _vars_reference(self, node: ast.AST | None) -> bool:
                if isinstance(node, ast.NamedExpr):
                    return self._vars_reference(node.value)
                if isinstance(node, ast.IfExp):
                    return self._vars_reference(node.body) or self._vars_reference(
                        node.orelse
                    )
                if isinstance(node, ast.BoolOp):
                    return any(self._vars_reference(candidate) for candidate in node.values)
                return (
                    _known_vars_reference_name(node, self._vars_aliases())
                    is not None
                )

            def _dynamic_reference_kind(self, node: ast.AST | None) -> str | None:
                if isinstance(node, ast.NamedExpr):
                    return self._dynamic_reference_kind(node.value)
                if isinstance(node, ast.IfExp):
                    return next(
                        (
                            kind
                            for candidate in (node.body, node.orelse)
                            if (kind := self._dynamic_reference_kind(candidate))
                        ),
                        None,
                    )
                if isinstance(node, ast.BoolOp):
                    return next(
                        (
                            kind
                            for candidate in node.values
                            if (kind := self._dynamic_reference_kind(candidate))
                        ),
                        None,
                    )
                if isinstance(node, ast.Name):
                    if node.id in self._resolver_aliases():
                        return "resolver"
                    if node.id in self._importlib_aliases():
                        return "importlib"
                    if node.id in self._builtins_aliases():
                        return "builtins"
                    return None
                if isinstance(node, ast.Attribute) and node.attr == "import_module":
                    owner = _dotted_name(node.value)
                    if owner in self._importlib_aliases():
                        return "resolver"
                if isinstance(node, ast.Attribute) and node.attr == "__import__":
                    owner = _dotted_name(node.value)
                    if owner in self._builtins_aliases():
                        return "resolver"
                lookup_source: ast.AST | None = None
                lookup_key: ast.AST | None = None
                if isinstance(node, ast.Subscript):
                    lookup_source = node.value
                    lookup_key = node.slice
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr
                    in {"__getitem__", "get", "pop", "setdefault"}
                    and node.args
                ):
                    lookup_source = node.func.value
                    lookup_key = node.args[0]
                elif (
                    isinstance(node, ast.Call)
                    and self._operator_transform_reference_kind(node.func)
                    == "getitem"
                    and len(node.args) == 2
                    and not node.keywords
                ):
                    lookup_source = node.args[0]
                    lookup_key = node.args[1]
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Call)
                    and self._operator_transform_reference_kind(node.func.func)
                    == "itemgetter"
                    and len(node.func.args) == 1
                    and not node.func.keywords
                    and len(node.args) == 1
                    and not node.keywords
                ):
                    lookup_source = node.args[0]
                    lookup_key = node.func.args[0]
                if (
                    _static_string_value(lookup_key) == "__import__"
                    and (
                        self._builtins_dict_reference(lookup_source)
                        or self._dynamic_reference_kind(lookup_source)
                        == "builtins"
                    )
                ):
                    return "resolver"
                if (
                    isinstance(node, ast.Call)
                    and self._builtin_getattr_reference(node.func)
                    and len(node.args) >= 2
                ):
                    attribute = _static_string_value(node.args[1])
                    owner_kind = self._dynamic_reference_kind(node.args[0])
                    if (attribute, owner_kind) in {
                        ("import_module", "importlib"),
                        ("__import__", "builtins"),
                    }:
                        return "resolver"
                return None

            def _resolved_process_module_kind(self, node: ast.AST) -> str | None:
                registered_kind = self._registered_process_module_kind(node)
                if registered_kind is not None:
                    return registered_kind
                if not isinstance(node, ast.Call):
                    return None
                if self._dynamic_reference_kind(node.func) != "resolver":
                    return None
                module_expression: ast.AST | None = node.args[0] if node.args else None
                if module_expression is None:
                    module_expression = next(
                        (
                            keyword.value
                            for keyword in node.keywords
                            if keyword.arg == "name"
                        ),
                        None,
                    )
                module_name = _static_string_value(module_expression)
                return _imported_process_module_kind(module_name)

            def _resolved_dynamic_process_call_kind(
                self,
                node: ast.Call,
            ) -> str | None:
                if not isinstance(node.func, ast.Attribute):
                    return None
                owner = node.func.value
                module_kind = self._resolved_process_module_kind(owner)
                if module_kind is None:
                    return None
                candidate = f"{module_kind}.{node.func.attr}"
                return candidate if _approved_process_call_kind(candidate) else None

            @staticmethod
            def _bound_names(target: ast.AST) -> tuple[str, ...]:
                if isinstance(target, ast.Name):
                    return (target.id,)
                if isinstance(target, (ast.Tuple, ast.List)):
                    return tuple(
                        name
                        for item in target.elts
                        for name in Visitor._bound_names(item)
                    )
                if isinstance(target, ast.Starred):
                    return Visitor._bound_names(target.value)
                return ()

            @staticmethod
            def _qualified_bound_references(target: ast.AST) -> tuple[str, ...]:
                if isinstance(target, (ast.Attribute, ast.Subscript)):
                    reference = _dotted_name(target)
                    return (reference,) if reference is not None else ()
                if isinstance(target, (ast.Tuple, ast.List)):
                    return tuple(
                        reference
                        for item in target.elts
                        for reference in Visitor._qualified_bound_references(item)
                    )
                if isinstance(target, ast.Starred):
                    return Visitor._qualified_bound_references(target.value)
                return ()

            def _bound_module_kinds(
                self,
                target: ast.AST,
                value: ast.AST | None,
                module_aliases: dict[str, set[str]],
                callable_aliases: dict[str, str],
            ) -> dict[str, str]:
                if isinstance(target, (ast.Name, ast.Attribute, ast.Subscript)):
                    target_reference = _dotted_name(target)
                    module_kind = (
                        self._observed_module_or_event_loop_reference_kind(
                            value,
                            module_aliases,
                            callable_aliases,
                        )
                    )
                    return (
                        {target_reference: module_kind}
                        if target_reference is not None and module_kind
                        else {}
                    )
                if isinstance(target, ast.Starred):
                    return self._bound_module_kinds(
                        target.value,
                        value,
                        module_aliases,
                        callable_aliases,
                    )
                if not isinstance(target, (ast.Tuple, ast.List)):
                    return {}
                if not isinstance(value, (ast.Tuple, ast.List)) or len(
                    target.elts
                ) != len(value.elts):
                    return {}
                projected: dict[str, str] = {}
                for nested_target, nested_value in zip(
                    target.elts,
                    value.elts,
                    strict=True,
                ):
                    projected.update(
                        self._bound_module_kinds(
                            nested_target,
                            nested_value,
                            module_aliases,
                            callable_aliases,
                        )
                    )
                return projected

            def _builtin_getattr_bound_names(
                self,
                target: ast.AST,
                value: ast.AST,
            ) -> set[str]:
                if isinstance(target, ast.Name):
                    return (
                        {target.id}
                        if self._builtin_getattr_reference(value)
                        else set()
                    )
                if isinstance(target, ast.Starred):
                    return self._builtin_getattr_bound_names(target.value, value)
                if not isinstance(target, (ast.Tuple, ast.List)):
                    return set()
                if isinstance(value, (ast.Tuple, ast.List)) and len(
                    target.elts
                ) == len(value.elts):
                    return set().union(
                        *(
                            self._builtin_getattr_bound_names(
                                nested_target,
                                nested_value,
                            )
                            for nested_target, nested_value in zip(
                                target.elts,
                                value.elts,
                                strict=True,
                            )
                        )
                    )
                contains_builtin_getattr = any(
                    self._builtin_getattr_reference(candidate)
                    for candidate in ast.walk(value)
                )
                return (
                    set(self._bound_names(target))
                    if contains_builtin_getattr
                    else set()
                )

            @staticmethod
            def _parameter_names(arguments: ast.arguments) -> tuple[str, ...]:
                names = [
                    *(argument.arg for argument in arguments.posonlyargs),
                    *(argument.arg for argument in arguments.args),
                    *(argument.arg for argument in arguments.kwonlyargs),
                ]
                if arguments.vararg is not None:
                    names.append(arguments.vararg.arg)
                if arguments.kwarg is not None:
                    names.append(arguments.kwarg.arg)
                return tuple(names)

            @staticmethod
            def _function_local_names(body: Sequence[ast.stmt]) -> set[str]:
                class LocalBindingCollector(ast.NodeVisitor):
                    def __init__(self) -> None:
                        self.bound: set[str] = set()
                        self.globals: set[str] = set()
                        self.nonlocals: set[str] = set()

                    def visit_Global(self, node: ast.Global) -> None:
                        self.globals.update(node.names)

                    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
                        self.nonlocals.update(node.names)

                    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
                        if node.name is not None:
                            self.bound.add(node.name)
                        self.generic_visit(node)

                    def visit_MatchAs(self, node: ast.MatchAs) -> None:
                        if node.name is not None:
                            self.bound.add(node.name)
                        if node.pattern is not None:
                            self.visit(node.pattern)

                    def visit_MatchStar(self, node: ast.MatchStar) -> None:
                        if node.name is not None:
                            self.bound.add(node.name)

                    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
                        if node.rest is not None:
                            self.bound.add(node.rest)
                        self.generic_visit(node)

                    def visit_Name(self, node: ast.Name) -> None:
                        if isinstance(node.ctx, (ast.Store, ast.Del)):
                            self.bound.add(node.id)

                    def visit_Import(self, node: ast.Import) -> None:
                        self.bound.update(
                            alias.asname or alias.name.split(".", maxsplit=1)[0]
                            for alias in node.names
                        )

                    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                        self.bound.update(
                            alias.asname or alias.name
                            for alias in node.names
                            if alias.name != "*"
                        )

                    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                        self.bound.add(node.name)

                    def visit_AsyncFunctionDef(
                        self,
                        node: ast.AsyncFunctionDef,
                    ) -> None:
                        self.bound.add(node.name)

                    def visit_ClassDef(self, node: ast.ClassDef) -> None:
                        self.bound.add(node.name)

                    def visit_Lambda(self, node: ast.Lambda) -> None:
                        return None

                    def _visit_comprehension(self, node: ast.AST) -> None:
                        for generator in getattr(node, "generators", ()):
                            self.visit(generator.iter)
                            for condition in generator.ifs:
                                self.visit(condition)
                        for field_name in ("elt", "key", "value"):
                            expression = getattr(node, field_name, None)
                            if expression is not None:
                                self.visit(expression)

                    visit_ListComp = _visit_comprehension
                    visit_SetComp = _visit_comprehension
                    visit_DictComp = _visit_comprehension
                    visit_GeneratorExp = _visit_comprehension

                collector = LocalBindingCollector()
                for statement in body:
                    collector.visit(statement)
                return collector.bound - collector.globals - collector.nonlocals

            def _parameter_default_bindings(
                self,
                arguments: ast.arguments,
            ) -> tuple[
                tuple[
                    str,
                    str | None,
                    str | None,
                    str | None,
                    bool,
                    bool,
                    bool,
                    bool,
                ],
                ...,
            ]:
                current_module_aliases = self._module_aliases()
                current_callable_aliases = self._callable_aliases()
                positional_parameters = [*arguments.posonlyargs, *arguments.args]
                positional_defaults = zip(
                    positional_parameters[-len(arguments.defaults) :],
                    arguments.defaults,
                    strict=True,
                ) if arguments.defaults else ()
                keyword_defaults = (
                    (parameter, default)
                    for parameter, default in zip(
                        arguments.kwonlyargs,
                        arguments.kw_defaults,
                        strict=True,
                    )
                    if default is not None
                )
                return tuple(
                    (
                        parameter.arg,
                        self._observed_module_or_event_loop_reference_kind(
                            default,
                            current_module_aliases,
                            current_callable_aliases,
                        ),
                        self._observed_callable_reference_kind(
                            default,
                            current_module_aliases,
                            current_callable_aliases,
                        )
                        or self._returned_callable_kind(default)
                        or self._dynamic_attribute_callable_kind(default),
                        self._dynamic_reference_kind(default),
                        self._vars_reference(default),
                        self._sys_reference(default),
                        self._module_registry_reference(default),
                        self._builtin_getattr_reference(default),
                    )
                    for parameter, default in (*positional_defaults, *keyword_defaults)
                )

            def _push_function_scope(
                self,
                arguments: ast.arguments,
                bindings: tuple[
                    tuple[
                        str,
                        str | None,
                        str | None,
                        str | None,
                        bool,
                        bool,
                        bool,
                        bool,
                    ],
                    ...,
                ],
                local_names: set[str] | frozenset[str] = frozenset(),
            ) -> None:
                self._push_alias_scope("function")
                for name in self._parameter_names(arguments):
                    self._shadow_name(name, preserve_conditional_vars=False)
                for name in local_names:
                    self._shadow_name(name, preserve_conditional_vars=False)
                for (
                    name,
                    module_kind,
                    call_kind,
                    dynamic_kind,
                    vars_alias,
                    sys_alias,
                    module_registry_alias,
                    builtin_getattr_alias,
                ) in bindings:
                    self._bind_aliases(
                        name,
                        module_kind=module_kind,
                        call_kind=call_kind,
                        dynamic_kind=dynamic_kind,
                        vars_alias=vars_alias,
                        sys_alias=sys_alias,
                        module_registry_alias=module_registry_alias,
                        builtin_getattr_alias=builtin_getattr_alias,
                    )

            @staticmethod
            def _push_alias_scope(kind: str) -> None:
                callable_alias_stack.append({})
                callable_shadow_stack.append(set())
                module_alias_stack.append({})
                module_shadow_stack.append(set())
                builtin_name_shadow_stack.append(set())
                builtin_getattr_alias_stack.append(set())
                builtin_getattr_shadow_stack.append(set())
                resolver_alias_stack.append(set())
                resolver_shadow_stack.append(set())
                importlib_alias_stack.append(set())
                importlib_shadow_stack.append(set())
                builtins_alias_stack.append(set())
                builtins_shadow_stack.append(set())
                vars_alias_stack.append(set())
                vars_shadow_stack.append(set())
                sys_alias_stack.append(set())
                sys_shadow_stack.append(set())
                module_registry_alias_stack.append(set())
                module_registry_shadow_stack.append(set())
                operator_alias_stack.append(set())
                operator_shadow_stack.append(set())
                operator_transform_alias_stack.append({})
                operator_transform_shadow_stack.append(set())
                functools_alias_stack.append(set())
                functools_shadow_stack.append(set())
                functools_partial_alias_stack.append(set())
                functools_partial_shadow_stack.append(set())
                qualified_vars_alias_stack.append(set())
                qualified_vars_exact_shadow_stack.append(set())
                qualified_vars_prefix_shadow_stack.append(set())
                receiver_dependent_vars_stack.append({})
                alias_scope_kind_stack.append(kind)
                global_name_stack.append(set())
                nonlocal_name_stack.append(set())

            @staticmethod
            def _pop_alias_scope() -> None:
                nonlocal_name_stack.pop()
                global_name_stack.pop()
                alias_scope_kind_stack.pop()
                receiver_dependent_vars_stack.pop()
                qualified_vars_prefix_shadow_stack.pop()
                qualified_vars_exact_shadow_stack.pop()
                qualified_vars_alias_stack.pop()
                module_registry_shadow_stack.pop()
                module_registry_alias_stack.pop()
                operator_transform_shadow_stack.pop()
                operator_transform_alias_stack.pop()
                operator_shadow_stack.pop()
                operator_alias_stack.pop()
                functools_partial_shadow_stack.pop()
                functools_partial_alias_stack.pop()
                functools_shadow_stack.pop()
                functools_alias_stack.pop()
                sys_shadow_stack.pop()
                sys_alias_stack.pop()
                vars_shadow_stack.pop()
                vars_alias_stack.pop()
                builtins_shadow_stack.pop()
                builtins_alias_stack.pop()
                importlib_shadow_stack.pop()
                importlib_alias_stack.pop()
                resolver_shadow_stack.pop()
                resolver_alias_stack.pop()
                builtin_getattr_shadow_stack.pop()
                builtin_getattr_alias_stack.pop()
                builtin_name_shadow_stack.pop()
                module_shadow_stack.pop()
                module_alias_stack.pop()
                callable_shadow_stack.pop()
                callable_alias_stack.pop()

            def _visit_definition_expressions(
                self,
                arguments: ast.arguments,
                decorators: list[ast.expr] | None = None,
                returns: ast.expr | None = None,
            ) -> None:
                for decorator in decorators or []:
                    self.visit(decorator)
                for default in arguments.defaults:
                    self._record_binding_source_exposure(default, default)
                    self.visit(default)
                for default in arguments.kw_defaults:
                    if default is not None:
                        self._record_binding_source_exposure(default, default)
                        self.visit(default)
                if annotations_are_deferred:
                    return
                annotations = [
                    *(argument.annotation for argument in arguments.posonlyargs),
                    *(argument.annotation for argument in arguments.args),
                    *(argument.annotation for argument in arguments.kwonlyargs),
                ]
                if arguments.vararg is not None:
                    annotations.append(arguments.vararg.annotation)
                if arguments.kwarg is not None:
                    annotations.append(arguments.kwarg.annotation)
                annotations.append(returns)
                for annotation in annotations:
                    if annotation is None:
                        continue
                    self._annotation_call_only_depth += 1
                    try:
                        self.visit(annotation)
                    finally:
                        self._annotation_call_only_depth -= 1

            @staticmethod
            def _in_class_body() -> bool:
                return bool(class_scope_depth_stack) and (
                    class_scope_depth_stack[-1] == len(module_alias_stack) - 1
                )

            def _record_class_exposure(
                self,
                node: ast.AST,
                *,
                bound_names: tuple[str, ...] = (),
                module_kind: str | None,
                call_kind: str | None,
                dynamic_kind: str | None = None,
                vars_alias: bool = False,
                sys_alias: bool = False,
                module_registry_alias: bool = False,
                builtin_getattr_alias: bool = False,
            ) -> None:
                if not self._in_class_body():
                    return
                if class_vars_export_stack:
                    for bound_name in bound_names:
                        if self._conditional_depth <= 0:
                            class_vars_export_stack[-1].difference_update(
                                {
                                    reference
                                    for reference in class_vars_export_stack[-1]
                                    if reference == bound_name
                                    or reference.startswith(f"{bound_name}.")
                                }
                            )
                        if vars_alias:
                            class_vars_export_stack[-1].add(bound_name)
                if class_builtin_getattr_export_stack:
                    for bound_name in bound_names:
                        if self._conditional_depth <= 0:
                            class_builtin_getattr_export_stack[-1].difference_update(
                                {
                                    reference
                                    for reference in class_builtin_getattr_export_stack[-1]
                                    if reference == bound_name
                                    or reference.startswith(f"{bound_name}.")
                                }
                            )
                        if builtin_getattr_alias:
                            class_builtin_getattr_export_stack[-1].add(bound_name)
                if call_kind == DYNAMIC_ATTRIBUTE_CALLABLE_KIND:
                    return
                if call_kind:
                    self._record(node, call_kind)
                elif module_kind:
                    # Class attributes can later be reached through the class, an
                    # instance, classmethod receivers, getattr, or metaprogramming.
                    # Treat exposing a process module on a class as a ratchet hit
                    # instead of pretending every qualified access can be resolved.
                    self._record(node, f"{module_kind}.*")
                elif dynamic_kind:
                    self._record(node, f"dynamic_import.{dynamic_kind}")
                elif builtin_getattr_alias:
                    # Class attributes are reachable through class, instance,
                    # descriptor, inheritance, and metaprogramming paths. Ratchet
                    # the export itself in addition to tracking qualified calls.
                    self._record(node, "builtin.getattr.class_export")
                elif sys_alias or module_registry_alias:
                    self._record(node, "dynamic_import.module_registry")

            def _record_cross_scope_exposure(
                self,
                node: ast.AST,
                *,
                bound_name: str,
                module_kind: str | None,
                call_kind: str | None,
                dynamic_kind: str | None = None,
                vars_alias: bool = False,
                sys_alias: bool = False,
                module_registry_alias: bool = False,
                builtin_getattr_alias: bool = False,
            ) -> None:
                if bound_name not in (
                    global_name_stack[-1] | nonlocal_name_stack[-1]
                ):
                    return
                if call_kind == DYNAMIC_ATTRIBUTE_CALLABLE_KIND:
                    if bound_name in global_name_stack[-1]:
                        callable_alias_stack[0][bound_name] = (
                            DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                        )
                    elif len(callable_alias_stack) > 1:
                        callable_alias_stack[-2][bound_name] = (
                            DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                        )
                    return
                if vars_alias:
                    if bound_name in global_name_stack[-1]:
                        vars_alias_stack[0].add(bound_name)
                    elif len(vars_alias_stack) > 1:
                        vars_alias_stack[-2].add(bound_name)
                if module_registry_alias:
                    if bound_name in global_name_stack[-1]:
                        module_registry_alias_stack[0].add(bound_name)
                    elif len(module_registry_alias_stack) > 1:
                        module_registry_alias_stack[-2].add(bound_name)
                if sys_alias:
                    if bound_name in global_name_stack[-1]:
                        sys_alias_stack[0].add(bound_name)
                    elif len(sys_alias_stack) > 1:
                        sys_alias_stack[-2].add(bound_name)
                if builtin_getattr_alias:
                    if bound_name in global_name_stack[-1]:
                        builtin_getattr_alias_stack[0].add(bound_name)
                    elif len(builtin_getattr_alias_stack) > 1:
                        builtin_getattr_alias_stack[-2].add(bound_name)
                if call_kind:
                    self._record(node, call_kind)
                elif module_kind:
                    # Propagating an execution-capable alias across a lexical
                    # boundary is itself ratcheted; no interprocedural execution
                    # order needs to be guessed to keep the boundary fail-closed.
                    self._record(node, f"{module_kind}.*")
                elif dynamic_kind:
                    self._record(node, f"dynamic_import.{dynamic_kind}")
                elif sys_alias or module_registry_alias:
                    self._record(node, "dynamic_import.module_registry")

            def _record_conditional_alias_exposure(
                self,
                node: ast.AST,
                *,
                bound_name: str,
                module_kind: str | None,
                call_kind: str | None,
                dynamic_kind: str | None = None,
                vars_alias: bool = False,
                sys_alias: bool = False,
                module_registry_alias: bool = False,
                builtin_getattr_alias: bool = False,
            ) -> None:
                if self._conditional_depth <= 0:
                    return
                visible_modules = self._module_aliases()
                previous_module_kind = next(
                    (
                        candidate
                        for candidate, aliases in visible_modules.items()
                        if bound_name in aliases
                    ),
                    None,
                )
                previous_call_kind = self._callable_aliases().get(bound_name)
                previous_sys_alias = bound_name in self._sys_aliases()
                previous_registry_alias = (
                    bound_name in self._module_registry_aliases()
                )
                previous_dynamic_kinds = {
                    kind
                    for kind, aliases in (
                        ("resolver", self._resolver_aliases()),
                        ("importlib", self._importlib_aliases()),
                        ("builtins", self._builtins_aliases()),
                    )
                    if bound_name in aliases
                }
                for observed_call_kind in sorted(
                    {kind for kind in (previous_call_kind, call_kind) if kind}
                ):
                    if observed_call_kind != DYNAMIC_ATTRIBUTE_CALLABLE_KIND:
                        self._record(node, observed_call_kind)
                for observed_module_kind in sorted(
                    {kind for kind in (previous_module_kind, module_kind) if kind}
                ):
                    self._record(node, f"{observed_module_kind}.*")
                for observed_dynamic_kind in sorted(
                    previous_dynamic_kinds | ({dynamic_kind} if dynamic_kind else set())
                ):
                    self._record(node, f"dynamic_import.{observed_dynamic_kind}")
                if (
                    previous_sys_alias
                    or previous_registry_alias
                    or sys_alias
                    or module_registry_alias
                ):
                    self._record(node, "dynamic_import.module_registry")

            def _visit_conditionally(self, nodes: Sequence[ast.AST]) -> None:
                self._conditional_depth += 1
                try:
                    for child in nodes:
                        self.visit(child)
                finally:
                    self._conditional_depth -= 1

            def _record_annotation_call_references(self, node: ast.Call) -> None:
                if not self._annotation_call_only_depth:
                    return
                module_aliases = self._module_aliases()
                callable_aliases = self._callable_aliases()
                vars_references = self._vars_aliases()
                runtime_expressions = [
                    node.func,
                    *node.args,
                    *(keyword.value for keyword in node.keywords),
                ]
                for expression in runtime_expressions:
                    for candidate in ast.walk(expression):
                        registered_module_kind = (
                            self._registered_process_module_kind(candidate)
                        )
                        if registered_module_kind:
                            self._record(
                                candidate,
                                f"{registered_module_kind}.*",
                            )
                        module_dict_kind = _static_module_dict_kind(
                            candidate,
                            module_aliases,
                            vars_references,
                        )
                        if module_dict_kind:
                            self._record(candidate, f"{module_dict_kind}.*")
                        call_kind = self._observed_callable_reference_kind(
                            candidate,
                            module_aliases,
                            callable_aliases,
                        )
                        if call_kind:
                            self._record(candidate, call_kind)

            def _record_binding_source_exposure(
                self,
                node: ast.AST,
                source: ast.AST,
            ) -> None:
                module_aliases = self._module_aliases()
                callable_aliases = self._callable_aliases()
                vars_references = self._vars_aliases()

                def collect(candidate: ast.AST) -> None:
                    if isinstance(candidate, ast.Call):
                        registered_call_kind = (
                            self._registered_process_callable_kind(candidate.func)
                        )
                        if registered_call_kind:
                            self._record(candidate, registered_call_kind)
                            return
                        direct_call_kind = _direct_call_kind(
                            candidate,
                            module_aliases,
                            {},
                            callable_aliases,
                            vars_references,
                            builtin_getattr_available=self._builtin_receiver_available(
                                "getattr"
                            ),
                            builtin_object_available=self._builtin_receiver_available(
                                "object"
                            ),
                            builtin_getattr_owners=self._builtins_aliases(),
                            builtin_getattr_aliases=self._builtin_getattr_aliases(),
                        )
                        if direct_call_kind:
                            self._record(candidate, direct_call_kind)
                            return
                    module_dict_kind = _static_module_dict_kind(
                        candidate,
                        module_aliases,
                        vars_references,
                    )
                    if module_dict_kind:
                        self._record(candidate, f"{module_dict_kind}.*")
                        return
                    module_kind = self._observed_module_reference_kind(
                        candidate,
                        module_aliases,
                    )
                    if module_kind:
                        self._record(candidate, f"{module_kind}.*")
                        return
                    call_kind = self._observed_callable_reference_kind(
                        candidate,
                        module_aliases,
                        callable_aliases,
                    )
                    if call_kind:
                        if call_kind != DYNAMIC_ATTRIBUTE_CALLABLE_KIND:
                            self._record(candidate, call_kind)
                        return
                    if isinstance(candidate, ast.Subscript) and _static_module_dict_kind(
                        candidate.value,
                        module_aliases,
                        vars_references,
                    ):
                        return
                    if (
                        isinstance(candidate, ast.Call)
                        and isinstance(candidate.func, ast.Attribute)
                        and candidate.func.attr
                        in {"__getitem__", "get", "pop", "setdefault"}
                        and _static_module_dict_kind(
                            candidate.func.value,
                            module_aliases,
                            vars_references,
                        )
                    ):
                        return
                    if isinstance(candidate, (ast.List, ast.Tuple, ast.Set)):
                        for item in candidate.elts:
                            collect(item)
                        return
                    if isinstance(candidate, ast.Dict):
                        for item in (*candidate.keys, *candidate.values):
                            if item is not None:
                                collect(item)
                        return
                    if isinstance(candidate, ast.IfExp):
                        collect(candidate.body)
                        collect(candidate.orelse)
                        return
                    if isinstance(candidate, ast.BoolOp):
                        for item in candidate.values:
                            collect(item)
                        return
                    if isinstance(candidate, (ast.NamedExpr, ast.Starred)):
                        collect(candidate.value)
                        return
                    if isinstance(candidate, ast.Lambda):
                        collect(candidate.body)
                        return
                    if isinstance(candidate, ast.Call):
                        # A call owner such as os.scandir does not flow into the
                        # bound target. Known container projection methods do;
                        # arguments may also be returned or invoked by a wrapper.
                        if (
                            isinstance(candidate.func, ast.Attribute)
                            and candidate.func.attr in VALUE_FLOW_METHOD_NAMES
                        ):
                            collect(candidate.func.value)
                        for item in candidate.args:
                            collect(item)
                        for keyword in candidate.keywords:
                            collect(keyword.value)
                        return
                    if isinstance(candidate, ast.Subscript):
                        collect(candidate.value)
                        return
                    if isinstance(candidate, (ast.Await, ast.Yield, ast.YieldFrom)):
                        if candidate.value is not None:
                            collect(candidate.value)
                        return
                    if isinstance(candidate, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
                        collect(candidate.elt)
                        for generator in candidate.generators:
                            collect(generator.iter)
                        return
                    if isinstance(candidate, ast.DictComp):
                        collect(candidate.key)
                        collect(candidate.value)
                        for generator in candidate.generators:
                            collect(generator.iter)

                collect(source)

            def _record_dynamic_callable_escape(
                self,
                node: ast.AST,
                value: ast.AST | None,
            ) -> None:
                # The reviewed CapabilitySpec handler registry is a bounded dispatch
                # table, not a process-launch escape. Any shape change falls back to
                # the conservative dynamic-callable rule below.
                if reviewed_handler_registry_escape(value):
                    return
                observed_kind = self._observed_callable_reference_kind(
                    value,
                    self._module_aliases(),
                    self._callable_aliases(),
                )
                if observed_kind == DYNAMIC_ATTRIBUTE_CALLABLE_KIND:
                    self._record(node, observed_kind)
                    return
                call_kind = self._dynamic_attribute_callable_kind(value)
                if call_kind == DYNAMIC_ATTRIBUTE_CALLABLE_KIND:
                    self._record(node, call_kind)

            def _record_dynamic_callable_argument_escape(
                self,
                node: ast.Call,
            ) -> None:
                contracts = callback_contract_references(node.func, context=node)
                if not contracts:
                    return
                callback_positions: set[int] = set()
                for contract in contracts:
                    positions = callback_parameter_positions.get(contract, set())
                    if callback_contract_consumes_receiver(node.func, contract):
                        callback_positions.update(
                            position - 1 for position in positions if position > 0
                        )
                    else:
                        callback_positions.update(positions)
                callback_names = set().union(
                    *(
                        callback_parameter_names.get(contract, set())
                        | callback_projected_keyword_names.get(contract, set())
                        for contract in contracts
                    )
                )
                candidates = [
                    argument.value
                    if isinstance(argument, ast.Starred)
                    else argument
                    for index, argument in enumerate(node.args)
                    if index in callback_positions
                    or (
                        isinstance(argument, ast.Starred)
                        and bool(callback_positions)
                    )
                ]
                candidates.extend(
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg in callback_names
                    or (keyword.arg is None and bool(callback_names))
                )
                for candidate in candidates:
                    call_kind = self._observed_callable_reference_kind(
                        candidate,
                        self._module_aliases(),
                        self._callable_aliases(),
                    ) or self._returned_callable_kind(
                        candidate
                    ) or self._dynamic_attribute_callable_kind(candidate)
                    if call_kind == DYNAMIC_ATTRIBUTE_CALLABLE_KIND:
                        self._record(node, call_kind)
                        return

            def _record_event_loop_receiver_argument_execution(
                self,
                node: ast.Call,
            ) -> None:
                contracts = callback_contract_references(node.func, context=node)
                if not contracts:
                    return
                candidates: list[tuple[ast.AST, set[str]]] = []
                for contract in contracts:
                    position_kinds = event_loop_receiver_parameter_positions.get(
                        contract,
                        {},
                    )
                    consumes_receiver = callback_contract_consumes_receiver(
                        node.func,
                        contract,
                    )
                    for position, kinds in position_kinds.items():
                        argument_position = (
                            position - 1 if consumes_receiver and position > 0 else position
                        )
                        if argument_position < len(node.args):
                            argument = node.args[argument_position]
                            candidates.append(
                                (
                                    argument.value
                                    if isinstance(argument, ast.Starred)
                                    else argument,
                                    set(kinds),
                                )
                            )
                        elif any(
                            isinstance(argument, ast.Starred)
                            for argument in node.args
                        ):
                            candidates.extend(
                                (argument.value, set(kinds))
                                for argument in node.args
                                if isinstance(argument, ast.Starred)
                            )
                    name_kinds = event_loop_receiver_parameter_names.get(
                        contract,
                        {},
                    )
                    for keyword in node.keywords:
                        if keyword.arg in name_kinds:
                            candidates.append(
                                (keyword.value, set(name_kinds[keyword.arg]))
                            )
                        elif keyword.arg is None and name_kinds:
                            candidates.append(
                                (
                                    keyword.value,
                                    set().union(*name_kinds.values()),
                                )
                            )
                module_aliases = self._module_aliases()
                function_references = {
                    **function_aliases,
                    **self._callable_aliases(),
                }
                recorded: set[str] = set()
                for candidate, kinds in candidates:
                    if not _event_loop_receiver(
                        candidate,
                        module_aliases,
                        function_references,
                    ):
                        continue
                    for kind in kinds - recorded:
                        self._record(node, kind)
                        recorded.add(kind)

            def _bind_dynamic_callable_container_mutation(
                self,
                node: ast.Call,
            ) -> None:
                if not isinstance(node.func, ast.Attribute):
                    return
                argument_indexes = {
                    "__setitem__": (1,),
                    "add": (0,),
                    "append": (0,),
                    "extend": (0,),
                    "insert": (1,),
                    "setdefault": (1,),
                    "update": (0,),
                }.get(node.func.attr)
                if argument_indexes is None:
                    return
                candidates = [
                    node.args[index]
                    for index in argument_indexes
                    if index < len(node.args)
                ]
                if node.func.attr == "update":
                    candidates.extend(keyword.value for keyword in node.keywords)
                for candidate in candidates:
                    call_kind = self._observed_callable_reference_kind(
                        candidate,
                        self._module_aliases(),
                        self._callable_aliases(),
                    ) or self._returned_callable_kind(
                        candidate
                    ) or self._dynamic_attribute_callable_kind(candidate)
                    if call_kind != DYNAMIC_ATTRIBUTE_CALLABLE_KIND:
                        continue
                    target_reference = _dotted_name(node.func.value)
                    if target_reference is None:
                        self._record(node, call_kind)
                        return
                    callable_alias_stack[-1][target_reference] = (
                        DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                    )
                    return

            def _record(self, node: ast.AST, call_kind: str) -> None:
                normalized_kind = _normalized_execution_kind(call_kind)
                function_name = function_stack[-1] if function_stack else "<module>"
                site = (
                    function_name,
                    getattr(node, "lineno", -1),
                    getattr(node, "col_offset", -1),
                    normalized_kind,
                )
                if site in recorded_sites:
                    return
                recorded_sites.add(site)
                records[(relative_path, function_name, normalized_kind)] += 1

            @staticmethod
            def _method_receiver_name(
                node: ast.FunctionDef | ast.AsyncFunctionDef,
            ) -> str | None:
                decorator_names = {
                    _dotted_name(decorator)
                    for decorator in node.decorator_list
                }
                if any(
                    name == "staticmethod" or name.endswith(".staticmethod")
                    for name in decorator_names
                    if name is not None
                ):
                    return None
                positional = [*node.args.posonlyargs, *node.args.args]
                return positional[0].arg if positional else None

            def _visit_class_method_body(
                self,
                node: ast.FunctionDef | ast.AsyncFunctionDef,
                *,
                class_reference: str,
                class_exports: set[str],
                super_exports: set[str],
                class_dynamic_callable_exports: set[str],
                super_dynamic_callable_exports: set[str],
            ) -> None:
                function_stack.append(node.name)
                self._push_function_scope(
                    node.args,
                    (),
                    self._function_local_names(node.body),
                )
                parameter_names = set(self._parameter_names(node.args))
                class_root = class_reference.partition(".")[0]
                if class_root not in parameter_names:
                    qualified_vars_alias_stack[-1].update(
                        f"{class_reference}.{export}"
                        for export in class_exports
                    )
                    callable_alias_stack[-1].update(
                        {
                            f"{class_reference}.{export}": (
                                DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                            )
                            for export in class_dynamic_callable_exports
                        }
                    )
                receiver_name = self._method_receiver_name(node)
                if receiver_name is not None:
                    receiver_references = {
                        reference
                        for export in class_exports
                        for reference in (
                            f"{receiver_name}.{export}",
                            f"{receiver_name}.__class__.{export}",
                        )
                    }
                    qualified_vars_alias_stack[-1].update(receiver_references)
                    receiver_dependent_vars_stack[-1].setdefault(
                        receiver_name,
                        set(),
                    ).update(receiver_references)
                    callable_alias_stack[-1].update(
                        {
                            reference: DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                            for export in class_dynamic_callable_exports
                            for reference in (
                                f"{receiver_name}.{export}",
                                f"{receiver_name}.__class__.{export}",
                            )
                        }
                    )
                    if "__call__" in class_dynamic_callable_exports:
                        callable_alias_stack[-1][receiver_name] = (
                            DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                        )
                    if self._builtin_receiver_available("type"):
                        type_references = {
                            f"type({receiver_name}).{export}"
                            for export in class_exports
                        }
                        qualified_vars_alias_stack[-1].update(type_references)
                        for dependency in (receiver_name, "type"):
                            receiver_dependent_vars_stack[-1].setdefault(
                                dependency,
                                set(),
                            ).update(type_references)
                        callable_alias_stack[-1].update(
                            {
                                f"type({receiver_name}).{export}": (
                                    DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                                )
                                for export in class_dynamic_callable_exports
                            }
                        )
                    if (
                        super_exports
                        and self._builtin_receiver_available("super")
                    ):
                        super_references = {
                            reference
                            for export in super_exports
                            for reference in (
                                f"super().{export}",
                                f"super({class_reference},{receiver_name}).{export}",
                            )
                        }
                        qualified_vars_alias_stack[-1].update(super_references)
                        for dependency in (receiver_name, "super"):
                            receiver_dependent_vars_stack[-1].setdefault(
                                dependency,
                                set(),
                            ).update(super_references)
                    if (
                        super_dynamic_callable_exports
                        and self._builtin_receiver_available("super")
                    ):
                        callable_alias_stack[-1].update(
                            {
                                reference: DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                                for export in super_dynamic_callable_exports
                                for reference in (
                                    f"super().{export}",
                                    f"super({class_reference},{receiver_name}).{export}",
                                )
                            }
                        )
                enclosing_conditional_depth = self._conditional_depth
                self._conditional_depth = 0
                try:
                    for statement in node.body:
                        self.visit(statement)
                finally:
                    self._conditional_depth = enclosing_conditional_depth
                    self._pop_alias_scope()
                    function_stack.pop()

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                if self._in_class_body() and class_method_stack:
                    class_method_stack[-1].append(node)
                self._record_conditional_alias_exposure(
                    node,
                    bound_name=node.name,
                    module_kind=None,
                    call_kind=None,
                )
                bindings = self._parameter_default_bindings(node.args)
                self._visit_definition_expressions(
                    node.args,
                    node.decorator_list,
                    node.returns,
                )
                function_stack.append(node.name)
                self._push_function_scope(
                    node.args,
                    bindings,
                    self._function_local_names(node.body),
                )
                self._shadow_name(node.name, preserve_conditional_vars=False)
                enclosing_conditional_depth = self._conditional_depth
                self._conditional_depth = 0
                try:
                    for statement in node.body:
                        self.visit(statement)
                finally:
                    self._conditional_depth = enclosing_conditional_depth
                    self._pop_alias_scope()
                    function_stack.pop()
                self._shadow_name(node.name)
                self._record_class_exposure(
                    node,
                    bound_names=(node.name,),
                    module_kind=None,
                    call_kind=None,
                )

            def visit_Lambda(self, node: ast.Lambda) -> None:
                bindings = self._parameter_default_bindings(node.args)
                self._visit_definition_expressions(node.args)
                function_stack.append("<lambda>")
                self._push_function_scope(node.args, bindings)
                enclosing_conditional_depth = self._conditional_depth
                self._conditional_depth = 0
                try:
                    self._record_dynamic_callable_escape(node, node.body)
                    self.visit(node.body)
                finally:
                    self._conditional_depth = enclosing_conditional_depth
                    self._pop_alias_scope()
                    function_stack.pop()

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                if self._in_class_body() and class_method_stack:
                    class_method_stack[-1].append(node)
                self._record_conditional_alias_exposure(
                    node,
                    bound_name=node.name,
                    module_kind=None,
                    call_kind=None,
                )
                bindings = self._parameter_default_bindings(node.args)
                self._visit_definition_expressions(
                    node.args,
                    node.decorator_list,
                    node.returns,
                )
                function_stack.append(node.name)
                self._push_function_scope(
                    node.args,
                    bindings,
                    self._function_local_names(node.body),
                )
                self._shadow_name(node.name, preserve_conditional_vars=False)
                enclosing_conditional_depth = self._conditional_depth
                self._conditional_depth = 0
                try:
                    for statement in node.body:
                        self.visit(statement)
                finally:
                    self._conditional_depth = enclosing_conditional_depth
                    self._pop_alias_scope()
                    function_stack.pop()
                self._shadow_name(node.name)
                self._record_class_exposure(
                    node,
                    bound_names=(node.name,),
                    module_kind=None,
                    call_kind=None,
                )

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                defined_in_class_body = self._in_class_body()
                class_reference = (
                    f"{class_reference_stack[-1]}.{node.name}"
                    if defined_in_class_body and class_reference_stack
                    else node.name
                )
                inherited_class_exports = {
                    suffix.removeprefix(".")
                    for base in node.bases
                    for suffix in self._qualified_vars_descendants(
                        self._qualified_vars_source_reference(base)
                    )
                }
                inherited_builtin_getattr_exports = {
                    suffix.removeprefix(".")
                    for base in node.bases
                    for suffix in self._builtin_getattr_descendants(
                        _dotted_name(base)
                    )
                }
                inherited_dynamic_callable_exports = {
                    suffix.removeprefix(".")
                    for base in node.bases
                    for suffix in self._dynamic_callable_descendants(
                        _dotted_name(base)
                    )
                }
                self._record_conditional_alias_exposure(
                    node,
                    bound_name=node.name,
                    module_kind=None,
                    call_kind=None,
                )
                for decorator in node.decorator_list:
                    self.visit(decorator)
                for base in node.bases:
                    self.visit(base)
                for keyword in node.keywords:
                    self.visit(keyword.value)
                function_stack.append(node.name)
                self._push_alias_scope("class")
                callable_alias_stack[-1].update(
                    {
                        export: DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                        for export in inherited_dynamic_callable_exports
                    }
                )
                class_scope_depth_stack.append(len(module_alias_stack) - 1)
                class_vars_export_stack.append(inherited_class_exports)
                class_builtin_getattr_export_stack.append(
                    inherited_builtin_getattr_exports
                )
                class_method_stack.append([])
                class_reference_stack.append(class_reference)
                class_exports: set[str] = set()
                class_builtin_getattr_exports: set[str] = set()
                class_dynamic_callable_exports: set[str] = set()
                try:
                    for statement in node.body:
                        self.visit(statement)
                    class_exports = set(class_vars_export_stack[-1])
                    class_builtin_getattr_exports = set(
                        class_builtin_getattr_export_stack[-1]
                    )
                    class_dynamic_callable_exports = {
                        reference
                        for reference, kind in callable_alias_stack[-1].items()
                        if kind == DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                    }
                    for method in tuple(class_method_stack[-1]):
                        self._visit_class_method_body(
                            method,
                            class_reference=class_reference,
                            class_exports=class_exports,
                            super_exports=inherited_class_exports,
                            class_dynamic_callable_exports=(
                                class_dynamic_callable_exports
                            ),
                            super_dynamic_callable_exports=(
                                inherited_dynamic_callable_exports
                            ),
                        )
                finally:
                    class_reference_stack.pop()
                    class_method_stack.pop()
                    class_builtin_getattr_export_stack.pop()
                    class_vars_export_stack.pop()
                    class_scope_depth_stack.pop()
                    self._pop_alias_scope()
                    function_stack.pop()
                self._shadow_name(node.name)
                for reference in tuple(callable_alias_stack[-1]):
                    if reference.startswith(f"{node.name}."):
                        callable_alias_stack[-1].pop(reference, None)
                callable_alias_stack[-1].update(
                    {
                        f"{node.name}.{export}": DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                        for export in class_dynamic_callable_exports
                    }
                )
                qualified_exports = {
                    f"{node.name}.{export}" for export in class_exports
                }
                qualified_builtin_getattr_exports = {
                    f"{node.name}.{export}"
                    for export in class_builtin_getattr_exports
                }
                if defined_in_class_body and class_vars_export_stack:
                    if self._conditional_depth <= 0:
                        class_vars_export_stack[-1].difference_update(
                            {
                                reference
                                for reference in class_vars_export_stack[-1]
                                if reference == node.name
                                or reference.startswith(f"{node.name}.")
                            }
                        )
                    class_vars_export_stack[-1].update(qualified_exports)
                    class_builtin_getattr_export_stack[-1].update(
                        qualified_builtin_getattr_exports
                    )
                else:
                    qualified_vars_alias_stack[-1].update(qualified_exports)
                    builtin_getattr_alias_stack[-1].update(
                        qualified_builtin_getattr_exports
                    )

            def visit_Import(self, node: ast.Import) -> None:
                for alias in node.names:
                    bound_name = alias.asname or alias.name.split(".", maxsplit=1)[0]
                    module_kind = _imported_process_module_kind(alias.name)
                    sys_alias = alias.name == "sys"
                    operator_alias = alias.name == "operator"
                    functools_alias = alias.name == "functools"
                    dynamic_kind = None
                    if alias.name == "importlib" or (
                        alias.name.startswith("importlib.") and alias.asname is None
                    ):
                        dynamic_kind = "importlib"
                    elif alias.name == "builtins":
                        dynamic_kind = "builtins"
                    self._record_conditional_alias_exposure(
                        node,
                        bound_name=bound_name,
                        module_kind=module_kind,
                        call_kind=None,
                        dynamic_kind=dynamic_kind,
                        sys_alias=sys_alias,
                    )
                    self._bind_aliases(
                        bound_name,
                        module_kind=module_kind,
                        call_kind=None,
                        dynamic_kind=dynamic_kind,
                        sys_alias=sys_alias,
                        operator_alias=operator_alias,
                        functools_alias=functools_alias,
                    )
                    if alias.asname is None and "." in alias.name and module_kind:
                        self._bind_aliases(
                            alias.name,
                            module_kind=module_kind,
                            call_kind=None,
                        )
                    self._record_class_exposure(
                        node,
                        bound_names=(bound_name,),
                        module_kind=module_kind,
                        call_kind=None,
                        dynamic_kind=dynamic_kind,
                        sys_alias=sys_alias,
                    )
                    self._record_cross_scope_exposure(
                        node,
                        bound_name=bound_name,
                        module_kind=module_kind,
                        call_kind=None,
                        dynamic_kind=dynamic_kind,
                        sys_alias=sys_alias,
                    )

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                module_kind = _imported_process_module_kind(node.module)
                for alias in node.names:
                    if alias.name == "*":
                        if module_kind:
                            self._record(node, f"{module_kind}.*")
                        if node.level == 0 and node.module == "functools":
                            self._shadow_name("partial")
                            functools_partial_alias_stack[-1].add("partial")
                        continue
                    bound_name = alias.asname or alias.name
                    imported_kind = (
                        f"{module_kind}.{alias.name}"
                        if module_kind is not None
                        else None
                    )
                    bound_module_kind = (
                        "asyncio"
                        if node.module == "asyncio"
                        and alias.name in {"events", "subprocess"}
                        else None
                    )
                    bound_call_kind = (
                        f"dynamic_code.{alias.name}"
                        if node.module == "builtins"
                        and alias.name in {"eval", "exec"}
                        else (
                            imported_kind
                            if imported_kind is not None
                            and _approved_process_call_kind(imported_kind)
                            else None
                        )
                    )
                    dynamic_kind = (
                        "resolver"
                        if (
                            node.module == "importlib" and alias.name == "import_module"
                        )
                        or (node.module == "builtins" and alias.name == "__import__")
                        else None
                    )
                    vars_alias = node.module == "builtins" and alias.name == "vars"
                    builtin_getattr_alias = (
                        node.module == "builtins" and alias.name == "getattr"
                    )
                    module_registry_alias = (
                        node.module == "sys" and alias.name == "modules"
                    )
                    operator_transform_kind = (
                        alias.name
                        if node.module == "operator"
                        and alias.name
                        in {"attrgetter", "methodcaller", "getitem", "itemgetter"}
                        else None
                    )
                    functools_partial_alias = (
                        node.level == 0
                        and node.module == "functools"
                        and alias.name == "partial"
                    )
                    self._record_conditional_alias_exposure(
                        node,
                        bound_name=bound_name,
                        module_kind=bound_module_kind,
                        call_kind=bound_call_kind,
                        dynamic_kind=dynamic_kind,
                        vars_alias=vars_alias,
                        module_registry_alias=module_registry_alias,
                        builtin_getattr_alias=builtin_getattr_alias,
                    )
                    self._bind_aliases(
                        bound_name,
                        module_kind=bound_module_kind,
                        call_kind=bound_call_kind,
                        dynamic_kind=dynamic_kind,
                        vars_alias=vars_alias,
                        module_registry_alias=module_registry_alias,
                        operator_transform_kind=operator_transform_kind,
                        builtin_getattr_alias=builtin_getattr_alias,
                        functools_partial_alias=functools_partial_alias,
                    )
                    self._record_class_exposure(
                        node,
                        bound_names=(bound_name,),
                        module_kind=bound_module_kind,
                        call_kind=bound_call_kind,
                        dynamic_kind=dynamic_kind,
                        vars_alias=vars_alias,
                        module_registry_alias=module_registry_alias,
                        builtin_getattr_alias=builtin_getattr_alias,
                    )
                    self._record_cross_scope_exposure(
                        node,
                        bound_name=bound_name,
                        module_kind=bound_module_kind,
                        call_kind=bound_call_kind,
                        dynamic_kind=dynamic_kind,
                        vars_alias=vars_alias,
                        module_registry_alias=module_registry_alias,
                        builtin_getattr_alias=builtin_getattr_alias,
                    )

            def visit_Global(self, node: ast.Global) -> None:
                global_name_stack[-1].update(node.names)

            def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
                nonlocal_name_stack[-1].update(node.names)

            def visit_If(self, node: ast.If) -> None:
                self.visit(node.test)
                self._visit_conditionally([*node.body, *node.orelse])

            def visit_IfExp(self, node: ast.IfExp) -> None:
                self.visit(node.test)
                self._visit_conditionally([node.body, node.orelse])

            def visit_BoolOp(self, node: ast.BoolOp) -> None:
                if not node.values:
                    return
                self.visit(node.values[0])
                self._visit_conditionally(node.values[1:])

            def _visit_iteration(
                self,
                node: ast.For | ast.AsyncFor,
            ) -> None:
                self._record_binding_source_exposure(node, node.iter)
                self.visit(node.iter)
                call_kind = self._observed_callable_reference_kind(
                    node.iter,
                    self._module_aliases(),
                    self._callable_aliases(),
                ) or self._returned_callable_kind(
                    node.iter
                ) or self._dynamic_attribute_callable_kind(node.iter)
                self._conditional_depth += 1
                try:
                    for name in self._bound_names(node.target):
                        self._record_conditional_alias_exposure(
                            node,
                            bound_name=name,
                            module_kind=None,
                            call_kind=call_kind,
                        )
                        self._bind_aliases(
                            name,
                            module_kind=None,
                            call_kind=call_kind,
                        )
                    self.visit(node.target)
                    for child in (*node.body, *node.orelse):
                        self.visit(child)
                finally:
                    self._conditional_depth -= 1

            def visit_For(self, node: ast.For) -> None:
                self._visit_iteration(node)

            def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
                self._visit_iteration(node)

            def visit_While(self, node: ast.While) -> None:
                self.visit(node.test)
                self._visit_conditionally([*node.body, *node.orelse])

            def visit_Try(self, node: ast.Try) -> None:
                conditional_nodes: list[ast.AST] = [*node.body, *node.orelse]
                for handler in node.handlers:
                    if handler.type is not None:
                        conditional_nodes.append(handler.type)
                    conditional_nodes.extend(handler.body)
                self._visit_conditionally(conditional_nodes)
                for statement in node.finalbody:
                    self.visit(statement)

            def visit_TryStar(self, node: ast.TryStar) -> None:
                self.visit_Try(node)

            def visit_Match(self, node: ast.Match) -> None:
                self._record_binding_source_exposure(node, node.subject)
                self.visit(node.subject)
                conditional_nodes: list[ast.AST] = []
                for case in node.cases:
                    conditional_nodes.append(case.pattern)
                    if case.guard is not None:
                        conditional_nodes.append(case.guard)
                    conditional_nodes.extend(case.body)
                self._visit_conditionally(conditional_nodes)

            def visit_With(self, node: ast.With) -> None:
                for item in node.items:
                    aliases = self._callable_aliases()
                    current_module_aliases = self._module_aliases()
                    self._record_binding_source_exposure(node, item.context_expr)
                    self.visit(item.context_expr)
                    if item.optional_vars is not None:
                        target_module_kinds = self._bound_module_kinds(
                            item.optional_vars,
                            item.context_expr,
                            current_module_aliases,
                            aliases,
                        )
                        for name in self._bound_names(item.optional_vars):
                            self._bind_aliases(
                                name,
                                module_kind=target_module_kinds.get(name),
                                call_kind=None,
                            )
                        for reference in self._qualified_bound_references(
                            item.optional_vars
                        ):
                            self._bind_aliases(
                                reference,
                                module_kind=target_module_kinds.get(reference),
                                call_kind=None,
                            )
                        self.visit(item.optional_vars)
                self._visit_conditionally(node.body)

            def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
                self.visit_With(node)

            def _visit_conditional_comprehension(self, node: ast.AST) -> None:
                self._conditional_depth += 1
                try:
                    for generator in getattr(node, "generators", ()):
                        self._record_binding_source_exposure(generator, generator.iter)
                    self.generic_visit(node)
                finally:
                    self._conditional_depth -= 1

            visit_ListComp = _visit_conditional_comprehension
            visit_SetComp = _visit_conditional_comprehension
            visit_DictComp = _visit_conditional_comprehension
            visit_GeneratorExp = _visit_conditional_comprehension

            def visit_Assign(self, node: ast.Assign) -> None:
                aliases = self._callable_aliases()
                current_module_aliases = self._module_aliases()
                module_kind = self._observed_module_or_event_loop_reference_kind(
                    node.value,
                    current_module_aliases,
                    aliases,
                )
                call_kind = self._observed_callable_reference_kind(
                    node.value,
                    current_module_aliases,
                    aliases,
                ) or self._returned_callable_kind(
                    node.value
                ) or self._dynamic_attribute_callable_kind(node.value)
                dynamic_kind = self._dynamic_reference_kind(node.value)
                vars_alias = self._vars_reference(node.value)
                sys_alias = self._sys_reference(node.value)
                module_registry_alias = self._module_registry_reference(node.value)
                builtin_getattr_alias = self._builtin_getattr_reference(node.value)
                functools_partial_alias = self._functools_partial_reference(
                    node.value
                )
                qualified_suffixes = self._qualified_vars_descendants(
                    self._qualified_vars_source_reference(node.value)
                )
                dynamic_callable_suffixes = self._dynamic_callable_descendants(
                    self._dynamic_callable_source_reference(node.value)
                )
                self._record_binding_source_exposure(node, node.value)
                for target in node.targets:
                    target_module_kinds = self._bound_module_kinds(
                        target,
                        node.value,
                        current_module_aliases,
                        aliases,
                    )
                    target_builtin_getattr_names = (
                        self._builtin_getattr_bound_names(target, node.value)
                    )
                    for name in self._bound_names(target):
                        self._record_conditional_alias_exposure(
                            node,
                            bound_name=name,
                            module_kind=target_module_kinds.get(name),
                            call_kind=(
                                call_kind
                                if isinstance(target, ast.Name)
                                or call_kind == DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                                else None
                            ),
                            dynamic_kind=(
                                dynamic_kind if isinstance(target, ast.Name) else None
                            ),
                            vars_alias=(vars_alias if isinstance(target, ast.Name) else False),
                            sys_alias=(sys_alias if isinstance(target, ast.Name) else False),
                            module_registry_alias=(
                                module_registry_alias
                                if isinstance(target, ast.Name)
                                else False
                            ),
                            builtin_getattr_alias=(
                                name in target_builtin_getattr_names
                            ),
                        )
                self.visit(node.value)
                self._record_class_exposure(
                    node,
                    bound_names=tuple(
                        name
                        for target in node.targets
                        if isinstance(target, ast.Name)
                        for name in self._bound_names(target)
                    ),
                    module_kind=module_kind,
                    call_kind=call_kind,
                    dynamic_kind=dynamic_kind,
                    vars_alias=vars_alias,
                    sys_alias=sys_alias,
                    module_registry_alias=module_registry_alias,
                    builtin_getattr_alias=builtin_getattr_alias,
                )
                for target in node.targets:
                    target_module_kinds = self._bound_module_kinds(
                        target,
                        node.value,
                        current_module_aliases,
                        aliases,
                    )
                    target_builtin_getattr_names = (
                        self._builtin_getattr_bound_names(target, node.value)
                    )
                    if not isinstance(target, ast.Name) and target_builtin_getattr_names:
                        self._record_class_exposure(
                            node,
                            bound_names=tuple(sorted(target_builtin_getattr_names)),
                            module_kind=None,
                            call_kind=None,
                            builtin_getattr_alias=True,
                        )
                    names = self._bound_names(target)
                    for name in names:
                        self._record_cross_scope_exposure(
                            node,
                            bound_name=name,
                            module_kind=target_module_kinds.get(name),
                            call_kind=(
                                call_kind
                                if isinstance(target, ast.Name)
                                or call_kind == DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                                else None
                            ),
                            dynamic_kind=(
                                dynamic_kind if isinstance(target, ast.Name) else None
                            ),
                            vars_alias=(vars_alias if isinstance(target, ast.Name) else False),
                            sys_alias=(sys_alias if isinstance(target, ast.Name) else False),
                            module_registry_alias=(
                                module_registry_alias
                                if isinstance(target, ast.Name)
                                else False
                            ),
                            builtin_getattr_alias=(
                                name in target_builtin_getattr_names
                            ),
                        )
                        self._bind_aliases(
                            name,
                            module_kind=target_module_kinds.get(name),
                            call_kind=(
                                call_kind
                                if isinstance(target, ast.Name)
                                or call_kind == DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                                else None
                            ),
                            dynamic_kind=(
                                dynamic_kind if isinstance(target, ast.Name) else None
                            ),
                            vars_alias=(vars_alias if isinstance(target, ast.Name) else False),
                            sys_alias=(sys_alias if isinstance(target, ast.Name) else False),
                            module_registry_alias=(
                                module_registry_alias
                                if isinstance(target, ast.Name)
                                else False
                            ),
                            builtin_getattr_alias=(
                                name in target_builtin_getattr_names
                            ),
                            functools_partial_alias=(
                                functools_partial_alias
                                if isinstance(target, ast.Name)
                                else False
                            ),
                        )
                        if isinstance(target, ast.Name):
                            qualified_vars_alias_stack[-1].update(
                                f"{name}{suffix}" for suffix in qualified_suffixes
                            )
                            callable_alias_stack[-1].update(
                                {
                                    f"{name}{suffix}": (
                                        DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                                    )
                                    for suffix in dynamic_callable_suffixes
                                }
                            )
                            if ".__call__" in dynamic_callable_suffixes:
                                callable_alias_stack[-1][name] = (
                                    DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                                )
                    for reference in self._qualified_bound_references(target):
                        self._bind_aliases(
                            reference,
                            module_kind=target_module_kinds.get(reference),
                            call_kind=None,
                        )
                    target_reference = _dotted_name(target)
                    if target_reference is not None and not isinstance(target, ast.Name):
                        self._bind_qualified_dynamic_callable_target(
                            target_reference,
                            call_kind=call_kind,
                            descendant_suffixes=dynamic_callable_suffixes,
                        )
                        self._bind_qualified_vars_target(
                            target_reference,
                            vars_alias=vars_alias,
                        )
                        self._bind_qualified_builtin_getattr_target(
                            node,
                            target_reference,
                            builtin_getattr_alias=builtin_getattr_alias,
                        )

            def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
                aliases = self._callable_aliases()
                current_module_aliases = self._module_aliases()
                module_kind = self._observed_module_or_event_loop_reference_kind(
                    node.value,
                    current_module_aliases,
                    aliases,
                )
                call_kind = self._observed_callable_reference_kind(
                    node.value,
                    current_module_aliases,
                    aliases,
                ) or self._returned_callable_kind(
                    node.value
                ) or self._dynamic_attribute_callable_kind(node.value)
                dynamic_kind = self._dynamic_reference_kind(node.value)
                vars_alias = self._vars_reference(node.value)
                sys_alias = self._sys_reference(node.value)
                module_registry_alias = self._module_registry_reference(node.value)
                builtin_getattr_alias = self._builtin_getattr_reference(node.value)
                functools_partial_alias = self._functools_partial_reference(
                    node.value
                )
                qualified_suffixes = self._qualified_vars_descendants(
                    self._qualified_vars_source_reference(node.value)
                )
                dynamic_callable_suffixes = self._dynamic_callable_descendants(
                    self._dynamic_callable_source_reference(node.value)
                )
                if node.value is not None:
                    self._record_binding_source_exposure(node, node.value)
                for name in self._bound_names(node.target):
                    self._record_conditional_alias_exposure(
                        node,
                        bound_name=name,
                        module_kind=(module_kind if isinstance(node.target, ast.Name) else None),
                        call_kind=(call_kind if isinstance(node.target, ast.Name) else None),
                        dynamic_kind=(
                            dynamic_kind if isinstance(node.target, ast.Name) else None
                        ),
                        vars_alias=(vars_alias if isinstance(node.target, ast.Name) else False),
                        sys_alias=(sys_alias if isinstance(node.target, ast.Name) else False),
                        module_registry_alias=(
                            module_registry_alias
                            if isinstance(node.target, ast.Name)
                            else False
                        ),
                        builtin_getattr_alias=(
                            builtin_getattr_alias
                            if isinstance(node.target, ast.Name)
                            else False
                        ),
                    )
                if node.value is not None:
                    self.visit(node.value)
                self._record_class_exposure(
                    node,
                    bound_names=(
                        self._bound_names(node.target)
                        if isinstance(node.target, ast.Name)
                        else ()
                    ),
                    module_kind=module_kind,
                    call_kind=call_kind,
                    dynamic_kind=dynamic_kind,
                    vars_alias=vars_alias,
                    sys_alias=sys_alias,
                    module_registry_alias=module_registry_alias,
                    builtin_getattr_alias=builtin_getattr_alias,
                )
                for name in self._bound_names(node.target):
                    self._record_cross_scope_exposure(
                        node,
                        bound_name=name,
                        module_kind=(module_kind if isinstance(node.target, ast.Name) else None),
                        call_kind=(call_kind if isinstance(node.target, ast.Name) else None),
                        dynamic_kind=(
                            dynamic_kind if isinstance(node.target, ast.Name) else None
                        ),
                        vars_alias=(vars_alias if isinstance(node.target, ast.Name) else False),
                        sys_alias=(sys_alias if isinstance(node.target, ast.Name) else False),
                        module_registry_alias=(
                            module_registry_alias
                            if isinstance(node.target, ast.Name)
                            else False
                        ),
                        builtin_getattr_alias=(
                            builtin_getattr_alias
                            if isinstance(node.target, ast.Name)
                            else False
                        ),
                    )
                    self._bind_aliases(
                        name,
                        module_kind=(module_kind if isinstance(node.target, ast.Name) else None),
                        call_kind=(call_kind if isinstance(node.target, ast.Name) else None),
                        dynamic_kind=(
                            dynamic_kind if isinstance(node.target, ast.Name) else None
                        ),
                        vars_alias=(vars_alias if isinstance(node.target, ast.Name) else False),
                        sys_alias=(sys_alias if isinstance(node.target, ast.Name) else False),
                        module_registry_alias=(
                            module_registry_alias
                            if isinstance(node.target, ast.Name)
                            else False
                        ),
                        builtin_getattr_alias=(
                            builtin_getattr_alias
                            if isinstance(node.target, ast.Name)
                            else False
                        ),
                        functools_partial_alias=(
                            functools_partial_alias
                            if isinstance(node.target, ast.Name)
                            else False
                        ),
                    )
                    if isinstance(node.target, ast.Name):
                        qualified_vars_alias_stack[-1].update(
                            f"{name}{suffix}" for suffix in qualified_suffixes
                        )
                        callable_alias_stack[-1].update(
                            {
                                f"{name}{suffix}": DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                                for suffix in dynamic_callable_suffixes
                            }
                        )
                        if ".__call__" in dynamic_callable_suffixes:
                            callable_alias_stack[-1][name] = (
                                DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                            )
                if node.value is not None:
                    target_module_kinds = self._bound_module_kinds(
                        node.target,
                        node.value,
                        current_module_aliases,
                        aliases,
                    )
                    for reference in self._qualified_bound_references(
                        node.target
                    ):
                        self._bind_aliases(
                            reference,
                            module_kind=target_module_kinds.get(reference),
                            call_kind=None,
                        )
                target_reference = _dotted_name(node.target)
                if (
                    node.value is not None
                    and target_reference is not None
                    and not isinstance(node.target, ast.Name)
                ):
                    self._bind_qualified_dynamic_callable_target(
                        target_reference,
                        call_kind=call_kind,
                        descendant_suffixes=dynamic_callable_suffixes,
                    )
                    self._bind_qualified_vars_target(
                        target_reference,
                        vars_alias=vars_alias,
                    )
                    self._bind_qualified_builtin_getattr_target(
                        node,
                        target_reference,
                        builtin_getattr_alias=builtin_getattr_alias,
                    )

            def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
                aliases = self._callable_aliases()
                current_module_aliases = self._module_aliases()
                module_kind = self._observed_module_or_event_loop_reference_kind(
                    node.value,
                    current_module_aliases,
                    aliases,
                )
                call_kind = self._observed_callable_reference_kind(
                    node.value,
                    current_module_aliases,
                    aliases,
                ) or self._returned_callable_kind(
                    node.value
                ) or self._dynamic_attribute_callable_kind(node.value)
                dynamic_kind = self._dynamic_reference_kind(node.value)
                vars_alias = self._vars_reference(node.value)
                sys_alias = self._sys_reference(node.value)
                module_registry_alias = self._module_registry_reference(node.value)
                builtin_getattr_alias = self._builtin_getattr_reference(node.value)
                functools_partial_alias = self._functools_partial_reference(
                    node.value
                )
                qualified_suffixes = self._qualified_vars_descendants(
                    self._qualified_vars_source_reference(node.value)
                )
                dynamic_callable_suffixes = self._dynamic_callable_descendants(
                    self._dynamic_callable_source_reference(node.value)
                )
                self._record_binding_source_exposure(node, node.value)
                if isinstance(node.target, ast.Name):
                    self._record_conditional_alias_exposure(
                        node,
                        bound_name=node.target.id,
                        module_kind=module_kind,
                        call_kind=call_kind,
                        dynamic_kind=dynamic_kind,
                        vars_alias=vars_alias,
                        sys_alias=sys_alias,
                        module_registry_alias=module_registry_alias,
                        builtin_getattr_alias=builtin_getattr_alias,
                    )
                self.visit(node.value)
                self._record_class_exposure(
                    node,
                    bound_names=(
                        (node.target.id,)
                        if isinstance(node.target, ast.Name)
                        else ()
                    ),
                    module_kind=module_kind,
                    call_kind=call_kind,
                    dynamic_kind=dynamic_kind,
                    vars_alias=vars_alias,
                    sys_alias=sys_alias,
                    module_registry_alias=module_registry_alias,
                    builtin_getattr_alias=builtin_getattr_alias,
                )
                if isinstance(node.target, ast.Name):
                    self._record_cross_scope_exposure(
                        node,
                        bound_name=node.target.id,
                        module_kind=module_kind,
                        call_kind=call_kind,
                        dynamic_kind=dynamic_kind,
                        vars_alias=vars_alias,
                        sys_alias=sys_alias,
                        module_registry_alias=module_registry_alias,
                        builtin_getattr_alias=builtin_getattr_alias,
                    )
                    self._bind_aliases(
                        node.target.id,
                        module_kind=module_kind,
                        call_kind=call_kind,
                        dynamic_kind=dynamic_kind,
                        vars_alias=vars_alias,
                        sys_alias=sys_alias,
                        module_registry_alias=module_registry_alias,
                        builtin_getattr_alias=builtin_getattr_alias,
                        functools_partial_alias=functools_partial_alias,
                    )
                    qualified_vars_alias_stack[-1].update(
                        f"{node.target.id}{suffix}"
                        for suffix in qualified_suffixes
                    )
                    callable_alias_stack[-1].update(
                        {
                            f"{node.target.id}{suffix}": (
                                DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                            )
                            for suffix in dynamic_callable_suffixes
                        }
                    )
                    if ".__call__" in dynamic_callable_suffixes:
                        callable_alias_stack[-1][node.target.id] = (
                            DYNAMIC_ATTRIBUTE_CALLABLE_KIND
                        )

            def visit_AugAssign(self, node: ast.AugAssign) -> None:
                call_kind = self._observed_callable_reference_kind(
                    node.value,
                    self._module_aliases(),
                    self._callable_aliases(),
                ) or self._returned_callable_kind(
                    node.value
                ) or self._dynamic_attribute_callable_kind(node.value)
                self._record_binding_source_exposure(node, node.value)
                self.visit(node.value)
                if call_kind is not None:
                    target_reference = _dotted_name(node.target)
                    if target_reference is None:
                        self._record(node, call_kind)
                    else:
                        callable_alias_stack[-1][target_reference] = call_kind
                self.visit(node.target)

            def visit_Delete(self, node: ast.Delete) -> None:
                for target in node.targets:
                    self.visit(target)
                    for name in self._bound_names(target):
                        self._delete_name(name)

            def visit_Call(self, node: ast.Call) -> None:
                aliases = self._callable_aliases()
                self._record_literal_code_execution(node)
                self._record_dynamic_callable_argument_escape(node)
                self._record_event_loop_receiver_argument_execution(node)
                self._bind_dynamic_callable_container_mutation(node)
                returned_event_loop_call_kind = (
                    self._returned_event_loop_process_call_kind(node)
                )
                if returned_event_loop_call_kind is not None:
                    self._record(node, returned_event_loop_call_kind)
                returned_callee_kind = self._returned_callable_kind(node.func)
                if returned_callee_kind is not None:
                    self._record(node, returned_callee_kind)
                operator_call_kind = self._operator_process_transform_kind(node)
                if operator_call_kind:
                    self._record(node, operator_call_kind)
                registered_call_kind = self._registered_process_callable_kind(node.func)
                if registered_call_kind:
                    self._record(node, registered_call_kind)
                dynamic_call_kind = self._resolved_dynamic_process_call_kind(node)
                if dynamic_call_kind:
                    self._record(node, dynamic_call_kind)
                resolved_module_kind = self._resolved_process_module_kind(node)
                if resolved_module_kind:
                    self._record(node, f"{resolved_module_kind}.*")
                call_kind = _direct_call_kind(
                    node,
                    self._module_aliases(),
                    {},
                    aliases,
                    self._vars_aliases(),
                    builtin_getattr_available=self._builtin_receiver_available(
                        "getattr"
                    ),
                    builtin_object_available=self._builtin_receiver_available(
                        "object"
                    ),
                    builtin_getattr_owners=self._builtins_aliases(),
                    builtin_getattr_aliases=self._builtin_getattr_aliases(),
                )
                if call_kind:
                    self._record(node, call_kind)
                reference_kind = self._observed_callable_reference_kind(
                    node,
                    self._module_aliases(),
                    aliases,
                )
                if reference_kind:
                    self._record(node, reference_kind)
                self._record_annotation_call_references(node)
                self.generic_visit(node)

            def visit_Return(self, node: ast.Return) -> None:
                if node.value is not None:
                    self._record_dynamic_callable_escape(node, node.value)
                    self._record_binding_source_exposure(node, node.value)
                    self.visit(node.value)

            def visit_Yield(self, node: ast.Yield) -> None:
                if node.value is not None:
                    self._record_dynamic_callable_escape(node, node.value)
                    self._record_binding_source_exposure(node, node.value)
                    self.visit(node.value)

            def visit_YieldFrom(self, node: ast.YieldFrom) -> None:
                self._record_dynamic_callable_escape(node, node.value)
                self._record_binding_source_exposure(node, node.value)
                self.visit(node.value)

            def visit_Attribute(self, node: ast.Attribute) -> None:
                if self._annotation_call_only_depth:
                    self.generic_visit(node)
                    return
                aliases = self._callable_aliases()
                module_aliases = self._module_aliases()
                call_kind = self._observed_callable_reference_kind(
                    node,
                    module_aliases,
                    aliases,
                )
                if call_kind and call_kind != DYNAMIC_ATTRIBUTE_CALLABLE_KIND:
                    self._record(node, call_kind)
                self.generic_visit(node)

            def visit_Name(self, node: ast.Name) -> None:
                if not isinstance(node.ctx, ast.Load):
                    return
                if self._annotation_call_only_depth:
                    return
                aliases = self._callable_aliases()
                call_kind = _callable_reference_kind(
                    node,
                    self._module_aliases(),
                    {},
                    aliases,
                    self._vars_aliases(),
                    builtin_getattr_available=self._builtin_receiver_available(
                        "getattr"
                    ),
                    builtin_object_available=self._builtin_receiver_available(
                        "object"
                    ),
                    builtin_getattr_owners=self._builtins_aliases(),
                    builtin_getattr_aliases=self._builtin_getattr_aliases(),
                )
                if call_kind and call_kind != DYNAMIC_ATTRIBUTE_CALLABLE_KIND:
                    self._record(node, call_kind)

            def visit_Subscript(self, node: ast.Subscript) -> None:
                registered_module_kind = self._registered_process_module_kind(node)
                if registered_module_kind:
                    self._record(node, f"{registered_module_kind}.*")
                call_kind = self._observed_callable_reference_kind(
                    node,
                    self._module_aliases(),
                    self._callable_aliases(),
                )
                if call_kind and call_kind != DYNAMIC_ATTRIBUTE_CALLABLE_KIND:
                    self._record(node, call_kind)
                self.generic_visit(node)

        Visitor().visit(tree)
    return records


def _event_loop_policy_receiver(
    node: ast.AST | None,
    module_aliases: dict[str, set[str]],
    function_aliases: dict[str, str],
) -> bool:
    if isinstance(node, ast.NamedExpr):
        return _event_loop_policy_receiver(
            node.value,
            module_aliases,
            function_aliases,
        )
    if isinstance(node, ast.Call):
        factory_reference = _dotted_name(node.func)
        if function_aliases.get(factory_reference or "") == (
            "asyncio.get_event_loop_policy"
        ):
            return True
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_event_loop_policy"
            and _module_reference_kind(node.func.value, module_aliases) == "asyncio"
        ):
            return True
    reference = _dotted_name(node)
    if reference is None:
        return False
    leaf = reference.rpartition(".")[2].lower()
    return leaf in {"event_loop_policy", "policy"} or leaf.endswith(
        "_loop_policy"
    )


def _asyncio_runner_receiver(
    node: ast.AST | None,
    module_aliases: dict[str, set[str]],
    function_aliases: dict[str, str],
) -> bool:
    if isinstance(node, ast.NamedExpr):
        return _asyncio_runner_receiver(
            node.value,
            module_aliases,
            function_aliases,
        )
    if isinstance(node, ast.IfExp):
        return _asyncio_runner_receiver(
            node.body,
            module_aliases,
            function_aliases,
        ) or _asyncio_runner_receiver(
            node.orelse,
            module_aliases,
            function_aliases,
        )
    if isinstance(node, ast.BoolOp):
        return any(
            _asyncio_runner_receiver(candidate, module_aliases, function_aliases)
            for candidate in node.values
        )
    if _module_reference_kind(node, module_aliases) == "asyncio_runner":
        return True
    if not isinstance(node, ast.Call):
        return False
    factory_reference = _dotted_name(node.func)
    if function_aliases.get(factory_reference or "") == "asyncio.Runner":
        return True
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "Runner"
        and _module_reference_kind(node.func.value, module_aliases) == "asyncio"
    )


def _asyncio_task_receiver(
    node: ast.AST | None,
    module_aliases: dict[str, set[str]],
    function_aliases: dict[str, str],
) -> bool:
    if isinstance(node, ast.NamedExpr):
        return _asyncio_task_receiver(
            node.value,
            module_aliases,
            function_aliases,
        )
    if isinstance(node, ast.IfExp):
        return _asyncio_task_receiver(
            node.body,
            module_aliases,
            function_aliases,
        ) or _asyncio_task_receiver(
            node.orelse,
            module_aliases,
            function_aliases,
        )
    if isinstance(node, ast.BoolOp):
        return any(
            _asyncio_task_receiver(candidate, module_aliases, function_aliases)
            for candidate in node.values
        )
    if _module_reference_kind(node, module_aliases) == "asyncio_task":
        return True
    if not isinstance(node, ast.Call):
        return False
    factory_reference = _dotted_name(node.func)
    if function_aliases.get(factory_reference or "") == "asyncio.current_task":
        return True
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "current_task"
        and _module_reference_kind(node.func.value, module_aliases) == "asyncio"
    )


def _event_loop_receiver(
    node: ast.AST | None,
    module_aliases: dict[str, set[str]],
    function_aliases: dict[str, str],
) -> bool:
    if isinstance(node, ast.NamedExpr):
        return _event_loop_receiver(node.value, module_aliases, function_aliases)
    if isinstance(node, ast.IfExp):
        return _event_loop_receiver(
            node.body,
            module_aliases,
            function_aliases,
        ) or _event_loop_receiver(node.orelse, module_aliases, function_aliases)
    if isinstance(node, ast.BoolOp):
        return any(
            _event_loop_receiver(candidate, module_aliases, function_aliases)
            for candidate in node.values
        )
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return any(
            _event_loop_receiver(candidate, module_aliases, function_aliases)
            for candidate in node.elts
        )
    if isinstance(node, ast.Dict):
        return any(
            _event_loop_receiver(candidate, module_aliases, function_aliases)
            for candidate in (*node.keys, *node.values)
            if candidate is not None
        )
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        return _event_loop_receiver(node.elt, module_aliases, function_aliases)
    if isinstance(node, ast.DictComp):
        return _event_loop_receiver(
            node.key,
            module_aliases,
            function_aliases,
        ) or _event_loop_receiver(node.value, module_aliases, function_aliases)
    if _module_reference_kind(node, module_aliases) == "asyncio":
        return True
    if isinstance(node, ast.Call):
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_loop"
            and (
                _asyncio_runner_receiver(
                    node.func.value,
                    module_aliases,
                    function_aliases,
                )
                or _asyncio_task_receiver(
                    node.func.value,
                    module_aliases,
                    function_aliases,
                )
            )
        ):
            return True
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in VALUE_FLOW_METHOD_NAMES
            and _event_loop_receiver(
                node.func.value,
                module_aliases,
                function_aliases,
            )
        ):
            return True
        factory_reference = _dotted_name(node.func)
        imported_reference = function_aliases.get(factory_reference or "")
        if imported_reference in {
            "asyncio.get_event_loop",
            "asyncio.get_running_loop",
            "asyncio.new_event_loop",
        }:
            return True
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr
            in {"get_event_loop", "get_running_loop", "new_event_loop"}
            and (
                _module_reference_kind(node.func.value, module_aliases) == "asyncio"
                or _event_loop_policy_receiver(
                    node.func.value,
                    module_aliases,
                    function_aliases,
                )
            )
        ):
            return True
    if isinstance(node, ast.Subscript):
        return _event_loop_receiver(node.value, module_aliases, function_aliases)
    reference = _dotted_name(node)
    if reference is None:
        return False
    leaf = reference.rpartition(".")[2].lower()
    return (
        leaf in {"event_loop", "event_loops", "loop", "loops", "running_loop"}
        or leaf.endswith("_loop")
        or leaf.endswith("_loops")
    )


def _direct_call_kind(
    node: ast.Call,
    module_aliases: dict[str, set[str]],
    function_aliases: dict[str, str],
    callable_aliases: dict[str, str],
    vars_references: set[str] | frozenset[str] = DEFAULT_VARS_REFERENCES,
    *,
    builtin_getattr_available: bool = True,
    builtin_object_available: bool = True,
    builtin_getattr_owners: set[str] | frozenset[str] = frozenset(),
    builtin_getattr_aliases: set[str] | frozenset[str] = frozenset(),
) -> str | None:
    function = node.func
    function_reference = _dotted_name(function)
    if function_reference is None and isinstance(function, ast.Attribute):
        receiver_reference = (
            _static_receiver_call_reference(function.value)
            if isinstance(function.value, ast.Call)
            else None
        )
        if receiver_reference is not None:
            function_reference = f"{receiver_reference}.{function.attr}"
    if function_reference in callable_aliases:
        return callable_aliases[function_reference]
    if isinstance(function, ast.Attribute) and function.attr == "run_tcl":
        return "run_tcl"
    if (
        isinstance(function, ast.Attribute)
        and function.attr in ASYNCIO_EVENT_LOOP_PROCESS_CALL_NAMES
        and _event_loop_receiver(
            function.value,
            module_aliases,
            function_aliases,
        )
    ):
        return f"asyncio.{function.attr}"
    if isinstance(function, (ast.Call, ast.NamedExpr, ast.Subscript)):
        dynamic_kind = _callable_reference_kind(
            function,
            module_aliases,
            function_aliases,
            callable_aliases,
            vars_references,
            builtin_getattr_available=builtin_getattr_available,
            builtin_object_available=builtin_object_available,
            builtin_getattr_owners=builtin_getattr_owners,
            builtin_getattr_aliases=builtin_getattr_aliases,
        )
        if dynamic_kind:
            return dynamic_kind
        owner_node, attribute = _static_attribute_lookup(
            function,
            builtin_getattr_available=builtin_getattr_available,
            builtin_object_available=builtin_object_available,
            builtin_getattr_owners=builtin_getattr_owners,
            builtin_getattr_aliases=builtin_getattr_aliases,
            vars_references=vars_references,
        )
        if owner_node is not None and attribute == "*":
            return "asyncio.*"
    if isinstance(function, ast.Name) and function.id in callable_aliases:
        return callable_aliases[function.id]
    if isinstance(function, ast.Name) and function.id in function_aliases:
        imported = function_aliases[function.id]
        if _approved_process_call_kind(imported):
            return imported
    builtin_getattr_reference = (
        _dotted_name(function) in builtin_getattr_aliases
        or (
            isinstance(function, ast.Name)
            and function.id == "getattr"
            and builtin_getattr_available
        )
    ) or (
        isinstance(function, ast.Attribute)
        and function.attr == "getattr"
        and _dotted_name(function.value) in builtin_getattr_owners
    )
    if (
        builtin_getattr_reference
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
    ):
        attribute = str(node.args[1].value)
        if attribute == "run_tcl":
            return "run_tcl_dynamic"
        if (
            attribute in ASYNCIO_EVENT_LOOP_PROCESS_CALL_NAMES
            and _event_loop_receiver(
                node.args[0],
                module_aliases,
                function_aliases,
            )
        ):
            return f"asyncio.{attribute}"
        owner = _dotted_name(node.args[0])
        if owner is not None:
            for module_kind, aliases in module_aliases.items():
                candidate = f"{module_kind}.{attribute}"
                if owner in aliases and _approved_process_call_kind(candidate):
                    return candidate
    if not isinstance(function, ast.Attribute):
        return None
    owner = _dotted_name(function.value)
    for module_kind, aliases in module_aliases.items():
        candidate = f"{module_kind}.{function.attr}"
        if owner in aliases and _approved_process_call_kind(candidate):
            return candidate
    return None


def _callable_reference_kind(
    node: ast.AST | None,
    module_aliases: dict[str, set[str]],
    function_aliases: dict[str, str],
    callable_aliases: dict[str, str] | None = None,
    vars_references: set[str] | frozenset[str] = DEFAULT_VARS_REFERENCES,
    *,
    builtin_getattr_available: bool = True,
    builtin_object_available: bool = True,
    builtin_getattr_owners: set[str] | frozenset[str] = frozenset(),
    builtin_getattr_aliases: set[str] | frozenset[str] = frozenset(),
) -> str | None:
    callable_aliases = callable_aliases or {}
    module_dict_kind = _static_module_dict_callable_kind(
        node,
        module_aliases,
        vars_references,
    )
    if module_dict_kind:
        return module_dict_kind
    if isinstance(node, ast.Attribute) and node.attr == "run_tcl":
        return "run_tcl_alias"
    if (
        isinstance(node, ast.Attribute)
        and node.attr in ASYNCIO_EVENT_LOOP_PROCESS_CALL_NAMES
        and _event_loop_receiver(
            node.value,
            module_aliases,
            function_aliases,
        )
    ):
        return f"asyncio.{node.attr}"
    if isinstance(node, ast.NamedExpr):
        return _callable_reference_kind(
            node.value,
            module_aliases,
            function_aliases,
            callable_aliases,
            vars_references,
            builtin_getattr_available=builtin_getattr_available,
            builtin_object_available=builtin_object_available,
            builtin_getattr_owners=builtin_getattr_owners,
            builtin_getattr_aliases=builtin_getattr_aliases,
        )
    if isinstance(node, ast.Call):
        constructor = _dotted_name(node.func)
        if (
            constructor is not None
            and callable_aliases.get(f"{constructor}.__call__")
            == DYNAMIC_ATTRIBUTE_CALLABLE_KIND
        ):
            return DYNAMIC_ATTRIBUTE_CALLABLE_KIND
    reference = _dotted_name(node)
    if reference in callable_aliases:
        return callable_aliases[reference]
    projected_source: ast.AST | None = None
    if isinstance(node, ast.Subscript):
        projected_source = node.value
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in VALUE_FLOW_METHOD_NAMES
    ):
        projected_source = node.func.value
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "next"
        and len(node.args) == 1
        and not node.keywords
    ):
        projected_source = node.args[0]
    if projected_source is not None:
        projected_kind = _callable_reference_kind(
            projected_source,
            module_aliases,
            function_aliases,
            callable_aliases,
            vars_references,
            builtin_getattr_available=builtin_getattr_available,
            builtin_object_available=builtin_object_available,
            builtin_getattr_owners=builtin_getattr_owners,
            builtin_getattr_aliases=builtin_getattr_aliases,
        )
        if projected_kind == DYNAMIC_ATTRIBUTE_CALLABLE_KIND:
            return projected_kind
    if isinstance(node, ast.Name) and node.id in callable_aliases:
        return callable_aliases[node.id]
    if isinstance(node, ast.Name) and node.id in function_aliases:
        imported = function_aliases[node.id]
        return imported if _approved_process_call_kind(imported) else None
    if isinstance(node, ast.Attribute):
        owner = _dotted_name(node.value)
        for module_kind, aliases in module_aliases.items():
            candidate = f"{module_kind}.{node.attr}"
            if owner in aliases and _approved_process_call_kind(candidate):
                return candidate
    owner_node, attribute = _static_attribute_lookup(
        node,
        builtin_getattr_available=builtin_getattr_available,
        builtin_object_available=builtin_object_available,
        builtin_getattr_owners=builtin_getattr_owners,
        builtin_getattr_aliases=builtin_getattr_aliases,
        vars_references=vars_references,
    )
    if owner_node is not None and attribute is not None:
        if attribute == "run_tcl":
            return "run_tcl_dynamic"
        if (
            attribute in ASYNCIO_EVENT_LOOP_PROCESS_CALL_NAMES
            and _event_loop_receiver(
                owner_node,
                module_aliases,
                function_aliases,
            )
        ):
            return f"asyncio.{attribute}"
        owner = _dotted_name(owner_node)
        if owner is not None:
            for module_kind, aliases in module_aliases.items():
                if owner not in aliases:
                    continue
                if attribute == "*":
                    return f"{module_kind}.*"
                candidate = f"{module_kind}.{attribute}"
                if _approved_process_call_kind(candidate):
                    return candidate
    return None


def _static_attribute_lookup(
    node: ast.AST | None,
    *,
    builtin_getattr_available: bool = True,
    builtin_object_available: bool = True,
    builtin_getattr_owners: set[str] | frozenset[str] = frozenset(),
    builtin_getattr_aliases: set[str] | frozenset[str] = frozenset(),
    vars_references: set[str] | frozenset[str] = DEFAULT_VARS_REFERENCES,
) -> tuple[ast.AST | None, str | None]:
    def attribute_name(
        value: ast.AST,
        *,
        allow_dynamic_name: bool = False,
    ) -> str | None:
        resolved = _static_string_value(value)
        if resolved is not None:
            return resolved
        return "*" if allow_dynamic_name else None

    def builtins_dict_reference(candidate: ast.AST | None) -> bool:
        if (
            isinstance(candidate, ast.Attribute)
            and candidate.attr == "__dict__"
        ):
            return _dotted_name(candidate.value) in builtin_getattr_owners
        if (
            isinstance(candidate, ast.Call)
            and len(candidate.args) == 1
            and not candidate.keywords
            and _known_vars_reference_name(
                candidate.func,
                vars_references,
            )
            is not None
        ):
            return _dotted_name(candidate.args[0]) in builtin_getattr_owners
        return False

    if not isinstance(node, ast.Call) or node.keywords:
        return None, None
    mapping_source: ast.AST | None = None
    mapping_key: ast.AST | None = None
    if isinstance(node.func, ast.Subscript):
        mapping_source = node.func.value
        mapping_key = node.func.slice
    elif (
        isinstance(node.func, ast.Call)
        and isinstance(node.func.func, ast.Attribute)
        and node.func.func.attr in {"__getitem__", "get", "pop", "setdefault"}
        and node.func.args
        and not node.func.keywords
    ):
        mapping_source = node.func.func.value
        mapping_key = node.func.args[0]
    mapping_getattr_reference = (
        mapping_source is not None
        and mapping_key is not None
        and _static_string_value(mapping_key) == "getattr"
        and builtins_dict_reference(mapping_source)
    )
    builtin_getattr_reference = (
        mapping_getattr_reference
        or _dotted_name(node.func) in builtin_getattr_aliases
        or (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and builtin_getattr_available
        )
    ) or (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "getattr"
        and _dotted_name(node.func.value) in builtin_getattr_owners
    )
    if (
        builtin_getattr_reference
        and len(node.args) in {2, 3}
    ):
        return node.args[0], attribute_name(
            node.args[1],
            allow_dynamic_name=True,
        )
    if not isinstance(node.func, ast.Attribute):
        return None, None
    if node.func.attr != "__getattribute__":
        return None, None
    if len(node.args) == 1:
        return node.func.value, attribute_name(
            node.args[0],
            allow_dynamic_name=True,
        )
    if (
        len(node.args) == 2
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "object"
        and builtin_object_available
    ):
        return node.args[0], attribute_name(
            node.args[1],
            allow_dynamic_name=True,
        )
    return None, None


def _module_reference_kind(
    node: ast.AST | None,
    module_aliases: dict[str, set[str]],
) -> str | None:
    if isinstance(node, ast.NamedExpr):
        return _module_reference_kind(node.value, module_aliases)
    reference = _dotted_name(node)
    if reference is None:
        return None
    return next(
        (module for module, aliases in module_aliases.items() if reference in aliases),
        None,
    )


def _known_vars_reference_name(
    node: ast.AST | None,
    vars_references: set[str] | frozenset[str],
) -> str | None:
    def resolve(candidate: ast.AST | None) -> str | None:
        direct = _dotted_name(candidate)
        if direct is not None and (
            direct in vars_references
            or any(
                reference.startswith(f"{direct}.")
                for reference in vars_references
            )
        ):
            return direct
        if isinstance(candidate, ast.Call):
            receiver_call = _static_receiver_call_reference(candidate)
            if receiver_call is not None and any(
                reference.startswith(f"{receiver_call}.")
                for reference in vars_references
            ):
                return receiver_call
            constructor = _dotted_name(candidate.func)
            if constructor is not None and any(
                reference.startswith(f"{constructor}.")
                for reference in vars_references
            ):
                return constructor
            return None
        if isinstance(candidate, ast.Attribute):
            owner = resolve(candidate.value)
            if owner is None:
                return None
            reference = f"{owner}.{candidate.attr}"
            if reference in vars_references or any(
                candidate_reference.startswith(f"{reference}.")
                for candidate_reference in vars_references
            ):
                return reference
        return None

    reference = resolve(node)
    return reference if reference in vars_references else None


def _static_receiver_call_reference(node: ast.Call) -> str | None:
    if node.keywords:
        return None
    function = _dotted_name(node.func)
    if function == "super":
        if not node.args:
            return "super()"
        if len(node.args) == 2:
            class_reference = _dotted_name(node.args[0])
            receiver_reference = _dotted_name(node.args[1])
            if class_reference is not None and receiver_reference is not None:
                return f"super({class_reference},{receiver_reference})"
    if function == "type" and len(node.args) == 1:
        receiver_reference = _dotted_name(node.args[0])
        if receiver_reference is not None:
            return f"type({receiver_reference})"
    return None


def _static_module_dict_kind(
    node: ast.AST | None,
    module_aliases: dict[str, set[str]],
    vars_references: set[str] | frozenset[str],
) -> str | None:
    kinds = _static_module_dict_kinds(node, module_aliases, vars_references)
    return min(kinds) if kinds else None


def _static_module_dict_kinds(
    node: ast.AST | None,
    module_aliases: dict[str, set[str]],
    vars_references: set[str] | frozenset[str],
    *,
    depth: int = 0,
) -> frozenset[str]:
    if node is None or depth > 16:
        return frozenset()
    if isinstance(node, ast.NamedExpr):
        return _static_module_dict_kinds(
            node.value,
            module_aliases,
            vars_references,
            depth=depth + 1,
        )
    if isinstance(node, ast.Attribute) and node.attr == "__dict__":
        module_kind = _module_reference_kind(node.value, module_aliases)
        return frozenset({module_kind}) if module_kind else frozenset()
    if isinstance(node, ast.Dict):
        retained: set[str] = set()
        for key, value in zip(node.keys, node.values, strict=True):
            if key is None:
                retained.update(
                    _static_module_dict_kinds(
                        value,
                        module_aliases,
                        vars_references,
                        depth=depth + 1,
                    )
                )
        return frozenset(retained)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _static_module_dict_kinds(
            node.left,
            module_aliases,
            vars_references,
            depth=depth + 1,
        ) | _static_module_dict_kinds(
            node.right,
            module_aliases,
            vars_references,
            depth=depth + 1,
        )
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "copy"
        and not node.args
        and not node.keywords
    ):
        return _static_module_dict_kinds(
            node.func.value,
            module_aliases,
            vars_references,
            depth=depth + 1,
        )
    if not isinstance(node, ast.Call):
        return frozenset()
    function_name = _dotted_name(node.func)
    if function_name == "dict" and len(node.args) == 1:
        return _static_module_dict_kinds(
            node.args[0],
            module_aliases,
            vars_references,
            depth=depth + 1,
        )
    vars_function_name = _known_vars_reference_name(
        node.func,
        vars_references,
    )
    if (
        vars_function_name is not None
        and len(node.args) == 1
        and not node.keywords
    ):
        module_kind = _module_reference_kind(node.args[0], module_aliases)
        return frozenset({module_kind}) if module_kind else frozenset()
    if (
        function_name == "getattr"
        and len(node.args) == 2
        and not node.keywords
        and _static_string_value(node.args[1]) == "__dict__"
    ):
        module_kind = _module_reference_kind(node.args[0], module_aliases)
        return frozenset({module_kind}) if module_kind else frozenset()
    return frozenset()


def _static_module_dict_callable_kind(
    node: ast.AST | None,
    module_aliases: dict[str, set[str]],
    vars_references: set[str] | frozenset[str],
) -> str | None:
    if isinstance(node, ast.NamedExpr):
        return _static_module_dict_callable_kind(
            node.value,
            module_aliases,
            vars_references,
        )
    module_kinds: frozenset[str] = frozenset()
    key_node: ast.AST | None = None
    if isinstance(node, ast.Subscript):
        module_kinds = _static_module_dict_kinds(
            node.value,
            module_aliases,
            vars_references,
        )
        key_node = node.slice
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"__getitem__", "get", "pop", "setdefault"}
        and node.args
    ):
        module_kinds = _static_module_dict_kinds(
            node.func.value,
            module_aliases,
            vars_references,
        )
        key_node = node.args[0]
    if not module_kinds:
        return None
    key = _static_string_value(key_node)
    if key is None:
        return None
    candidates = (
        f"{module_kind}.{key}" for module_kind in sorted(module_kinds)
    )
    return next(
        (candidate for candidate in candidates if _approved_process_call_kind(candidate)),
        None,
    )


def _static_string_value(node: ast.AST | None, *, depth: int = 0) -> str | None:
    if node is None or depth > 8:
        return None
    if isinstance(node, ast.Constant) and type(node.value) is str:
        return node.value if len(node.value) <= 256 else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string_value(node.left, depth=depth + 1)
        right = _static_string_value(node.right, depth=depth + 1)
        if left is None or right is None or len(left) + len(right) > 256:
            return None
        return left + right
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if not isinstance(value, ast.Constant) or type(value.value) is not str:
                return None
            parts.append(value.value)
        combined = "".join(parts)
        return combined if len(combined) <= 256 else None
    return None


def _dotted_name(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _dotted_name(node.value)
        return f"{owner}.{node.attr}" if owner is not None else None
    if isinstance(node, ast.Subscript):
        owner = _dotted_name(node.value)
        key = _static_string_value(node.slice)
        return f"{owner}.{key}" if owner is not None and key is not None else None
    owner_node, attribute = _static_attribute_lookup(node)
    if owner_node is not None and attribute is not None:
        owner = _dotted_name(owner_node)
        return f"{owner}.{attribute}" if owner is not None else None
    return None


def _imported_process_module_kind(module_name: str | None) -> str | None:
    root_module = (module_name or "").partition(".")[0]
    return (
        root_module
        if root_module in {"subprocess", "os", "asyncio", "pty", "posix", "nt"}
        else None
    )


def _normalized_execution_kind(call_kind: str) -> str:
    if call_kind == DYNAMIC_ATTRIBUTE_CALLABLE_KIND:
        return "asyncio.*"
    return "run_tcl" if call_kind.startswith("run_tcl") else call_kind


def _approved_process_call_kind(value: str) -> bool:
    module, _, name = value.partition(".")
    if module == "subprocess":
        return name in SUBPROCESS_CALL_NAMES
    if module in PLATFORM_PROCESS_MODULES:
        return name in OS_PROCESS_CALL_NAMES or name.startswith(
            ("exec", "spawn", "posix_spawn")
        )
    if module == "asyncio":
        return name.startswith(
            "create_subprocess_"
        ) or name in ASYNCIO_EVENT_LOOP_PROCESS_CALL_NAMES
    if module == "pty":
        return name in PTY_PROCESS_CALL_NAMES
    return False


def test_direct_execution_call_footprint_is_frozen_until_executor_migration() -> None:
    records = _direct_execution_records()
    payload = [
        [path, function, call_kind, count]
        for (path, function, call_kind), count in sorted(records.items())
    ]
    digest = hashlib.sha256(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    assert digest == DIRECT_EXECUTION_BASELINE_SHA256
    assert Counter(
        {
            call_kind: sum(count for (*_, observed_kind), count in records.items() if observed_kind == call_kind)
            for call_kind in DIRECT_EXECUTION_BASELINE_COUNTS
        }
    ) == Counter(DIRECT_EXECUTION_BASELINE_COUNTS)


def test_direct_execution_calls_remain_inside_reviewed_modules() -> None:
    records = _direct_execution_records()
    run_tcl_files = {path for (path, _, kind) in records if kind.startswith("run_tcl")}
    subprocess_files = {path for (path, _, kind) in records if kind.startswith("subprocess.")}
    unapproved_shell_calls = {
        (path, function, kind)
        for (path, function, kind) in records
        if kind.startswith(
            ("os.", "asyncio.", "pty.", "posix.", "nt.", "dynamic_code.")
        )
    }

    assert run_tcl_files == RUN_TCL_ALLOWED_FILES
    assert subprocess_files == SUBPROCESS_ALLOWED_FILES
    assert unapproved_shell_calls == set()


@pytest.mark.parametrize(
    ("expression", "module_aliases", "function_aliases", "callable_aliases", "expected"),
    [
        (
            "session.run_tcl('version')",
            {"subprocess": {"subprocess"}, "os": {"os"}, "asyncio": {"asyncio"}},
            {},
            {},
            "run_tcl",
        ),
        (
            "runner('version')",
            {"subprocess": {"subprocess"}, "os": {"os"}, "asyncio": {"asyncio"}},
            {},
            {"runner": "run_tcl_alias"},
            "run_tcl_alias",
        ),
        (
            "getattr(sp, 'run')([])",
            {"subprocess": {"subprocess", "sp"}, "os": {"os"}, "asyncio": {"asyncio"}},
            {},
            {},
            "subprocess.run",
        ),
        (
            "run_cmd([])",
            {"subprocess": {"subprocess"}, "os": {"os"}, "asyncio": {"asyncio"}},
            {"run_cmd": "subprocess.check_output"},
            {},
            "subprocess.check_output",
        ),
        (
            "subprocess.getoutput('vivado -mode batch')",
            {"subprocess": {"subprocess"}, "os": {"os"}, "asyncio": {"asyncio"}},
            {},
            {},
            "subprocess.getoutput",
        ),
        (
            "getattr(subprocess, 'getstatusoutput')('vivado')",
            {"subprocess": {"subprocess"}, "os": {"os"}, "asyncio": {"asyncio"}},
            {},
            {},
            "subprocess.getstatusoutput",
        ),
        (
            "os.spawnv(0, 'vivado', [])",
            {"subprocess": {"subprocess"}, "os": {"os"}, "asyncio": {"asyncio"}},
            {},
            {},
            "os.spawnv",
        ),
    ],
)
def test_direct_call_classifier_detects_common_alias_and_dynamic_bypasses(
    expression: str,
    module_aliases: dict[str, set[str]],
    function_aliases: dict[str, str],
    callable_aliases: dict[str, str],
    expected: str,
) -> None:
    call = ast.parse(expression, mode="eval").body
    assert isinstance(call, ast.Call)
    assert _direct_call_kind(call, module_aliases, function_aliases, callable_aliases) == expected


@pytest.mark.parametrize(
    ("source", "expected_kind"),
    [
        (
            "def bypass(session):\n"
            "    first = session.run_tcl\n"
            "    second = first\n"
            "    second('version')\n",
            "run_tcl",
        ),
        (
            "def bypass(session):\n"
            "    (runner := session.run_tcl)('version')\n",
            "run_tcl",
        ),
        (
            "import subprocess\n"
            "def bypass():\n"
            "    first = subprocess.getoutput\n"
            "    second = first\n"
            "    second('vivado -mode batch')\n",
            "subprocess.getoutput",
        ),
        (
            "def bypass(executor, session, command):\n"
            "    executor.submit(session.run_tcl, command)\n",
            "run_tcl",
        ),
        (
            "import subprocess\n"
            "def bypass(executor, argv):\n"
            "    executor.submit(fn=subprocess.run, argv=argv)\n",
            "subprocess.run",
        ),
        (
            "def bypass(executor, session, command):\n"
            "    executor.submit(*(session.run_tcl, command))\n",
            "run_tcl",
        ),
        (
            "import subprocess\n"
            "def bypass(executor, argv):\n"
            "    executor.submit(**{'fn': subprocess.run, 'argv': argv})\n",
            "subprocess.run",
        ),
        (
            "import functools\n"
            "def bypass(executor, session, command):\n"
            "    executor.submit(functools.partial(session.run_tcl, command))\n",
            "run_tcl",
        ),
        (
            "import subprocess\n"
            "def bypass(executor, argv):\n"
            "    executor.submit(**dict(fn=subprocess.run, argv=argv))\n",
            "subprocess.run",
        ),
        (
            "def bypass(executor, session, command):\n"
            "    executor.submit(*(item for item in (session.run_tcl, command)))\n",
            "run_tcl",
        ),
        (
            "def bypass(session):\n"
            "    callbacks = {'run': session.run_tcl}\n"
            "    callbacks['run']('version')\n",
            "run_tcl",
        ),
        (
            "import subprocess\n"
            "def bypass(argv):\n"
            "    sp = subprocess\n"
            "    sp.run(argv)\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "def bypass(argv, sp=subprocess):\n"
            "    sp.run(argv)\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "def bypass(argv, *, sp=subprocess):\n"
            "    sp.run(argv)\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "def bypass(argv, execute=subprocess.run):\n"
            "    execute(argv)\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "runner = lambda argv, sp=subprocess: sp.run(argv)\n"
            "runner(['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import os\n"
            "def bypass(argv):\n"
            "    proc = os\n"
            "    proc.spawnv(os.P_WAIT, argv[0], argv)\n",
            "os.spawnv",
        ),
        (
            "import asyncio.subprocess as asp\n"
            "async def bypass(argv):\n"
            "    await asp.create_subprocess_exec(*argv)\n",
            "asyncio.create_subprocess_exec",
        ),
        (
            "import asyncio.subprocess\n"
            "async def bypass(argv):\n"
            "    await asyncio.subprocess.create_subprocess_exec(*argv)\n",
            "asyncio.create_subprocess_exec",
        ),
        (
            "from asyncio.subprocess import create_subprocess_shell as launch\n"
            "async def bypass(command):\n"
            "    await launch(command)\n",
            "asyncio.create_subprocess_shell",
        ),
        (
            "from asyncio import subprocess as asp\n"
            "async def bypass(argv):\n"
            "    await asp.create_subprocess_exec(*argv)\n",
            "asyncio.create_subprocess_exec",
        ),
        (
            "import asyncio\n"
            "async def bypass(argv):\n"
            "    await getattr(asyncio, 'subprocess').create_subprocess_exec(*argv)\n",
            "asyncio.create_subprocess_exec",
        ),
        (
            "import asyncio\n"
            "async def bypass(protocol_factory, argv):\n"
            "    await asyncio.get_running_loop().subprocess_exec(protocol_factory, *argv)\n",
            "asyncio.subprocess_exec",
        ),
        (
            "import asyncio\n"
            "async def bypass(protocol_factory, argv):\n"
            "    engine = asyncio.get_running_loop()\n"
            "    await engine.subprocess_exec(protocol_factory, *argv)\n",
            "asyncio.subprocess_exec",
        ),
        (
            "import asyncio\n"
            "async def bypass(protocol_factory, argv):\n"
            "    bucket = [asyncio.get_running_loop()]\n"
            "    engine = bucket[0]\n"
            "    await engine.subprocess_exec(protocol_factory, *argv)\n",
            "asyncio.subprocess_exec",
        ),
        (
            "import asyncio\n"
            "async def bypass(protocol_factory, argv):\n"
            "    engine, = (asyncio.get_running_loop(),)\n"
            "    await engine.subprocess_exec(protocol_factory, *argv)\n",
            "asyncio.subprocess_exec",
        ),
        (
            "import asyncio\n"
            "async def bypass(protocol_factory, argv):\n"
            "    await asyncio.get_event_loop_policy().get_event_loop().subprocess_exec(\n"
            "        protocol_factory, *argv\n"
            "    )\n",
            "asyncio.subprocess_exec",
        ),
        (
            "async def bypass(loop, protocol_factory, argv):\n"
            "    await loop.subprocess_exec(protocol_factory, *argv)\n",
            "asyncio.subprocess_exec",
        ),
        (
            "async def bypass(loop, protocol_factory, argv):\n"
            "    await loop._make_subprocess_transport(\n"
            "        protocol_factory, argv, False, None, None, None, 0\n"
            "    )\n",
            "asyncio._make_subprocess_transport",
        ),
        (
            "async def bypass(loop, protocol_factory, command):\n"
            "    await getattr(loop, 'subprocess_shell')(protocol_factory, command)\n",
            "asyncio.subprocess_shell",
        ),
        (
            "async def bypass(loop, protocol_factory, argv):\n"
            "    name = 'subprocess_exec'\n"
            "    await getattr(loop, name)(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "async def bypass(loop, name, protocol_factory, command):\n"
            "    await loop.__getattribute__(name)(protocol_factory, command)\n",
            "asyncio.*",
        ),
        (
            "async def bypass(loop, protocol_factory, argv):\n"
            "    name = 'subprocess_exec'\n"
            "    launch = getattr(loop, name)\n"
            "    forwarded = launch\n"
            "    await forwarded(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "async def bypass(loop, enabled, noop, name, protocol_factory, argv):\n"
            "    launch = getattr(loop, name) if enabled else noop\n"
            "    await launch(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "async def bypass(loop, noop, name, protocol_factory, argv):\n"
            "    launch = noop or getattr(loop, name)\n"
            "    await launch(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "async def bypass(loop, enabled, noop, name, protocol_factory, argv):\n"
            "    if enabled:\n"
            "        launch = getattr(loop, name)\n"
            "    else:\n"
            "        launch = noop\n"
            "    await launch(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "import operator\n"
            "def bypass(loop, name, protocol_factory, argv):\n"
            "    operator.methodcaller(name, protocol_factory, *argv)(loop)\n",
            "asyncio.*",
        ),
        (
            "import operator\n"
            "def bypass(loop, name, protocol_factory, argv):\n"
            "    operator.attrgetter(name)(loop)(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "import operator\n"
            "def bypass(loop, name, protocol_factory, argv):\n"
            "    launch = operator.methodcaller(name, protocol_factory, *argv)\n"
            "    forwarded = launch\n"
            "    forwarded(loop)\n",
            "asyncio.*",
        ),
        (
            "import operator\n"
            "def bypass(loop, name, protocol_factory, argv):\n"
            "    getter = operator.attrgetter(name)\n"
            "    forwarded = getter\n"
            "    forwarded(loop)(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    launches = [getattr(loop, name)]\n"
            "    await launches[0](protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    launches = {'go': getattr(loop, name)}\n"
            "    await launches.get('go')(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    launches = [getattr(loop, name) for _ in range(1)]\n"
            "    await launches[0](protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    launches = {getattr(loop, name) for _ in range(1)}\n"
            "    await launches.pop()(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    launches = {key: getattr(loop, name) for key in ['go']}\n"
            "    await launches.get('go')(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    launches = (getattr(loop, name) for _ in range(1))\n"
            "    await next(launches)(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    launches = []\n"
            "    launches.append(getattr(loop, name))\n"
            "    await launches[0](protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    launches = []\n"
            "    launches += [getattr(loop, name)]\n"
            "    await launches[0](protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    launches = {}\n"
            "    launches.update(go=getattr(loop, name))\n"
            "    await launches.get('go')(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "async def bypass(holder, loop, name, protocol_factory, argv):\n"
            "    holder.launch = getattr(loop, name)\n"
            "    await holder.launch(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "async def bypass(holder, loop, name, protocol_factory, argv):\n"
            "    holder.launch: object = getattr(loop, name)\n"
            "    await holder.launch(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "import functools\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    launch = functools.partial(getattr(loop, name), protocol_factory)\n"
            "    await launch(*argv)\n",
            "asyncio.*",
        ),
        (
            "import functools as ft\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    launch = ft.partial(getattr(loop, name), protocol_factory)\n"
            "    await launch(*argv)\n",
            "asyncio.*",
        ),
        (
            "from functools import partial as bind\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    launch = bind(getattr(loop, name), protocol_factory)\n"
            "    await launch(*argv)\n",
            "asyncio.*",
        ),
        (
            "from functools import partial\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    bind = partial\n"
            "    launch = bind(getattr(loop, name), protocol_factory)\n"
            "    await launch(*argv)\n",
            "asyncio.*",
        ),
        (
            "def maker(loop, name):\n"
            "    launch = getattr(loop, name)\n"
            "    return launch\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    await maker(loop, name)(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "def maker(loop, name):\n"
            "    return [getattr(loop, name)]\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    await maker(loop, name)[0](protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "def identity(value):\n"
            "    return value\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    launch = identity(getattr(loop, name))\n"
            "    await launch(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "def invoke(callback):\n"
            "    def inner(*args):\n"
            "        return callback(*args)\n"
            "    return inner\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    await invoke(getattr(loop, name))(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "def invoke(callback):\n"
            "    def middle():\n"
            "        def inner(*args):\n"
            "            return callback(*args)\n"
            "        return inner\n"
            "    return middle()\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    await invoke(getattr(loop, name))(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    for launch in [getattr(loop, name)]:\n"
            "        await launch(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "async def bypass(source, loop, name, protocol_factory, argv):\n"
            "    launches = (getattr(loop, name) async for _ in source)\n"
            "    async for launch in launches:\n"
            "        await launch(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "def makers(loop, name):\n"
            "    yield getattr(loop, name)\n",
            "asyncio.*",
        ),
        (
            "def invoke(callback, *args):\n"
            "    return callback(*args)\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    await invoke(\n"
            "        getattr(loop, name), protocol_factory, argv\n"
            "    )\n",
            "asyncio.*",
        ),
        (
            "def invoke(holder, callback, *args):\n"
            "    holder.cb = callback\n"
            "    return holder.cb(*args)\n"
            "async def bypass(holder, loop, name, protocol_factory, argv):\n"
            "    await invoke(\n"
            "        holder, getattr(loop, name), protocol_factory, *argv\n"
            "    )\n",
            "asyncio.*",
        ),
        (
            "import operator\n"
            "def invoke(callback, *args):\n"
            "    return operator.call(callback, *args)\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    await invoke(\n"
            "        getattr(loop, name), protocol_factory, *argv\n"
            "    )\n",
            "asyncio.*",
        ),
        (
            "def invoke(callback, *args):\n"
            "    from operator import call\n"
            "    return call(callback, *args)\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    await invoke(\n"
            "        getattr(loop, name), protocol_factory, *argv\n"
            "    )\n",
            "asyncio.*",
        ),
        (
            "def invoke(callback, *args):\n"
            "    return next(map(lambda fn: fn(*args), [callback]))\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    await invoke(\n"
            "        getattr(loop, name), protocol_factory, *argv\n"
            "    )\n",
            "asyncio.*",
        ),
        (
            "import itertools\n"
            "def invoke(callback, *args):\n"
            "    return next(itertools.starmap(callback, [args]))\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    await invoke(\n"
            "        getattr(loop, name), protocol_factory, *argv\n"
            "    )\n",
            "asyncio.*",
        ),
        (
            "def invoke(callback, *args):\n"
            "    from itertools import starmap\n"
            "    return next(starmap(callback, [args]))\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    await invoke(\n"
            "        getattr(loop, name), protocol_factory, *argv\n"
            "    )\n",
            "asyncio.*",
        ),
        (
            "import itertools\n"
            "def invoke(callback, *args):\n"
            "    values = itertools.accumulate(args, callback)\n"
            "    next(values)\n"
            "    return next(values)\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    await invoke(\n"
            "        getattr(loop, name), protocol_factory, *argv\n"
            "    )\n",
            "asyncio.*",
        ),
        (
            "import itertools\n"
            "def invoke(callback, *args):\n"
            "    values = itertools.accumulate(args, **{'func': callback})\n"
            "    return next(itertools.islice(values, 1, None))\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    await invoke(\n"
            "        getattr(loop, name), protocol_factory, *argv\n"
            "    )\n",
            "asyncio.*",
        ),
        (
            "def invoke(callback, *args):\n"
            "    forwarded = callback\n"
            "    return forwarded(*args)\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    await invoke(\n"
            "        getattr(loop, name), protocol_factory, argv\n"
            "    )\n",
            "asyncio.*",
        ),
        (
            "def invoke(callback, *args):\n"
            "    return callback.__call__(*args)\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    await invoke(\n"
            "        getattr(loop, name), protocol_factory, *argv\n"
            "    )\n",
            "asyncio.*",
        ),
        (
            "invoke = lambda callback, *args: callback(*args)\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    await invoke(\n"
            "        getattr(loop, name), protocol_factory, *argv\n"
            "    )\n",
            "asyncio.*",
        ),
        (
            "def invoke(*args):\n"
            "    return args[0](*args[1:])\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    await invoke(\n"
            "        getattr(loop, name), protocol_factory, *argv\n"
            "    )\n",
            "asyncio.*",
        ),
        (
            "def invoke(**kwargs):\n"
            "    return kwargs['callback'](*kwargs['args'])\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    await invoke(\n"
            "        callback=getattr(loop, name),\n"
            "        args=(protocol_factory, *argv),\n"
            "    )\n",
            "asyncio.*",
        ),
        (
            "def invoke(callback, *args):\n"
            "    return callback(*args)\n"
            "forward = invoke\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    await forward(\n"
            "        getattr(loop, name), protocol_factory, argv\n"
            "    )\n",
            "asyncio.*",
        ),
        (
            "class Helpers:\n"
            "    @staticmethod\n"
            "    def invoke(callback, *args):\n"
            "        return callback(*args)\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    await Helpers.invoke(\n"
            "        getattr(loop, name), protocol_factory, argv\n"
            "    )\n",
            "asyncio.*",
        ),
        (
            "class Helpers:\n"
            "    def invoke(self, callback, *args):\n"
            "        return callback(*args)\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    await Helpers().invoke(\n"
            "        getattr(loop, name), protocol_factory, *argv\n"
            "    )\n",
            "asyncio.*",
        ),
        (
            "class Helpers:\n"
            "    def invoke(self, callback, *args):\n"
            "        return callback(*args)\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    helper = Helpers()\n"
            "    await helper.invoke(\n"
            "        getattr(loop, name), protocol_factory, *argv\n"
            "    )\n",
            "asyncio.*",
        ),
        (
            "class Helpers:\n"
            "    def invoke(self, callback, *args):\n"
            "        return callback(*args)\n"
            "def make_helper():\n"
            "    return Helpers()\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    helper = make_helper()\n"
            "    await helper.invoke(\n"
            "        getattr(loop, name), protocol_factory, *argv\n"
            "    )\n",
            "asyncio.*",
        ),
        (
            "class Invoke:\n"
            "    def __call__(self, callback, *args):\n"
            "        return callback(*args)\n"
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    await Invoke()(\n"
            "        getattr(loop, name), protocol_factory, *argv\n"
            "    )\n",
            "asyncio.*",
        ),
        (
            "async def bypass(loop, name, protocol_factory, argv):\n"
            "    launch, = (getattr(loop, name),)\n"
            "    await launch(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "async def bypass(loop, enabled, name, protocol_factory, argv):\n"
            "    if enabled:\n"
            "        launch = getattr(loop, name)\n"
            "    else:\n"
            "        del launch\n"
            "    await launch(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "name = 'subprocess_exec'\n"
            "class Launcher:\n"
            "    launch = getattr(object, name)\n"
            "def bypass(protocol_factory, argv):\n"
            "    Launcher.launch(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "name = 'subprocess_exec'\n"
            "class Launcher:\n"
            "    launch = getattr(object, name)\n"
            "def bypass(protocol_factory, argv):\n"
            "    instance = Launcher()\n"
            "    instance.launch(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "name = 'subprocess_exec'\n"
            "class Launcher:\n"
            "    launch = getattr(object, name)\n"
            "    def bypass(self, protocol_factory, argv):\n"
            "        self.launch(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "name = 'subprocess_exec'\n"
            "class Launcher:\n"
            "    __call__ = getattr(object, name)\n"
            "async def bypass(protocol_factory, argv):\n"
            "    await Launcher()(protocol_factory, *argv)\n",
            "asyncio.*",
        ),
        (
            "import asyncio\n"
            "async def bypass(protocol_factory, command):\n"
            "    await asyncio.get_running_loop().subprocess_shell(protocol_factory, command)\n",
            "asyncio.subprocess_shell",
        ),
        (
            "import asyncio\n"
            "async def bypass(protocol_factory, argv):\n"
            "    launch = getattr(asyncio.get_running_loop(), 'subprocess_exec')\n"
            "    await launch(protocol_factory, *argv)\n",
            "asyncio.subprocess_exec",
        ),
        (
            "import asyncio\n"
            "async def bypass(protocol_factory, command):\n"
            "    await getattr(asyncio.get_running_loop(), 'subprocess_shell')(protocol_factory, command)\n",
            "asyncio.subprocess_shell",
        ),
        (
            "import asyncio\n"
            "async def bypass(protocol_factory, argv):\n"
            "    loop = asyncio.get_running_loop()\n"
            "    await loop.subprocess_exec(protocol_factory, *argv)\n",
            "asyncio.subprocess_exec",
        ),
        (
            "from asyncio import get_running_loop as current_loop\n"
            "async def bypass(protocol_factory, command):\n"
            "    loop = current_loop()\n"
            "    await getattr(loop, 'subprocess_shell')(protocol_factory, command)\n",
            "asyncio.subprocess_shell",
        ),
        (
            "from asyncio.events import get_running_loop as current_loop\n"
            "async def bypass(protocol_factory, argv):\n"
            "    loop = current_loop()\n"
            "    await loop.subprocess_exec(protocol_factory, *argv)\n",
            "asyncio.subprocess_exec",
        ),
        (
            "import asyncio\n"
            "async def bypass(protocol_factory, argv):\n"
            "    loop_factory = asyncio.get_running_loop\n"
            "    loop = loop_factory()\n"
            "    await loop.subprocess_exec(protocol_factory, *argv)\n",
            "asyncio.subprocess_exec",
        ),
    ],
)
def test_execution_scanner_propagates_secondary_and_named_expression_aliases(
    tmp_path: Path,
    source: str,
    expected_kind: str,
) -> None:
    path = tmp_path / "bypass.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert sum(count for (*_, kind), count in records.items() if kind == expected_kind) >= 1


def test_execution_scanner_keeps_callback_contracts_qualified_by_owner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "qualified_callback_contracts.py"
    path.write_text(
        "class Executes:\n"
        "    @staticmethod\n"
        "    def invoke(callback, *args):\n"
        "        return callback(*args)\n"
        "class Stores:\n"
        "    @staticmethod\n"
        "    def invoke(value):\n"
        "        return value\n"
        "def safe(obj, name):\n"
        "    Stores.invoke(getattr(obj, name))\n"
        "    helper = Stores()\n"
        "    helper.invoke(getattr(obj, name))\n"
        "    return None\n",
        encoding="utf-8",
    )

    assert _direct_execution_records([path]) == Counter()


def test_execution_scanner_clears_reassigned_callback_contract_provenance(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reassigned_callback_contract.py"
    path.write_text(
        "def invoke(callback, noop, *args):\n"
        "    callback = noop\n"
        "    return callback(*args)\n"
        "async def safe(loop, name, noop, protocol_factory, argv):\n"
        "    return await invoke(\n"
        "        getattr(loop, name), noop, protocol_factory, *argv\n"
        "    )\n",
        encoding="utf-8",
    )

    assert _direct_execution_records([path]) == Counter()


@pytest.mark.parametrize(
    "mapping_expression",
    [
        "{'func': callback} | {'func': noop}",
        "{'func': callback, **{'func': noop}}",
        "dict({'func': callback}, func=noop)",
    ],
)
def test_execution_scanner_honors_rightmost_static_keyword_mapping_values(
    tmp_path: Path,
    mapping_expression: str,
) -> None:
    path = tmp_path / "overridden_callback_mapping.py"
    path.write_text(
        "import itertools\n"
        "def invoke(callback, noop, values):\n"
        "    return list(itertools.accumulate(\n"
        f"        values, **({mapping_expression})\n"
        "    ))\n"
        "def safe(loop, name, noop, values):\n"
        "    return invoke(getattr(loop, name), noop, values)\n",
        encoding="utf-8",
    )

    records = _direct_execution_records([path])

    assert not any(kind.startswith("asyncio.") for *_, kind in records), records


@pytest.mark.parametrize(
    "source",
    [
        (
            "import itertools\n"
            "def dict(**kwargs):\n"
            "    return {}\n"
            "def invoke(callback, values):\n"
            "    return list(itertools.accumulate(\n"
            "        values, **dict(func=callback)\n"
            "    ))\n"
            "def safe(loop, name, values):\n"
            "    return invoke(getattr(loop, name), values)\n"
        ),
        (
            "import itertools\n"
            "def invoke(builtins, callback, values):\n"
            "    return list(itertools.accumulate(\n"
            "        values, **builtins.dict(func=callback)\n"
            "    ))\n"
            "def safe(fake_builtins, loop, name, values):\n"
            "    return invoke(fake_builtins, getattr(loop, name), values)\n"
        ),
    ],
)
def test_execution_scanner_respects_builtin_dict_shadowing(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "shadowed_builtin_dict.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert not any(kind.startswith("asyncio.") for *_, kind in records), records


@pytest.mark.parametrize(
    "source",
    [
        (
            "import itertools\n"
            "def invoke(callback, values):\n"
            "    return list(itertools.accumulate(\n"
            "        values, **dict(func=callback)\n"
            "    ))\n"
            "def bypass(loop, name, values):\n"
            "    return invoke(getattr(loop, name), values)\n"
        ),
        (
            "import builtins, itertools\n"
            "def invoke(callback, values):\n"
            "    return list(itertools.accumulate(\n"
            "        values, **builtins.dict(func=callback)\n"
            "    ))\n"
            "def bypass(loop, name, values):\n"
            "    return invoke(getattr(loop, name), values)\n"
        ),
        (
            "import itertools\n"
            "from builtins import dict as make_mapping\n"
            "def invoke(callback, values):\n"
            "    return list(itertools.accumulate(\n"
            "        values, **make_mapping(func=callback)\n"
            "    ))\n"
            "def bypass(loop, name, values):\n"
            "    return invoke(getattr(loop, name), values)\n"
        ),
        (
            "import itertools\n"
            "def invoke(callback, overrides, values):\n"
            "    return list(itertools.accumulate(\n"
            "        values, **{'func': callback, **overrides}\n"
            "    ))\n"
            "def bypass(loop, name, overrides, values):\n"
            "    return invoke(getattr(loop, name), overrides, values)\n"
        ),
        (
            "import itertools\n"
            "def invoke(callback, noop, enabled, values):\n"
            "    return list(itertools.accumulate(\n"
            "        values, **(\n"
            "            {'func': callback}\n"
            "            | ({'func': noop} if enabled else {})\n"
            "        )\n"
            "    ))\n"
            "def bypass(loop, name, noop, enabled, values):\n"
            "    return invoke(getattr(loop, name), noop, enabled, values)\n"
        ),
    ],
)
def test_execution_scanner_preserves_live_keyword_mapping_callbacks(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "live_callback_mapping.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(kind.startswith("asyncio.") for *_, kind in records), records


def test_execution_scanner_ignores_process_named_methods_on_non_loop_receivers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "non_loop_process_method.py"
    path.write_text(
        "class Renderer:\n"
        "    @staticmethod\n"
        "    def subprocess_exec():\n"
        "        return 'rendered'\n"
        "def safe():\n"
        "    renderer = Renderer()\n"
        "    return renderer.subprocess_exec()\n",
        encoding="utf-8",
    )

    assert _direct_execution_records([path]) == Counter()


def test_execution_scanner_does_not_treat_local_event_loop_access_as_launch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "local_event_loop.py"
    path.write_text(
        "import asyncio\n"
        "async def monotonic_time():\n"
        "    loop = asyncio.get_running_loop()\n"
        "    return loop.time()\n",
        encoding="utf-8",
    )

    records = _direct_execution_records([path])

    assert not any(kind.startswith("asyncio.") for *_, kind in records)


def test_execution_scanner_does_not_execute_dynamic_attribute_data_flow(
    tmp_path: Path,
) -> None:
    path = tmp_path / "dynamic_attribute_data.py"
    path.write_text(
        "name = 'upper'\n"
        "class Renderer:\n"
        "    attribute = getattr(str, name)\n"
        "exported = None\n"
        "def export_attribute(value, name):\n"
        "    global exported\n"
        "    exported = getattr(value, name)\n"
        "def render_attribute(value, name):\n"
        "    attribute = getattr(value, name)\n"
        "    return str(attribute)\n"
        "def preserve_callback(callback):\n"
        "    return next(map(lambda fn: str(fn), [callback]))\n"
        "def inspect_callback(value, name):\n"
        "    return preserve_callback(getattr(value, name))\n"
        "def map(callback, values):\n"
        "    return values\n"
        "def preserve_with_shadow(callback, values):\n"
        "    return map(callback, values)\n"
        "def inspect_shadowed_map(value, name):\n"
        "    return preserve_with_shadow(getattr(value, name), [])\n",
        encoding="utf-8",
    )

    records = _direct_execution_records([path])

    assert not any(kind.startswith("asyncio.") for *_, kind in records)


def test_execution_scanner_drops_reassigned_class_dynamic_callable_markers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reassigned_class_attribute.py"
    path.write_text(
        "name = 'subprocess_exec'\n"
        "class FormerLauncher:\n"
        "    launch = getattr(object, name)\n"
        "    launch = lambda *args: None\n"
        "class Unrelated:\n"
        "    launch = lambda *args: None\n"
        "FormerLauncher.launch()\n"
        "Unrelated.launch()\n",
        encoding="utf-8",
    )

    records = _direct_execution_records([path])

    assert not any(kind.startswith("asyncio.") for *_, kind in records)


def test_execution_scanner_ignores_relative_functools_partial_import(
    tmp_path: Path,
) -> None:
    path = tmp_path / "relative_partial.py"
    path.write_text(
        "from .functools import partial\n"
        "def render(value, name):\n"
        "    selected = partial(getattr(value, name))\n"
        "    return selected()\n",
        encoding="utf-8",
    )

    records = _direct_execution_records([path])

    assert not any(kind.startswith("asyncio.") for *_, kind in records)


def test_execution_scanner_clears_dynamic_marker_in_unconditional_finally(
    tmp_path: Path,
) -> None:
    path = tmp_path / "finally_reassignment.py"
    path.write_text(
        "def bypass(loop, name, noop):\n"
        "    launch = getattr(loop, name)\n"
        "    try:\n"
        "        work()\n"
        "    finally:\n"
        "        launch = noop\n"
        "    launch()\n",
        encoding="utf-8",
    )

    records = _direct_execution_records([path])

    assert not any(kind.startswith("asyncio.") for *_, kind in records)


@pytest.mark.parametrize(
    "source",
    [
        (
            "import asyncio\n"
            "async def bypass(holder, protocol_factory, argv):\n"
            "    holder.loop = asyncio.get_running_loop()\n"
            "    await holder.loop.subprocess_exec(protocol_factory, *argv)\n"
        ),
        (
            "import asyncio\n"
            "async def bypass(protocol_factory, argv):\n"
            "    (loop,) = (asyncio.get_running_loop(),)\n"
            "    await loop.subprocess_exec(protocol_factory, *argv)\n"
        ),
        (
            "import asyncio\n"
            "async def bypass(protocol_factory, argv):\n"
            "    loops = [asyncio.get_running_loop()]\n"
            "    await loops[0].subprocess_exec(protocol_factory, *argv)\n"
        ),
    ],
)
def test_execution_scanner_records_unpropagated_event_loop_bindings(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "unpropagated_event_loop.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(kind.startswith("asyncio.") for *_, kind in records)


@pytest.mark.parametrize(
    "source",
    [
        (
            "import asyncio\n"
            "async def bypass(holder, protocol_factory, argv):\n"
            "    holder.engine = asyncio.get_running_loop()\n"
            "    await holder.engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
        (
            "import asyncio\n"
            "async def bypass(holder, protocol_factory, argv):\n"
            "    holder.engine: object = asyncio.get_running_loop()\n"
            "    await holder.engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
        (
            "import asyncio\n"
            "async def bypass(holder, protocol_factory, argv):\n"
            "    holder['engine'] = asyncio.get_running_loop()\n"
            "    await holder['engine'].subprocess_exec(protocol_factory, *argv)\n"
        ),
        (
            "import asyncio\n"
            "async def bypass(holder, protocol_factory, argv):\n"
            "    (holder.engine,) = (asyncio.get_running_loop(),)\n"
            "    await holder.engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
    ],
)
def test_execution_scanner_preserves_qualified_event_loop_bindings(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "qualified_event_loop_binding.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(kind == "asyncio.subprocess_exec" for *_, kind in records), records


def test_execution_scanner_clears_reassigned_qualified_event_loop_bindings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reassigned_qualified_event_loop.py"
    path.write_text(
        "import asyncio\n"
        "async def safe(holder, renderer, protocol_factory, argv):\n"
        "    holder.engine = asyncio.get_running_loop()\n"
        "    holder.engine = renderer\n"
        "    await holder.engine.subprocess_exec(protocol_factory, *argv)\n",
        encoding="utf-8",
    )

    records = _direct_execution_records([path])

    assert not any(kind.startswith("asyncio.") for *_, kind in records), records


@pytest.mark.parametrize(
    "source",
    [
        (
            "import asyncio\n"
            "async def invoke(receiver, protocol_factory, argv):\n"
            "    await receiver.subprocess_exec(protocol_factory, *argv)\n"
            "async def bypass(protocol_factory, argv):\n"
            "    await invoke(asyncio.get_running_loop(), protocol_factory, argv)\n"
        ),
        (
            "import asyncio\n"
            "async def invoke(receiver, protocol_factory, argv):\n"
            "    await receiver.subprocess_exec(protocol_factory, *argv)\n"
            "async def bypass(protocol_factory, argv):\n"
            "    await invoke(\n"
            "        receiver=asyncio.get_running_loop(),\n"
            "        protocol_factory=protocol_factory,\n"
            "        argv=argv,\n"
            "    )\n"
        ),
        (
            "import asyncio\n"
            "async def invoke(receiver, protocol_factory, argv):\n"
            "    await receiver.subprocess_exec(protocol_factory, *argv)\n"
            "forward = invoke\n"
            "async def bypass(protocol_factory, argv):\n"
            "    await forward(asyncio.get_running_loop(), protocol_factory, argv)\n"
        ),
        (
            "import asyncio\n"
            "class Helpers:\n"
            "    async def invoke(self, receiver, protocol_factory, argv):\n"
            "        await receiver.subprocess_exec(protocol_factory, *argv)\n"
            "async def bypass(protocol_factory, argv):\n"
            "    await Helpers().invoke(\n"
            "        asyncio.get_running_loop(), protocol_factory, argv\n"
            "    )\n"
        ),
        (
            "import asyncio\n"
            "async def launch(receiver, protocol_factory, argv):\n"
            "    engine = receiver\n"
            "    await engine.subprocess_exec(protocol_factory, *argv)\n"
            "async def relay(target, protocol_factory, argv):\n"
            "    await launch(target, protocol_factory, argv)\n"
            "async def bypass(protocol_factory, argv):\n"
            "    await relay(asyncio.get_running_loop(), protocol_factory, argv)\n"
        ),
    ],
)
def test_execution_scanner_propagates_event_loop_receivers_into_helpers(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "event_loop_helper.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(kind == "asyncio.subprocess_exec" for *_, kind in records), records


def test_execution_scanner_ignores_non_event_loop_helper_receivers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "non_event_loop_helper.py"
    path.write_text(
        "async def invoke(receiver, protocol_factory, argv):\n"
        "    await receiver.subprocess_exec(protocol_factory, *argv)\n"
        "async def safe(renderer, protocol_factory, argv):\n"
        "    await invoke(renderer, protocol_factory, argv)\n",
        encoding="utf-8",
    )

    records = _direct_execution_records([path])

    assert not any(kind.startswith("asyncio.") for *_, kind in records), records


@pytest.mark.parametrize(
    "source",
    [
        (
            "import asyncio\n"
            "def identity(receiver):\n"
            "    return receiver\n"
            "async def bypass(protocol_factory, argv):\n"
            "    engine = identity(asyncio.get_running_loop())\n"
            "    await engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
        (
            "import asyncio\n"
            "def identity(receiver):\n"
            "    return receiver\n"
            "async def bypass(protocol_factory, argv):\n"
            "    await identity(asyncio.get_running_loop()).subprocess_exec(\n"
            "        protocol_factory, *argv\n"
            "    )\n"
        ),
        (
            "import asyncio\n"
            "def identity(receiver):\n"
            "    return receiver\n"
            "def relay(value):\n"
            "    return identity(value)\n"
            "async def bypass(protocol_factory, argv):\n"
            "    engine = relay(asyncio.get_running_loop())\n"
            "    await engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
        (
            "import asyncio\n"
            "class Helpers:\n"
            "    def identity(self, receiver):\n"
            "        return receiver\n"
            "async def bypass(protocol_factory, argv):\n"
            "    engine = Helpers().identity(asyncio.get_running_loop())\n"
            "    await engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
    ],
)
def test_execution_scanner_propagates_returned_event_loop_receivers(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "returned_event_loop.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(kind == "asyncio.subprocess_exec" for *_, kind in records), records


def test_execution_scanner_ignores_returned_non_event_loop_receivers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "returned_non_event_loop.py"
    path.write_text(
        "def identity(receiver):\n"
        "    return receiver\n"
        "async def safe(renderer, protocol_factory, argv):\n"
        "    engine = identity(renderer)\n"
        "    await engine.subprocess_exec(protocol_factory, *argv)\n",
        encoding="utf-8",
    )

    records = _direct_execution_records([path])

    assert not any(kind.startswith("asyncio.") for *_, kind in records), records


@pytest.mark.parametrize(
    "source",
    [
        (
            "import asyncio\n"
            "async def bypass(protocol_factory, argv):\n"
            "    engine = next(iter([asyncio.get_running_loop()]))\n"
            "    await engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
        (
            "import asyncio, builtins\n"
            "async def bypass(protocol_factory, argv):\n"
            "    engine = builtins.next(\n"
            "        builtins.iter([asyncio.get_running_loop()])\n"
            "    )\n"
            "    await engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
    ],
)
def test_execution_scanner_propagates_event_loops_through_iterator_extraction(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "iterator_event_loop.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(kind == "asyncio.subprocess_exec" for *_, kind in records), records


def test_execution_scanner_respects_iterator_builtin_shadowing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "shadowed_iterator_builtins.py"
    path.write_text(
        "import asyncio\n"
        "def iter(values):\n"
        "    return values\n"
        "def next(values):\n"
        "    return object()\n"
        "async def safe(protocol_factory, argv):\n"
        "    engine = next(iter([asyncio.get_running_loop()]))\n"
        "    await engine.subprocess_exec(protocol_factory, *argv)\n",
        encoding="utf-8",
    )

    records = _direct_execution_records([path])

    assert not any(kind.startswith("asyncio.") for *_, kind in records), records


@pytest.mark.parametrize(
    "source",
    [
        (
            "import asyncio\n"
            "async def bypass(protocol_factory, argv):\n"
            "    with asyncio.Runner() as runner:\n"
            "        engine = runner.get_loop()\n"
            "        await engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
        (
            "import asyncio\n"
            "async def bypass(protocol_factory, argv):\n"
            "    runner = asyncio.Runner()\n"
            "    engine = runner.get_loop()\n"
            "    await engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
        (
            "import asyncio\n"
            "async def bypass(protocol_factory, argv):\n"
            "    engine = asyncio.Runner().get_loop()\n"
            "    await engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
        (
            "from asyncio import Runner as LoopRunner\n"
            "async def bypass(protocol_factory, argv):\n"
            "    engine = LoopRunner().get_loop()\n"
            "    await engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
    ],
)
def test_execution_scanner_recognizes_asyncio_runner_event_loops(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "asyncio_runner_loop.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(kind == "asyncio.subprocess_exec" for *_, kind in records), records


def test_execution_scanner_ignores_non_asyncio_runner_get_loop(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fake_runner_loop.py"
    path.write_text(
        "async def safe(runner, protocol_factory, argv):\n"
        "    engine = runner.get_loop()\n"
        "    await engine.subprocess_exec(protocol_factory, *argv)\n",
        encoding="utf-8",
    )

    records = _direct_execution_records([path])

    assert not any(kind.startswith("asyncio.") for *_, kind in records), records


@pytest.mark.parametrize(
    "source",
    [
        (
            "import asyncio\n"
            "async def bypass(protocol_factory, argv):\n"
            "    engine = asyncio.current_task().get_loop()\n"
            "    await engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
        (
            "from asyncio import current_task as active_task\n"
            "async def bypass(protocol_factory, argv):\n"
            "    engine = active_task().get_loop()\n"
            "    await engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
        (
            "import asyncio\n"
            "async def bypass(protocol_factory, argv):\n"
            "    task = asyncio.current_task()\n"
            "    engine = task.get_loop()\n"
            "    await engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
    ],
)
def test_execution_scanner_recognizes_asyncio_task_event_loops(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "asyncio_task_loop.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(kind == "asyncio.subprocess_exec" for *_, kind in records), records


@pytest.mark.parametrize(
    "source",
    [
        (
            "import asyncio.events\n"
            "async def bypass(protocol_factory, argv):\n"
            "    engine = asyncio.events.get_running_loop()\n"
            "    await engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
        (
            "import asyncio.events as async_events\n"
            "async def bypass(protocol_factory, argv):\n"
            "    engine = async_events.get_running_loop()\n"
            "    await engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
        (
            "from asyncio import events\n"
            "async def bypass(protocol_factory, argv):\n"
            "    engine = events.get_running_loop()\n"
            "    await engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
        (
            "async def bypass(protocol_factory, argv):\n"
            "    import asyncio.events\n"
            "    engine = asyncio.events.get_running_loop()\n"
            "    await engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
        (
            "async def bypass(protocol_factory, argv):\n"
            "    from asyncio import events as async_events\n"
            "    engine = async_events.get_running_loop()\n"
            "    await engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
    ],
)
def test_execution_scanner_preserves_asyncio_submodule_imports(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "asyncio_submodule_import.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(kind == "asyncio.subprocess_exec" for *_, kind in records), records


def test_execution_scanner_respects_shadowed_asyncio_submodule_roots(
    tmp_path: Path,
) -> None:
    path = tmp_path / "shadowed_asyncio_submodule.py"
    path.write_text(
        "import asyncio.events\n"
        "async def safe(asyncio, protocol_factory, argv):\n"
        "    engine = asyncio.events.get_running_loop()\n"
        "    await engine.subprocess_exec(protocol_factory, *argv)\n",
        encoding="utf-8",
    )

    records = _direct_execution_records([path])

    assert not any(kind.startswith("asyncio.") for *_, kind in records), records


@pytest.mark.parametrize(
    "source",
    [
        (
            "import asyncio\n"
            "class Holder:\n"
            "    @property\n"
            "    def engine(self):\n"
            "        return asyncio.get_running_loop()\n"
            "async def bypass(protocol_factory, argv):\n"
            "    await Holder().engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
        (
            "import asyncio\n"
            "class Holder:\n"
            "    @property\n"
            "    def engine(self):\n"
            "        return asyncio.get_running_loop()\n"
            "async def bypass(protocol_factory, argv):\n"
            "    holder = Holder()\n"
            "    engine = holder.engine\n"
            "    await engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
        (
            "import asyncio\n"
            "def identity(value):\n"
            "    return value\n"
            "class Holder:\n"
            "    @property\n"
            "    def engine(self):\n"
            "        return identity(asyncio.get_running_loop())\n"
            "async def bypass(protocol_factory, argv):\n"
            "    await Holder().engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
    ],
)
def test_execution_scanner_propagates_event_loops_from_property_getters(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "event_loop_property.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(kind == "asyncio.subprocess_exec" for *_, kind in records), records


def test_execution_scanner_ignores_non_event_loop_property_getters(
    tmp_path: Path,
) -> None:
    path = tmp_path / "non_event_loop_property.py"
    path.write_text(
        "class Holder:\n"
        "    @property\n"
        "    def engine(self):\n"
        "        return object()\n"
        "async def safe(protocol_factory, argv):\n"
        "    await Holder().engine.subprocess_exec(protocol_factory, *argv)\n",
        encoding="utf-8",
    )

    records = _direct_execution_records([path])

    assert not any(kind.startswith("asyncio.") for *_, kind in records), records


@pytest.mark.parametrize(
    "source",
    [
        (
            "import asyncio\n"
            "import functools\n"
            "class Holder:\n"
            "    @functools.cached_property\n"
            "    def engine(self):\n"
            "        return asyncio.get_running_loop()\n"
            "async def bypass(protocol_factory, argv):\n"
            "    await Holder().engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
        (
            "import asyncio\n"
            "from functools import cached_property\n"
            "class Holder:\n"
            "    @cached_property\n"
            "    def engine(self):\n"
            "        return asyncio.get_running_loop()\n"
            "async def bypass(protocol_factory, argv):\n"
            "    await Holder().engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
        (
            "import asyncio\n"
            "import functools as tools\n"
            "class Holder:\n"
            "    @tools.cached_property\n"
            "    def engine(self):\n"
            "        return asyncio.get_running_loop()\n"
            "async def bypass(protocol_factory, argv):\n"
            "    await Holder().engine.subprocess_exec(protocol_factory, *argv)\n"
        ),
    ],
)
def test_execution_scanner_propagates_event_loops_from_cached_properties(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "event_loop_cached_property.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(kind == "asyncio.subprocess_exec" for *_, kind in records), records


def test_execution_scanner_ignores_untrusted_cached_property_decorators(
    tmp_path: Path,
) -> None:
    path = tmp_path / "untrusted_cached_property.py"
    path.write_text(
        "import asyncio\n"
        "def cached_property(function):\n"
        "    return function\n"
        "class Holder:\n"
        "    @cached_property\n"
        "    def engine(self):\n"
        "        return asyncio.get_running_loop()\n"
        "async def safe(protocol_factory, argv):\n"
        "    await Holder().engine.subprocess_exec(protocol_factory, *argv)\n",
        encoding="utf-8",
    )

    records = _direct_execution_records([path])

    assert not any(kind.startswith("asyncio.") for *_, kind in records), records


@pytest.mark.parametrize(
    ("source", "expected_kind"),
    [
        (
            "import posix\n"
            "def bypass():\n"
            "    posix.system('vivado')\n",
            "posix.system",
        ),
        (
            "import posix\n"
            "def bypass():\n"
            "    posix.posix_spawn('vivado', ['vivado'], {})\n",
            "posix.posix_spawn",
        ),
        (
            "from posix import system as launch\n"
            "def bypass():\n"
            "    launch('vivado')\n",
            "posix.system",
        ),
        (
            "import nt\n"
            "def bypass():\n"
            "    nt.system('vivado')\n",
            "nt.system",
        ),
        (
            "import os\n"
            "def bypass():\n"
            "    os.fork()\n",
            "os.fork",
        ),
        (
            "import os\n"
            "def bypass():\n"
            "    os.forkpty()\n",
            "os.forkpty",
        ),
        (
            "import posix\n"
            "def bypass():\n"
            "    posix.fork()\n",
            "posix.fork",
        ),
        (
            "import pty\n"
            "def bypass():\n"
            "    pty.fork()\n",
            "pty.fork",
        ),
    ],
)
def test_execution_scanner_covers_low_level_platform_process_modules(
    tmp_path: Path,
    source: str,
    expected_kind: str,
) -> None:
    path = tmp_path / "low_level_process.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(kind == expected_kind for (*_, kind) in records)


@pytest.mark.parametrize(
    ("source", "expected_kind"),
    [
        (
            "import os\n"
            "name = 'system'\n"
            "def bypass(command):\n"
            "    getattr(os, name)(command)\n",
            "os.*",
        ),
        (
            "import subprocess\n"
            "name = 'run'\n"
            "def bypass(argv):\n"
            "    getattr(subprocess, name)(argv)\n",
            "subprocess.*",
        ),
        (
            "import os\n"
            "def bypass(command, suffix):\n"
            "    getattr(os, 'sys' + suffix)(command)\n",
            "os.*",
        ),
        (
            "import subprocess\n"
            "def bypass(argv, choose_name):\n"
            "    getattr(subprocess, choose_name())(argv)\n",
            "subprocess.*",
        ),
        (
            "import os\n"
            "def bypass(flag, name, command):\n"
            "    global getattr\n"
            "    if flag:\n"
            "        getattr = lambda *_: (lambda *_: None)\n"
            "    getattr(os, name)(command)\n",
            "os.*",
        ),
        (
            "import builtins\n"
            "import os\n"
            "def bypass(choose_name, command):\n"
            "    builtins.getattr(os, choose_name())(command)\n",
            "os.*",
        ),
        (
            "import builtins as runtime_builtins\n"
            "import subprocess\n"
            "def bypass(choose_name, argv):\n"
            "    runtime_builtins.getattr(subprocess, choose_name())(argv)\n",
            "subprocess.*",
        ),
        (
            "from builtins import getattr\n"
            "import os\n"
            "def bypass(command):\n"
            "    getattr(os, 'system')(command)\n",
            "os.system",
        ),
        (
            "from builtins import getattr as reflect\n"
            "import subprocess\n"
            "def bypass(name, argv):\n"
            "    reflect(subprocess, name)(argv)\n",
            "subprocess.*",
        ),
        (
            "import os\n"
            "reflect = getattr\n"
            "def bypass(name, command):\n"
            "    reflect(os, name)(command)\n",
            "os.*",
        ),
        (
            "import os\n"
            "def bypass(name, command):\n"
            "    reflect = getattr\n"
            "    reflect(os, name)(command)\n",
            "os.*",
        ),
        (
            "import os\n"
            "def bypass(name, command, reflect=getattr):\n"
            "    reflect(os, name)(command)\n",
            "os.*",
        ),
        (
            "import os\n"
            "getattr = lambda *_: (lambda *_: None)\n"
            "del getattr\n"
            "getattr(os, name)(command)\n",
            "os.*",
        ),
        (
            "import os\n"
            "class Reflect:\n"
            "    lookup = getattr\n"
            "def bypass(name, command):\n"
            "    Reflect.lookup(os, name)(command)\n",
            "os.*",
        ),
        (
            "import os\n"
            "def bypass(flag, safe, name, command):\n"
            "    reflect = getattr if flag else safe\n"
            "    reflect(os, name)(command)\n",
            "os.*",
        ),
        (
            "import os\n"
            "def bypass(name, command):\n"
            "    reflect = None or getattr\n"
            "    reflect(os, name)(command)\n",
            "os.*",
        ),
        (
            "import os\n"
            "class Holder:\n"
            "    pass\n"
            "holder = Holder()\n"
            "holder.reflect = getattr\n"
            "holder.reflect(os, name)(command)\n",
            "os.*",
        ),
        (
            "import builtins\n"
            "import os\n"
            "def bypass(name, command):\n"
            "    builtins.__dict__['getattr'](os, name)(command)\n",
            "os.*",
        ),
        (
            "import builtins\n"
            "import os\n"
            "def bypass(name, command):\n"
            "    vars(builtins)['getattr'](os, name)(command)\n",
            "os.*",
        ),
        (
            "import os\n"
            "def bypass(name, command):\n"
            "    reflect, ignored = (getattr, None)\n"
            "    reflect(os, name)(command)\n",
            "os.*",
        ),
    ],
)
def test_execution_scanner_conservatively_tracks_named_reflective_attributes(
    tmp_path: Path,
    source: str,
    expected_kind: str,
) -> None:
    path = tmp_path / "named_reflective_attribute.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(kind == expected_kind for (*_, kind) in records)


@pytest.mark.parametrize(
    ("source", "expected_kind"),
    [
        (
            "import subprocess\n"
            "sp = subprocess\n"
            "def safe(sp):\n"
            "    sp.run(['not-a-process'])\n",
            "subprocess.run",
        ),
        (
            "from subprocess import run as execute\n"
            "def safe(execute, data):\n"
            "    execute(data)\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "sp = subprocess\n"
            "def safe():\n"
            "    sp = object()\n"
            "    sp.run(['not-a-process'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "execute = subprocess.run\n"
            "def safe(data):\n"
            "    execute = lambda value: value\n"
            "    execute(data)\n",
            "subprocess.run",
        ),
        (
            "import pty\n"
            "def safe(pty):\n"
            "    pty.spawn(['not-a-process'])\n",
            "pty.spawn",
        ),
        (
            "from pty import spawn\n"
            "def safe(spawn):\n"
            "    spawn(['not-a-process'])\n",
            "pty.spawn",
        ),
        (
            "import posix\n"
            "def safe(posix):\n"
            "    posix.system('not-a-process')\n",
            "posix.system",
        ),
        (
            "from nt import system\n"
            "def safe(system):\n"
            "    system('not-a-process')\n",
            "nt.system",
        ),
        (
            "import os\n"
            "def safe(os):\n"
            "    os.fork()\n",
            "os.fork",
        ),
        (
            "from pty import fork\n"
            "def safe(fork):\n"
            "    fork()\n",
            "pty.fork",
        ),
        (
            "import os\n"
            "name = 'system'\n"
            "def safe(os):\n"
            "    getattr(os, name)('not-a-process')\n",
            "os.*",
        ),
        (
            "import subprocess\n"
            "name = 'run'\n"
            "def safe(subprocess):\n"
            "    getattr(subprocess, name)(['not-a-process'])\n",
            "subprocess.*",
        ),
        (
            "import os\n"
            "def safe(os, suffix):\n"
            "    getattr(os, 'sys' + suffix)('not-a-process')\n",
            "os.*",
        ),
        (
            "import os\n"
            "def safe(getattr, name):\n"
            "    getattr(os, name)('not-a-process')\n",
            "os.*",
        ),
        (
            "import os\n"
            "def safe(name):\n"
            "    getattr = lambda *_: (lambda *_: None)\n"
            "    getattr(os, name)('not-a-process')\n",
            "os.*",
        ),
        (
            "import builtins\n"
            "import os\n"
            "def safe(builtins, name):\n"
            "    builtins.getattr(os, name)('not-a-process')\n",
            "os.*",
        ),
        (
            "from builtins import getattr as reflect\n"
            "import os\n"
            "def safe(reflect, name):\n"
            "    reflect(os, name)('not-a-process')\n",
            "os.*",
        ),
        (
            "import os\n"
            "def safe(name):\n"
            "    getattr(os, name)('not-a-process')\n"
            "    getattr = lambda *_: (lambda *_: None)\n",
            "os.*",
        ),
        (
            "import os\n"
            "def safe(name):\n"
            "    getattr(os, name)('not-a-process')\n"
            "    try:\n"
            "        pass\n"
            "    except Exception as getattr:\n"
            "        pass\n",
            "os.*",
        ),
        (
            "import os\n"
            "def safe(name):\n"
            "    getattr = lambda *_: (lambda *_: None)\n"
            "    del getattr\n"
            "    getattr(os, name)('not-a-process')\n",
            "os.*",
        ),
        (
            "import os\n"
            "class Reflect:\n"
            "    lookup = getattr\n"
            "class Reflect:\n"
            "    lookup = lambda *_: (lambda *_: None)\n"
            "def safe(name):\n"
            "    Reflect.lookup(os, name)('not-a-process')\n",
            "os.*",
        ),
        (
            "import os\n"
            "def safe(value, name):\n"
            "    getattr(os, name)('not-a-process')\n"
            "    match value:\n"
            "        case getattr:\n"
            "            pass\n",
            "os.*",
        ),
        (
            "import os\n"
            "def safe(object, name):\n"
            "    object.__getattribute__(os, name)('not-a-process')\n",
            "os.*",
        ),
        (
            "import os\n"
            "def safe(name):\n"
            "    class SafeObject:\n"
            "        def __getattribute__(self, *_):\n"
            "            return lambda *_: None\n"
            "    object = SafeObject()\n"
            "    object.__getattribute__(os, name)('not-a-process')\n",
            "os.*",
        ),
        (
            "import os\n"
            "class SafeObject:\n"
            "    def __getattribute__(self, *_):\n"
            "        return lambda *_: None\n"
            "object = SafeObject()\n"
            "def safe(name):\n"
            "    object.__getattribute__(os, name)('not-a-process')\n",
            "os.*",
        ),
    ],
)
def test_execution_scanner_respects_parameter_and_local_alias_shadowing(
    tmp_path: Path,
    source: str,
    expected_kind: str,
) -> None:
    path = tmp_path / "shadowed.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert not any(
        function == "safe" and kind == expected_kind
        for (_, function, kind) in records
    )


@pytest.mark.parametrize(
    ("source", "expected_kind"),
    [
        (
            "def importing(argv):\n"
            "    import subprocess as sp\n"
            "    sp.run(argv)\n"
            "def safe(argv):\n"
            "    sp.run(argv)\n",
            "subprocess.run",
        ),
        (
            "def importing(argv):\n"
            "    from subprocess import run as execute\n"
            "    execute(argv)\n"
            "def safe(argv):\n"
            "    execute(argv)\n",
            "subprocess.run",
        ),
    ],
)
def test_execution_scanner_keeps_local_import_aliases_lexical(
    tmp_path: Path,
    source: str,
    expected_kind: str,
) -> None:
    path = tmp_path / "local_imports.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(
        function == "importing" and kind == expected_kind
        for (_, function, kind) in records
    )
    assert not any(
        function == "safe" and kind == expected_kind
        for (_, function, kind) in records
    )


@pytest.mark.parametrize(
    "qualified_call",
    [
        "C.sp.run(argv)",
        "self.sp.run(argv)",
        "cls.sp.run(argv)",
    ],
)
def test_execution_scanner_conservatively_records_class_process_module_exposure(
    tmp_path: Path,
    qualified_call: str,
) -> None:
    path = tmp_path / "class_process_alias.py"
    path.write_text(
        "class C:\n"
        "    import subprocess as sp\n"
        "    def launch(self, cls, argv):\n"
        f"        {qualified_call}\n"
        "def safe(argv):\n"
        "    sp.run(argv)\n",
        encoding="utf-8",
    )

    records = _direct_execution_records([path])

    assert any(
        function == "C" and kind == "subprocess.*"
        for (_, function, kind) in records
    )
    assert not any(function == "safe" for (_, function, _) in records)


def test_execution_scanner_skips_class_local_shadow_for_method_bare_name(
    tmp_path: Path,
) -> None:
    path = tmp_path / "class_local_shadow.py"
    path.write_text(
        "import subprocess\n"
        "class C:\n"
        "    subprocess = object()\n"
        "    def launch(self):\n"
        "        subprocess.run([])\n",
        encoding="utf-8",
    )

    records = _direct_execution_records([path])

    assert any(
        function == "launch" and kind == "subprocess.run"
        for (_, function, kind) in records
    )


def test_execution_scanner_applies_class_local_shadow_inside_class_body(
    tmp_path: Path,
) -> None:
    path = tmp_path / "class_body_shadow.py"
    path.write_text(
        "import subprocess\n"
        "class C:\n"
        "    subprocess = object()\n"
        "    subprocess.run([])\n",
        encoding="utf-8",
    )

    assert _direct_execution_records([path]) == Counter()


@pytest.mark.parametrize(
    ("source", "binding_function"),
    [
        (
            "import subprocess\n"
            "def configure():\n"
            "    global sp\n"
            "    sp = subprocess\n"
            "def launch(argv):\n"
            "    sp.run(argv)\n",
            "configure",
        ),
        (
            "def outer():\n"
            "    import subprocess\n"
            "    sp = None\n"
            "    def configure():\n"
            "        nonlocal sp\n"
            "        sp = subprocess\n"
            "    def launch(argv):\n"
            "        sp.run(argv)\n",
            "configure",
        ),
    ],
)
def test_execution_scanner_records_cross_scope_process_alias_binding(
    tmp_path: Path,
    source: str,
    binding_function: str,
) -> None:
    path = tmp_path / "cross_scope_alias.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(
        function == binding_function and kind == "subprocess.*"
        for (_, function, kind) in records
    )


@pytest.mark.parametrize(
    "source",
    [
        (
            "import subprocess\n"
            "def annotated(value: subprocess.Popen(['vivado'])):\n"
            "    return value\n"
        ),
        (
            "import subprocess\n"
            "def annotated() -> subprocess.Popen(['vivado']):\n"
            "    return None\n"
        ),
        (
            "import subprocess\n"
            "from typing import Annotated\n"
            "def annotated(value: Annotated[int, subprocess.Popen(['vivado'])]):\n"
            "    return value\n"
        ),
        (
            "import subprocess\n"
            "class Executor:\n"
            "    def submit(self, function, *args):\n"
            "        return function(*args)\n"
            "executor = Executor()\n"
            "def annotated(value: executor.submit(subprocess.run, ['vivado'])):\n"
            "    return value\n"
        ),
        (
            "import subprocess\n"
            "class Executor:\n"
            "    def submit(self, function, *args):\n"
            "        return function(*args)\n"
            "executor = Executor()\n"
            "def annotated() -> executor.submit(subprocess.run, ['vivado']):\n"
            "    return None\n"
        ),
    ],
)
def test_execution_scanner_detects_runtime_annotation_process_calls(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "runtime_annotation.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(
        kind in {"subprocess.Popen", "subprocess.run"}
        for (*_, kind) in records
    )


def test_execution_scanner_skips_deferred_annotation_expressions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deferred_annotation.py"
    path.write_text(
        "from __future__ import annotations\n"
        "import subprocess\n"
        "def annotated(value: subprocess.Popen(['vivado'])):\n"
        "    return value\n",
        encoding="utf-8",
    )

    assert _direct_execution_records([path]) == Counter()


@pytest.mark.parametrize(
    "source",
    [
        (
            "import subprocess as sp\n"
            "flag = False\n"
            "if flag:\n"
            "    sp = object()\n"
            "sp.run(['vivado'])\n"
        ),
        (
            "import subprocess as sp\n"
            "items = []\n"
            "for item in items:\n"
            "    sp = object()\n"
            "sp.run(['vivado'])\n"
        ),
        (
            "import subprocess as sp\n"
            "try:\n"
            "    missing_name\n"
            "    sp = object()\n"
            "except NameError:\n"
            "    pass\n"
            "sp.run(['vivado'])\n"
        ),
    ],
)
def test_execution_scanner_ratchets_conditional_process_alias_rebinding(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "conditional_alias.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(kind.startswith("subprocess.") for (*_, kind) in records)


@pytest.mark.parametrize(
    ("source", "expected_kind"),
    [
        (
            "import os.path\n"
            "os.system('vivado')\n",
            "os.system",
        ),
        (
            "import asyncio.tasks\n"
            "asyncio.create_subprocess_exec('vivado')\n",
            "asyncio.create_subprocess_exec",
        ),
    ],
)
def test_execution_scanner_retains_process_parent_for_qualified_imports(
    tmp_path: Path,
    source: str,
    expected_kind: str,
) -> None:
    path = tmp_path / "qualified_parent_import.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(kind == expected_kind for (*_, kind) in records)


@pytest.mark.parametrize(
    "source",
    [
        (
            "import subprocess\n"
            "for sp in [subprocess]:\n"
            "    sp.run(['vivado'])\n"
        ),
        (
            "import subprocess\n"
            "results = [sp.run(['vivado']) for sp in [subprocess]]\n"
        ),
        (
            "from contextlib import nullcontext\n"
            "import subprocess\n"
            "with nullcontext(subprocess) as sp:\n"
            "    sp.run(['vivado'])\n"
        ),
        (
            "import subprocess\n"
            "match subprocess:\n"
            "    case sp:\n"
            "        sp.run(['vivado'])\n"
        ),
        (
            "import subprocess\n"
            "for sp in {'sp': subprocess}.values():\n"
            "    sp.run(['vivado'])\n"
        ),
        (
            "import subprocess\n"
            "for sp in [subprocess].__iter__():\n"
            "    sp.run(['vivado'])\n"
        ),
        (
            "import subprocess\n"
            "supplier = lambda: subprocess\n"
            "sp = supplier()\n"
            "sp.run(['vivado'])\n"
        ),
        (
            "import subprocess\n"
            "for sp in map(lambda _: subprocess, [0]):\n"
            "    sp.run(['vivado'])\n"
        ),
    ],
)
def test_execution_scanner_ratchets_process_alias_binding_sources(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "binding_source.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(kind.startswith("subprocess.") for (*_, kind) in records)


@pytest.mark.parametrize(
    "source",
    [
        (
            "import subprocess\n"
            "flag = True\n"
            "sp = subprocess if flag else object()\n"
            "sp.run(['vivado'])\n"
        ),
        (
            "import subprocess\n"
            "(sp,) = (subprocess,)\n"
            "sp.run(['vivado'])\n"
        ),
        (
            "import subprocess\n"
            "def identity(value):\n"
            "    return value\n"
            "sp = identity(subprocess)\n"
            "sp.run(['vivado'])\n"
        ),
    ],
)
def test_execution_scanner_ratchets_composite_process_alias_flow(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "composite_alias.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(kind.startswith("subprocess.") for (*_, kind) in records)


@pytest.mark.parametrize(
    "source",
    [
        (
            "def bypass(argv):\n"
            "    __import__('subprocess').run(argv)\n"
        ),
        (
            "import importlib\n"
            "def bypass(argv):\n"
            "    importlib.import_module('subprocess').run(argv)\n"
        ),
        (
            "def bypass(argv):\n"
            "    __import__(name='subprocess').run(argv)\n"
        ),
        (
            "def bypass(argv):\n"
            "    __import__('sub' + 'process').run(argv)\n"
        ),
        (
            "import importlib as il\n"
            "def bypass(argv):\n"
            "    il.import_module(name='subprocess').run(argv)\n"
        ),
        (
            "from importlib import import_module as load\n"
            "def bypass(argv):\n"
            "    load('subprocess').run(argv)\n"
        ),
        (
            "def bypass(argv):\n"
            "    from importlib import import_module as load\n"
            "    load(name='subprocess').run(argv)\n"
        ),
        (
            "from importlib import import_module as load\n"
            "def bypass(argv):\n"
            "    load_again = load\n"
            "    load_again('subprocess').run(argv)\n"
        ),
        (
            "import importlib as il\n"
            "loader = il.import_module\n"
            "def bypass(argv):\n"
            "    loader(f'subprocess').run(argv)\n"
        ),
        (
            "import importlib\n"
            "loader = getattr(importlib, 'import_module')\n"
            "def bypass(argv):\n"
            "    loader('subprocess').run(argv)\n"
        ),
        (
            "import builtins\n"
            "def bypass(argv):\n"
            "    builtins.__import__('subprocess').run(argv)\n"
        ),
        (
            "from builtins import __import__ as load\n"
            "def bypass(argv):\n"
            "    load('subprocess').run(argv)\n"
        ),
        (
            "import builtins\n"
            "loader = getattr(builtins, '__import__')\n"
            "def bypass(argv):\n"
            "    loader('subprocess').run(argv)\n"
        ),
        (
            "def bypass(argv):\n"
            "    import builtins as runtime_builtins\n"
            "    runtime_builtins.__import__(name='subprocess').run(argv)\n"
        ),
        (
            "def bypass(argv):\n"
            "    __builtins__.__import__('subprocess').run(argv)\n"
        ),
        (
            "def bypass(argv):\n"
            "    getattr(__builtins__, '__import__')('subprocess').run(argv)\n"
        ),
        (
            "def bypass(argv):\n"
            "    __builtins__['__import__']('subprocess').run(argv)\n"
        ),
        (
            "import builtins\n"
            "def bypass(argv):\n"
            "    builtins.__dict__['__import__']('subprocess').run(argv)\n"
        ),
        (
            "import builtins\n"
            "def bypass(argv):\n"
            "    builtins.__dict__.get('__import__')('subprocess').run(argv)\n"
        ),
        (
            "import builtins, operator\n"
            "def bypass(argv):\n"
            "    operator.getitem(builtins.__dict__, '__import__')('subprocess').run(argv)\n"
        ),
        (
            "import builtins, operator\n"
            "def bypass(argv):\n"
            "    operator.itemgetter('__import__')(vars(builtins))('subprocess').run(argv)\n"
        ),
        (
            "import builtins\n"
            "def bypass(argv):\n"
            "    {**builtins.__dict__}['__import__']('subprocess').run(argv)\n"
        ),
    ],
)
def test_execution_scanner_detects_dynamic_process_module_resolution(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "dynamic_import.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(kind == "subprocess.run" for (*_, kind) in records)


@pytest.mark.parametrize(
    "source",
    [
        "import pty\npty.spawn(['vivado'])\n",
        "from pty import spawn\nspawn(['vivado'])\n",
        "import pty as terminal\nterminal.spawn(['vivado'])\n",
    ],
)
def test_execution_scanner_detects_pty_process_launches(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "pty_process_launch.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(kind == "pty.spawn" for (*_, kind) in records), records


@pytest.mark.parametrize(
    ("source", "execution_kind", "process_kind"),
    [
        (
            "eval(\"__import__('subprocess').run([])\")\n",
            "dynamic_code.eval",
            "subprocess.run",
        ),
        (
            "exec(\"import os; os.system('vivado')\")\n",
            "dynamic_code.exec",
            "os.system",
        ),
        (
            "import builtins\n"
            "builtins.eval(\"__import__('subprocess').check_call([])\")\n",
            "dynamic_code.eval",
            "subprocess.check_call",
        ),
        (
            "from builtins import exec as execute\n"
            "execute(\"import pty; pty.spawn(['vivado'])\")\n",
            "dynamic_code.exec",
            "pty.spawn",
        ),
        (
            "import builtins, operator\n"
            "operator.getitem(builtins.__dict__, 'eval')"
            "(\"__import__('subprocess').run([])\")\n",
            "dynamic_code.eval",
            "subprocess.run",
        ),
    ],
)
def test_execution_scanner_inspects_literal_eval_and_exec_payloads(
    tmp_path: Path,
    source: str,
    execution_kind: str,
    process_kind: str,
) -> None:
    path = tmp_path / "literal_dynamic_code.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(kind == execution_kind for (*_, kind) in records), records
    assert any(kind == process_kind for (*_, kind) in records), records


@pytest.mark.parametrize(
    "source",
    [
        (
            "def safe(eval):\n"
            "    eval(\"__import__('subprocess').run([])\")\n"
        ),
        (
            "from fake import exec as execute\n"
            "execute(\"import os; os.system('vivado')\")\n"
        ),
        (
            "import builtins\n"
            "def safe(builtins):\n"
            "    builtins.exec(\"import os; os.system('vivado')\")\n"
        ),
    ],
)
def test_execution_scanner_respects_dynamic_code_builtin_shadowing(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "shadowed_dynamic_code.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert not any(
        kind.startswith(("dynamic_code.", "subprocess.", "os.", "pty."))
        for (*_, kind) in records
    ), records


@pytest.mark.parametrize(
    "source",
    [
        (
            "class FakeModule:\n"
            "    def run(self, argv):\n"
            "        return argv\n"
            "def lookup(name):\n"
            "    return FakeModule()\n"
            "def safe(argv):\n"
            "    lookup('subprocess').run(argv)\n"
        ),
        (
            "def safe(__import__, argv):\n"
            "    __import__(name='subprocess').run(argv)\n"
        ),
        (
            "def safe(importlib, argv):\n"
            "    importlib.import_module('subprocess').run(argv)\n"
        ),
        (
            "def safe(builtins, argv):\n"
            "    builtins.__import__('subprocess').run(argv)\n"
        ),
        (
            "def safe(builtins, argv):\n"
            "    builtins.__dict__['__import__']('subprocess').run(argv)\n"
        ),
        (
            "def safe(__builtins__, argv):\n"
            "    __builtins__['__import__']('subprocess').run(argv)\n"
        ),
        (
            "import builtins, operator\n"
            "def safe(operator, argv):\n"
            "    operator.getitem(builtins.__dict__, '__import__')('subprocess').run(argv)\n"
        ),
    ],
)
def test_execution_scanner_does_not_treat_arbitrary_resolvers_as_imports(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "safe_lookup.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert not any(kind.startswith("subprocess.") for (*_, kind) in records)


@pytest.mark.parametrize(
    ("source", "expected_kind"),
    [
        (
            "import subprocess\n"
            "subprocess.__dict__['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "vars(subprocess)['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import os\n"
            "os.__dict__['system']('vivado')\n",
            "os.system",
        ),
        (
            "import subprocess as sp\n"
            "sp.__dict__['r' + 'un'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "getattr(subprocess, '__dict__')['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "subprocess.__dict__.get('run')(['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import os\n"
            "vars(os).__getitem__('system')('vivado')\n",
            "os.system",
        ),
        (
            "import subprocess\n"
            "runner = subprocess.__dict__['run']\n"
            "runner(['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "def submit(callback, argv):\n"
            "    callback(argv)\n"
            "submit(subprocess.__dict__['run'], ['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "inspect = vars\n"
            "inspect_again = inspect\n"
            "inspect_again(subprocess)['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import builtins\n"
            "import subprocess\n"
            "builtins.vars(subprocess)['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "from builtins import vars as inspect\n"
            "import subprocess\n"
            "inspect(subprocess)['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import builtins\n"
            "import subprocess\n"
            "inspect = getattr(builtins, 'vars')\n"
            "inspect(subprocess)['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "def submit(callback, argv):\n"
            "    callback(argv)\n"
            "submit(subprocess.__dict__.get('run'), ['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import os\n"
            "def submit(callback, command):\n"
            "    callback(command)\n"
            "submit(vars(os).__getitem__('system'), 'vivado')\n",
            "os.system",
        ),
        (
            "import subprocess\n"
            "subprocess.__dict__.pop('run')\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "__builtins__['vars'](subprocess)['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "class C:\n"
            "    inspect = vars\n"
            "C.inspect(subprocess)['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "class Outer:\n"
            "    class Inner:\n"
            "        inspect = vars\n"
            "Outer.Inner.inspect(subprocess)['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "class C:\n"
            "    inspect = vars\n"
            "Alias = C\n"
            "Alias.inspect(subprocess)['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "class C:\n"
            "    inspect = vars\n"
            "instance = C()\n"
            "instance.inspect(subprocess)['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "class C:\n"
            "    inspect = vars\n"
            "C().inspect(subprocess)['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "class C:\n"
            "    inspect = vars\n"
            "Alias = C\n"
            "instance = Alias()\n"
            "instance.inspect(subprocess)['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "class C:\n"
            "    inspect = vars\n"
            "    def execute(self):\n"
            "        C.inspect(subprocess)['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "class C:\n"
            "    def execute(self):\n"
            "        self.inspect(subprocess)['run'](['vivado'])\n"
            "    inspect = vars\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "class C:\n"
            "    inspect = vars\n"
            "    @classmethod\n"
            "    def execute(cls):\n"
            "        cls.inspect(subprocess)['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "class C:\n"
            "    inspect = vars\n"
            "class D(C):\n"
            "    pass\n"
            "D.inspect(subprocess)['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "class C:\n"
            "    inspect = vars\n"
            "class D(C):\n"
            "    pass\n"
            "instance = D()\n"
            "instance.inspect(subprocess)['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "subprocess.__dict__.copy()['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "{**subprocess.__dict__}['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "(subprocess.__dict__ | {})['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "({} | subprocess.__dict__)['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "dict(subprocess.__dict__)['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "{**{}, **subprocess.__dict__, 'sentinel': None}['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "vars(subprocess).copy().get('run')(['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "class Base:\n"
            "    inspect = vars\n"
            "class D(Base):\n"
            "    def execute(self):\n"
            "        super().inspect(subprocess)['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "class Base:\n"
            "    inspect = vars\n"
            "class D(Base):\n"
            "    def execute(self):\n"
            "        super(D, self).inspect(subprocess)['run'](['vivado'])\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "class C:\n"
            "    def execute(self):\n"
            "        self.__class__.inspect(subprocess)['run'](['vivado'])\n"
            "    inspect = vars\n",
            "subprocess.run",
        ),
        (
            "import subprocess\n"
            "class C:\n"
            "    inspect = vars\n"
            "    def execute(self):\n"
            "        type(self).inspect(subprocess)['run'](['vivado'])\n",
            "subprocess.run",
        ),
    ],
)
def test_execution_scanner_detects_static_process_module_dictionary_calls(
    tmp_path: Path,
    source: str,
    expected_kind: str,
) -> None:
    path = tmp_path / "module_dictionary_call.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(kind == expected_kind for (*_, kind) in records)


@pytest.mark.parametrize(
    "source",
    [
        (
            "import subprocess\n"
            "def safe(subprocess):\n"
            "    subprocess.__dict__['run']([])\n"
        ),
        (
            "import subprocess\n"
            "def safe(vars):\n"
            "    vars(subprocess)['run']([])\n"
        ),
        (
            "import subprocess\n"
            "vars = lambda value: {'run': lambda argv: argv}\n"
            "vars(subprocess)['run']([])\n"
        ),
        (
            "class Fake:\n"
            "    pass\n"
            "fake = Fake()\n"
            "fake.__dict__['run'] = lambda argv: argv\n"
            "fake.__dict__['run']([])\n"
        ),
        (
            "class Fake:\n"
            "    pass\n"
            "fake = Fake()\n"
            "vars(fake)['run']([])\n"
        ),
        (
            "import subprocess\n"
            "def lookup(value):\n"
            "    return {'run': lambda argv: argv}\n"
            "lookup(subprocess)['run']([])\n"
        ),
        (
            "import subprocess\n"
            "subprocess.__dict__['DEVNULL']\n"
        ),
        (
            "import subprocess\n"
            "sentinel = subprocess.__dict__['DEVNULL']\n"
        ),
        (
            "import subprocess\n"
            "sentinel = subprocess.__dict__.get('DEVNULL')\n"
        ),
        (
            "import subprocess\n"
            "vars()\n"
        ),
        (
            "import builtins\n"
            "import subprocess\n"
            "def safe(builtins):\n"
            "    builtins.vars(subprocess)['run']([])\n"
        ),
        (
            "import subprocess\n"
            "def safe(__builtins__):\n"
            "    __builtins__['vars'](subprocess)['run']([])\n"
        ),
        (
            "import subprocess\n"
            "class C:\n"
            "    inspect = vars\n"
            "C.inspect = lambda value: {'run': lambda argv: argv}\n"
            "C.inspect(subprocess)['run']([])\n"
        ),
        (
            "import subprocess\n"
            "class C:\n"
            "    inspect = vars\n"
            "Alias = C\n"
            "Alias = object()\n"
            "Alias.inspect(subprocess)['run']([])\n"
        ),
        (
            "import subprocess\n"
            "class Outer:\n"
            "    class Inner:\n"
            "        inspect = vars\n"
            "    Inner = object()\n"
            "Outer.Inner.inspect(subprocess)['run']([])\n"
        ),
        (
            "import subprocess\n"
            "class C:\n"
            "    inspect = vars\n"
            "instance = C()\n"
            "instance.inspect = lambda value: {'run': lambda argv: argv}\n"
            "instance.inspect(subprocess)['run']([])\n"
        ),
        (
            "import subprocess\n"
            "class C:\n"
            "    inspect = vars\n"
            "class Other:\n"
            "    pass\n"
            "instance = C()\n"
            "instance = Other()\n"
            "instance.inspect(subprocess)['run']([])\n"
        ),
        (
            "import subprocess\n"
            "class C:\n"
            "    inspect = vars\n"
            "class D(C):\n"
            "    inspect = lambda value: {'run': lambda argv: argv}\n"
            "D.inspect(subprocess)['run']([])\n"
        ),
        (
            "import subprocess\n"
            "class C:\n"
            "    inspect = vars\n"
            "    @staticmethod\n"
            "    def safe(self):\n"
            "        self.inspect(subprocess)['run']([])\n"
        ),
        (
            "import subprocess\n"
            "class C:\n"
            "    inspect = vars\n"
            "    def outer(self):\n"
            "        def inner(self):\n"
            "            self.inspect(subprocess)['run']([])\n"
        ),
        (
            "import subprocess\n"
            "class Fake:\n"
            "    pass\n"
            "fake = Fake()\n"
            "fake.__dict__.copy()['run'] = lambda argv: argv\n"
            "fake.__dict__.copy()['run']([])\n"
        ),
        (
            "import subprocess\n"
            "subprocess.__dict__.copy('ignored')['run']([])\n"
        ),
        (
            "import subprocess\n"
            "class SafeBase:\n"
            "    inspect = lambda value: {'run': lambda argv: argv}\n"
            "class D(SafeBase):\n"
            "    def safe(self):\n"
            "        super().inspect(subprocess)['run']([])\n"
        ),
        (
            "import subprocess\n"
            "class Base:\n"
            "    inspect = vars\n"
            "class D(Base):\n"
            "    def safe(self, super):\n"
            "        super().inspect(subprocess)['run']([])\n"
        ),
        (
            "import subprocess\n"
            "class C:\n"
            "    inspect = vars\n"
            "    def safe(self, type):\n"
            "        type(self).inspect(subprocess)['run']([])\n"
        ),
        (
            "import subprocess\n"
            "class Other:\n"
            "    pass\n"
            "class C:\n"
            "    inspect = vars\n"
            "    def safe(self):\n"
            "        self = Other()\n"
            "        type(self).inspect(subprocess)['run']([])\n"
            "        self.__class__.inspect(subprocess)['run']([])\n"
        ),
    ],
)
def test_execution_scanner_rejects_noncanonical_module_dictionary_lookups(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "safe_module_dictionary_lookup.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert not any(
        kind.startswith(("subprocess.", "os.", "asyncio.", "pty."))
        for (*_, kind) in records
    )


@pytest.mark.parametrize(
    "source",
    [
        "class C:\n    inspect = vars\n",
        "if flag:\n    inspect = vars\n",
        (
            "def configure():\n"
            "    global inspect\n"
            "    inspect = vars\n"
        ),
    ],
)
def test_execution_scanner_does_not_record_vars_alias_exposure_without_execution(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "vars_alias_only.py"
    path.write_text(source, encoding="utf-8")

    assert _direct_execution_records([path]) == Counter()


@pytest.mark.parametrize(
    ("source", "expected_kind"),
    [
        (
            "import subprocess, sys\n"
            "def bypass(argv):\n"
            "    sys.modules['subprocess'].run(argv)\n",
            "subprocess.run",
        ),
        (
            "def bypass(argv):\n"
            "    import sys as runtime\n"
            "    runtime.modules['os'].spawnv(0, argv[0], argv)\n",
            "os.spawnv",
        ),
        (
            "def bypass(argv):\n"
            "    from sys import modules as loaded\n"
            "    loaded.get('asyncio').create_subprocess_exec(*argv)\n",
            "asyncio.create_subprocess_exec",
        ),
        (
            "import sys\n"
            "registry = sys.modules\n"
            "module = registry['subprocess']\n"
            "runner = module.check_call\n"
            "def bypass(argv):\n"
            "    runner(argv)\n",
            "subprocess.check_call",
        ),
        (
            "import sys\n"
            "def bypass(argv):\n"
            "    sys.modules.__getitem__('subprocess').Popen(argv)\n",
            "subprocess.Popen",
        ),
        (
            "import sys\n"
            "def bypass():\n"
            "    sys.modules.pop('os').system('vivado')\n",
            "os.system",
        ),
        (
            "import sys\n"
            "async def bypass():\n"
            "    await sys.modules.setdefault('asyncio').create_subprocess_shell('vivado')\n",
            "asyncio.create_subprocess_shell",
        ),
        (
            "import sys\n"
            "def bypass():\n"
            "    sys.modules.copy()['subprocess'].getoutput('vivado')\n",
            "subprocess.getoutput",
        ),
        (
            "import sys\n"
            "def bypass(argv):\n"
            "    getattr(sys, 'modules')['subprocess'].call(argv)\n",
            "subprocess.call",
        ),
        (
            "import sys\n"
            "def bypass(argv):\n"
            "    sys.__dict__['modules']['subprocess'].check_output(argv)\n",
            "subprocess.check_output",
        ),
        (
            "import sys\n"
            "def bypass():\n"
            "    getattr(sys, '__dict__')['modules']['os'].popen('vivado')\n",
            "os.popen",
        ),
        (
            "import sys\n"
            "def bypass(argv):\n"
            "    vars(sys)['modules']['subprocess'].run(argv)\n",
            "subprocess.run",
        ),
        (
            "import sys\n"
            "def bypass(argv):\n"
            "    sys.modules['sub' + 'process'].run(argv)\n",
            "subprocess.run",
        ),
        (
            "import sys\n"
            "def bypass(argv, registry=sys.modules):\n"
            "    registry['subprocess'].run(argv)\n",
            "subprocess.run",
        ),
        (
            "import sys\n"
            "def bypass(executor, argv):\n"
            "    executor.submit(sys.modules['subprocess'].run, argv)\n",
            "subprocess.run",
        ),
        (
            "import sys\n"
            "def bypass(argv):\n"
            "    (registry := sys.modules)['subprocess'].run(argv)\n",
            "subprocess.run",
        ),
        (
            "import sys\n"
            "def bypass(argv):\n"
            "    registry: dict = sys.modules\n"
            "    registry['subprocess'].run(argv)\n",
            "subprocess.run",
        ),
        (
            "import sys\n"
            "def configure():\n"
            "    global registry\n"
            "    registry = sys.modules\n"
            "def bypass(argv):\n"
            "    registry['subprocess'].run(argv)\n",
            "subprocess.run",
        ),
        (
            "import sys\n"
            "def outer():\n"
            "    registry = {}\n"
            "    def configure():\n"
            "        nonlocal registry\n"
            "        registry = sys.modules\n"
            "    def bypass(argv):\n"
            "        registry['subprocess'].run(argv)\n",
            "subprocess.run",
        ),
    ],
)
def test_execution_scanner_detects_sys_modules_process_paths(
    tmp_path: Path,
    source: str,
    expected_kind: str,
) -> None:
    path = tmp_path / "sys_modules_bypass.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(kind == expected_kind for (*_, kind) in records), records


@pytest.mark.parametrize(
    "source",
    [
        (
            "import sys\n"
            "def safe(sys, argv):\n"
            "    sys.modules['subprocess'].run(argv)\n"
        ),
        (
            "from sys import modules\n"
            "def safe(modules, argv):\n"
            "    modules['subprocess'].run(argv)\n"
        ),
        (
            "import sys\n"
            "sys = object()\n"
            "sys.modules['subprocess'].run([])\n"
        ),
        (
            "import sys\n"
            "registry = sys.modules\n"
            "registry = {'subprocess': object()}\n"
            "registry['subprocess'].run([])\n"
        ),
        (
            "class Fake:\n"
            "    modules = {'subprocess': object()}\n"
            "fake = Fake()\n"
            "fake.modules['subprocess'].run([])\n"
        ),
        (
            "from fake import modules\n"
            "modules['subprocess'].run([])\n"
        ),
        (
            "import sys\n"
            "name = 'subprocess'\n"
            "sys.modules[name].run([])\n"
        ),
        (
            "import sys\n"
            "sys.modules['json'].loads('{}')\n"
        ),
        (
            "import sys\n"
            "tuple(sys.modules.keys())\n"
        ),
        (
            "import sys\n"
            "sys.modules.copy('ignored')['subprocess'].run([])\n"
        ),
        (
            "import sys\n"
            "def safe(vars):\n"
            "    vars(sys)['modules']['subprocess'].run([])\n"
        ),
        (
            "class Fake:\n"
            "    pass\n"
            "fake = Fake()\n"
            "fake.__dict__['modules'] = {'subprocess': object()}\n"
            "fake.__dict__['modules']['subprocess'].run([])\n"
        ),
    ],
)
def test_execution_scanner_rejects_noncanonical_sys_modules_lookups(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "safe_sys_modules_lookup.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert not any(
        kind.startswith(("subprocess.", "os.", "asyncio.", "pty."))
        for (*_, kind) in records
    ), records


def test_execution_scanner_does_not_record_sys_modules_registry_alias_alone(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sys_modules_registry_alias_only.py"
    path.write_text(
        "import sys\n"
        "registry = sys.modules\n"
        "registry_copy = registry.copy()\n",
        encoding="utf-8",
    )

    assert _direct_execution_records([path]) == Counter()


@pytest.mark.parametrize(
    ("source", "expected_kind"),
    [
        (
            "import subprocess\n"
            "subprocess.__getattribute__('run')([])\n",
            "subprocess.run",
        ),
        (
            "import os as runtime_os\n"
            "runtime_os.__getattribute__('system')('vivado')\n",
            "os.system",
        ),
        (
            "import asyncio\n"
            "asyncio.__getattribute__('create_subprocess_exec')('vivado')\n",
            "asyncio.create_subprocess_exec",
        ),
        (
            "def bypass(session):\n"
            "    session.__getattribute__('run_tcl')('version')\n",
            "run_tcl",
        ),
        (
            "import subprocess\n"
            "object.__getattribute__(subprocess, 'check_call')([])\n",
            "subprocess.check_call",
        ),
        (
            "import subprocess\n"
            "runner = subprocess.__getattribute__('run')\n"
            "runner([])\n",
            "subprocess.run",
        ),
        (
            "import sys\n"
            "sys.modules['subprocess'].__getattribute__('getoutput')('vivado')\n",
            "subprocess.getoutput",
        ),
        (
            "import os\n"
            "name = 'system'\n"
            "os.__getattribute__(name)('vivado')\n",
            "os.*",
        ),
        (
            "import subprocess\n"
            "name = 'run'\n"
            "object.__getattribute__(subprocess, name)([])\n",
            "subprocess.*",
        ),
    ],
)
def test_execution_scanner_detects_explicit_getattribute_execution_lookups(
    tmp_path: Path,
    source: str,
    expected_kind: str,
) -> None:
    path = tmp_path / "getattribute_bypass.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(kind == expected_kind for (*_, kind) in records), records


@pytest.mark.parametrize(
    "source",
    [
        (
            "import subprocess\n"
            "subprocess.__getattribute__('PIPE')\n"
        ),
        (
            "class Fake:\n"
            "    pass\n"
            "fake = Fake()\n"
            "fake.__getattribute__('run')([])\n"
        ),
        (
            "import subprocess\n"
            "subprocess.__getattribute__('run', None)([])\n"
        ),
        (
            "import subprocess\n"
            "subprocess.__getattribute__(name='run')([])\n"
        ),
        (
            "class Fake:\n"
            "    pass\n"
            "fake = Fake()\n"
            "object.__getattribute__(fake, 'run')([])\n"
        ),
    ],
)
def test_execution_scanner_rejects_noncanonical_getattribute_lookups(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "safe_getattribute_lookup.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert not any(
        kind in {
            "run_tcl",
            "run_tcl_alias",
            "run_tcl_dynamic",
        }
        or kind.startswith(("subprocess.", "os.", "asyncio.", "pty."))
        for (*_, kind) in records
    ), records


@pytest.mark.parametrize(
    ("source", "expected_kind"),
    [
        (
            "import operator, subprocess\n"
            "operator.methodcaller('run', [])(subprocess)\n",
            "subprocess.run",
        ),
        (
            "import operator, subprocess\n"
            "operator.attrgetter('check_call')(subprocess)([])\n",
            "subprocess.check_call",
        ),
        (
            "import operator as op\n"
            "import os\n"
            "op.methodcaller('system', 'vivado')(os)\n",
            "os.system",
        ),
        (
            "from operator import methodcaller as call_method\n"
            "import subprocess\n"
            "call_method('getoutput', 'vivado')(subprocess)\n",
            "subprocess.getoutput",
        ),
        (
            "import operator, asyncio\n"
            "operator.attrgetter('create_subprocess_exec')(asyncio)('vivado')\n",
            "asyncio.create_subprocess_exec",
        ),
        (
            "import operator\n"
            "def bypass(session):\n"
            "    operator.methodcaller('run_tcl', 'version')(session)\n",
            "run_tcl",
        ),
        (
            "import operator, sys\n"
            "operator.methodcaller('run', [])(sys.modules['subprocess'])\n",
            "subprocess.run",
        ),
        (
            "import operator, subprocess\n"
            "operator.getitem(subprocess.__dict__, 'run')([])\n",
            "subprocess.run",
        ),
        (
            "from operator import getitem as lookup\n"
            "import subprocess\n"
            "lookup(vars(subprocess), 'check_output')([])\n",
            "subprocess.check_output",
        ),
        (
            "import operator, subprocess\n"
            "operator.itemgetter('check_call')(subprocess.__dict__)([])\n",
            "subprocess.check_call",
        ),
    ],
)
def test_execution_scanner_detects_operator_execution_transforms(
    tmp_path: Path,
    source: str,
    expected_kind: str,
) -> None:
    path = tmp_path / "operator_bypass.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert any(kind == expected_kind for (*_, kind) in records), records


@pytest.mark.parametrize(
    "source",
    [
        (
            "import operator, subprocess\n"
            "def safe(operator):\n"
            "    operator.methodcaller('run', [])(subprocess)\n"
        ),
        (
            "import operator, subprocess\n"
            "operator = object()\n"
            "operator.methodcaller('run', [])(subprocess)\n"
        ),
        (
            "import operator, subprocess\n"
            "name = 'run'\n"
            "operator.methodcaller(name, [])(subprocess)\n"
        ),
        (
            "import operator\n"
            "class Fake:\n"
            "    pass\n"
            "operator.methodcaller('run', [])(Fake())\n"
        ),
        (
            "import operator, subprocess\n"
            "operator.attrgetter('PIPE')(subprocess)\n"
        ),
        (
            "from fake import methodcaller\n"
            "import subprocess\n"
            "methodcaller('run', [])(subprocess)\n"
        ),
        (
            "import operator, subprocess\n"
            "operator.attrgetter('run', 'call')(subprocess)\n"
        ),
        (
            "import operator, subprocess\n"
            "name = 'run'\n"
            "operator.getitem(subprocess.__dict__, name)([])\n"
        ),
        (
            "from fake import getitem\n"
            "import subprocess\n"
            "getitem(subprocess.__dict__, 'run')([])\n"
        ),
    ],
)
def test_execution_scanner_rejects_noncanonical_operator_transforms(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "safe_operator_transform.py"
    path.write_text(source, encoding="utf-8")

    records = _direct_execution_records([path])

    assert not any(
        kind in {"run_tcl", "run_tcl_alias", "run_tcl_dynamic"}
        or kind.startswith(("subprocess.", "os.", "asyncio.", "pty."))
        for (*_, kind) in records
    ), records
