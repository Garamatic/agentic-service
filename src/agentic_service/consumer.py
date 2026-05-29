"""Event Consumer for Garamatic Agentic Service.

Consumes events from RabbitMQ and triggers automated notifications via
NotificationComposer + narrow ports.
"""

import asyncio
import contextlib
import json
import signal

import aio_pika
import structlog

from .clients import RabbitMQPublisher
from .notification_adapters import (
    MessageQueueEmailSender,
    ServiceEmailSender,
    ServiceTicketLookup,
)
from .notifications import NotificationComposer
from .service import GaramaticService, create_service

logger = structlog.get_logger()

# Global flag for graceful shutdown
running = True


class EventConsumer:
    """RabbitMQ event consumer with workflow automation."""

    def __init__(
        self,
        service: GaramaticService,
        amqp_url: str,
        mq_publisher: RabbitMQPublisher | None = None,
        owns_service: bool = False,
    ):
        self._service = service
        self._owns_service = owns_service
        self.amqp_url = amqp_url

        email_sender = (
            MessageQueueEmailSender(mq_publisher)
            if mq_publisher is not None
            else ServiceEmailSender(service)
        )
        self._composer = NotificationComposer(
            email_sender=email_sender,
            ticket_lookup=ServiceTicketLookup(service),
        )

        self.connection: aio_pika.RobustConnection | None = None
        self.channel: aio_pika.Channel | None = None
        self._exchange: aio_pika.Exchange | None = None
        self.queue: aio_pika.Queue | None = None

    async def connect(self) -> aio_pika.Queue:
        """Establish RabbitMQ connection and declare topology."""
        self.connection = await aio_pika.connect_robust(self.amqp_url)
        self.channel = await self.connection.channel()
        await self.channel.set_qos(prefetch_count=1)

        self._exchange = await self.channel.declare_exchange(
            "event_exchange",
            aio_pika.ExchangeType.TOPIC,
            durable=True
        )

        dlx_exchange = await self.channel.declare_exchange(
            "event_exchange.dlx",
            aio_pika.ExchangeType.TOPIC,
            durable=True
        )

        dlq_queue = await self.channel.declare_queue(
            "agentic_consumer.dlq",
            durable=True
        )
        await dlq_queue.bind(dlx_exchange, routing_key="agentic_consumer.dlq")

        self.queue = await self.channel.declare_queue(
            "agentic_consumer",
            durable=True,
            arguments={
                "x-dead-letter-exchange": "event_exchange.dlx",
                "x-dead-letter-routing-key": "agentic_consumer.dlq"
            }
        )

        bindings = [
            "event.payment.received",
            "event.invoice.created",
            "event.invoice.overdue",
            "event.ticket.resolved",
            "event.ticket.escalated",
            "event.ticket.created",
            "event.ticket.assigned",
        ]

        for routing_key in bindings:
            await self.queue.bind(self._exchange, routing_key=routing_key)

        logger.info("consumer.connected", queue=self.queue.name)
        return self.queue

    MAX_RETRIES = 3

    async def handle_message(self, message: aio_pika.IncomingMessage):
        """Process a single message with explicit ack/reject control.

        Bad JSON is acked (won't get better with retries).
        Processing failures are requeued with a retry counter; after MAX_RETRIES
        the message is dead-lettered.
        """
        retry_count = self._get_retry_count(message)

        try:
            body = json.loads(message.body)
            routing_key = message.routing_key
            event_data = body.get("data") if "data" in body else body

            logger.info("consumer.received", routing_key=routing_key, retry_count=retry_count)

            normalized_key = routing_key.removeprefix("event.")
            result = await self._composer.notify(normalized_key, event_data)
            logger.info(
                "notification.result",
                routing_key=result.routing_key,
                status=result.status,
                reason=result.reason,
            )
            await message.ack()
        except json.JSONDecodeError as e:
            logger.error("consumer.invalid_json", error=str(e))
            await message.ack()
        except Exception as e:
            logger.error("consumer.message_failed", error=str(e), retry_count=retry_count)
            await self._handle_retry(message, retry_count)

    def _get_retry_count(self, message: aio_pika.IncomingMessage) -> int:
        if message.headers and "x-retry-count" in message.headers:
            return int(message.headers["x-retry-count"])
        return 0

    async def _handle_retry(self, message: aio_pika.IncomingMessage, retry_count: int):
        if retry_count >= self.MAX_RETRIES:
            await message.reject(requeue=False)
            logger.error(
                "consumer.dead_lettered",
                routing_key=message.routing_key,
                retry_count=retry_count,
                max_retries=self.MAX_RETRIES,
            )
            return

        new_headers = dict(message.headers or {})
        new_headers["x-retry-count"] = retry_count + 1

        try:
            await self._exchange.publish(
                aio_pika.Message(
                    body=message.body,
                    headers=new_headers,
                    content_type=message.content_type,
                    delivery_mode=message.delivery_mode,
                    correlation_id=message.correlation_id,
                    message_id=message.message_id,
                ),
                routing_key=message.routing_key,
            )
            await message.ack()
            logger.warning(
                "consumer.requeued",
                routing_key=message.routing_key,
                retry_count=retry_count + 1,
                max_retries=self.MAX_RETRIES,
            )
        except Exception as repub_ex:
            logger.error(
                "consumer.republish_failed",
                routing_key=message.routing_key,
                error=str(repub_ex),
                retry_count=retry_count,
            )
            await message.reject(requeue=False)

    async def run(self):
        """Main consumer loop."""
        global running

        queue = await self.connect()
        logger.info("consumer.started", queue=queue.name)

        async with queue.iterator() as queue_iter:
            async for message in queue_iter:
                if not running:
                    break
                await self.handle_message(message)

    async def close(self):
        if self.connection:
            try:
                await self.connection.close()
            except Exception:
                pass
        if self._owns_service and self._service:
            try:
                await self._service.close()
            except Exception:
                pass


def signal_handler(signum, frame):
    """Handle shutdown signals."""
    global running
    logger.info("consumer.shutdown_signal", signal=signum)
    running = False


async def main_async():
    """Async main entry point."""
    from .config import get_settings

    config = get_settings()
    service = await create_service()

    consumer = EventConsumer(
        service=service,
        amqp_url=config.rabbitmq_url,
        owns_service=True,
    )

    # Set up signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler, sig, None)

    try:
        await consumer.run()
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        await consumer.close()
        logger.info("consumer.stopped")


def main():
    """Run the event consumer."""
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
