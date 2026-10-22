# Windows first-use validation

The GitHub URL install in FLUJO must preserve the published MCP configuration:
`uvx mcp-virtual-computer`, including its environment values. The repository's
`package.json` builds the dashboard; it is not the Python server's installer.
FLUJO's headless GitHub install should prefer the pinned repository's `server.json`
and show the actual published-package command in its consent preview.

`COMPUTER_ID` is optional after this change. An omitted value selects the persistent
computer `agent-workstation`. Set a different ID for a separate computer; an
explicitly empty or invalid value remains an error. For the currently published
0.2.11 package, explicitly set `COMPUTER_ID=agent-workstation`.

## Why a standard hosted runner is insufficient

GitHub's standard x64 Windows runners are Windows Server images. Docker Desktop
does not support Windows Server. GitHub also does not officially support nested
virtualization, and hosted Windows runners run as administrators with UAC disabled.
A successful hosted-runner script test therefore cannot prove the ordinary-user,
UAC, WSL, reboot, and Docker Desktop experience on a fresh PC.

Use hosted runners for mocked Windows logic and package/manifest checks at release
pre-flight. Use a disposable Windows 11 client VM or spare physical PC for real
installation. GitHub Actions can control it as a **self-hosted** runner. No new
remote workflow is needed during routine development.

Sources: [GitHub runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners),
[nested virtualization limitations](https://docs.github.com/en/actions/concepts/runners/github-hosted-runners),
[Docker Windows requirements](https://docs.docker.com/desktop/setup/install/windows-install/),
[WSL elevation requirements](https://docs.docker.com/desktop/setup/install/windows-permission-requirements/).

## Release pre-flight on Windows 11

Use an owned disposable VM, at least 8 GB RAM, sufficient free disk for the desktop
image, supported Windows updates, and nested virtualization exposed by its host.
Do not repurpose an existing business VM. Use a normal interactive user session;
a runner installed as a Windows service cannot exercise the UAC flow. Keep UAC
enabled. Restrict any self-hosted workflow to manual trusted release candidates,
never arbitrary fork pull requests.

1. Take a checkpoint with Windows, WinGet and the FLUJO prerequisites installed,
   but no Docker Desktop or WSL. Record Windows build, FLUJO revision, MCP package
   version/hash, WinGet version, and whether virtualization is enabled.
2. Install the FLUJO candidate. Add
   `https://github.com/flujo-app/mcp-virtual-computer` through the GitHub UI.
   Confirm the detected command is `uvx`, no npm install/build steps are requested,
   and the documented environment values are retained. For an unpublished MCP
   candidate, use its built wheel explicitly; do not mistake the old PyPI version
   for the candidate.
3. Also connect the candidate with `COMPUTER_ID` absent. Confirm initialization and
   tool listing succeed before Docker exists. First use (`computer_ui`, then
   `look_at_screen`) must report actual prerequisite/install/start progress.
4. Accept the native WSL administrator prompt. If Windows requires a reboot, the
   run is **pending**, not passed. Reboot manually, reopen FLUJO, and retry. Setup
   must not force a restart or keep downloading Docker while WSL is unavailable.
5. Require `docker info` to identify a Linux engine, a real Xfce screenshot from
   `look_at_screen`, and `terminal_execute` successfully creating/reading a file
   inside the computer. Reconnect and verify that file persists. A successful
   installer exit or MCP tool listing alone is not acceptance.
6. Restore the clean checkpoint. Repeat with UAC cancelled, firmware/nested
   virtualization disabled, and with old WSL requiring an update. Each must show
   a useful recovery action; after repair, retry must reach the same success gate.
7. Separately check existing Docker Desktop stopped/running, and an all-users
   Hyper-V installation. Existing working engines must not trigger reinstallation
   or unnecessary WSL changes.
8. Keep logs/screenshots and exact revisions with the release evidence. Restore
   the disposable checkpoint after completion through the authorized cleanup
   tooling. Do not prune another user's Docker images or volumes.

## Diagnosing the reported installer failure

The screenshot shows WinGet exit `2316632070` (`0x8A150006`) and Docker's installer
exit `1`, after a verified download. Those codes do not identify the underlying
Docker failure. The new error includes current Docker installer log tails and
correctly decodes WSL output, including UTF-16LE on Windows.

For the existing installation, run `scripts/windows-first-use-diagnostics.ps1`
in PowerShell on the affected PC. It is read-only and prints Windows/WSL state and
the recent Docker installer errors. Check the output before sharing it; log paths
can include the Windows user name. It does not install, restart, or upload anything.
