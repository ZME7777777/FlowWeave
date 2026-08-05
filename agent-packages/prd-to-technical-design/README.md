# PRD to Technical Design Plugin

This optional OpenHands plugin contains reusable skills for a normal FlowWeave node. It has no built-in FlowWeave API tools, approval capability, or connector credentials.

Reference it from a node with:

```json
{"plugins": ["prd-to-technical-design"]}
```

Feishu, knowledge-base, and other MCP servers are configured separately through the server-side `ALLOWED_MCP_CONFIG_JSON` allowlist and referenced by name from the node definition.
