"""Tests for the /api/variables dashboard routes.

Hermetic: every case redirects ``config_path`` at a ``tmp_path`` file and hands the
handler a config object directly, so nothing reads or writes the real data home
and the loader's fingerprint cache never participates.
"""

from __future__ import annotations

import inspect
import json
import os
import stat
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, WorkspaceConfig
from kiro_crew.dashboard.handlers import variables as vh

_NOT_POSIX = os.name == "nt"

# Every test here awaits a handler directly.
pytestmark = pytest.mark.asyncio


def _request(method: str, body: Any = ...):
    """A mocked request. ``body=None`` models a malformed payload, which is what
    the handler's ``except Exception -> 400`` branch is written for."""
    req = make_mocked_request(method, "/api/variables")
    if body is None:
        req.json = AsyncMock(side_effect=ValueError("not json"))  # type: ignore[method-assign]
    elif body is not ...:
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _config() -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.variables = {"baseUrl": "https://global.test", "orgName": "Acme"}
    cfg.workspaces = {
        "default": WorkspaceConfig(dir="workspace"),
        "ops": WorkspaceConfig(dir="workspace-ops", variables={"baseUrl": "https://ops.test"}),
    }
    cfg.default_workspace = "default"
    cfg.agents = {
        "crew1": KiroCrewAgentConfig(
            kiro_agent="kirocrew", workspace="ops", variables={"queue": "oncall"}
        )
    }
    cfg.default_agent = "crew1"
    return cfg


@pytest.fixture()
def wired(monkeypatch, tmp_path: Path):
    """Redirect the handler at a temp config file and a fixed config object."""
    cfg = _config()
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"workspaces": {"ops": {"dir": "workspace-ops"}}}), encoding="utf-8")
    monkeypatch.setattr(vh, "config_path", lambda: path)
    monkeypatch.setattr(vh.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    return cfg, path


async def test_get_reports_every_scope(wired):
    cfg, _ = wired
    resp = await vh.api_variables(_request("GET"))
    assert resp.status == 200
    payload = json.loads(resp.text)
    assert payload["global"] == {"baseUrl": "https://global.test", "orgName": "Acme"}
    assert payload["workspaces"]["ops"] == {"baseUrl": "https://ops.test"}
    assert payload["crews"]["crew1"] == {"queue": "oncall"}


async def test_get_reports_resolution_and_provenance(wired):
    resp = await vh.api_variables(_request("GET"))
    payload = json.loads(resp.text)
    # crew1 binds workspace ops, so the workspace value wins over global.
    assert payload["effective"]["baseUrl"] == "https://ops.test"
    assert payload["winning_scope"]["baseUrl"] == "workspace"
    assert payload["shadowed"]["baseUrl"] == ["global"]
    assert payload["effective"]["queue"] == "oncall"
    assert payload["winning_scope"]["queue"] == "crew"
    assert payload["active_workspace"] == "ops"
    assert payload["active_agent"] == "crew1"


async def test_put_global_persists(wired):
    _, path = wired
    resp = await vh.api_variables(
        _request("PUT", {"scope": "global", "values": {"a": "1", "b": ""}})
    )
    assert resp.status == 200
    assert json.loads(resp.text)["ok"] is True
    assert json.loads(path.read_text(encoding="utf-8"))["variables"] == {"a": "1", "b": ""}


async def test_put_is_a_whole_scope_replace(wired):
    """Absence from values is how a pair is deleted; there is no unset verb."""
    _, path = wired
    await vh.api_variables(_request("PUT", {"scope": "global", "values": {"a": "1", "b": "2"}}))
    await vh.api_variables(_request("PUT", {"scope": "global", "values": {"a": "1"}}))
    assert json.loads(path.read_text(encoding="utf-8"))["variables"] == {"a": "1"}


async def test_put_workspace_persists_under_that_workspace(wired):
    _, path = wired
    resp = await vh.api_variables(
        _request("PUT", {"scope": "workspace", "workspace": "ops", "values": {"queue": "tier2"}})
    )
    assert resp.status == 200
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["workspaces"]["ops"]["variables"] == {"queue": "tier2"}
    assert data["workspaces"]["ops"]["dir"] == "workspace-ops"


async def test_put_widens_a_legacy_flat_workspace_entry(monkeypatch, tmp_path: Path):
    """The flat form maps a name straight to its directory; assigning a key onto
    that string would raise, and replacing it would lose the directory."""
    cfg = _config()
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"workspaces": {"ops": "legacy-dir"}}), encoding="utf-8")
    monkeypatch.setattr(vh, "config_path", lambda: path)
    monkeypatch.setattr(vh.KiroCrewConfig, "load", classmethod(lambda cls: cfg))

    resp = await vh.api_variables(
        _request("PUT", {"scope": "workspace", "workspace": "ops", "values": {"a": "1"}})
    )
    assert resp.status == 200
    entry = json.loads(path.read_text(encoding="utf-8"))["workspaces"]["ops"]
    assert entry == {"dir": "legacy-dir", "variables": {"a": "1"}}


