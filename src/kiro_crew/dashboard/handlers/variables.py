"""Dashboard handlers for the crew-variables cascade.

``GET`` reports every scope's own pairs plus the resolved map and where each
winning value came from. ``PUT`` replaces ONE scope's pairs wholesale rather than
patching single keys: deleting a pair is then just its absence from ``values``,
with no second verb and no ambiguity between "unset" and "set to empty string" —
an empty string is a legal value that still overrides a broader scope, so the two
cannot share an encoding.

Validation refuses rather than drops. The config loader deliberately drops a bad
pair with a warning so one hand-edited mistake cannot cost the rest of a scope or
fail a load, but a dashboard write is interactive: silently discarding a pair the
user just typed would look like a save that worked.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.config.loader import (
    ConfigReadError,
    KiroCrewConfig,
    config_path,
    resolve_variables,
    update_config_locked,
)
from kiro_crew.sel import sel
from kiro_crew.variables import validate_pair

logger = logging.getLogger(__name__)

SCOPE_GLOBAL = "global"
SCOPE_WORKSPACE = "workspace"
_WRITABLE_SCOPES = (SCOPE_GLOBAL, SCOPE_WORKSPACE)


def _view(cfg: KiroCrewConfig) -> dict:
    """Every scope's own pairs, plus the resolved map for the active context."""
    resolution = resolve_variables(cfg)
    return {
        "global": dict(cfg.variables),
        "workspaces": {name: dict(ws.variables) for name, ws in cfg.workspaces.items()},
        "crews": {name: dict(agent.variables) for name, agent in cfg.agents.items()},
        "effective": dict(resolution.values),
        "winning_scope": dict(resolution.winning_scope),
        "shadowed": {key: list(scopes) for key, scopes in resolution.shadowed.items()},
        "active_workspace": resolution.workspace_name,
        "active_agent": resolution.agent_name,
    }


async def api_variables(request: web.Request) -> web.Response:
    """GET/PUT /api/variables — read the cascade, or replace one scope."""
    if request.method != "PUT":
        return web.json_response(_view(KiroCrewConfig.load()))

    caller = request.get("user", "dashboard")

    def _deny(code: str, error: str) -> web.Response:
        """Refuse a malformed request.

        The status is a literal rather than a parameter: the error-code contract
        gate counts a computed ``status=`` separately precisely because hoisting
        it into a variable would defeat the static check, and every refusal on
        this path is a 400 anyway.
        """
        sel().log_api_access(
            caller=caller,
            operation="variables.update",
            outcome="denied",
            error=error,
        )
        return web.json_response({"error": error, "code": code}, status=400)

    try:
        body = await request.json()
    except Exception:
        return _deny("variables_invalid_json", "invalid JSON")
    if not isinstance(body, dict):
        return _deny("variables_invalid_body", "body must be an object")

    scope = body.get("scope")
    if scope not in _WRITABLE_SCOPES:
        return _deny(
            "variables_invalid_scope",
            f"scope must be one of {', '.join(_WRITABLE_SCOPES)}",
        )

    raw_values = body.get("values")
    if not isinstance(raw_values, dict):
        return _deny("variables_invalid_values", "values must be an object")

    values: dict[str, str] = {}
    for key, value in raw_values.items():
        name, outcome = validate_pair(key, value)
        if name is None:
            sel().log_api_access(
                caller=caller,
                operation="variables.update",
                outcome="denied",
                error=f"invalid variable: {outcome}",
            )
            return web.json_response(
                {"error": outcome, "code": "variables_invalid_pair", "key": str(key)},
                status=400,
            )
        values[name] = outcome

    cfg = KiroCrewConfig.load()
    workspace = body.get("workspace") or ""
    if scope == SCOPE_WORKSPACE:
        if not isinstance(workspace, str) or workspace not in cfg.workspaces:
            return _deny(
                "variables_unknown_workspace",
                f"unknown workspace: {workspace!r}",
            )

    path = config_path()
    fallback_dir = cfg.workspaces[workspace].dir if scope == SCOPE_WORKSPACE else ""

    def _mutate(data: dict) -> dict:
        """Apply this scope's replacement inside the locked critical section."""
        if scope == SCOPE_GLOBAL:
            data["variables"] = values
            return data
        workspaces = data.get("workspaces")
        if not isinstance(workspaces, dict):
            workspaces = {}
            data["workspaces"] = workspaces
        entry = workspaces.get(workspace)
        if isinstance(entry, str):
            # The legacy flat form maps a workspace name straight to its directory.
            # Widening it in place keeps the directory the operator set; assigning
            # a key onto the string would raise.
            entry = {"dir": entry}
            workspaces[workspace] = entry
        elif not isinstance(entry, dict):
            entry = {"dir": fallback_dir}
            workspaces[workspace] = entry
        entry["variables"] = values
        return data

    # update_config_locked is the required path for a new config.json mutation: it
    # holds an advisory lock across the whole read-modify-write, so a concurrent CLI
    # or dashboard write cannot land between the read and the rename and have its
    # settings deleted by this whole-file replacement. It also preserves the file's
    # permission bits. Offloaded because it reads, locks and fsyncs — blocking work
    # that must not run on the gateway event loop.
    try:
        await asyncio.to_thread(update_config_locked, path, mutate=_mutate)
    except ConfigReadError:
        sel().log_api_access(
            caller=caller,
            operation="variables.update",
            outcome="error",
            error="config.json is corrupt",
        )
        return web.json_response(
            {"error": "config.json is corrupt", "code": "config_corrupt"}, status=500
        )

    sel().log_api_access(
        caller=caller,
        operation="variables.update",
        outcome="ok",
        resources=f"{scope}:{workspace}" if scope == SCOPE_WORKSPACE else scope,
    )
    return web.json_response({"ok": True, **_view(KiroCrewConfig.load())})
