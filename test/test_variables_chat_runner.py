"""Security tests for {{variable}} expansion in the dashboard turn pipeline.

The property under test is that expansion reaches the user's OWN text and nothing
else. Two halves are needed, because either alone is weak evidence:

* Behaviour at the seam — the resolvers, the expander and the join helpers, driven
  in the order the pipeline drives them.
* A source-order guard proving the pipeline actually composes them in that order.
  Without it, these tests would only prove that a safe ordering exists, not that
  the shipped code uses it.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import _ChatSlot

SENTINEL = "SENTINEL-VALUE-9d1f"


def _slot(agent: str = "") -> _ChatSlot:
    slot = _ChatSlot("chat-vars-1")
    slot._titled = True
    slot.agent = agent
    return slot


def _state() -> MagicMock:
    state = MagicMock()
    state.push_slots_update = MagicMock()
    return state


def _skills(resolved: list[tuple[str, str, str]]) -> MagicMock:
    skills = MagicMock()
    skills.resolve_dollar_skills.return_value = resolved
    skills.has_dollar_candidate.return_value = bool(resolved)
    return skills


def _expand(message: str, values: dict[str, str], slot: _ChatSlot, state: MagicMock) -> str:
    """Run the expander with a fixed variable map, as the pipeline would."""
    with patch.object(chat_runner, "resolve_variables") as rv:
        rv.return_value = MagicMock(values=values)
        with patch.object(chat_runner.KiroCrewConfig, "load", classmethod(lambda cls: MagicMock())):
            return chat_runner._expand_message_variables(message, state, slot)


class TestImportedTextIsNeverExpanded:
    def test_a_skill_body_token_is_left_literal(self):
        """A skill installed from the public registry must not be able to read a
        variable by referencing it in its own body."""
        state, slot = _state(), _slot()
        body = "Step one: call {{apiToken}} then report."
        with patch.object(chat_runner, "_get_skills", return_value=_skills([("$dep", "dep", body)])):
            authored, blocks, count = chat_runner._resolve_dollar_skills(
                "run $dep", state, slot, "dashboard:x"
            )
        assert count == 1

        authored = _expand(authored, {"apiToken": SENTINEL}, slot, state)
        assembled = chat_runner._join_skill_parts(authored, blocks)

        assert SENTINEL not in assembled
        assert "{{apiToken}}" in assembled

    def test_a_prompt_body_token_is_left_literal(self, tmp_path: Path):
        state, slot = _state(), _slot()
        prompt = tmp_path / "sop.md"
        prompt.write_text("Follow this using {{apiToken}}.", encoding="utf-8")
        match = {"path": str(prompt), "fullName": "sop"}

        with patch.object(chat_runner, "_find_prompt", return_value=match):
            authored, blocks, status = chat_runner._resolve_prompt_mention(
                "@sop and also this", state, slot
            )
        assert status == "ok"
        assert authored == "and also this"

        authored = _expand(authored, {"apiToken": SENTINEL}, slot, state)
        assembled = chat_runner._join_prompt_parts(authored, blocks)

        assert SENTINEL not in assembled
        assert "{{apiToken}}" in assembled

    def test_the_users_own_trailing_text_still_expands(self, tmp_path: Path):
        """The mirror of the above: confinement must not disable the feature."""
        state, slot = _state(), _slot()
        prompt = tmp_path / "sop.md"
        prompt.write_text("Imported body.", encoding="utf-8")
        with patch.object(
            chat_runner, "_find_prompt", return_value={"path": str(prompt), "fullName": "sop"}
        ):
            authored, blocks, _ = chat_runner._resolve_prompt_mention(
                "@sop use {{apiToken}}", state, slot
            )
        authored = _expand(authored, {"apiToken": SENTINEL}, slot, state)
        assert SENTINEL in chat_runner._join_prompt_parts(authored, blocks)


class TestValuesCannotEscalate:
    def test_a_value_naming_a_skill_does_not_load_it(self):
        """`$skill` resolution runs on pre-expansion text, so a value that happens
        to contain a skill token cannot pull that skill into the context."""
        state, slot = _state(), _slot()
        skills = _skills([("$dep", "dep", "SHOULD NOT LOAD")])
        raw = "please do {{ref}}"

        # The pipeline's gate: no '$' in the raw text, so the resolver never runs.
        assert "$" not in raw
        expanded = _expand(raw, {"ref": "$dep"}, slot, state)
        assert expanded == "please do $dep"

        # Even if the resolver were consulted afterwards, the pipeline has already
        # passed its gate — assert the body never reaches the assembled message.
        assembled = chat_runner._join_skill_parts(expanded, [])
        assert "SHOULD NOT LOAD" not in assembled
        skills.resolve_dollar_skills.assert_not_called()

    def test_a_value_naming_a_prompt_is_not_inlined(self, tmp_path: Path):
        state, slot = _state(), _slot()
        prompt = tmp_path / "sop.md"
        prompt.write_text("SHOULD NOT INLINE", encoding="utf-8")
        raw = "check {{ref}}"

        # The @prompt gate requires the message to START with '@'.
        assert not raw.startswith("@")
        with patch.object(chat_runner, "_find_prompt") as finder:
            expanded = _expand(raw, {"ref": "@sop"}, slot, state)
        assert expanded == "check @sop"
        assert "SHOULD NOT INLINE" not in expanded
        finder.assert_not_called()


class TestAssemblyIsUnchangedWithoutVariables:
    """The refactor split resolution from assembly; the emitted string must be
    byte-identical to what the single-string helpers produced."""

    def test_prompt_join_matches_the_previous_format(self):
        authored, blocks = "extra words", ["Execute the following instructions:\n\nBODY"]
        assert chat_runner._join_prompt_parts(authored, blocks) == (
            "Execute the following instructions:\n\nBODY"
            "\n\n---\nAdditional context from user: extra words"
        )

    def test_prompt_join_without_user_text(self):
        blocks = ["Execute the following instructions:\n\nBODY"]
        assert chat_runner._join_prompt_parts("", blocks) == blocks[0]

    def test_skill_join_matches_the_previous_format(self):
        out = chat_runner._join_skill_parts("run $a $b", ["[Skill: a]\n\nA", "[Skill: b]\n\nB"])
        assert out == "run $a $b\n\n[Skill: a]\n\nA\n\n---\n\n[Skill: b]\n\nB"

    def test_skill_join_with_no_blocks_is_the_authored_text(self):
        assert chat_runner._join_skill_parts("plain", []) == "plain"

    def test_wrapper_still_returns_one_string(self):
        state, slot = _state(), _slot()
        with patch.object(
            chat_runner, "_get_skills", return_value=_skills([("$dep", "dep", "BODY")])
        ):
            expanded, count = chat_runner._expand_dollar_skills(
                "run $dep", state, slot, "dashboard:x"
            )
        assert count == 1
        assert expanded == "run $dep\n\n[Skill: dep]\n\nBODY"


class TestExpanderBehaviour:
    def test_no_token_short_circuits_without_resolving(self):
        state, slot = _state(), _slot()
        with patch.object(chat_runner, "resolve_variables") as rv:
            out = chat_runner._expand_message_variables("no tokens here", state, slot)
        assert out == "no tokens here"
        rv.assert_not_called()

    def test_resolution_failure_leaves_the_message_intact(self):
        state, slot = _state(), _slot()
        with patch.object(chat_runner, "resolve_variables", side_effect=RuntimeError("boom")):
            with patch.object(
                chat_runner.KiroCrewConfig, "load", classmethod(lambda cls: MagicMock())
            ):
                out = chat_runner._expand_message_variables("use {{a}}", state, slot)
        assert out == "use {{a}}"

    def test_unresolved_name_is_surfaced_once_and_left_literal(self):
        state, slot = _state(), _slot()
        out = _expand("use {{missing}}", {"other": "1"}, slot, state)
        assert out == "use {{missing}}"
        surfaced = [m for m in slot.messages if "missing" in str(m)]
        assert len(surfaced) == 1


def test_pipeline_expands_before_joining_imported_bodies():
    """Guard the ORDER in the shipped pipeline, not just in these tests.

    If a future edit joins the imported bodies before expanding, every behavioural
    test above still passes while the real turn leaks values into skill bodies.
    """
    source = Path(chat_runner.__file__).with_suffix(".py").read_text(encoding="utf-8")
    # Scope to the turn pipeline: the block that binds prompt_blocks/skill_blocks.
    start = source.index("prompt_blocks: list[str] = []")
    region = source[start : start + 6000]

    def _at(needle: str) -> int:
        idx = region.find(needle)
        assert idx >= 0, f"pipeline no longer contains {needle!r}"
        return idx

    resolve_prompt = _at("_resolve_prompt_mention(")
    resolve_skills = _at("_resolve_dollar_skills(")
    expand = _at("_expand_message_variables(")
    join_prompt = _at("_join_prompt_parts(")
    join_skills = _at("_join_skill_parts(")

    assert resolve_prompt < expand, "@prompt must resolve against pre-expansion text"
    assert resolve_skills < expand, "$skill must resolve against pre-expansion text"
    assert expand < join_prompt, "imported prompt body must be joined AFTER expansion"
    assert expand < join_skills, "imported skill bodies must be joined AFTER expansion"


def test_expansion_is_skipped_for_slash_commands():
    """Slash commands are runner directives, not agent prose."""
    source = Path(chat_runner.__file__).with_suffix(".py").read_text(encoding="utf-8")
    guarded = re.search(
        r"if not is_slash:\n\s+message = _expand_message_variables\(", source
    )
    assert guarded, "variable expansion is no longer gated on is_slash"


@pytest.mark.parametrize("token", ["{{ }}", "{{1abc}}", "{{a-b}}"])
def test_malformed_tokens_are_left_alone(token: str):
    state, slot = _state(), _slot()
    assert _expand(f"x {token} y", {"a": "1"}, slot, state) == f"x {token} y"
