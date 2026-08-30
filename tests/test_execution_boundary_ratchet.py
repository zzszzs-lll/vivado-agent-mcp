from __future__ import annotations

import ast
import hashlib
import json
from collections import Counter
from collections.abc import Sequence
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
OS_PROCESS_CALL_NAMES = {"system", "popen", "startfile"}
PTY_PROCESS_CALL_NAMES = {"spawn"}
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
        qualified_vars_alias_stack: list[set[str]] = [set()]
        qualified_vars_exact_shadow_stack: list[set[str]] = [set()]
        qualified_vars_prefix_shadow_stack: list[set[str]] = [set()]
        receiver_dependent_vars_stack: list[dict[str, set[str]]] = [{}]
        alias_scope_kind_stack = ["module"]
        class_vars_export_stack: list[set[str]] = []
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
                    module_kind = _imported_process_module_kind(alias.name)
                    if module_kind:
                        module_aliases[module_kind].add(alias.asname or module_kind)
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
                    if node.module == "builtins" and alias.name in {"eval", "exec"}:
                        function_aliases[alias.asname or alias.name] = (
                            f"dynamic_code.{alias.name}"
                        )
                    if node.module == "asyncio" and alias.name == "subprocess":
                        module_aliases["asyncio"].add(alias.asname or alias.name)
                        continue
                    module_kind = _imported_process_module_kind(node.module)
                    if module_kind:
                        function_aliases[alias.asname or alias.name] = (
                            f"{module_kind}.{alias.name}"
                        )

        class Visitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self._annotation_call_only_depth = 0
                self._conditional_depth = 0
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
                        visible.pop(name, None)
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
                    for index, shadowed in enumerate(module_shadow_stack)
                    if self._bare_name_scope_is_visible(
                        index,
                        len(module_shadow_stack),
                    )
                )

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
                if attribute is None:
                    return None
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
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
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
                    isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
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
                owner_node, attribute = _static_attribute_lookup(node)
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
                    owner_node, callable_name = _static_attribute_lookup(node)
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
                return self._registered_process_callable_kind(
                    node
                ) or _callable_reference_kind(
                    node,
                    module_aliases,
                    {},
                    callable_aliases,
                    self._vars_aliases(),
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

            def _shadow_name(
                self,
                name: str,
                *,
                preserve_conditional_vars: bool = True,
            ) -> None:
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
                preserve_operator_transform = (
                    self._operator_transform_aliases().get(name)
                    if preserve_conditional_vars and self._conditional_depth > 0
                    else None
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
                callable_alias_stack[-1].pop(name, None)
                module_shadow_stack[-1].add(name)
                module_alias_stack[-1].pop(name, None)
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
                if preserve_sys_alias:
                    sys_alias_stack[-1].add(name)
                if preserve_registry_alias:
                    module_registry_alias_stack[-1].add(name)
                if preserve_operator_alias:
                    operator_alias_stack[-1].add(name)
                if preserve_operator_transform is not None:
                    operator_transform_alias_stack[-1][name] = (
                        preserve_operator_transform
                    )
                qualified_vars_alias_stack[-1].update(
                    preserve_qualified_aliases
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
                operator_alias: bool = False,
                operator_transform_kind: str | None = None,
            ) -> None:
                self._shadow_name(name)
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
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
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
                        self._observed_module_reference_kind(
                            default,
                            current_module_aliases,
                        ),
                        self._observed_callable_reference_kind(
                            default,
                            current_module_aliases,
                            current_callable_aliases,
                        ),
                        self._dynamic_reference_kind(default),
                        self._vars_reference(default),
                        self._sys_reference(default),
                        self._module_registry_reference(default),
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
                    ],
                    ...,
                ],
            ) -> None:
                self._push_alias_scope("function")
                for name in self._parameter_names(arguments):
                    self._shadow_name(name, preserve_conditional_vars=False)
                for (
                    name,
                    module_kind,
                    call_kind,
                    dynamic_kind,
                    vars_alias,
                    sys_alias,
                    module_registry_alias,
                ) in bindings:
                    self._bind_aliases(
                        name,
                        module_kind=module_kind,
                        call_kind=call_kind,
                        dynamic_kind=dynamic_kind,
                        vars_alias=vars_alias,
                        sys_alias=sys_alias,
                        module_registry_alias=module_registry_alias,
                    )

            @staticmethod
            def _push_alias_scope(kind: str) -> None:
                callable_alias_stack.append({})
                callable_shadow_stack.append(set())
                module_alias_stack.append({})
                module_shadow_stack.append(set())
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
            ) -> None:
                if bound_name not in (
                    global_name_stack[-1] | nonlocal_name_stack[-1]
                ):
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
            ) -> None:
                function_stack.append(node.name)
                self._push_function_scope(node.args, ())
                parameter_names = set(self._parameter_names(node.args))
                class_root = class_reference.partition(".")[0]
                if class_root not in parameter_names:
                    qualified_vars_alias_stack[-1].update(
                        f"{class_reference}.{export}"
                        for export in class_exports
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
                self._push_function_scope(node.args, bindings)
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
                self._push_function_scope(node.args, bindings)
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
                class_scope_depth_stack.append(len(module_alias_stack) - 1)
                class_vars_export_stack.append(inherited_class_exports)
                class_method_stack.append([])
                class_reference_stack.append(class_reference)
                class_exports: set[str] = set()
                try:
                    for statement in node.body:
                        self.visit(statement)
                    class_exports = set(class_vars_export_stack[-1])
                    for method in tuple(class_method_stack[-1]):
                        self._visit_class_method_body(
                            method,
                            class_reference=class_reference,
                            class_exports=class_exports,
                            super_exports=inherited_class_exports,
                        )
                finally:
                    class_reference_stack.pop()
                    class_method_stack.pop()
                    class_vars_export_stack.pop()
                    class_scope_depth_stack.pop()
                    self._pop_alias_scope()
                    function_stack.pop()
                self._shadow_name(node.name)
                qualified_exports = {
                    f"{node.name}.{export}" for export in class_exports
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
                else:
                    qualified_vars_alias_stack[-1].update(qualified_exports)

            def visit_Import(self, node: ast.Import) -> None:
                for alias in node.names:
                    bound_name = alias.asname or alias.name.split(".", maxsplit=1)[0]
                    module_kind = _imported_process_module_kind(alias.name)
                    sys_alias = alias.name == "sys"
                    operator_alias = alias.name == "operator"
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
                        continue
                    bound_name = alias.asname or alias.name
                    imported_kind = (
                        f"{module_kind}.{alias.name}"
                        if module_kind is not None
                        else None
                    )
                    bound_module_kind = (
                        "asyncio"
                        if node.module == "asyncio" and alias.name == "subprocess"
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
                    self._record_conditional_alias_exposure(
                        node,
                        bound_name=bound_name,
                        module_kind=bound_module_kind,
                        call_kind=bound_call_kind,
                        dynamic_kind=dynamic_kind,
                        vars_alias=vars_alias,
                        module_registry_alias=module_registry_alias,
                    )
                    self._bind_aliases(
                        bound_name,
                        module_kind=bound_module_kind,
                        call_kind=bound_call_kind,
                        dynamic_kind=dynamic_kind,
                        vars_alias=vars_alias,
                        module_registry_alias=module_registry_alias,
                        operator_transform_kind=operator_transform_kind,
                    )
                    self._record_class_exposure(
                        node,
                        bound_names=(bound_name,),
                        module_kind=bound_module_kind,
                        call_kind=bound_call_kind,
                        dynamic_kind=dynamic_kind,
                        vars_alias=vars_alias,
                        module_registry_alias=module_registry_alias,
                    )
                    self._record_cross_scope_exposure(
                        node,
                        bound_name=bound_name,
                        module_kind=bound_module_kind,
                        call_kind=bound_call_kind,
                        dynamic_kind=dynamic_kind,
                        vars_alias=vars_alias,
                        module_registry_alias=module_registry_alias,
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

            def visit_For(self, node: ast.For) -> None:
                self._record_binding_source_exposure(node, node.iter)
                self.visit(node.iter)
                self.visit(node.target)
                self._visit_conditionally([*node.body, *node.orelse])

            def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
                self._record_binding_source_exposure(node, node.iter)
                self.visit(node.iter)
                self.visit(node.target)
                self._visit_conditionally([*node.body, *node.orelse])

            def visit_While(self, node: ast.While) -> None:
                self.visit(node.test)
                self._visit_conditionally([*node.body, *node.orelse])

            def visit_Try(self, node: ast.Try) -> None:
                conditional_nodes: list[ast.AST] = [*node.body, *node.orelse, *node.finalbody]
                for handler in node.handlers:
                    if handler.type is not None:
                        conditional_nodes.append(handler.type)
                    conditional_nodes.extend(handler.body)
                self._visit_conditionally(conditional_nodes)

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
                    self._record_binding_source_exposure(node, item.context_expr)
                    self.visit(item.context_expr)
                    if item.optional_vars is not None:
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
                module_kind = self._observed_module_reference_kind(
                    node.value,
                    current_module_aliases,
                )
                call_kind = self._observed_callable_reference_kind(
                    node.value,
                    current_module_aliases,
                    aliases,
                )
                dynamic_kind = self._dynamic_reference_kind(node.value)
                vars_alias = self._vars_reference(node.value)
                sys_alias = self._sys_reference(node.value)
                module_registry_alias = self._module_registry_reference(node.value)
                qualified_suffixes = self._qualified_vars_descendants(
                    self._qualified_vars_source_reference(node.value)
                )
                self._record_binding_source_exposure(node, node.value)
                for target in node.targets:
                    for name in self._bound_names(target):
                        self._record_conditional_alias_exposure(
                            node,
                            bound_name=name,
                            module_kind=(
                                module_kind if isinstance(target, ast.Name) else None
                            ),
                            call_kind=(
                                call_kind if isinstance(target, ast.Name) else None
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
                )
                for target in node.targets:
                    names = self._bound_names(target)
                    for name in names:
                        self._record_cross_scope_exposure(
                            node,
                            bound_name=name,
                            module_kind=(
                                module_kind if isinstance(target, ast.Name) else None
                            ),
                            call_kind=(
                                call_kind if isinstance(target, ast.Name) else None
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
                        )
                        self._bind_aliases(
                            name,
                            module_kind=(
                                module_kind if isinstance(target, ast.Name) else None
                            ),
                            call_kind=(
                                call_kind if isinstance(target, ast.Name) else None
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
                        )
                        if isinstance(target, ast.Name):
                            qualified_vars_alias_stack[-1].update(
                                f"{name}{suffix}" for suffix in qualified_suffixes
                            )
                    target_reference = _dotted_name(target)
                    if target_reference is not None and not isinstance(target, ast.Name):
                        self._bind_qualified_vars_target(
                            target_reference,
                            vars_alias=vars_alias,
                        )

            def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
                aliases = self._callable_aliases()
                current_module_aliases = self._module_aliases()
                module_kind = self._observed_module_reference_kind(
                    node.value,
                    current_module_aliases,
                )
                call_kind = self._observed_callable_reference_kind(
                    node.value,
                    current_module_aliases,
                    aliases,
                )
                dynamic_kind = self._dynamic_reference_kind(node.value)
                vars_alias = self._vars_reference(node.value)
                sys_alias = self._sys_reference(node.value)
                module_registry_alias = self._module_registry_reference(node.value)
                qualified_suffixes = self._qualified_vars_descendants(
                    self._qualified_vars_source_reference(node.value)
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
                    )
                    if isinstance(node.target, ast.Name):
                        qualified_vars_alias_stack[-1].update(
                            f"{name}{suffix}" for suffix in qualified_suffixes
                        )
                target_reference = _dotted_name(node.target)
                if (
                    node.value is not None
                    and target_reference is not None
                    and not isinstance(node.target, ast.Name)
                ):
                    self._bind_qualified_vars_target(
                        target_reference,
                        vars_alias=vars_alias,
                    )

            def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
                aliases = self._callable_aliases()
                current_module_aliases = self._module_aliases()
                module_kind = self._observed_module_reference_kind(
                    node.value,
                    current_module_aliases,
                )
                call_kind = self._observed_callable_reference_kind(
                    node.value,
                    current_module_aliases,
                    aliases,
                )
                dynamic_kind = self._dynamic_reference_kind(node.value)
                vars_alias = self._vars_reference(node.value)
                sys_alias = self._sys_reference(node.value)
                module_registry_alias = self._module_registry_reference(node.value)
                qualified_suffixes = self._qualified_vars_descendants(
                    self._qualified_vars_source_reference(node.value)
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
                    )
                    self._bind_aliases(
                        node.target.id,
                        module_kind=module_kind,
                        call_kind=call_kind,
                        dynamic_kind=dynamic_kind,
                        vars_alias=vars_alias,
                        sys_alias=sys_alias,
                        module_registry_alias=module_registry_alias,
                    )
                    qualified_vars_alias_stack[-1].update(
                        f"{node.target.id}{suffix}"
                        for suffix in qualified_suffixes
                    )

            def visit_Call(self, node: ast.Call) -> None:
                aliases = self._callable_aliases()
                self._record_literal_code_execution(node)
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
                    self._record_binding_source_exposure(node, node.value)
                    self.visit(node.value)

            def visit_Yield(self, node: ast.Yield) -> None:
                if node.value is not None:
                    self._record_binding_source_exposure(node, node.value)
                    self.visit(node.value)

            def visit_YieldFrom(self, node: ast.YieldFrom) -> None:
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
                if call_kind:
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
                )
                if call_kind:
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
                if call_kind:
                    self._record(node, call_kind)
                self.generic_visit(node)

        Visitor().visit(tree)
    return records


def _direct_call_kind(
    node: ast.Call,
    module_aliases: dict[str, set[str]],
    function_aliases: dict[str, str],
    callable_aliases: dict[str, str],
    vars_references: set[str] | frozenset[str] = DEFAULT_VARS_REFERENCES,
) -> str | None:
    function = node.func
    if isinstance(function, ast.Attribute) and function.attr == "run_tcl":
        return "run_tcl"
    if isinstance(function, (ast.Call, ast.NamedExpr, ast.Subscript)):
        dynamic_kind = _callable_reference_kind(
            function,
            module_aliases,
            function_aliases,
            callable_aliases,
            vars_references,
        )
        if dynamic_kind:
            return dynamic_kind
    if isinstance(function, ast.Name) and function.id in callable_aliases:
        return callable_aliases[function.id]
    if isinstance(function, ast.Name) and function.id in function_aliases:
        imported = function_aliases[function.id]
        if _approved_process_call_kind(imported):
            return imported
    if (
        isinstance(function, ast.Name)
        and function.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
    ):
        attribute = str(node.args[1].value)
        if attribute == "run_tcl":
            return "run_tcl_dynamic"
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
    if isinstance(node, ast.NamedExpr):
        return _callable_reference_kind(
            node.value,
            module_aliases,
            function_aliases,
            callable_aliases,
            vars_references,
        )
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
    owner_node, attribute = _static_attribute_lookup(node)
    if owner_node is not None and attribute is not None:
        if attribute == "run_tcl":
            return "run_tcl_dynamic"
        owner = _dotted_name(owner_node)
        if owner is not None:
            for module_kind, aliases in module_aliases.items():
                if owner not in aliases:
                    continue
                candidate = f"{module_kind}.{attribute}"
                if _approved_process_call_kind(candidate):
                    return candidate
    return None


def _static_attribute_lookup(
    node: ast.AST | None,
) -> tuple[ast.AST | None, str | None]:
    if not isinstance(node, ast.Call) or node.keywords:
        return None, None
    if (
        isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) in {2, 3}
    ):
        return node.args[0], _static_string_value(node.args[1])
    if not isinstance(node.func, ast.Attribute):
        return None, None
    if node.func.attr != "__getattribute__":
        return None, None
    if len(node.args) == 1:
        return node.func.value, _static_string_value(node.args[0])
    if (
        len(node.args) == 2
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "object"
    ):
        return node.args[0], _static_string_value(node.args[1])
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
        return name.startswith("create_subprocess_")
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
            "name = 'run'\n"
            "subprocess.__getattribute__(name)([])\n"
        ),
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
