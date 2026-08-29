## Scope

Describe the Agent or maintainer outcome and the affected MCP workflow.

## Validation

- [ ] Targeted tests pass
- [ ] `python -m pytest` passes
- [ ] `python -m compileall src` passes
- [ ] `git diff --check` passes
- [ ] MCP schema/stdio tests were run when tool contracts changed
- [ ] Distribution smoke was run when packaging or entrypoints changed

## Boundaries

- [ ] No credentials, private HDL, license data, machine-specific paths, or generated Vivado artifacts are included
- [ ] Hardware-related results remain `hardware_validation.status=NOT_VALIDATED` and `validated=false`
- [ ] The change does not use Shell or raw Tcl to bypass MCP safety gates
