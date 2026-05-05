# Garamatic Agentic Service

AI-agenten integratieservice die uw Garamatic platform beschikbaar maakt via Model Context Protocol (MCP).

Inclusief: MCP Server, REST API, Event Consumer en LangGraph Agent voor volledige automatisering.

## Inleiding

Deze service biedt een [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server die uw bestaande Garamatic services beschikbaar maakt als AI-aanroepbare tools:

- Raadpleeg tickets van `ticket-masala`
- Maak facturen aan via `odoo-bridge`
- Verstuur e-mails via `mailing-service`
- Verkrijg klantcontext op services

## Architectuuroverzicht

```
                    ┌─────────────────┐
                    │   AI Agents     │
                    │  (Claude/GPT)   │
                    └────────┬────────┘
                             │ MCP
                    ┌────────▼────────┐
   ┌──────────┐     │  Agentic       │     ┌──────────┐
   │ REST API │────>│   Service      │<────│  Web UI  │
   │ Clients  │     │                │     │  Scripts │
   └──────────┘     │ ┌────────────┐ │     └──────────┘
   ┌──────────┐     │ │LangGraph   │ │     ┌──────────┐
   │ Consumer │────>│ │Agent       │ │     │  CLI     │
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

## Service Componenten

### MCP Server

Modelcontextprotocol server voor AI-agenten (Claude, GPT):
- Exposeert Garamatic-tools als MCP resources
- Draait op stdio voor Claude Desktop integratie
- Vraagt authenticatie en input van gebruiker

### REST API

HTTP API voor web en mobiele integraties:
- Volledige CRUD voor tickets, facturen, e-mails
- Input validatie en request tracing
- OpenAPI documentatie op `/docs`

### Event Consumer

Geautomatiseerde werkstroom-engine:
- Luistert naar RabbitMQ events
- Triggert workflows (factuur betaald → dank e-mail)
- Loggt alle transities

### LangGraph Agent

Intelligent agentprogramma met planning en geheugen:
- Multi-stap redenering
- Klantcontextanalyse
- Goedkeuring door mens voor grote acties
- Tool orchestratie met fallback

## Installatie en Setup

### 1. Afhankelijkheden Installeren

```bash
# Met uv (aanbevolen)
uv sync

# Of met pip
pip install -e ".[dev]"
```

### 2. Omgeving Configureren

```bash
cp .env.example .env
# Bewerk .env met uw service URLs
```

### 3. Testen met Claude Desktop

Voeg toe aan uw Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json` op macOS):

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

Start Claude Desktop opnieuw. Garamatic tools zijn nu beschikbaar!

### 4. Services Uitvoeren

MCP Server (voor Claude Desktop):
```bash
uv run garamatic-mcp
```

REST API (voor web/mobiele integraties):
```bash
uv run garamatic-api
# API beschikbaar op http://localhost:3001
# Documentatie op http://localhost:3001/docs
```

Event Consumer (voor geautomatiseerde workflows):
```bash
uv run garamatic-consumer
```

AI Agent (interactieve LangGraph agent):
```bash
export OPENAI_API_KEY='your-key-here'
uv run garamatic-agent
```

## Beschikbare Tools

### Ticketbeheer
| Tool | Beschrijving |
|------|-------------|
| `get_ticket_status` | Huidige status van een ticket opvragen |
| `list_customer_tickets` | Alle tickets van een klant weergeven |
| `create_ticket` | Nieuw ondersteuningsticket aanmaken |
| `resolve_ticket` | Ticket oplossen met optionele factuur |

### Facturering
| Tool | Beschrijving |
|------|-------------|
| `create_invoice` | Factuur voor ticket aanmaken |
| `get_invoice_status` | Controleer of factuur is betaald |

### E-mail
| Tool | Beschrijving |
|------|-------------|
| `send_email` | E-mail naar klant versturen |

### Resources (Context)
| Resource | Beschrijving |
|----------|-------------|
| `customer://{email}` | Volledig klantprofiel op services |
| `ticket://{id}` | Ticketdetails met gerelateerde facturen |

## Voorbeeld Agent Conversatie

**Gebruiker**: "Wat is de status van ticket #123?"

