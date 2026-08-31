Each phase should follow this pattern:

- Read the `specs/implementation_plan.md` for a list of phases
- Choose what the next step of implementation is based on the checkmarks in implementation plan, and confirm that isn’t already done in src. Confirm with user it's the right thing to work on (if they didn't already tell you which phase to do)
- Check if we already have a phase plan for this step, if so can skip reading architecture doc. If not:
   read `specs/architecture/architecture_summary.md` and related docs 
  - Write a plan for this phase in `specs/phase_plans/phase[N].md` pulling out additional details from the spec, into a more detailed implementation plan.
    - Order to build (steps)
    - pointers to key technical specs in architecture docs (don't repeat code/decisions, point to them) 
    - tests to implement for this phase
    - ask me to review and approve plan
- Work without my assistance, continuing until done the phase.
- Build the component, extending the existing project.
- Compile and fix any typing, test, formatting and linting issues using relevant commands (ruff format, ruff check fix). See `CLAUDE.md` for commands. the `uv run ./checks.sh` command should pass without issue, if it has any issues iterate until resolved.
- Self code review your new work for issues.
- Write tests for the the component/step.
- Run tests. Iterate until build and tests pass. See `CLAUDE.md` for commands.
- Run check_all mcp server. Fix any issues, and iterate until it passes. Do not end implementation phased without all checks passing.
- Mark the step as complete in `specs/implementation_plan.md`. Only edit this file to toggle checkboxes, nothing else.
- Stop after a single phase is done, don’t continue until asked.
