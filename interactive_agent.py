import asyncio
import logging
from typing import Any

from nexus.adapters.notifications.interactive import InteractiveClientPlugin
from nexus.adapters.registry import AdapterRegistry

from src.handlers.chat_command_handlers import (
    chat_agents_handler,
    chat_menu_handler,
)
from src.handlers.hands_free_routing_handler import hands_free_handler
from src.handlers.issue_command_handlers import (
    assign_handler,
    comments_handler,
    implement_handler,
    myissues_handler,
    prepare_handler,
    respond_handler,
    track_handler,
    tracked_handler,
    untrack_handler,
)
from src.handlers.monitoring_command_handlers import (
    active_handler,
    fuse_handler,
    logs_handler,
    logsfull_handler,
    status_handler,
    tail_handler,
    tailstop_handler,
)
from src.handlers.ops_command_handlers import (
    agents_handler,
    audit_handler,
    direct_handler,
    stats_handler,
)
from src.handlers.visualize_command_handlers import visualize_handler
from src.handlers.workflow_command_handlers import (
    continue_handler,
    forget_handler,
    kill_handler,
    pause_handler,
    reconcile_handler,
    reprocess_handler,
    resume_handler,
    stop_handler,
    wfstate_handler,
)
from src.interactive_context import InteractiveContext, InteractiveMessage
from src.telegram_bot import (
    _callback_handler_deps,
    _hands_free_routing_handler_deps,
    _issue_handler_deps,
    _monitoring_handler_deps,
    _ops_handler_deps,
    _visualize_handler_deps,
    _workflow_handler_deps,
    cancel,
    help_handler,
    menu_handler,
    progress_handler,
    rename_handler,
    start_handler,
)

logger = logging.getLogger(__name__)

async def _wrap_command(handler: Any, client: InteractiveClientPlugin, msg: InteractiveMessage, deps_factory=None):
    ctx = InteractiveContext(
        client=client,
        user_id=msg.user_id,
        text=msg.text,
        args=msg.args,
        raw_event=msg.raw_event,
        user_state={}
    )
    if deps_factory:
        deps = deps_factory()
        await handler(ctx, deps)
    else:
        await handler(ctx)

async def _wrap_callback(handler: Any, client: InteractiveClientPlugin, msg: InteractiveMessage, deps_factory=None):
    # Callbacks don't have text args but have callback data.
    ctx = InteractiveContext(
        client=client,
        user_id=msg.user_id,
        text=msg.text,
        args=msg.args,
        raw_event=msg.raw_event,
        user_state={}
    )
    if deps_factory:
        deps = deps_factory()
        await handler(ctx, deps)
    else:
        await handler(ctx)

async def _wrap_message(handler: Any, client: InteractiveClientPlugin, msg: InteractiveMessage, deps_factory=None):
    ctx = InteractiveContext(
        client=client,
        user_id=msg.user_id,
        text=msg.text,
        args=msg.args,
        raw_event=msg.raw_event,
        user_state={}
    )
    if deps_factory:
        deps = deps_factory()
        await handler(ctx, deps)
    else:
        await handler(ctx)

async def run_interactive_agent():
    logger.info("Starting interactive agents...")
    registry = AdapterRegistry()
    registry.load_from_config("project_config.yaml")

    interactive_plugins = registry.get_plugins_by_kind("INTERACTIVE_CLIENT")
    
    if not interactive_plugins:
        logger.warning("No INTERACTIVE_CLIENT plugins found in config.")
        return

    for name, plugin in interactive_plugins.items():
        logger.info(f"Binding commands for interactive plugin: {name}")
        
        # We need to correctly map commands from telegram_bot to our new wrapper.
        commands = [
            ("start", start_handler, None),
            ("help", help_handler, None),
            ("menu", menu_handler, None),
            ("rename", rename_handler, None),
            ("cancel", cancel, None),
            ("status", status_handler, _callback_handler_deps),
            ("active", active_handler, _monitoring_handler_deps),
            ("progress", progress_handler, None),
            ("track", track_handler, _issue_handler_deps),
            ("tracked", tracked_handler, _issue_handler_deps),
            ("untrack", untrack_handler, _issue_handler_deps),
            ("myissues", myissues_handler, _issue_handler_deps),
            ("logs", logs_handler, _monitoring_handler_deps),
            ("logsfull", logsfull_handler, _monitoring_handler_deps),
            ("tail", tail_handler, _monitoring_handler_deps),
            ("tailstop", tailstop_handler, _monitoring_handler_deps),
            ("fuse", fuse_handler, _monitoring_handler_deps),
            ("audit", audit_handler, _ops_handler_deps),
            ("wfstate", wfstate_handler, _workflow_handler_deps),
            ("visualize", visualize_handler, _visualize_handler_deps),
            ("stats", stats_handler, _ops_handler_deps),
            ("comments", comments_handler, _issue_handler_deps),
            ("reprocess", reprocess_handler, _workflow_handler_deps),
            ("reconcile", reconcile_handler, _workflow_handler_deps),
            ("continue", continue_handler, _workflow_handler_deps),
            ("forget", forget_handler, _workflow_handler_deps),
            ("kill", kill_handler, _workflow_handler_deps),
            ("pause", pause_handler, _workflow_handler_deps),
            ("resume", resume_handler, _workflow_handler_deps),
            ("stop", stop_handler, _workflow_handler_deps),
            ("agents", agents_handler, _ops_handler_deps),
            ("direct", direct_handler, _ops_handler_deps),
            ("respond", respond_handler, _issue_handler_deps),
            ("assign", assign_handler, _issue_handler_deps),
            ("implement", implement_handler, _issue_handler_deps),
            ("prepare", prepare_handler, _issue_handler_deps),
            ("chat", chat_menu_handler, None),
            ("chatagents", chat_agents_handler, None),
        ]
        
        for cmd_name, handler, deps_factory in commands:
            plugin.register_command_handler(cmd_name, lambda msg, h=handler, df=deps_factory, p=plugin: _wrap_command(h, p, msg, df))

        # We'd bind the generic message router to hands_free_handler
        plugin.register_message_handler(lambda msg, h=hands_free_handler, df=_hands_free_routing_handler_deps, p=plugin: _wrap_message(h, p, msg, df))
        
        # Lifecycle start
        await plugin.start()

    logger.info("All interactive plugins started. Keeping main thread alive.")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_interactive_agent())