async def test_put_preserves_unrelated_config_keys(monkeypatch, tmp_path: Path):
    cfg = _config()
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"agent": {"model": "auto"}, "workspaces": {"ops": {"dir": "d"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(vh, "config_path", lambda: path)
    monkeypatch.setattr(vh.KiroCrewConfig, "load", classmethod(lambda cls: cfg))

    await vh.api_variables(_request("PUT", {"scope": "global", "values": {"a": "1"}}))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["agent"] == {"model": "auto"}
    assert data["workspaces"]["ops"] == {"dir": "d"}


@pytest.mark.skipif(_NOT_POSIX, reason="POSIX file modes")
async def test_put_preserves_existing_file_permissions(monkeypatch, tmp_path: Path):
    """Widening an operator's tightened config.json on an unrelated save would be
    a silent downgrade."""
    cfg = _config()
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr(vh, "config_path", lambda: path)
    monkeypatch.setattr(vh.KiroCrewConfig, "load", classmethod(lambda cls: cfg))

    await vh.api_variables(_request("PUT", {"scope": "global", "values": {"a": "1"}}))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


class TestRejections:
    """Every non-2xx body carries a machine-readable ``code`` — the dashboard
    renders server prose verbatim, so the identifier is what a client switches on."""

    async def test_malformed_json(self, wired):
        resp = await vh.api_variables(_request("PUT", None))
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "variables_invalid_json"

    async def test_non_object_body(self, wired):
        resp = await vh.api_variables(_request("PUT", ["not", "an", "object"]))
        assert json.loads(resp.text)["code"] == "variables_invalid_body"

    async def test_unknown_scope(self, wired):
        resp = await vh.api_variables(_request("PUT", {"scope": "crew", "values": {}}))
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "variables_invalid_scope"

    async def test_missing_values(self, wired):
        resp = await vh.api_variables(_request("PUT", {"scope": "global"}))
        assert json.loads(resp.text)["code"] == "variables_invalid_values"

    async def test_unknown_workspace(self, wired):
        resp = await vh.api_variables(
            _request("PUT", {"scope": "workspace", "workspace": "nope", "values": {}})
        )
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "variables_unknown_workspace"

    async def test_invalid_name_names_the_key(self, wired):
        resp = await vh.api_variables(
            _request("PUT", {"scope": "global", "values": {"1bad": "x"}})
        )
        assert resp.status == 400
        payload = json.loads(resp.text)
        assert payload["code"] == "variables_invalid_pair"
        assert payload["key"] == "1bad"

    async def test_reserved_name_is_refused(self, wired):
        resp = await vh.api_variables(
            _request("PUT", {"scope": "global", "values": {"MAX_SUBAGENTS": "9"}})
        )
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "variables_invalid_pair"

    async def test_control_character_is_refused(self, wired):
        resp = await vh.api_variables(
            _request("PUT", {"scope": "global", "values": {"a": "one\ntwo"}})
        )
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "variables_invalid_pair"

    async def test_a_rejected_write_persists_nothing(self, wired):
        _, path = wired
        before = path.read_text(encoding="utf-8")
        await vh.api_variables(_request("PUT", {"scope": "global", "values": {"1bad": "x"}}))
        assert path.read_text(encoding="utf-8") == before


async def test_payload_satisfies_the_frontend_interface(wired):
    """The panel reads a typed `VariablesView`; a field renamed on one side and
    not the other type-checks fine and fails only in a browser."""
    client = (
        Path(__file__).resolve().parents[1] / "website" / "src" / "api" / "client.ts"
    ).read_text(encoding="utf-8")
    start = client.index("export interface VariablesView")
    block = client[start : client.index("}", start)]
    declared = {
        line.split(":")[0].strip()
        for line in block.splitlines()[1:]
        if ":" in line and not line.strip().startswith(("/*", "*", "//"))
    }
    assert declared, "could not parse VariablesView — the interface moved or was renamed"

    resp = await vh.api_variables(_request("GET"))
    payload = json.loads(resp.text)
    missing = declared - set(payload)
    assert not missing, f"handler payload is missing fields the panel reads: {sorted(missing)}"


class TestTheWriteIsLockedAndOffLoop:
    """A whole-file replacement must not race another config writer, and must not
    block the gateway's event loop."""

    async def test_the_mutation_goes_through_the_locked_helper(self, wired):
        _, path = wired
        seen: dict[str, object] = {}

        def _fake(target, *, mutate, **kwargs):
            seen["path"] = target
            data = {"agent": {"model": "auto"}}
            seen["result"] = mutate(data)
            return data

        with patch.object(vh, "update_config_locked", _fake) as _:
            resp = await vh.api_variables(
                _request("PUT", {"scope": "global", "values": {"a": "1"}})
            )
        assert resp.status == 200
        assert seen["path"] == path
        # The callback applied the scope write to the dict the lock handed it,
        # rather than to a copy read before the lock was taken.
        assert seen["result"]["variables"] == {"a": "1"}
        assert seen["result"]["agent"] == {"model": "auto"}

    async def test_the_locked_helper_runs_off_the_event_loop(self, wired):
        """It reads, locks and fsyncs; on the loop that freezes every task."""
        source = inspect.getsource(vh)
        assert "asyncio.to_thread(update_config_locked" in source
        # The unlocked forms must be gone.
        assert "write_config_atomically" not in source
        assert "path.read_text" not in source

    async def test_a_corrupt_config_fails_closed_without_writing(self, wired):
        def _raise(*_args, **_kwargs):
            raise vh.ConfigReadError("bad json")

        with patch.object(vh, "update_config_locked", _raise):
            resp = await vh.api_variables(
                _request("PUT", {"scope": "global", "values": {"a": "1"}})
            )
        assert resp.status == 500
        assert json.loads(resp.text)["code"] == "config_corrupt"

    async def test_a_workspace_write_carries_the_known_directory(self, wired):
        """The fallback dir is captured BEFORE the lock, so the callback stays a
        pure function of already-resolved state."""
        captured: dict[str, object] = {}

        def _fake(_target, *, mutate, **_kwargs):
            data: dict = {}
            captured["result"] = mutate(data)
            return data

        with patch.object(vh, "update_config_locked", _fake):
            resp = await vh.api_variables(
                _request(
                    "PUT",
                    {"scope": "workspace", "workspace": "ops", "values": {"q": "1"}},
                )
            )
        assert resp.status == 200
        entry = captured["result"]["workspaces"]["ops"]
        assert entry["variables"] == {"q": "1"}
        assert entry["dir"] == "workspace-ops"
