"""Crew-variable ``{{name}}`` expansion at the three backend boundaries.

Boundaries covered here:

* the agent system prompt (``context.ContextBuilder.build_message``), for a
  built-in AND a custom agent;
* a cron job's message, at DISPATCH time only
  (``cron.build_cron_session_context``);
* a monitor-loop nudge body
  (``dashboard.handlers.autonudge.render_nudge_message``).

And the two negatives that carry the security argument: a variable named after a
reserved prompt token cannot change that token's substituted value, and a
steering file — project-scoped content that can arrive from a cloned repo — is
left byte-identical.

Hermetic: every case builds config objects in memory, patches
``KiroCrewConfig.load``, and keeps all filesystem state under ``tmp_path``. No
absolute path literal is used, so nothing anchors to a drive Windows CI does not
have.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import context as ctx_mod
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    WorkspaceConfig,
    coerce_variables,
)
from kiro_crew.context import ContextBuilder
from kiro_crew.cron import CronJob, build_cron_session_context
from kiro_crew.dashboard.handlers.autonudge import render_nudge_message
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader
from kiro_crew.variables import RESERVED_TOKENS, validate_pair

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _config(
    *,
    global_vars: dict[str, str] | None = None,
    crew_vars: dict[str, str] | None = None,
    crew: str = "mycrew",
) -> KiroCrewConfig:
    """A config carrying one crew and one workspace, plus variable layers."""
    cfg = KiroCrewConfig()
    cfg.variables = dict(global_vars or {})
    cfg.workspaces = {"default": WorkspaceConfig(dir="workspace")}
    cfg.default_workspace = "default"
    cfg.agents = {
        crew: KiroCrewAgentConfig(
            kiro_agent="kirocrew",
            workspace="default",
            variables=dict(crew_vars or {}),
        )
    }
    cfg.default_agent = crew
    return cfg


def _patch_config(monkeypatch: pytest.MonkeyPatch, cfg: KiroCrewConfig) -> None:
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda _cls: cfg))


def _builder(tmp_path) -> ContextBuilder:
    """A ContextBuilder whose every store lives under *tmp_path*."""
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path / "lessons"),
        bot_name="Kiro",
    )


def _write_builtin_prompt(monkeypatch: pytest.MonkeyPatch, tmp_path, body: str) -> None:
    """Point the built-in prompt loader at a prompt file under *tmp_path*."""
    p = tmp_path / "prompt.md"
    p.write_text(body, encoding="utf-8")
    monkeypatch.setattr(ctx_mod, "_prompt_path", lambda **_kw: p)


def _write_custom_agent(monkeypatch: pytest.MonkeyPatch, tmp_path, name: str, body: str) -> None:
    """Register a custom agent whose inline prompt is *body*."""
    agents = tmp_path / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{name}.json").write_text(
        json.dumps({"name": name, "prompt": body}), encoding="utf-8"
    )
    monkeypatch.setattr(ctx_mod, "kiro_agents_dir", lambda: agents)


# ---------------------------------------------------------------------------
# Boundary 1 — agent system prompt
# ---------------------------------------------------------------------------


class TestSystemPromptExpansion:
    def test_builtin_agent_prompt_expands(self, tmp_path, monkeypatch):
        """The built-in prompt file's {{name}} tokens resolve from config."""
        _patch_config(monkeypatch, _config(global_vars={"baseUrl": "https://ops.example"}))
        _write_builtin_prompt(monkeypatch, tmp_path, "Health check: {{baseUrl}}/health")

        msg, _ = _builder(tmp_path).build_message("hi", is_new_session=True)

        assert "Health check: https://ops.example/health" in msg
        assert "{{baseUrl}}" not in msg

    def test_custom_agent_prompt_expands(self, tmp_path, monkeypatch):
        """A CUSTOM agent's prompt is covered too.

        The custom branch loads via ``_load_agent_prompt`` and would bypass any
        expansion placed inside ``_resolve_prompt_templates``; the call site is
        where both branches converge, which is what this pins.
        """
        _patch_config(
            monkeypatch,
            _config(crew="mycrew", crew_vars={"baseUrl": "https://crew.example"}),
        )
        _write_custom_agent(monkeypatch, tmp_path, "mycrew", "Deploy target: {{baseUrl}}")

        msg, _ = _builder(tmp_path).build_message("hi", is_new_session=True, agent="mycrew")

        assert "Deploy target: https://crew.example" in msg

    def test_crew_layer_wins_in_prompt(self, tmp_path, monkeypatch):
        """The session's own crew layer overrides global for the same key."""
        _patch_config(
            monkeypatch,
            _config(
                crew="mycrew",
                global_vars={"env": "prod"},
                crew_vars={"env": "staging"},
            ),
        )
        _write_custom_agent(monkeypatch, tmp_path, "mycrew", "Env is {{env}}.")

        msg, _ = _builder(tmp_path).build_message("hi", is_new_session=True, agent="mycrew")

        assert "Env is staging." in msg

    def test_unknown_token_left_byte_identical(self, tmp_path, monkeypatch):
        """An undefined name survives rather than blanking the sentence."""
        _patch_config(monkeypatch, _config(global_vars={"other": "x"}))
        _write_builtin_prompt(monkeypatch, tmp_path, "curl {{baseUrl}}/health")

        msg, _ = _builder(tmp_path).build_message("hi", is_new_session=True)

        assert "curl {{baseUrl}}/health" in msg


