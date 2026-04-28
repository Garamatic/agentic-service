"""Event Consumer for Garamatic Agentic Service.

Consumes events from RabbitMQ and triggers automated workflows:
- payment.received -> email customer, resolve ticket
- invoice.created -> notify customer
- etc.
"""

import asyncio
import json
import signal
from typing import Any

import aio_pika
import structlog

from .clients import TicketClient, OdooBridgeClient, RabbitMQPublisher
from .config import get_settings

logger = structlog.get_logger()

# Global flag for graceful shutdown
running = True


class EventConsumer:
    """RabbitMQ event consumer with workflow automation (async-native)."""

    def __init__(self, amqp_url: str, ticket_api_url: str, odoo_api_url: str):
        self.amqp_url = amqp_url
        self.ticket_client = TicketClient(ticket_api_url)
        self.odoo_client = OdooBridgeClient(odoo_api_url)
        self.mq_publisher = RabbitMQPublisher(amqp_url)
        self.connection: aio_pika.RobustConnection | None = None
        self.channel: aio_pika.Channel | None = None
        self.queue: aio_pika.Queue | None = None

    async def connect(self) -> aio_pika.Queue:
        """Establish RabbitMQ connection (async-native)."""
        self.connection = await aio_pika.connect_robust(self.amqp_url)
        self.channel = await self.connection.channel()

        # Set QoS to process one message at a time per consumer
        await self.channel.set_qos(prefetch_count=1)

        # Declare exchange
        exchange = await self.channel.declare_exchange(
            "event_exchange",
            aio_pika.ExchangeType.TOPIC,
            durable=True
        )

        # Declare queue
        self.queue = await self.channel.declare_queue(
            "agentic_consumer",
            durable=True
        )

        # Bind to relevant events (FIXED: match odoo-integration emissions)
        bindings = [
            "payment.received",   # from odoo-integration InvoiceStatusPoller
            "invoice.created",    # from odoo-integration TicketResolvedConsumer
            "invoice.overdue",    # from odoo-integration InvoiceStatusPoller
            "ticket.resolved",    # from ticket-masala TicketWorkflowService
        ]

        for routing_key in bindings:
            await self.queue.bind(exchange, routing_key=routing_key)

        logger.info("consumer.connected", queue=self.queue.name)
        return self.queue

    async def process_payment_received(self, event: dict[str, Any]):
        """Handle payment.received event: email customer, resolve ticket."""
        ticket_id = event.get("ticket_id") or event.get("TicketId")
        invoice_id = (
            event.get("invoice_id")
            or event.get("InvoiceId")
            or event.get("OdooInvoiceId")
        )
        customer_email = event.get("customer_email") or event.get("CustomerEmail")
        amount = event.get("amount") or event.get("Amount") or 0
        logger.info("workflow.payment_received", ticket_id=ticket_id, invoice_id=invoice_id)

        if not self.mq_publisher.exchange:
            logger.error("workflow.mq_not_connected", event="payment.received")
            return

        # Send thank you email (flat structure per email.send schema)
        payload = {
            "event_type": "email.send",
            "to_email": customer_email,
            "from_email": "billing@garamatic.com",
            "from_name": "Garamatic Billing",
            "subject": f"Payment Received - Invoice #{invoice_id}",
            "body_html": f"""
            <h2>Thank you for your payment!</h2>
            <p>We've received payment of €{amount:.2f} for invoice #{invoice_id}.</p>
            <p>Your ticket #{ticket_id} has been closed.</p>
            <p>Best regards,<br>Garamatic Team</p>
            """,
        }

        try:
            await self.mq_publisher.publish("email.send", payload)
            logger.info("workflow.email_sent", to=customer_email, type="payment_thanks")
        except Exception as e:
            logger.error("workflow.email_failed", error=str(e))

    async def process_invoice_created(self, event: dict[str, Any]):
        """Handle invoice.created event: notify customer."""
        ticket_id = event.get("ticket_id") or event.get("TicketId")
        invoice_id = (
            event.get("invoice_id")
            or event.get("InvoiceId")
            or event.get("OdooInvoiceId")
        )
        customer_email = event.get("customer_email") or event.get("CustomerEmail")
        amount = event.get("amount") or event.get("Amount") or 0
        logger.info("workflow.invoice_created", ticket_id=ticket_id, invoice_id=invoice_id)

        if not self.mq_publisher.exchange:
            logger.error("workflow.mq_not_connected", event="invoice.created")
            return

        payload = {
            "event_type": "email.send",
            "to_email": customer_email,
            "from_email": "billing@garamatic.com",
            "from_name": "Garamatic Billing",
            "subject": f"New Invoice #{invoice_id} - €{amount:.2f}",
            "body_html": f"""
            <h2>New Invoice Available</h2>
            <p>An invoice for €{amount:.2f} has been generated for ticket #{ticket_id}.</p>
            <p>Invoice #{invoice_id}</p>
            <p>Please review and process payment at your earliest convenience.</p>
            <p>Best regards,<br>Garamatic Team</p>
            """,
        }

        try:
            await self.mq_publisher.publish("email.send", payload)
            logger.info("workflow.email_sent", to=customer_email, type="invoice_notification")
        except Exception as e:
            logger.error("workflow.email_failed", error=str(e))

    async def process_ticket_escalated(self, event: dict[str, Any]):
        """Handle ticket.escalated event: notify manager."""
        ticket_id = event.get("ticket_id") or event.get("TicketId")
        reason = event.get("reason") or event.get("Reason") or "Unknown"
        logger.warning("workflow.ticket_escalated", ticket_id=ticket_id, reason=reason)

        if not self.mq_publisher.exchange:
            logger.error("workflow.mq_not_connected", event="ticket.escalated")
            return

        # Get ticket details for the alert
        try:
            ticket = await self.ticket_client.get_ticket(ticket_id)
        except Exception as e:
            logger.error("workflow.ticket_lookup_failed", ticket_id=ticket_id, error=str(e))
            ticket = {}

        manager_email = "manager@garamatic.com"  # TODO: Make configurable via env var

        payload = {
            "event_type": "email.send",
            "to_email": manager_email,
            "from_email": "system@garamatic.com",
            "from_name": "Garamatic System",
            "subject": f"URGENT: Ticket #{ticket_id} Escalated",
            "body_html": f"""
            <h2>Ticket Escalation Alert</h2>
            <p>Ticket #{ticket_id} has been escalated.</p>
            <p><strong>Reason:</strong> {reason}</p>
            <p><strong>Customer ID:</strong> {ticket.get('customerId', 'N/A')}</p>
            <p><strong>Title:</strong> {ticket.get('title', 'Untitled')}</p>
            <p>Please review immediately.</p>
            """,
        }

        try:
            await self.mq_publisher.publish("email.send", payload)
            logger.info("workflow.manager_notified", ticket_id=ticket_id)
        except Exception as e:
            logger.error("workflow.escalation_failed", error=str(e))

    async def process_ticket_created(self, event: dict[str, Any]):
        """Handle ticket.created event: send confirmation."""
        ticket_id = event.get("ticket_id") or event.get("TicketId")
        customer_email = event.get("customer_email") or event.get("CustomerEmail")
        customer_name = event.get("customer_name") or event.get("CustomerName")
        title = event.get("title") or event.get("Title")
        logger.info("workflow.ticket_created", ticket_id=ticket_id, customer=customer_email)

        if not self.mq_publisher.exchange:
            logger.error("workflow.mq_not_connected", event="ticket.created")
            return

        payload = {
            "event_type": "email.send",
            "to_email": customer_email,
            "from_email": "support@garamatic.com",
            "from_name": "Garamatic Support",
            "subject": f"Ticket #{ticket_id} Created - {title}",
            "body_html": f"""
            <h2>Your Support Ticket</h2>
            <p>Hi {customer_name},</p>
            <p>We've received your support request and created ticket #{ticket_id}.</p>
            <p><strong>Title:</strong> {title}</p>
            <p>Our team will review and respond within 24 hours.</p>
            <p>Best regards,<br>Garamatic Support Team</p>
            """,
        }

        try:
            await self.mq_publisher.publish("email.send", payload)
            logger.info("workflow.confirmation_sent", to=customer_email, ticket_id=ticket_id)
        except Exception as e:
            logger.error("workflow.confirmation_failed", error=str(e))

    async def process_invoice_overdue(self, event: dict[str, Any]):
        """Handle invoice.overdue event: notify customer + manager."""
        invoice_id = (
            event.get("invoice_id")
            or event.get("InvoiceId")
            or event.get("OdooInvoiceId")
        )
        customer_email = event.get("customer_email") or event.get("CustomerEmail")
        amount = event.get("amount") or event.get("Amount") or 0
        days_overdue = event.get("days_overdue") or event.get("DaysOverdue") or 0

        logger.warning(
            "workflow.invoice_overdue", invoice_id=invoice_id, days=days_overdue
        )

        if not self.mq_publisher.exchange:
            logger.error("workflow.mq_not_connected", event="invoice.overdue")
            return

        if not customer_email:
            logger.warning("workflow.invoice_overdue.no_email", invoice_id=invoice_id)
            return

        payload = {
            "event_type": "email.send",
            "to_email": customer_email,
            "from_email": "billing@garamatic.com",
            "from_name": "Garamatic Billing",
            "subject": f"Overdue Invoice #{invoice_id}",
            "body_html": f"""
            <h2>Invoice Overdue</h2>
            <p>Invoice #{invoice_id} for €{amount:.2f} is now {days_overdue} days overdue.</p>
            <p>Please process payment at your earliest convenience.</p>
            """,
        }

        try:
            await self.mq_publisher.publish("email.send", payload)
            logger.info("workflow.overdue_notification_sent", to=customer_email)
        except Exception as e:
            logger.error("workflow.overdue_notification_failed", error=str(e))

    async def process_ticket_resolved(self, event: dict[str, Any]):
        """Handle ticket.resolved event: send confirmation to customer."""
        ticket_id = event.get("ticket_id") or event.get("TicketId")
        customer_email = event.get("customer_email") or event.get("CustomerEmail")
        customer_name = event.get("customer_name") or event.get("CustomerName")
        resolution_notes = (
            event.get("resolution_notes")
            or event.get("ResolutionNotes")
            or "Your issue has been resolved."
        )
        logger.info(
            "workflow.ticket_resolved", ticket_id=ticket_id, customer=customer_email
        )

        if not self.mq_publisher.exchange:
            logger.error("workflow.mq_not_connected", event="ticket.resolved")
            return

        if not customer_email:
            logger.warning("workflow.ticket_resolved.no_email", ticket_id=ticket_id)
            return

        payload = {
            "event_type": "email.send",
            "to_email": customer_email,
            "from_email": "support@garamatic.com",
            "from_name": "Garamatic Support",
            "subject": f"Ticket #{ticket_id} Resolved",
            "body_html": f"""
            <h2>Your ticket has been resolved</h2>
            <p>Hi {customer_name or 'there'},</p>
            <p>Ticket #{ticket_id} has been resolved.</p>
            <p><strong>Resolution:</strong> {resolution_notes}</p>
            <p>If you have any further questions, please reply to this email.</p>
            """,
        }

        try:
            await self.mq_publisher.publish("email.send", payload)
            logger.info(
                "workflow.resolution_email_sent",
                to=customer_email,
                ticket_id=ticket_id,
            )
        except Exception as e:
            logger.error("workflow.resolution_email_failed", error=str(e))

    async def handle_message(self, message: aio_pika.IncomingMessage):
        """Process a single message (async-native, no threads)."""
        async with message.process():
            try:
                body = json.loads(message.body)
                routing_key = message.routing_key

                # Check for integration-contract envelope, fallback to flat object
                event_type = body.get("eventType") or body.get("event_type")
                event_data = body.get("data") if "data" in body else body

                logger.info(
                    "consumer.received",
                    routing_key=routing_key,
                    event_type=event_type
                )

                # Dispatch to appropriate handler
                handlers = {
                    "payment.received": self.process_payment_received,
                    "invoice.created": self.process_invoice_created,
                    "invoice.overdue": self.process_invoice_overdue,
                    "ticket.resolved": self.process_ticket_resolved,
                    # Kept for potential future producers; currently no emitters:
                    "ticket.escalated": self.process_ticket_escalated,
                    "ticket.created": self.process_ticket_created,
                }

                handler = handlers.get(routing_key)
                if handler:
                    await handler(event_data)
                else:
                    logger.warning("consumer.unhandled_routing_key", key=routing_key)

            except json.JSONDecodeError as e:
                logger.error("consumer.invalid_json", error=str(e))
                # Message is auto-acked by context manager even on error
            except Exception as e:
                logger.error("consumer.message_failed", error=str(e))
                # Requeue on unexpected errors
                raise

    async def run(self):
        """Main consumer loop (async-native)."""
        global running

        queue = await self.connect()
        await self.mq_publisher.connect()

        logger.info("consumer.started", queue=queue.name)

        # Start consuming messages
        async with queue.iterator() as queue_iter:
            async for message in queue_iter:
                if not running:
                    break

                # Process message (auto-acks on success, requeues on failure)
                try:
                    await self.handle_message(message)
                except Exception:
                    # Message will be requeued by aio-pika
                    pass

    async def close(self):
        """Cleanup resources (async-native)."""
        if self.ticket_client:
            await self.ticket_client.close()
        if self.odoo_client:
            await self.odoo_client.close()
        if self.mq_publisher:
            await self.mq_publisher.close()
        if self.connection:
            await self.connection.close()


def signal_handler(signum, _frame):
    """Handle shutdown signals."""
    global running
    logger.info("consumer.shutdown_signal", signal=signum)
    running = False


async def main_async():
    """Async main entry point."""
    global running

    config = get_settings()
    consumer = EventConsumer(
        amqp_url=config.rabbitmq_url,
        ticket_api_url=config.ticket_api_url,
        odoo_api_url=config.odoo_bridge_url
    )

    # Set up signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler, sig, None)

    try:
        await consumer.run()
    finally:
        # Remove signal handlers
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        await consumer.close()
        logger.info("consumer.stopped")


def main():
    """Run the event consumer."""
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
