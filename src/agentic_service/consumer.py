"""Event Consumer for Garamatic Agentic Service.

Consumes events from RabbitMQ and triggers automated notifications via
NotificationComposer + narrow ports.

Uses TTL-based retry queues for delayed retry instead of immediate republish.
"""

import asyncio
import contextlib
import json
import signal

import aio_pika
import structlog

from .idempotency import IdempotencyStore, idempotency_key_for_event
from .notification_adapters import MessageQueueEmailSender
from .notifications import NotificationComposer, TicketSnapshot
from .service import GaramaticService, create_service

logger = structlog.get_logger()

# Retry delay queues: [retry_count_before, exchange_name, queue_name, ttl_ms]
RETRY_QUEUES = [
    (1, "event_exchange.retry1m", "retry-1m", 60_000),  # 1st retry → 1 minute
    (2, "event_exchange.retry5m", "retry-5m", 300_000),  # 2nd retry → 5 minutes
]


# ------------------------------------------------------------------
# Inline ticket lookup adapter — narrows GaramaticService to TicketLookup port
# ------------------------------------------------------------------


class _TicketLookup:
    """Narrow adapter: GaramaticService.get_ticket() → TicketSnapshot."""

    def __init__(self, service: GaramaticService) -> None:
        self._service = service

    async def get_ticket(self, ticket_id: str) -> TicketSnapshot | None:
        try:
            ticket = await self._service.get_ticket(ticket_id)
            return TicketSnapshot(
                id=ticket.id,
                customer_email=ticket.customer_email,
                customer_name=ticket.customer_name,
                title=ticket.title,
            )
        except Exception as exc:
            logger.warning(
                "ticket_lookup.failed",
                ticket_id=ticket_id,
                error=str(exc),
            )
            return None


# Global flag for graceful shutdown
running = True


