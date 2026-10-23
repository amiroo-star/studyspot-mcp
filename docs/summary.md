# Part 1 — MCP and the Fetch server

**Amir Darwich · EECE 503P/798S Agentic Systems · C5 Agent Tools**

## What the article says

Ksenia Se's article explains why the Model Context Protocol, open-sourced by Anthropic in November 2024, took off in early 2025. Before MCP, every model-to-tool link was custom glue, so M applications and N tools meant M×N integrations. MCP is an open, model-agnostic standard: each side implements it once, giving M+N. The article contrasts it with ChatGPT plugins (proprietary, mostly one-way lookups), agent frameworks such as LangChain (which standardise tools for developers, whereas MCP lets the model discover them at runtime), and RAG (passive retrieval, while MCP can also act). It is candid about gaps: weak tool descriptions still cause poor tool choice, and remote deployment, authentication and monitoring were unfinished.

The three roles fit together as follows. The **Host** is the application the user talks to (Claude Desktop, an IDE, or my Agents SDK program); it owns the model and decides what the model may do. Inside it, one **Client** per server keeps a 1:1 session, discovers capabilities, and relays JSON-RPC calls. Each **Server** exposes three primitives: *tools* the model calls, *resources* the application reads, and *prompts* the user invokes.

## The server I explored: Fetch

The reference Fetch server exposes one tool, `fetch(url, max_length=5000, start_index=0, raw=false)`, plus a `fetch` prompt.

**What it does well.** It has a well-defined interface: strictly typed arguments with bounds, accurate read-only and open-world annotations, and failures (robots.txt refusal, HTTP errors, refused private addresses) returned as `isError` results the model can read. Isolation is respected: one capability, with chunked reading through `start_index` instead of a second tool. It also blocks private and cloud-metadata addresses by default, closing the obvious SSRF hole.

**Where it falls short of the class principles.**

1. The tool description mixes documentation with behavioural instructions to the model ("this tool now grants you internet access…"). That is exactly what tool-poisoning attacks exploit, so a host cannot easily tell trusted guidance from manipulation.
2. `raw` is a mode flag that makes one tool behave as two (simplified markdown vs. raw HTML), the "and" smell from class.
3. Reading past the end returns an `<error>` string as normal content instead of `isError`, which breaks the otherwise consistent error contract.
4. Fetched pages are untrusted text returned without any provenance marking, so the server is a direct channel for indirect prompt injection. Defending against that is left entirely to the host.

These four points shaped my own design.
