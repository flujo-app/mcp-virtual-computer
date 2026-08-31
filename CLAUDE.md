# Kilntainers: Secure Agent Sandboxes

This fork extends Kilntainers into named sandbox computers. `terminal_execute` supports stable computer IDs and temporary/permanent lifecycles; optional provider-neutral management tools and a stable MCP App dashboard manage Docker containers and Fly Machines. Set `ENABLE_LIFECYCLE_TOOLS=true` to expose the `computer_*` tools and dashboard; they are disabled by default.

## Specs

This repository has detailed specs, and follows spec-driven design. Key files include:

- [specs/functional_spec.md] External behavior — the MCP tool interface, server configuration, connection lifecycle, backend behavioral contract, and security model. Not an architecture or implementation document.
- [specs/implementation_plan.md] our ordered implementation plan, including progress tracking
- [specs/phase_instructions.md] A set of instructions to follow when asked to implement a phase of the implmentation plan
- [specs/architecture/architecture_summary.md] A summary of architecture docs covering detailed technical designs. Additional architecture docs live in this path.
- [specs/decisions.md](decisions.md) — design decisions with rationale (source of truth). However these are generally captured in functional_spec and architecture, and does not need to be reviewed.
- [specs/project_overview.md](project_overview.md) — original motivation and vision, but not maintained. Functional spec is generally more complete and up to date. Do not review unless looking for original motivation.
- [specs/spec_queue.md](spec_queue.md) — tracking of items requiring specification

## Commands & Tools

You have access ot a MCP server to running tools like lint, format, types, test. 

Always use the MCP commands to perform these actions, not direct bash calls. Bash calls require approval and will stop our work.
