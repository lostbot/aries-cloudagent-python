"""
The Conductor.

The conductor is responsible for coordinating messages that are received
over the network, communicating with the ledger, passing messages to handlers,
instantiating concrete implementations of required modules and storing data in the
wallet.

"""

import asyncio
import hashlib
import logging

from .admin.base_server import BaseAdminServer
from .admin.server import AdminServer
from .config.default_context import ContextBuilder
from .config.injection_context import InjectionContext
from .config.ledger import ledger_config
from .config.logging import LoggingConfigurator
from .config.wallet import wallet_config
from .dispatcher import Dispatcher
from .protocols.connections.manager import ConnectionManager, ConnectionManagerError
from .messaging.responder import BaseResponder
from .messaging.task_queue import TaskQueue
from .stats import Collector
from .transport.inbound.manager import InboundTransportManager
from .transport.inbound.message import InboundMessage
from .transport.inbound.session import InboundSession
from .transport.outbound.base import OutboundDeliveryError
from .transport.outbound.manager import OutboundTransportManager
from .transport.outbound.message import OutboundMessage

LOGGER = logging.getLogger(__name__)


class Conductor:
    """
    Conductor class.

    Class responsible for initializing concrete implementations
    of our require interfaces and routing inbound and outbound message data.
    """

    def __init__(self, context_builder: ContextBuilder) -> None:
        """
        Initialize an instance of Conductor.

        Args:
            inbound_transports: Configuration for inbound transports
            outbound_transports: Configuration for outbound transports
            settings: Dictionary of various settings

        """
        self.admin_server = None
        self.context: InjectionContext = None
        self.context_builder = context_builder
        self.dispatcher: Dispatcher = None
        self.inbound_transport_manager: InboundTransportManager = None
        self.outbound_transport_manager: OutboundTransportManager = None

    async def setup(self):
        """Initialize the global request context."""

        context = await self.context_builder.build()

        self.dispatcher = Dispatcher(context)

        # Register all inbound transports
        self.inbound_transport_manager = InboundTransportManager(
            context, self.inbound_message_router
        )
        await self.inbound_transport_manager.setup()

        # Register all outbound transports
        self.outbound_transport_manager = OutboundTransportManager(
            context, self.dispatcher.run_task
        )
        await self.outbound_transport_manager.setup()

        # Admin API
        if context.settings.get("admin.enabled"):
            try:
                admin_host = context.settings.get("admin.host", "0.0.0.0")
                admin_port = context.settings.get("admin.port", "80")
                self.admin_server = AdminServer(
                    admin_host,
                    admin_port,
                    context,
                    self.outbound_message_router,
                    self.dispatcher.put_task,
                )
                webhook_urls = context.settings.get("admin.webhook_urls")
                if webhook_urls:
                    for url in webhook_urls:
                        self.admin_server.add_webhook_target(url)
                context.injector.bind_instance(BaseAdminServer, self.admin_server)
            except Exception:
                LOGGER.exception("Unable to register admin server")
                raise

        # Fetch stats collector, if any
        collector = await context.inject(Collector, required=False)
        if collector:
            # add stats to our own methods
            collector.wrap(
                self,
                (
                    # "inbound_message_router",
                    "outbound_message_router",
                    # "create_inbound_session",
                ),
            )
            collector.wrap(self.dispatcher, "handle_message")
            # at the class level (!) should not be performed multiple times
            collector.wrap(
                ConnectionManager,
                (
                    "get_connection_targets",
                    "fetch_did_document",
                    "find_message_connection",
                ),
            )

        self.context = context

    async def start(self) -> None:
        """Start the agent."""

        context = self.context

        # Configure the wallet
        public_did = await wallet_config(context)

        # Configure the ledger
        await ledger_config(context, public_did)

        # Start up transports
        try:
            await self.inbound_transport_manager.start()
        except Exception:
            LOGGER.exception("Unable to start inbound transports")
            raise
        try:
            await self.outbound_transport_manager.start()
        except Exception:
            LOGGER.exception("Unable to start outbound transports")
            raise

        # asyncio.get_event_loop().create_task(self.log_status())

        # Start up Admin server
        if self.admin_server:
            try:
                await self.admin_server.start()
            except Exception:
                LOGGER.exception("Unable to start administration API")
            # Make admin responder available during message parsing
            # This allows webhooks to be called when a connection is marked active,
            # for example
            context.injector.bind_instance(BaseResponder, self.admin_server.responder)

        # Get agent label
        default_label = context.settings.get("default_label")

        # Show some details about the configuration to the user
        LoggingConfigurator.print_banner(
            default_label,
            self.inbound_transport_manager.registered_transports,
            self.outbound_transport_manager.registered_transports.values(),
            public_did,
            self.admin_server,
        )

        # Create a static connection for use by the test-suite
        if context.settings.get("debug.test_suite_endpoint"):
            mgr = ConnectionManager(self.context)
            their_endpoint = context.settings["debug.test_suite_endpoint"]
            test_conn = await mgr.create_static_connection(
                my_seed=hashlib.sha256(b"aries-protocol-test-subject").digest(),
                their_seed=hashlib.sha256(b"aries-protocol-test-suite").digest(),
                their_endpoint=their_endpoint,
                their_role="tester",
                alias="test-suite",
            )
            print("Created static connection for test suite")
            print(" - My DID:", test_conn.my_did)
            print(" - Their DID:", test_conn.their_did)
            print(" - Their endpoint:", their_endpoint)
            print()

        # Print an invitation to the terminal
        if context.settings.get("debug.print_invitation"):
            try:
                mgr = ConnectionManager(self.context)
                _connection, invitation = await mgr.create_invitation(
                    their_role=context.settings.get("debug.invite_role"),
                    my_label=context.settings.get("debug.invite_label"),
                    multi_use=context.settings.get("debug.invite_multi_use", False),
                    public=context.settings.get("debug.invite_public", False),
                )
                base_url = context.settings.get("invite_base_url")
                invite_url = invitation.to_url(base_url)
                print("Invitation URL:")
                print(invite_url)
            except Exception:
                LOGGER.exception("Error creating invitation")

    async def stop(self, timeout=1.0):
        """Stop the agent."""
        shutdown = TaskQueue()
        if self.admin_server:
            shutdown.run(self.admin_server.stop())
        if self.inbound_transport_manager:
            shutdown.run(self.inbound_transport_manager.stop())
        if self.outbound_transport_manager:
            shutdown.run(self.outbound_transport_manager.stop())
        await shutdown.complete(timeout)

    def inbound_message_router(self, message: InboundMessage):
        """
        Route inbound messages.

        Args:
            message: The inbound message instance

        """

        if (
            message.receipt.direct_response_requested
            and message.receipt.direct_response_requested
            != InboundSession.REPLY_MODE_NONE
        ):
            LOGGER.warning(
                "Direct response requested, but not supported by transport: %s",
                message.transport_type,
            )

        # Note: at this point we could send the message to a shared queue
        # if this pod is too busy to process it

        self.dispatcher.queue_message(
            message,
            self.outbound_message_router,
            lambda task, exc_info: self.inbound_transport_manager.dispatch_complete(
                message, task, exc_info
            ),
        )

    async def log_status(self):
        while True:
            await asyncio.sleep(5.0)
            e = 0
            p = 0
            t = 0
            for m in self.outbound_buffer:
                if m.state == m.STATE_ENCODE:
                    e += 1
                if m.state == m.STATE_DELIVER:
                    p += 1
                t += 1
            s = len(self.inbound_sessions)
            r = self.dispatcher.task_queue.active
            q = self.dispatcher.task_queue.pending
            print(
                f"{s:>4} sess  {r:>4} run  {q:>4} que  "
                f"{e:>4} pack  {p:>4} send  {t:>4} out"
            )

    async def outbound_message_router(
        self,
        context: InjectionContext,
        outbound: OutboundMessage,
        inbound: InboundMessage = None,
    ) -> None:
        """
        Route an outbound message.

        Args:
            context: The request context
            message: An outbound message to be sent
            inbound: The inbound message that produced this response, if available
        """

        # if inbound and inbound.direct_response:
        #     if outbound.reply_to_verkey

        # FIXME - use dispatch task
        # always populate connection targets using provided context
        if not outbound.target and not outbound.target_list and outbound.connection_id:
            mgr = ConnectionManager(context)
            try:
                outbound.target_list = await mgr.get_connection_targets(
                    connection_id=outbound.connection_id
                )
            except ConnectionManagerError:
                LOGGER.exception("Error preparing outbound message for transmission")
                return

        try:
            self.outbound_transport_manager.deliver(context, outbound)
        except OutboundDeliveryError:
            # Add message to outbound queue, indexed by key
            # if self.undelivered_queue:
            #     self.undelivered_queue.add_message(message)

            LOGGER.warning("Cannot queue message for delivery, no supported transport")
            return  # drop message