**Agent** (roept `get_ticket_status` aan):
```json
{
  "ticket_id": "123",
  "status": "resolved",
  "customer_email": "john@example.com",
  "resolution_notes": "Netwerkprobleem opgelost"
}
```

**Gebruiker**: "Maak een factuur van 150 euro"

**Agent** (roept `create_invoice` aan):
```json
{
  "ticket_id": "123",
  "amount": 150.00,
  "description": "Netwerkprobleemoplossing"
}
```

**Agent**: "Factuur aangemaakt in Odoo. Wordt automatisch gesynchroniseerd."

**Gebruiker**: "Stuur de klant een e-mail hierover"

**Agent** (roept `send_email` aan):
- Stelt professioneel e-mailbericht op
- Verzendt via mailing-service

## Configuratie

### Omgevingsvariabelen

| Variabele | Standaard | Beschrijving |
|----------|-----------|-------------|
| `TICKET_API_URL` | `http://localhost:5000` | ticket-masala API |
| `ODOO_BRIDGE_URL` | `http://localhost:5001` | odoo-bridge API |
| `RABBITMQ_URL` | `amqp://guest:guest@localhost:5672` | RabbitMQ |
| `LOG_LEVEL` | `INFO` | Logniveau |

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

## Uitbreidingen

### REST API Gateway

Volledige REST API voor niet-AI integraties:

```bash
uv run garamatic-api
```

Beschikbare endpoints:
- `GET /health` - Service gezondheidsstatus
- `GET /tickets/{id}` - Ticket ophalen
- `GET /tickets` - Tickets weergeven (met filters)
- `POST /tickets` - Ticket aanmaken
- `POST /tickets/{id}/resolve` - Ticket oplossen
- `GET /invoices/by-ticket/{id}` - Factuur ophalen
- `POST /invoices` - Factuur aanmaken
- `POST /emails` - E-mail versturen
- `GET /customers/{email}/context` - Klantcontext

Alle endpoints bevatten input validatie, request tracing en foutafhandeling.

### Event Consumer

Geautomatiseerde werkstroom-engine die RabbitMQ events verwerkt:

```bash
uv run garamatic-consumer
```

Geautomatiseerde workflows:
- `invoice.paid` - Stuur bedankmail naar klant
- `invoice.sent` - Notificeer klant van nieuwe factuur
- `ticket.escalated` - Waarschuw manager via e-mail
- `ticket.created` - Stuur bevestiging naar klant

### AI Agent (LangGraph)

Intelligent agentprogramma met planning en geheugen:

```bash
export OPENAI_API_KEY='your-key'
uv run garamatic-agent
```

Capaciteiten:
- Multi-stap redenering (zoek ticket → controleer status → los op + factuur)
- Klantcontextanalyse op services
- Menselijke goedkeuring voor grote facturen (€500+)
- Gespreksgeheugen
- Tool orchestratie met fallback

Voorbeeldinteractie:
```
U: Klant john@example.com heeft hulp nodig bij ticket T-123
Agent: Ik zoek klantcontext en ticketdetails op...
[Agent haalt context op, controleert ticket, geeft oplossing]

U: Maak een factuur van €200 en los het ticket op
Agent: Ticket T-123 opgelost met €200 factuur in wachtrij.
```

## Testen

```bash
# Tests uitvoeren
uv run pytest

# Met coverage
uv run pytest --cov=agentic_service --cov-report=html

# API endpoints testen
curl http://localhost:3001/health
```

## Docker

```bash
# Build
docker build -t garamatic-agentic .

# Uitvoeren
docker run -p 3001:3001 --env-file .env garamatic-agentic
```

## Bronnen

- [MCP Specificatie](https://modelcontextprotocol.io/)
- [FastMCP Documentatie](https://github.com/jlowin/fastmcp)
- [Voorbeeld MCP Servers](https://github.com/modelcontextprotocol/servers)

## Leeruitkomsten

- Hoe MCP AI-agenten in staat stelt uw APIs aan te roepen
- AI-vriendelijke tool interfaces ontwerpen
- Gestructureerde vs ongestructureerde LLM interacties

---

Gebouwd met AI-compatibiliteit in het oog
