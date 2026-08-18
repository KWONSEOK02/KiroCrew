"""Tests for crew-variable expansion on inbound channel text.

A channel message (Slack, Discord, Telegram, Webex, WeCom, Teams, Weixin) reaches
the agent through one shared dispatch, so the expansion lives there once. Driving
the whole dispatch would require a session store, a renderer and a turn driver, so
these cover the two things the change actually introduces: the shared resolver, and
the structural guarantee that the dispatch expands the user's text before handing it
to the context builder.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

from kiro_crew.config import loader as loader_mod
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    WorkspaceConfig,
    variable_values_for,
)
from kiro_crew.messaging import dispatch as dispatch_mod


def _config() -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.variables = {"baseUrl": "https://global.test"}
    cfg.workspaces = {"ops": WorkspaceConfig(dir="w-ops", variables={"queue": "oncall"})}
    cfg.default_workspace = "ops"
    cfg.agents = {"crew1": KiroCrewAgentConfig(workspace="ops", variables={"baseUrl": "https://crew.test"})}
    cfg.default_agent = "crew1"
    return cfg


class TestVariableValuesFor:
    def test_returns_the_effective_map(self):
        with patch.object(loader_mod.KiroCrewConfig, "load", classmethod(lambda cls: _config())):
            values = variable_values_for("crew1")
        assert values == {"baseUrl": "https://crew.test", "queue": "oncall"}

    def test_unknown_agent_falls_back_to_the_default_crew(self):
        with patch.object(loader_mod.KiroCrewConfig, "load", classmethod(lambda cls: _config())):
            assert variable_values_for("no-such-crew")["baseUrl"] == "https://crew.test"

    def test_a_broken_config_yields_an_empty_map_rather_than_raising(self):
        """Text is left unexpanded rather than failing the turn: a variable is a
        convenience, and the message is still what its author meant to send."""
        with patch.object(
            loader_mod.KiroCrewConfig, "load", classmethod(lambda cls: (_ for _ in ()).throw(OSError("boom")))
        ):
            assert variable_values_for("crew1") == {}

    def test_returns_a_copy_so_a_caller_cannot_mutate_config_state(self):
        cfg = _config()
        with patch.object(loader_mod.KiroCrewConfig, "load", classmethod(lambda cls: cfg)):
            values = variable_values_for("crew1")
        values["baseUrl"] = "mutated"
        assert cfg.agents["crew1"].variables["baseUrl"] == "https://crew.test"


class TestDispatchExpandsInboundText:
    def test_the_dispatch_resolves_and_expands_before_building_the_message(self):
        """Structural guard: the expanded text, not turn.user_text, is what gets
        handed to build_message. A future edit that passes the raw text again would
        silently switch every channel back off."""
        source = inspect.getsource(dispatch_mod)
        assert "variable_values_for(turn.agent)" in source
        expand_at = source.index("expand_variables(turn.user_text")
        # The module docstring also names ctx_builder.build_message, so anchor on
        # the call form (trailing comma) rather than the first mention.
        build_at = source.index("ctx_builder.build_message,")
        assert expand_at < build_at, "expansion must happen before build_message is called"
        # The raw text must no longer be the argument passed through.
        call = source[build_at : build_at + 200]
        assert "turn.user_text," not in call
        assert "_user_text," in call

    def test_expansion_is_skipped_when_no_variables_are_defined(self):
        """The empty-map path must not pay for a scan, and must not alter the text."""
        source = inspect.getsource(dispatch_mod)
        assert "if _vars else turn.user_text" in source

    def test_the_helper_used_is_the_shared_one(self):
        """Four sites had grown their own copy of resolve-and-swallow; the fifth
        uses the shared helper so the failure semantics cannot drift per surface."""
        assert hasattr(dispatch_mod, "variable_values_for")
        assert dispatch_mod.variable_values_for is variable_values_for


class TestSharedHelperIsWiredWhereItMatters:
    def test_expand_is_the_single_pass_implementation(self):
        """Every surface must go through the leaf expander, not a local regex."""
        from kiro_crew.variables import expand

        assert dispatch_mod.expand_variables is expand

    def test_a_value_containing_a_token_is_not_rescanned_on_the_channel_path(self):
        from kiro_crew.variables import expand

        out, unresolved = expand("{{a}}", {"a": "{{b}}", "b": "boom"})
        assert out == "{{b}}"
        assert unresolved == frozenset()

    def test_resolution_is_scoped_to_the_turns_agent(self):
        cfg = _config()
        cfg.agents["crew2"] = KiroCrewAgentConfig(
            workspace="ops", variables={"baseUrl": "https://two.test"}
        )
        with patch.object(loader_mod.KiroCrewConfig, "load", classmethod(lambda cls: cfg)):
            assert variable_values_for("crew2")["baseUrl"] == "https://two.test"
            assert variable_values_for("crew1")["baseUrl"] == "https://crew.test"


def test_turn_agent_is_the_attribute_the_dispatch_reads():
    """Guard the attribute name: `turn.agent` is what resolution keys on, and a
    rename would silently resolve the default crew for every channel."""
    turn = MagicMock()
    assert hasattr(turn, "agent")
    source = inspect.getsource(dispatch_mod)
    assert "variable_values_for(turn.agent)" in source
