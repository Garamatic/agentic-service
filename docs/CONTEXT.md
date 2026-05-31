# Garamatic Agentic Service — Domain Glossary

## Core Concepts

- **GaramaticService** — The single entry point for all business logic. Owns client lifecycle, orchestration, validation, logging, and graceful degradation. Callers (MCP, FastAPI, LangGraph) only adapt results to their native response formats.

- **CustomerSession** — A scoped view of GaramaticService tied to a single customer email. Provides convenience methods that default the customer_email parameter.

- **Ticket** — A domain dataclass representing a support ticket with id, status, customer_email, title, description, created_at, resolved_at, resolution_notes, and billable_amount.

- **Invoice** — A domain dataclass representing an invoice status with invoice_id, status, amount, created_at, and paid_at.

- **CustomerContext** — A composite view of a customer across all services: summary statistics, recent tickets, and recent invoices.

- **ResolutionResult** — The outcome of resolving a ticket: ticket_id, status, resolution_notes, and optionally an invoice queue result.

## Ports (Seams)

- **TicketClientPort** — Protocol for ticket-masala API operations: get_ticket, list_tickets, create_ticket, resolve_ticket.

- **OdooClientPort** — Protocol for odoo-bridge API operations: get_invoice_status, list_customer_invoices.

- **MessageQueuePort** — Protocol for async message queue publishing: publish, close.

## Adapters

- **TicketClient** — HTTP adapter for ticket-masala API with circuit breakers and retry logic.

- **OdooBridgeClient** — HTTP adapter for odoo-bridge API with circuit breakers and retry logic.

- **RabbitMQPublisher** — Async RabbitMQ publisher adapter using aio-pika.

## Entry Points

- **MCP Server** — FastMCP-based server exposing tools and resources for AI agents. Runs via stdio or SSE.

- **REST API** — FastAPI-based HTTP gateway for web and mobile integrations.

- **Event Consumer** — RabbitMQ event consumer that triggers automated workflows via the NotificationComposer.

- **LangGraph Agent** — Intelligent agent with planning, memory, and tool orchestration. Built on LangGraph.

## Notifications

- **NotificationComposer** — Single entry point for event-to-email notifications. Hides normalization, template lookup, rendering, enrichment, sender selection, and graceful degradation.

- **NotificationTemplate** — A registered template for a routing key: recipient selector, renderer, optional enricher, and sender key.

- **EventView** — Normalized read facade over raw event payloads. Resolves snake_case, PascalCase, camelCase, and registered aliases automatically.

- **SenderIdentity** — Email address and display name for notification senders (billing, support, system).

## Tenant Context

- **TenantContext** — Immutable tenant configuration with override chaining and parent fallback.

- **TenantContextManager** — Central factory and cache for tenant contexts. Loads from pluggable backends.

## Error Taxonomy

- **ServiceError** — Base for all service-layer errors. Carries a request_id for tracing.

- **NotFoundError** — Upstream returned 404.

- **UpstreamError** — Upstream 5xx or network failure (already retried by client).

- **ValidationError** — Input failed business validation.

- **ServiceUnavailableError** — Required dependency not initialized.

## Infrastructure

- **Circuit Breaker** — pybreaker-based circuit breaker per upstream service (ticket_api, odoo_bridge).

- **Retry Policy** — tenacity-based exponential backoff with 3 attempts for retryable errors (5xx, network, timeout).