class TestReservedTokenCannotBeShadowed:
    def test_reserved_name_refused_by_grammar(self):
        """A variable may not be NAMED after a built-in token."""
        for name in sorted(RESERVED_TOKENS):
            key, reason = validate_pair(name, "9999")
            assert key is None, name
            assert "reserved" in reason
        assert coerce_variables({"MAX_SUBAGENTS": "9999", "ok": "v"}, "global") == {"ok": "v"}

    def test_reserved_token_value_unchanged_even_if_present(self, tmp_path, monkeypatch):
        """Ordering, not just the grammar, protects the built-in tokens.

        A hand-edited config that smuggles a reserved name past validation still
        cannot change what ``{{MAX_SUBAGENTS}}`` resolves to: expansion runs
        AFTER every built-in pass, so by then the token is already gone.
        """
        cfg = _config(global_vars={"MAX_SUBAGENTS": "9999", "VERBOSITY_BLOCK": "PWNED"})
        _patch_config(monkeypatch, cfg)
        monkeypatch.setattr(
            "kiro_crew.subagent.resolve_max_subagents", lambda _cfg: 7, raising=True
        )
        _write_builtin_prompt(
            monkeypatch, tmp_path, "cap={{MAX_SUBAGENTS}}\n{{VERBOSITY_BLOCK}}\nend"
        )

        msg, _ = _builder(tmp_path).build_message("hi", is_new_session=True)

        assert "cap=7" in msg
        assert "9999" not in msg
        assert "PWNED" not in msg


class TestSteeringNotExpanded:
    def test_steering_token_left_byte_identical(self, tmp_path, monkeypatch):
        """Steering content is project-scoped and must never be expanded.

        ``_load_steering_resources`` reads ``file://`` resources that can come
        from a cloned repo, so its ``{{token}}`` text stays verbatim while the
        agent prompt in the SAME message expands.
        """
        _patch_config(monkeypatch, _config(global_vars={"baseUrl": "https://ops.example"}))
        _write_builtin_prompt(monkeypatch, tmp_path, "Prompt target {{baseUrl}}")
        monkeypatch.setattr(
            ctx_mod,
            "_load_steering_resources",
            lambda: "[STEERING]\ncurl {{baseUrl}}/admin\n",
        )

        msg, _ = _builder(tmp_path).build_message(
            "hi", is_new_session=True, provider_type="claude_code"
        )

        # The prompt expanded...
        assert "Prompt target https://ops.example" in msg
        # ...and the steering body did not.
        assert "curl {{baseUrl}}/admin" in msg


# ---------------------------------------------------------------------------
# Boundary 2 — cron dispatch
# ---------------------------------------------------------------------------


def _job(**kw) -> CronJob:
    base = {"id": "j1", "name": "nightly", "message": "Poll {{baseUrl}}/status"}
    base.update(kw)
    return CronJob(**base)  # type: ignore[arg-type]


