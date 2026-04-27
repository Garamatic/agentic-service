# Garamatic Agentic Service

**MCP Server** exposing your Garamatic platform to AI agents (Claude, GPT, etc.)

## 🎯 What is this?

This service provides a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that exposes your existing Garamatic services as AI-callable tools:

- ✅ Query tickets from `ticket-masala`
- ✅ Create invoices via `odoo-bridge`
- ✅ Send emails via `mailing-service`
- ✅ Get customer context across services

## 🏗️ Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   Claude/GPT    │────▶│  MCP Protocol    │────▶│  agentic-service│
│   (AI Agent)    │     │  (stdio/sse)     │     │  (Python/FastMCP)
└─────────────────┘     └──────────────────┘     └─────────────────┘
                                                          │
          ┌─────────────────┬─────────────────┬──────────┘
          ▼                 ▼                 ▼
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

### 4. Test manually
```bash
# Run the server
uv run garamatic-mcp

# Or in development mode with SSE transport
uv run python -m agentic_service.server --transport sse --port 3001
```

## 🛠️ Available Tools

### Ticket Management
| Tool | Description |
|------|-------------|
| `get_ticket_status` | Get current status of a ticket |
| `list_customer_tickets` | List all tickets for a customer |
| `create_ticket` | Create a new support ticket |
| `update_ticket` | Update ticket status/notes |

### Invoicing
| Tool | Description |
|------|-------------|
| `create_invoice` | Create invoice for a ticket |
| `get_invoice_status` | Check if invoice is paid |
| `list_outstanding_invoices` | Get unpaid invoices |

### Email
| Tool | Description |
|------|-------------|
| `send_email` | Send email to customer |
| `send_invoice_email` | Send invoice PDF to customer |

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

## 🧪 Testing

```bash
# Run tests
uv run pytest

# With coverage
uv run pytest --cov=agentic_service --cov-report=html
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