class EventConsumer:
    """RabbitMQ event consumer with workflow automation and delayed retry."""

    def __init__(
        self,
        service: GaramaticService,
        amqp_url: str,
        owns_service: bool = False,
    ):
        self._service = service
        self._owns_service = owns_service
        self.amqp_url = amqp_url

        self._composer = NotificationComposer(
            email_sender=MessageQueueEmailSender(service.mq),
            ticket_lookup=_TicketLookup(service),
        )

        self.connection: aio_pika.abc.AbstractRobustConnection | None = None
        self.channel: aio_pika.abc.AbstractChannel | None = None
        self._exchange: aio_pika.abc.AbstractExchange | None = None
        self.queue: aio_pika.abc.AbstractQueue | None = None
        self._idempotency_store = IdempotencyStore(db_path=":memory:")
        self._retry_queues: dict[int, str] = {}  # retry_count -> queue_name

    async def _declare_retry_queue(
        self,
        exchange_name: str,
        queue_name: str,
        ttl_ms: int,
        dlx_name: str,
    ) -> tuple[aio_pika.abc.AbstractExchange, aio_pika.abc.AbstractQueue]:
        """Declare a TTL-based delayed retry queue."""
        assert self.channel is not None

        retry_exchange = await self.channel.declare_exchange(
            exchange_name,
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )

        retry_queue = await self.channel.declare_queue(
            queue_name,
            durable=True,
            arguments={
                "x-message-ttl": ttl_ms,
                "x-dead-letter-exchange": dlx_name,
            },
        )
        # Bind with # to accept all routing keys
        await retry_queue.bind(retry_exchange, routing_key="#")

        logger.info(
            "consumer.retry_queue_declared",
            queue=queue_name,
            ttl_ms=ttl_ms,
            dead_letter_exchange=dlx_name,
        )
        return retry_exchange, retry_queue

    async def connect(self) -> aio_pika.abc.AbstractQueue:
        """Establish RabbitMQ connection and declare topology."""
        self.connection = await aio_pika.connect_robust(self.amqp_url)
        self.channel = await self.connection.channel()
        await self.channel.set_qos(prefetch_count=1)

        await self._idempotency_store.initialize()

        exchange_name = "event_exchange"
        self._exchange = await self.channel.declare_exchange(
            exchange_name, aio_pika.ExchangeType.TOPIC, durable=True
        )

        dlx_exchange = await self.channel.declare_exchange(
            "event_exchange.dlx", aio_pika.ExchangeType.TOPIC, durable=True
        )

        dlq_queue = await self.channel.declare_queue("agentic_consumer.dlq", durable=True)
        await dlq_queue.bind(dlx_exchange, routing_key="agentic_consumer.dlq")

        # Declare TTL-based retry queues
        for retry_count, retry_exchange_name, retry_queue_name, ttl_ms in RETRY_QUEUES:
            await self._declare_retry_queue(
                exchange_name=retry_exchange_name,
                queue_name=retry_queue_name,
                ttl_ms=ttl_ms,
                dlx_name=exchange_name,
            )
            self._retry_queues[retry_count] = retry_exchange_name

        self.queue = await self.channel.declare_queue(
            "agentic_consumer",
            durable=True,
            arguments={
                "x-dead-letter-exchange": "event_exchange.dlx",
                "x-dead-letter-routing-key": "agentic_consumer.dlq",
            },
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
            assert self._exchange is not None
            if self.queue is None:
                raise RuntimeError("Consumer not connected")
            await self.queue.bind(self._exchange, routing_key=routing_key)

        logger.info("consumer.connected", queue=self.queue.name)
        assert self.queue is not None
        return self.queue  # type: ignore[return-value]

    MAX_RETRIES = 3

    async def handle_message(self, message: aio_pika.IncomingMessage):
        """Process a single message with explicit ack/reject control.

        Bad JSON is acked (won't get better with retries).
        Processing failures are routed through TTL-based retry queues.
        After MAX_RETRIES the message is dead-lettered.
        """
        retry_count = self._get_retry_count(message)

        try:
            body = json.loads(message.body)
            routing_key = message.routing_key or ""

            if not isinstance(body, dict):
                logger.error("consumer.invalid_body", body_type=type(body).__name__)
                await message.ack()
                return

            await self._process_message(body, routing_key, message, retry_count)
        except json.JSONDecodeError as e:
            logger.error("consumer.invalid_json", error=str(e))
            await message.ack()
        except Exception as e:
            logger.error("consumer.message_failed", error=str(e), retry_count=retry_count)
            await self._handle_retry(message, retry_count)

    async def _process_message(
        self, body: dict, routing_key: str, message: aio_pika.IncomingMessage, retry_count: int
    ) -> None:
        """Process a single event with idempotency check."""
        event_data = body["data"] if "data" in body and isinstance(body["data"], dict) else body
        if not isinstance(event_data, dict):
            logger.error("consumer.invalid_event_data", data_type=type(event_data).__name__)
            await message.ack()
            return

        event_type = routing_key.removeprefix("event.")

        # Idempotency check
        ikey = idempotency_key_for_event(event_type, event_data)
        if ikey and await self._idempotency_store.is_processed(ikey):
            logger.info(
                "consumer.duplicate_skipped",
                routing_key=routing_key,
                idempotency_key=ikey,
            )
            await message.ack()
            return

        logger.info("consumer.received", routing_key=routing_key, retry_count=retry_count)

        result = await self._composer.notify(event_type, event_data)
        logger.info(
            "notification.result",
            routing_key=result.routing_key,
            status=result.status,
            reason=result.reason,
        )

        # Mark as processed on success or skipped — allow retry on failures
        if result.status in ("queued", "skipped", "unhandled"):
            if ikey:
                await self._idempotency_store.mark_processed(ikey, result.status)
            await message.ack()
        else:
            raise RuntimeError(f"Notification failed: {result.reason}")

    def _get_retry_count(self, message: aio_pika.IncomingMessage) -> int:
        if message.headers and "x-retry-count" in message.headers:
            raw = message.headers["x-retry-count"]
            try:
                return int(str(raw))
            except (ValueError, TypeError):
                return 0
        return 0

    async def _handle_retry(self, message: aio_pika.IncomingMessage, retry_count: int):
        """Route a failed message through TTL-based retry queues.

        Each retry level has a dedicated queue with increasing TTL delays:
          - 1st retry → retry-1m (60 second delay)
          - 2nd retry → retry-5m (5 minute delay)
          - 3rd retry → dead-letter queue

        The TTL queues dead-letter back to the main exchange with the
        preserved routing key, so the message lands in the main queue
        after the delay.
        """
        next_retry = retry_count + 1

        if next_retry > self.MAX_RETRIES:
            await message.reject(requeue=False)
            logger.error(
                "consumer.dead_lettered",
                routing_key=message.routing_key,
                retry_count=retry_count,
                max_retries=self.MAX_RETRIES,
            )
            return

        # Find the retry queue for this retry count
        retry_exchange_name = self._retry_queues.get(next_retry)
        if retry_exchange_name is None:
            # Fallback: immediate republish (shouldn't happen with RETRY_QUEUES configured)
            logger.warning(
                "consumer.no_retry_queue_configured",
                retry_count=next_retry,
                fallback="immediate_republish",
            )
            try:
                assert self._exchange is not None
                await self._exchange.publish(
                    aio_pika.Message(
                        body=message.body,
                        headers={**(message.headers or {}), "x-retry-count": next_retry},
                        content_type=message.content_type,
                        delivery_mode=message.delivery_mode,
                        correlation_id=message.correlation_id,
                        message_id=message.message_id,
                    ),
                    routing_key=message.routing_key or "",
                )
                await message.ack()
            except Exception as e:
                logger.error("consumer.republish_failed", error=str(e))
                await message.reject(requeue=False)
            return

        try:
            assert self.channel is not None
            retry_exchange = await self.channel.declare_exchange(
                retry_exchange_name,
                aio_pika.ExchangeType.TOPIC,
                durable=True,
            )

            await retry_exchange.publish(
                aio_pika.Message(
                    body=message.body,
                    headers={**(message.headers or {}), "x-retry-count": next_retry},
                    content_type=message.content_type,
                    delivery_mode=message.delivery_mode,
                    correlation_id=message.correlation_id,
                    message_id=message.message_id,
                ),
                routing_key=message.routing_key or "",
            )

            await message.ack()
            logger.warning(
                "consumer.queued_for_retry",
                routing_key=message.routing_key,
                retry_count=next_retry,
                retry_queue=retry_exchange_name,
            )
        except Exception as repub_ex:
            logger.error(
                "consumer.retry_publish_failed",
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
        with contextlib.suppress(Exception):
            await self.connection.close()
        if self._owns_service and self._service:
            with contextlib.suppress(Exception):
                await self._service.close()
        with contextlib.suppress(Exception):
            await self._idempotency_store.close()


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
