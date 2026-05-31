# RFC: Integrating Rivet agentOS into Garamatic Agentic Service

## 1. Introduction & Context

The Garamatic Agentic Service currently relies on a custom-built infrastructure using LangGraph for agent orchestration, RabbitMQ for event processing, and standalone Python processes for MCP integration and REST APIs.

As the platform aims to scale and handle more complex, multi-tenant, and resilient agent sessions, evaluating a dedicated agent runtime is highly beneficial. This RFC analyzes the integration of **Rivet agentOS** as a foundational infrastructure layer for the service.

---

## 2. What is Rivet agentOS?

Rivet agentOS is a portable, open-source operating system specifically designed for production AI agents.

- **Technology**: Built on WebAssembly and V8 Isolates (similar to browser sandboxes).
- **Performance**: Extremely fast cold starts (~6ms) and low memory footprint (~131 MB per execution) compared to traditional Docker-based sandboxes.
- **Capabilities**: Provides built-in session persistence, webhooks, durable queues, LLM token metering, and secure execution of arbitrary code with granular resource limits.

---

## 3. Current Architecture vs. Proposed Architecture

### Current Architecture

- **MCP Server (`garamatic-mcp`)**: Exposes domain-specific Garamatic tools via standard I/O.
- **Event Consumer (`garamatic-consumer`)**: Listens to RabbitMQ to trigger automated workflows.
- **AI Agent (`garamatic-agent`)**: A LangGraph-based script that maintains its own memory and multi-step reasoning logic.
- **REST API (`garamatic-api`)**: Standard HTTP interface.

### Proposed Architecture with agentOS

- **MCP Server & REST API**: Remain intact. They continue to act as the secure domain boundary for Garamatic backend services (Odoo, Ticket Masala, etc.).
- **Agent Runtime**: The `garamatic-agent` execution is offloaded *into* the agentOS runtime. agentOS handles session initialization, state persistence across conversation turns, and memory management automatically.
- **Workflows & Queues**: The RabbitMQ `garamatic-consumer` logic can be gradually migrated to or augmented by agentOS's native durable Webhooks and Queues, potentially simplifying the infrastructure footprint.

---

## 4. Integration Analysis: Opportunities (The "Why")

### A. Infrastructure Simplification (Basis)

Maintaining multi-turn conversations and long-running agent workflows currently requires manual state management (e.g., storing LangGraph state in an external database). agentOS natively persists sessions and provides a "Universal Transcript Format", completely offloading state management from the Garamatic codebase.

### B. Scalability & Cost

Running individual Docker containers or heavy Python processes for each active customer support agent session scales poorly. agentOS's V8 isolates allow running thousands of concurrent agent sessions on a single machine with minimal resource overhead.

### C. Secure Code Execution (Complement)

If a future requirement demands the agent to run code dynamically (e.g., *"Write a Python script to analyze Odoo invoices and generate a chart"*), executing this in the host environment is a massive security risk. agentOS provides an isolated, secure sandbox with strict CPU, memory, and network restrictions out of the box, complementing our existing MCP tools with raw, safe compute capabilities.

### D. Native MCP Synergy

agentOS natively supports agents that consume MCP servers. Our existing `garamatic-mcp` server plugs directly into an agentOS session without requiring any modifications.

---

## 5. Potential Challenges & Risks

1. **Polyglot Complexity**:
   The current `agentic-service` is written in Python (indicated by `uv` and `pytest`). Rivet agentOS relies heavily on a JavaScript/TypeScript SDK (`@rivet-dev/agent-os-core`). Integrating this might require introducing Node.js/TypeScript into the stack to run the agent host, while keeping the MCP server and APIs in Python.
   *(Note: Based on the global user rules preferring TypeScript/JavaScript and strict mode, migrating the agent orchestration layer to TS might actually align well with long-term goals).*

2. **Migration Effort**:
   Moving the existing LangGraph orchestration into the agentOS paradigm requires refactoring the `garamatic-agent` logic. We need to map LangGraph's node/edge logic into agentOS workflows.

3. **Vendor/Ecosystem Reliance**:
   Adopting agentOS ties the core execution loop of our agents to the Rivet ecosystem, making future migrations away from it more difficult.

---

## 6. Conclusion & Recommendation

Integrating Rivet agentOS offers immense benefits for **scalability, out-of-the-box state management, and secure sandboxing**. It is highly recommended to explore this as the future runtime for Garamatic agents.

### Proposed Rollout Plan

1. **Phase 1 (Proof of Concept)**:
   Keep the existing Python/RabbitMQ infrastructure intact. Build a small, experimental TypeScript/Node.js service using `@rivet-dev/agent-os-core` that spins up a test agent. Connect this test agent to the existing Python `garamatic-mcp` server.

2. **Phase 2 (Evaluation)**:
   Evaluate the PoC. Does the automatic session persistence and low overhead justify rewriting the `garamatic-agent` core logic in TypeScript?

3. **Phase 3 (Adoption & Consolidation)**:
   If successful, fully migrate `garamatic-agent` to run on agentOS. Following that, migrate the RabbitMQ event handlers to agentOS native webhooks to consolidate the architecture.
