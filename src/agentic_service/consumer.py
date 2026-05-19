"""Event Consumer for Garamatic Agentic Service.

Consumes events from RabbitMQ and triggers automated workflows via
WorkflowEngine + GaramaticService.
"""

import asyncio
import contextlib
import json
import signal

import aio_pika
import structlog

from .clients import RabbitMQPublisher
from .service import GaramaticService, create_service
from .workflows import WorkflowEngine

logger = structlog.get_logger()

# Global flag for graceful shutdown
running = True


class EventConsumer:
    """RabbitMQ event consumer with workflow automation."""

    def __init__(self, service: GaramaticService, amqp_url: str):
        self._service = service
        self._workflow_engine = WorkflowEngine(service)
        self.amqp_url = amqp_url
        self._mq_publisher: RabbitMQPublisher | None = None
        self.connection: aio_pika.RobustConnection | None = None
        self.channel: aio_pika.Channel | None = None
        self.queue: aio_pika.Queue | None = None

    async def connect(self) -> aio_pika.Queue:
        """Establish RabbitMQ connection and declare topology."""
        self.connection = await aio_pika.connect_robust(self.amqp_url)
        self.channel = await self.connection.channel()
        await self.channel.set_qos(prefetch_count=1)

        exchange = await self.channel.declare_exchange(
            "event_exchange",
            aio_pika.ExchangeType.TOPIC,
            durable=True
        )

        self.queue = await self.channel.declare_queue(
            "agentic_consumer",
            durable=True
        )

        bindings = [
            "payment.received",
            "invoice.created",
            "invoice.overdue",
            "ticket.resolved",
            "ticket.escalated",
            "ticket.created",
        ]

        for routing_key in bindings:
            await self.queue.bind(exchange, routing_key=routing_key)

        logger.info("consumer.connected", queue=self.queue.name)
        return self.queue

    async def handle_message(self, message: aio_pika.IncomingMessage):
        """Process a single message with explicit ack/reject control.

        Bad JSON is acked (won't get better with retries).
        Processing failures are requeued so they can be retried.
        """
        try:
            body = json.loads(message.body)
            routing_key = message.routing_key

            # Check for integration-contract envelope, fallback to flat object
            event_data = body.get("data") if "data" in body else body

            logger.info("consumer.received", routing_key=routing_key)

            await self._workflow_engine.handle(routing_key, event_data)
            await message.ack()
        except json.JSONDecodeError as e:
            logger.error("consumer.invalid_json", error=str(e))
            await message.ack()  # Bad JSON won't get better with retries
        except Exception as e:
            logger.error("consumer.message_failed", error=str(e))
            await message.reject(requeue=True)

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
        """Cleanup resources."""
        for resource in (self._service, self.connection):
            if resource:
                with contextlib.suppress(Exception):
                    await resource.close()


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
