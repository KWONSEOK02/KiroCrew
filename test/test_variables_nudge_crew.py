"""The crew a monitor loop was armed under governs its nudge-body expansion.

Without this, a loop armed in a session bound to a non-default crew resolved the
DEFAULT crew's variables, so that crew's tokens were left literal or -- worse --
substituted with another crew's values.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

from kiro_crew.autonudge import NudgeLoop
from kiro_crew.dashboard.handlers import autonudge as nudge_mod
from kiro_crew.slack import gateway as gateway_mod


class TestLoopCarriesItsCrew:
    def test_the_field_exists_and_defaults_to_the_default_crew(self):
        assert NudgeLoop("i", "chat-1", "m").agent == ""

    def test_a_persisted_loop_without_the_field_still_loads(self):
        """The store filters raw keys through __dataclass_fields__, so a record
        written before this field takes the default rather than raising."""
        raw = {
            "id": "abc",
            "slot_key": "chat-1",
            "message": "check the PR",
            "idle_secs": 300,
            "unknown_future_key": 1,
        }
        loop = NudgeLoop(**{k: raw[k] for k in raw if k in NudgeLoop.__dataclass_fields__})
        assert loop.agent == ""
        assert loop.message == "check the PR"

    def test_add_accepts_and_stores_the_armed_crew(self):
        from kiro_crew.autonudge import AutoNudgeService

        assert "agent" in inspect.signature(AutoNudgeService.add).parameters
        assert "agent" in inspect.signature(AutoNudgeService._add_locked).parameters


class TestFirePathsPassTheCrew:
    def test_every_fire_site_passes_the_loops_crew(self):
        """A fire path that drops the argument silently reverts to the default
        crew, which is invisible in output — so pin all three sites."""
        source = inspect.getsource(gateway_mod)
        calls = source.count("render_nudge_message(loop.message, loop.stop_sentinel_path, loop.agent)")
        assert calls == 3, f"expected 3 crew-passing fire sites, found {calls}"
        assert "render_nudge_message(loop.message, loop.stop_sentinel_path)" not in source

    def test_the_renderer_still_resolves_the_stop_file_token(self):
        with patch.object(nudge_mod, "resolve_variables", side_effect=RuntimeError("no config")):
            with patch.object(
                nudge_mod.KiroCrewConfig, "load", classmethod(lambda cls: MagicMock())
            ):
                out = nudge_mod.render_nudge_message("halt at {{STOP_FILE}}", "/tmp/stop", "crew1")
        assert out == "halt at /tmp/stop"

    def test_the_crew_reaches_resolution(self):
        seen: list[str | None] = []

        def _capture(_cfg, agent_name=None):
            seen.append(agent_name)
            return MagicMock(values={})

        with patch.object(nudge_mod, "resolve_variables", _capture):
            with patch.object(
                nudge_mod.KiroCrewConfig, "load", classmethod(lambda cls: MagicMock())
            ):
                nudge_mod.render_nudge_message("body", "", "oncall")
        assert seen == ["oncall"]

    def test_an_empty_crew_resolves_the_default(self):
        seen: list[str | None] = []

        def _capture(_cfg, agent_name=None):
            seen.append(agent_name)
            return MagicMock(values={})

        with patch.object(nudge_mod, "resolve_variables", _capture):
            with patch.object(
                nudge_mod.KiroCrewConfig, "load", classmethod(lambda cls: MagicMock())
            ):
                nudge_mod.render_nudge_message("body", "", "")
        assert seen == [None], "an empty crew must resolve the default, not the literal ''"


class TestArmRecordsTheCrew:
    def test_the_chokepoint_reads_the_real_slots_attribute(self):
        """DashboardState exposes `_slots`; there is no `chat_slots` and no
        __getattr__, so the wrong name silently yields {} and every loop would
        record an empty crew while looking correct."""
        source = inspect.getsource(__import__("kiro_crew.autonudge_authz", fromlist=["x"]))
        assert 'getattr(state, "_slots", None)' in source
        assert "chat_slots" not in source

    def test_the_chokepoint_passes_the_crew_to_add(self):
        source = inspect.getsource(__import__("kiro_crew.autonudge_authz", fromlist=["x"]))
        assert "agent=armed_agent" in source
