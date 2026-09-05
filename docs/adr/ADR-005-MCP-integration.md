# ADR-005: MCP integration

## Context

Model Context Protocol servers exist for Jira, GitHub, GitLab, AWS, Kubernetes, filesystems,
databases and internal APIs, and coding agents (Claude Code, OpenCode, Copilot) speak MCP. The
harness should both consume MCP servers as tool sources and expose its own governed tools to
those agents.

## Decision

* **Consume**: `agent/mcp/client.py` implements a stdio JSON-RPC client (initialize, tools/list,
  tools/call). `adapters/generic/mcp.py` registers each remote tool as an `McpTool` whose
  permission/risk/approval metadata comes from `.agent/config.yaml` (`mcp_servers[].tools`),
  defaulting to MODIFY + approval; tools whose names start with read-only prefixes default to READ.
  Disabled tools are never registered.
* **Expose**: `agent/mcp/server.py` (`devops-agent mcp-serve`) serves the registry over stdio.
  Calls run through the executor with a session task, so policy, approvals (non-interactive ->
  deny unless pre-approved), redaction and audit apply identically to CLI usage.
* Structured APIs (MCP/REST/`kubectl -o json`) are preferred over shell scraping.

## Alternatives

* Only shell tools - rejected: fragile output parsing and weaker permission granularity.
* Only MCP - rejected: many environments have no MCP servers; native/CLI tools remain necessary.

## Consequences

* New capabilities can be added by configuration alone (an MCP server entry) without code.
* External tools never bypass policy: risk metadata is configured by the operator, not declared by the server.
* Coding agents get one governed tool surface instead of raw cluster/cloud credentials.
