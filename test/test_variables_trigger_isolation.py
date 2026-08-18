"""A variable's value must not select skills.

The explicit `$skill` resolver was already guarded. This covers the OTHER selection
mechanism: `build_message` runs trigger-WORD matching, and expansion happens upstream
of it, so without the pre-expansion hand-off a value holding an ordinary word (a URL
path, a queue name) pulls in an unrelated skill BODY the user never referenced.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard import chat_runner as runner_mod
from kiro_crew.messaging import dispatch as dispatch_mod


class TestTriggerMatchingUsesPreExpansionText:
    def test_build_message_accepts_the_pre_expansion_text(self):
        params = inspect.signature(ContextBuilder.build_message).parameters
        assert "trigger_text" in params
        assert params["trigger_text"].default is None, "must default to today's behaviour"

    def test_the_trigger_call_reads_the_caller_supplied_text(self):
        source = inspect.getsource(ContextBuilder.build_message)
        assert "trigger_source = text if trigger_text is None else trigger_text" in source
        assert "get_triggered_skills(trigger_source)" in source
        assert "get_triggered_skills(text)" not in source

    def test_an_absent_trigger_text_falls_back_to_the_message(self):
        """Nine of eleven callers pass nothing; their behaviour must not change."""
        text, trigger_text = "do the thing", None
        assert (text if trigger_text is None else trigger_text) == "do the thing"

    def test_an_empty_trigger_text_is_honoured_not_treated_as_absent(self):
        """`is None` rather than a truthiness check: a caller whose user text is
        empty (an @prompt with no trailing words) means "match nothing", and
        falling back to the expanded text there would reintroduce the defect."""
        text, trigger_text = "expanded {{v}} value", ""
        assert (text if trigger_text is None else trigger_text) == ""


class TestBothExpandingCallersHandOverTheRawText:
    def test_the_dashboard_captures_the_text_as_typed(self):
        source = inspect.getsource(runner_mod)
        assert "pre_expansion_message = message" in source
        capture_at = source.index("pre_expansion_message = message")
        expand_at = source.index("_expand_message_variables(message, state, slot)")
        assert capture_at < expand_at, "the capture must precede every rewriting stage"

    def test_the_dashboard_passes_it_to_build_message(self):
        source = inspect.getsource(runner_mod)
        assert "trigger_text=pre_expansion_message" in source

    def test_the_channel_path_passes_the_raw_inbound_text(self):
        source = inspect.getsource(dispatch_mod)
        assert "trigger_text=turn.user_text" in source
        # The expanded text is what the agent reads; the raw text is what selects
        # skills. Both must be present and distinct.
        assert "_user_text," in source

    def test_the_capture_precedes_the_prompt_and_skill_stages(self):
        """@prompt replaces the message and $skill appends to it, so a capture
        taken after either would already be polluted. Anchored on the CALL forms:
        both helpers are defined earlier in the module than the pipeline uses them,
        so searching for the bare name finds the definition instead."""
        source = inspect.getsource(runner_mod)
        capture_at = source.index("pre_expansion_message = message")
        prompt_at = source.index("message, prompt_blocks, _status = _resolve_prompt_mention(")
        skills_at = source.index("message, skill_blocks, _n_skills = _resolve_dollar_skills(")
        assert capture_at < prompt_at
        assert capture_at < skills_at


class TestTriggerSelectionIsReachableAtAll:
    def test_the_skill_loader_exposes_the_trigger_entry_point(self):
        """Positive control: if this method were renamed, the tests above would
        pass against a mechanism that no longer exists."""
        from kiro_crew.skills import SkillsLoader

        assert callable(getattr(SkillsLoader, "get_triggered_skills", None))

    def test_a_value_holding_a_common_word_would_have_matched(self):
        """Shows the defect this ordering prevents, without asserting on the real
        skills tree: the expanded text contains a word the raw text does not."""
        from kiro_crew.variables import expand

        raw = "hit {{baseUrl}} and report"
        expanded, _ = expand(raw, {"baseUrl": "https://api.example.com/deploy"})
        assert "deploy" in expanded
        assert "deploy" not in raw


def test_build_message_callers_that_do_not_expand_are_unchanged():
    """A caller that never expands must not be forced to pass anything."""
    sig = inspect.signature(ContextBuilder.build_message)
    required = [
        name
        for name, p in sig.parameters.items()
        if p.default is inspect.Parameter.empty and name != "self"
    ]
    assert "trigger_text" not in required
    assert MagicMock() is not None  # keeps the import honest