class TestCronDispatchExpansion:
    def test_expands_at_dispatch_and_stored_message_keeps_token(self, monkeypatch):
        _patch_config(monkeypatch, _config(global_vars={"baseUrl": "https://ops.example"}))
        job = _job()

        _key, prompt = build_cron_session_context(job)

        assert prompt == "Poll https://ops.example/status"
        # The STORE is untouched: the literal token is what gets persisted.
        assert job.message == "Poll {{baseUrl}}/status"

    def test_editing_a_variable_changes_the_next_dispatch(self, monkeypatch):
        cfg = _config(global_vars={"baseUrl": "https://old.example"})
        _patch_config(monkeypatch, cfg)
        job = _job()

        _k1, first = build_cron_session_context(job)
        assert first == "Poll https://old.example/status"

        # The user edits the variable — no job rewrite anywhere.
        cfg.variables["baseUrl"] = "https://new.example"

        _k2, second = build_cron_session_context(job)
        assert second == "Poll https://new.example/status"
        assert job.message == "Poll {{baseUrl}}/status"

    def test_uses_the_jobs_own_crew(self, monkeypatch):
        _patch_config(
            monkeypatch,
            _config(
                crew="mycrew",
                global_vars={"env": "prod"},
                crew_vars={"env": "staging"},
            ),
        )
        job = _job(message="Env {{env}}", agent_id="mycrew")

        _key, prompt = build_cron_session_context(job)

        assert prompt == "Env staging"

    def test_stateless_job_expands_too(self, monkeypatch):
        _patch_config(monkeypatch, _config(global_vars={"baseUrl": "https://ops.example"}))
        job = _job(persistent_session=False)

        key, prompt = build_cron_session_context(job)

        assert key.startswith("cron:j1:")
        assert prompt == "Poll https://ops.example/status"

    def test_previous_run_result_is_not_expanded(self, monkeypatch):
        """``last_result`` is a prior run's MODEL OUTPUT, not user-authored."""
        _patch_config(monkeypatch, _config(global_vars={"baseUrl": "https://ops.example"}))
        job = _job(last_result="earlier I emitted {{baseUrl}} verbatim")

        _key, prompt = build_cron_session_context(job)

        assert "earlier I emitted {{baseUrl}} verbatim" in prompt
        assert "Poll https://ops.example/status" in prompt

    def test_no_variables_configured_is_a_passthrough(self, monkeypatch):
        _patch_config(monkeypatch, _config())
        job = _job()

        _key, prompt = build_cron_session_context(job)

        assert prompt == "Poll {{baseUrl}}/status"


# ---------------------------------------------------------------------------
# Boundary 3 — monitor loop nudge
# ---------------------------------------------------------------------------


class TestNudgeExpansion:
    def test_variable_expands_and_stop_file_still_resolves(self, tmp_path, monkeypatch):
        _patch_config(monkeypatch, _config(global_vars={"prNum": "4161"}))
        sentinel = tmp_path / ".stop-loop"

        out = render_nudge_message("Check PR {{prNum}}; halt via {{STOP_FILE}}", str(sentinel))

        assert f"Check PR 4161; halt via {sentinel}" == out

    def test_stop_file_resolves_when_no_variables_configured(self, tmp_path, monkeypatch):
        _patch_config(monkeypatch, _config())
        sentinel = tmp_path / ".stop-loop"

        assert render_nudge_message("halt: {{STOP_FILE}}", str(sentinel)) == f"halt: {sentinel}"
        assert render_nudge_message("halt: {{STOP_FILE}}", None) == "halt: "

    def test_variable_cannot_forge_the_sentinel_path(self, tmp_path, monkeypatch):
        """A value containing ``{{STOP_FILE}}`` resolves to the REAL sentinel.

        Expansion is single-pass, so the inserted text is not rescanned as a
        variable; the gateway's own ``{{STOP_FILE}}`` replace then runs last and
        can only produce the sentinel it was given.
        """
        cfg = _config(global_vars={"note": "or touch {{STOP_FILE}} to bail"})
        _patch_config(monkeypatch, cfg)
        sentinel = tmp_path / ".stop-loop"
        attacker = tmp_path / "attacker-chosen"

        out = render_nudge_message("Keep going ({{note}})", str(sentinel))

        assert str(sentinel) in out
        assert str(attacker) not in out

    def test_crew_scope_used_when_agent_named(self, tmp_path, monkeypatch):
        _patch_config(
            monkeypatch,
            _config(crew="mycrew", global_vars={"env": "prod"}, crew_vars={"env": "staging"}),
        )
        sentinel = tmp_path / ".stop-loop"

        out = render_nudge_message("Env {{env}} — {{STOP_FILE}}", str(sentinel), "mycrew")

        assert out == f"Env staging — {sentinel}"
