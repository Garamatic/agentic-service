"""Event Consumer for Garamatic Agentic Service.

Consumes events from RabbitMQ and triggers automated workflows:
- invoice.paid -> resolve ticket, email customer
- ticket.escalated -> notify manager
- etc.
"""

import asyncio
import json
import signal
from typing import Any

import structlog

from .clients import TicketClient, OdooBridgeClient, RabbitMQPublisher
from .config import get_settings

logger = structlog.get_logger()

# Global flag for graceful shutdown
running = True

# Track in-flight message processing
_in_flight_messages = 0


class EventConsumer:
    """RabbitMQ event consumer with workflow automation."""
    
    def __init__(self, amqp_url: str, ticket_api_url: str, odoo_api_url: str):
        self.amqp_url = amqp_url
        self.ticket_client = TicketClient(ticket_api_url)
        self.odoo_client = OdooBridgeClient(odoo_api_url)
        self.mq_publisher = RabbitMQPublisher(amqp_url)
        self.connection = None
        self.channel = None
    
    async def connect(self):
        """Establish RabbitMQ connection."""
        import pika
        
        loop = asyncio.get_running_loop()
        
        def _connect():
            params = pika.URLParameters(self.amqp_url)
            return pika.BlockingConnection(params)
        
        self.connection = await loop.run_in_executor(None, _connect)
        self.channel = self.connection.channel()
        
        # Declare exchange and queue
        self.channel.exchange_declare(
            exchange="event_exchange",
            exchange_type="topic",
            durable=True
        )
        
        # Create queue for this consumer
        result = self.channel.queue_declare(queue="agentic_consumer", durable=True)
        queue_name = result.method.queue
        
        # Bind to relevant events
        bindings = [
            "invoice.paid",
            "invoice.sent",
            "ticket.created",
            "ticket.escalated",
        ]
        
        for routing_key in bindings:
            self.channel.queue_bind(
                exchange="event_exchange",
                queue=queue_name,
                routing_key=routing_key
            )
        
        logger.info("consumer.connected", queue=queue_name)
        return queue_name
    
    async def process_invoice_paid(self, event: dict[str, Any]):
        """Handle invoice.paid event: email customer, resolve ticket."""
        ticket_id = event.get("ticket_id")
        invoice_id = event.get("invoice_id")
        customer_email = event.get("customer_email")
        amount = event.get("amount")
        
        logger.info("workflow.invoice_paid", ticket_id=ticket_id, invoice_id=invoice_id)
        
        if not self.mq_publisher.channel:
            logger.error("workflow.mq_not_connected", event="invoice.paid")
            return
        
        # Send thank you email
        email_event = {
            "event_type": "email.send",
            "to_email": customer_email,
            "from_name": "Garamatic Billing",
            "subject": f"Payment Received - Invoice #{invoice_id}",
            "body_html": f"""
            <h2>Thank you for your payment!</h2>
            <p>We've received payment of €{amount:.2f} for invoice #{invoice_id}.</p>
            <p>Your ticket #{ticket_id} has been closed.</p>
            <p>Best regards,<br>Garamatic Team</p>
            """,
            "sent_at": event.get("paid_at")
        }
        
        try:
            await self.mq_publisher.publish("email.send", email_event)
            logger.info("workflow.email_sent", to=customer_email, type="payment_thanks")
        except Exception as e:
            logger.error("workflow.email_failed", error=str(e))
    
    async def process_invoice_sent(self, event: dict[str, Any]):
        """Handle invoice.sent event: notify customer."""
        ticket_id = event.get("ticket_id")
        invoice_id = event.get("invoice_id")
        customer_email = event.get("customer_email")
        amount = event.get("amount")
        
        logger.info("workflow.invoice_sent", ticket_id=ticket_id, invoice_id=invoice_id)
        
        if not self.mq_publisher.channel:
            logger.error("workflow.mq_not_connected", event="invoice.sent")
            return
        
        email_event = {
            "event_type": "email.send",
            "to_email": customer_email,
            "from_name": "Garamatic Billing",
            "subject": f"New Invoice #{invoice_id} - €{amount:.2f}",
            "body_html": f"""
            <h2>New Invoice Available</h2>
            <p>An invoice for €{amount:.2f} has been generated for ticket #{ticket_id}.</p>
            <p>Invoice #{invoice_id}</p>
            <p>Please review and process payment at your earliest convenience.</p>
            <p>Best regards,<br>Garamatic Team</p>
            """,
            "sent_at": event.get("sent_at")
        }
        
        try:
            await self.mq_publisher.publish("email.send", email_event)
            logger.info("workflow.email_sent", to=customer_email, type="invoice_notification")
        except Exception as e:
            logger.error("workflow.email_failed", error=str(e))
    
    async def process_ticket_escalated(self, event: dict[str, Any]):
        """Handle ticket.escalated event: notify manager."""
        ticket_id = event.get("ticket_id")
        reason = event.get("reason", "Unknown")
        
        logger.warning("workflow.ticket_escalated", ticket_id=ticket_id, reason=reason)
        
        if not self.mq_publisher.channel:
            logger.error("workflow.mq_not_connected", event="ticket.escalated")
            return
        
        # Get ticket details
        try:
            ticket = await self.ticket_client.get_ticket(ticket_id)
            
            # Notify manager (could be email, Slack, etc.)
            # For now, just log and send email
            alert_event = {
                "event_type": "email.send",
                "to_email": "manager@garamatic.com",  # Configure as needed
                "from_name": "Garamatic System",
                "subject": f"URGENT: Ticket #{ticket_id} Escalated",
                "body_html": f"""
                <h2>Ticket Escalation Alert</h2>
                <p>Ticket #{ticket_id} has been escalated.</p>
                <p><strong>Reason:</strong> {reason}</p>
                <p><strong>Customer:</strong> {ticket.get('customerName')} ({ticket.get('customerEmail')})</p>
                <p><strong>Title:</strong> {ticket.get('title')}</p>
                <p>Please review immediately.</p>
                """,
                "sent_at": event.get("escalated_at")
            }
            
            await self.mq_publisher.publish("email.send", alert_event)
            logger.info("workflow.manager_notified", ticket_id=ticket_id)
        except Exception as e:
            logger.error("workflow.escalation_failed", error=str(e))
    
    async def process_ticket_created(self, event: dict[str, Any]):
        """Handle ticket.created event: send confirmation."""
        ticket_id = event.get("ticket_id")
        customer_email = event.get("customer_email")
        customer_name = event.get("customer_name")
        title = event.get("title")
        
        logger.info("workflow.ticket_created", ticket_id=ticket_id, customer=customer_email)
        
        if not self.mq_publisher.channel:
            logger.error("workflow.mq_not_connected", event="ticket.created")
            return
        
        email_event = {
            "event_type": "email.send",
            "to_email": customer_email,
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
            "sent_at": event.get("created_at")
        }
        
        try:
            await self.mq_publisher.publish("email.send", email_event)
            logger.info("workflow.confirmation_sent", to=customer_email, ticket_id=ticket_id)
        except Exception as e:
            logger.error("workflow.confirmation_failed", error=str(e))
    
    def handle_message(self, ch, method, properties, body):
        """Callback for RabbitMQ messages."""
        global _in_flight_messages
        
        # Ensure loop is available (set in run() before consuming starts)
        if not hasattr(self, '_loop') or self._loop is None:
            logger.error("consumer.no_loop_reference", routing_key=method.routing_key if method else None)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            return
        
        _in_flight_messages += 1
        try:
            event = json.loads(body)
            routing_key = method.routing_key
            
            logger.info("consumer.received", routing_key=routing_key, event_type=event.get("event_type"))
            
            # Dispatch to appropriate handler
            handlers = {
                "invoice.paid": self.process_invoice_paid,
                "invoice.sent": self.process_invoice_sent,
                "ticket.escalated": self.process_ticket_escalated,
                "ticket.created": self.process_ticket_created,
            }
            
            handler = handlers.get(routing_key)
            if handler:
                # Schedule async handler in the main event loop
                # self._loop is set in run() method
                asyncio.run_coroutine_threadsafe(handler(event), self._loop)
            else:
                logger.warning("consumer.unhandled_routing_key", key=routing_key)
            
            ch.basic_ack(delivery_tag=method.delivery_tag)
            
        except Exception as e:
            logger.error("consumer.message_failed", error=str(e))
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        finally:
            _in_flight_messages -= 1
    
    async def run(self):
        """Main consumer loop."""
        global running
        
        # Store reference to event loop for thread-safe callback scheduling
        self._loop = asyncio.get_running_loop()
        
        # Verify the loop is set before any message can arrive
        if self._loop is None:
            raise RuntimeError("Failed to get event loop")
        
        queue_name = await self.connect()
        await self.mq_publisher.connect()
        
        # Set up message consumption
        def _start_consuming():
            self.channel.basic_consume(
                queue=queue_name,
                on_message_callback=self.handle_message
            )
            self.channel.start_consuming()
        
        # Run consumer in executor thread
        consumer_task = self._loop.run_in_executor(None, _start_consuming)
        
        logger.info("consumer.started", queue=queue_name)
        
        # Wait for shutdown signal
        while running:
            await asyncio.sleep(0.1)
        
        # Stop consuming
        def _stop():
            self.channel.stop_consuming()
        
        await self._loop.run_in_executor(None, _stop)
        
        try:
            await consumer_task
        except Exception:
            pass
    
    async def close(self):
        """Cleanup resources."""
        if self.ticket_client:
            await self.ticket_client.close()
        if self.odoo_client:
            await self.odoo_client.close()
        if self.mq_publisher:
            await self.mq_publisher.close()
        if self.connection and not self.connection.is_closed:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self.connection.close)


def signal_handler(signum, _frame):
    """Handle shutdown signals."""
    global running
    logger.info("consumer.shutdown_signal", signal=signum)
    running = False


async def main_async():
    """Async main entry point."""
    global running, _in_flight_messages
    
    # Set up signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    config = get_settings()
    consumer = EventConsumer(
        amqp_url=config.rabbitmq_url,
        ticket_api_url=config.ticket_api_url,
        odoo_api_url=config.odoo_bridge_url
    )
    
    try:
        await consumer.run()
    finally:
        # Wait for in-flight messages to complete before closing
        if _in_flight_messages > 0:
            logger.info("consumer.waiting_for_inflight", count=_in_flight_messages)
            # Brief grace period for in-flight processing
            for _ in range(50):  # 5 seconds max
                if _in_flight_messages == 0:
                    break
                await asyncio.sleep(0.1)
        
        await consumer.close()
        logger.info("consumer.stopped")


def main():
    """Run the event consumer."""
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
