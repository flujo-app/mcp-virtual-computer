# Kilntainers - Secure Agent Sanboxes

This is a brand new project, to create isolated sandboxes for LLM agents to do work inside of. 

 - It will be exposed as a MCP server to agents.
 - It will expose a simple but powerful API for running any linux command, like docker exec.
 - The interface is so simple, it could be implemented by different backends: containers, VMs, BusyBox like systems, remote-VMs, etc.
 - It will support multiple backends including docker/podman, Modal, E2B, WASI Sandboxes with linux containers or busyboxes, etc. Which backend is used is a server-startup time option.

## Motivation

Giving an agent a linux container is powerful. Claude code is a good example of how it can do amazing work with a terminal:

 - LLM agents are very good at using bash commands. They can use grep, find, jq, and dozens more -- with complex pipe operations. This makes them very token efficient, only parsing the output.
 - Filesystem as memory has been shown to be the most flexible and SOTA technique for agent memory.

 We want to give every agent it's own ephemerial linux sandbox, without security concerns of letting it call commands on host OS.

## Security Model

The security model is simple: 
 - the sandbox is ephemerial
 - the agent isn't running inside the sandbox, it's calling into the sandbox
 - networking is off by default, and must be manually enabled by MCP startup command

Since we're not running the agent in the sandbox, it doesn't need secrets. Since it's a sandbox, it can't hurt host OS.

The only risk is 1) agent writes secret into sandbox, 2) agent calls command like CURL which sends secret off device. We mitigate that by disabling networking by default.

## Sandbox Setup

MCP server startup takes a number of parameters which control which sandbox is created:

 - backend: docker, podman, Modal.dev, E2B, WASI Sandbox w Container, Wasi BusyBox, etc
 - Backend specific options that make sense for each backend. For example
   - Docker: the docker image name/URL, flags for docker to control details (CPU, RAM, etc)
   - Modal/E2B: API key, base image name
   - WASI: image path
 - network_enabled: default false
 - extended_tool_instruction: a string instruction to append to the standard exec tool description. This could include hints about the sandbox capabilities like "Note, you have a full python environment with numpy, so to solve complex problems write and execute python scripts".
 - tool_instruction_override: a string to replace the standard exec tool instruction. Like above, but also replacing the default instead of extending it. 

These are simply CLI params, passed when the server starts up. You can't run 1 instance with multuple backends (one docker, one WASI), or 1 instance with multiple configs (2 different docker images). However, you can simply run several instances.

On startup the selected backend will validate and parse params. It should raise user-facing startup errors immediately on statup if the parameters aren't valid.

TBD: should we namespace backend params? All docker params start with "docker-"?

## Backend API

 - Initilization: including parameter parsing and passing back user
 - Start sandbox: start a sandbox, returning a sandbox handle
 - Stop sandbox (handle): stop a sandbox. 
 - Exec: see below
 - Tool Instructions (function returning string): get the tool instructions for the exec MCP command. For example, if the backend is a busybox with limited capabilities it might list all the CLI tools available and warn against using others. Can return null, and if it does the MCP server will fail to startup unless the user provided a tool_instruction_override.

## Exec Function

I'm leaning towards keeping the API exposed to agents over MCP extremely simple: 1 "exec" tool

 - There isn't an long running shell / each exec call is independent. Calling "cd A_DIR" then "ls" in 2 calls will will list the root directory not A_DIR. However you could call "cd a & ls" in 1 exec for the desired effect.
 - working_directory: [optional], the working directory to run the command in. If omitted, whatever this container considers it's root dir (could be "~"). Similar to calling exec with "cd working_directory & some_command"
 - Return type is an object with stdout, stderr, and status code
 - Technical open: how do we allow passing commands and escaping
   - array of commands for escaping. Passing ["echo", "A\nman\nby\nthe\nsea", ">", "poem.txt"]
   - simple string
   - allow both

## Tech Notes

 - Containers are 100% ephemerial. They can't be restarted. They shouldn't save state. The handles/IDs mean nothing after you call stop.
 - Reasonable defaults, powerful ottions
   - Each backend will have reasonable defaults, ideally zero options. For example, maybe Docker starts up alpine linux if you don't pass anything else. 
   - However you can go deep and specify many options to get the power of the underlying backends. For example for docker: a custom container, with custom CPU/RAM/networing config/timeouts, custom mounts, etc.
 - No parallel exec: queue any calls and issue serially
 - The MCP process should be very lightweight, as we're typically just issuing calls to docker, Modal, etc. We're a thin orchistrator, and should require minimal resources ourselves.

## Connection Lifecycle

start/shutdown: not exposed directly to agents via MCP. When you establish a connection it starts up sandbox, when you shutdown the connection it shuts down the sandbox.

 - stdio based MCP servers: 1 container for lifetime of server (as that's the connection)
 - https streaming: 1 container per connection for lifetime of connection.

## Tech Stack

Python or Golang: TBD

## Text Editor

We plan on implementing a text-editing CLI and include it in our base images. This is an enhancement that can and should come after the core project is done.

Why: great agents like Claude Code and OpenCode have this, and it let's them make more surgical edits to files. We suspect it will be needed here as well.

However: this is just another command (or set of commands) you can call through exec! No change to the core interface of the MCP server. Our images may contain it, and our MCP tool description may describe the commands, but there's no API level change, just a string/image change.

See OpenCode and Claude Code for examples. Roughly:

OpenCode:
 - read: read fileby path, with optional offset and limit
 - write: write whole file by path
 - edit tool: edit file by path doing a search and replace. oldString (anchor), newString, replaceAll?
 - multi-edit: may multiple edits to a file 

 Claude:
 - a command [view, str_replace, create, insert, undo_edit], a path, and option. Insert is line-number based insert.

Tech stack TBD: It's more important we implement this so it's easy to integrate into many containers/VMs/WASM/etc, then to match the stack. Maybe rust? We could make a coreutils busybox work in WASM, and rust compiles everywhere. 

## Future: Mapped Working Directory

In the future we want to be able map the core working directory to the host OS. This is both so we can:

1) pre-populate the root working directory with some content
2) Save the state of the working directory after the agent is done
