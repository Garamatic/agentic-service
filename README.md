# Garamatic Agentic Service

**MCP Server** exposing your Garamatic platform to AI agents (Claude, GPT, etc.)

Now includes: REST API, Event Consumer, and AI Agent for complete automation.

## 🎯 What is this?

This service provides a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that exposes your existing Garamatic services as AI-callable tools:

- ✅ Query tickets from `ticket-masala`
- ✅ Create invoices via `odoo-bridge`
- ✅ Send emails via `mailing-service`
- ✅ Get customer context across services

## 🏗️ Architecture

```
                    ┌─────────────────┐
                    │   AI Agents     │
                    │  (Claude/GPT)   │
                    └────────┬────────┘
                             │ MCP
                    ┌────────▼────────┐
   ┌──────────┐     │  Agentic       │     ┌──────────┐
   │ REST API │────▶│   Service      │◀────│  Web UI  │
   │ Clients  │     │                │     │  Scripts │
   └──────────┘     │ ┌────────────┐ │     └──────────┘
   ┌──────────┐     │ │LangGraph   │ │     ┌──────────┐
   │ Consumer │────▶│ │Agent       │ │     │  CLI     │
   │ (Events) │     │ └────────────┘ │     └──────────┘
   └──────────┘     └────────┬───────┘
                             │
          ┌──────────────────┼──────────────────┐
          ▼                  ▼                  ▼
   ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
   │ticket-masala │ │odoo-bridge   │ │mailing-      │
   │(REST API)    │ │(RabbitMQ)    │ │service       │
   └──────────────┘ └──────────────┘ └──────────────┘
```

## 🚀 Quick Start

### 1. Install dependencies
```bash
# Using uv (recommended)
uv sync

# Or using pip
pip install -e ".[dev]"
```

### 2. Configure environment
```bash
cp .env.example .env
# Edit .env with your service URLs
```

### 3. Test with Claude Desktop

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "garamatic": {
      "command": "uv",
      "args": ["run", "garamatic-mcp"],
      "cwd": "/path/to/agentic-service"
    }
  }
}
```

Restart Claude Desktop. You'll now see Garamatic tools available!

### 4. Run the services

**MCP Server (for Claude Desktop):**
```bash
uv run garamatic-mcp
```

**REST API (for web/mobile integrations):**
```bash
uv run garamatic-api
# API available at http://localhost:3001
# Docs at http://localhost:3001/docs
```

**Event Consumer (for automated workflows):**
```bash
uv run garamatic-consumer
```

**AI Agent (interactive LangGraph agent):**
```bash
export OPENAI_API_KEY='your-key-here'
uv run garamatic-agent
```

## 🛠️ Available Tools

### Ticket Management
| Tool | Description |
|------|-------------|
| `get_ticket_status` | Get current status of a ticket |
| `list_customer_tickets` | List all tickets for a customer |
| `create_ticket` | Create a new support ticket |
| `resolve_ticket` | Resolve ticket with optional invoice |

### Invoicing
| Tool | Description |
|------|-------------|
| `create_invoice` | Create invoice for a ticket |
| `get_invoice_status` | Check if invoice is paid |


### Email
| Tool | Description |
|------|-------------|
| `send_email` | Send email to customer |


### Resources (Context)
| Resource | Description |
|----------|-------------|
| `customer://{email}` | Full customer profile across services |
| `ticket://{id}` | Ticket details with related invoices |

## 📝 Example Agent Conversation

**User**: "What's the status of ticket #123?"

**Agent** (calls `get_ticket_status`):
```json
{
  "ticket_id": "123",
  "status": "resolved",
  "customer_email": "john@example.com",
  "resolution_notes": "Fixed network issue"
}
```

**User**: "Create an invoice for $150"

**Agent** (calls `create_invoice`):
```json
{
  "ticket_id": "123",
  "amount": 150.00,
  "description": "Network troubleshooting"
}
```

**Agent**: "✅ Invoice created in Odoo. It will be synced automatically."

**User**: "Email the customer about this"

**Agent** (calls `send_email`):
- Composes professional email
- Sends via mailing-service

## 🔧 Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TICKET_API_URL` | `http://localhost:5000` | ticket-masala API |
| `ODOO_BRIDGE_URL` | `http://localhost:5001` | odoo-bridge API |
| `RABBITMQ_URL` | `amqp://guest:guest@localhost:5672` | RabbitMQ |
| `LOG_LEVEL` | `INFO` | Logging level |

### .env.example
```bash
# APIs
TICKET_API_URL=http://localhost:5000
ODOO_BRIDGE_URL=http://localhost:5001

# RabbitMQ
RABBITMQ_HOST=localhost
RABBITMQ_PORT=5672
RABBITMQ_USER=guest
RABBITMQ_PASS=guest

# Logging
LOG_LEVEL=INFO
```

## 🔌 Extensions

### REST API Gateway
Full REST API for non-AI integrations:

```bash
uv run garamatic-api
```

**Available endpoints:**
- `GET /health` - Service health check
- `GET /tickets/{id}` - Get ticket
- `GET /tickets` - List tickets (with filters)
- `POST /tickets` - Create ticket
- `POST /tickets/{id}/resolve` - Resolve ticket
- `GET /invoices/by-ticket/{id}` - Get invoice
- `POST /invoices` - Create invoice
- `POST /emails` - Send email
- `GET /customers/{email}/context` - Customer context

All endpoints include input validation, request tracing, and proper error handling.

### Event Consumer
Automated workflow engine consuming RabbitMQ events:

```bash
uv run garamatic-consumer
```

**Automated workflows:**
- `invoice.paid` → Send thank you email to customer
- `invoice.sent` → Notify customer of new invoice
- `ticket.escalated` → Alert manager via email
- `ticket.created` → Send confirmation to customer

### AI Agent (LangGraph)
Intelligent agent with planning and memory:

```bash
export OPENAI_API_KEY='your-key'
uv run garamatic-agent
```

**Capabilities:**
- Multi-step reasoning (find ticket → check status → resolve + invoice)
- Customer context analysis across all services
- Human approval for large invoices (€500+)
- Conversation memory
- Tool orchestration with fallback

Example interaction:
```
You: Customer john@example.com needs help with ticket T-123
Agent: I'll look up the customer context and ticket details...
[Agent retrieves context, checks ticket, provides solution]

You: Create an invoice for €200 and resolve the ticket
Agent: ✅ Ticket T-123 resolved with €200 invoice queued.
```

## 🧪 Testing

```bash
# Run tests
uv run pytest

# With coverage
uv run pytest --cov=agentic_service --cov-report=html

# Test API endpoints
curl http://localhost:3001/health
```

## 🐳 Docker

```bash
# Build
docker build -t garamatic-agentic .

# Run
docker run -p 3001:3001 --env-file .env garamatic-agentic
```

## 📚 MCP Resources

- [MCP Specification](https://modelcontextprotocol.io/)
- [FastMCP Docs](https://github.com/jlowin/fastmcp)
- [Example MCP Servers](https://github.com/modelcontextprotocol/servers)

## 🎓 Learning Outcomes

- How MCP enables AI agents to call your APIs
- Designing AI-friendly tool interfaces
- Structured vs unstructured LLM interactions

---

**Built with 🤖 AI compatibility in mind**
