# Implementation Plan

## Status

Shipped: the lexical core, the four-scope config schema and layered resolution, the
expansion boundaries (agent system prompt, dashboard message, inbound channel
message, cron dispatch, monitor loop), the caller-controlled assembly that keeps
imported bodies out of expansion, the security ratchets, `GET`/`PUT /api/variables`,
and the Settings panel for the global and per-workspace layers.

Deferred, each independent of the above: per-key file provenance (task 6), the CLI
verb group (14), the crew-form pairs and composer hint (17), `doctor` reporting (18),
the user-facing docs page (19), and the session layer (20).


> Code citations name a file and a symbol, never a line number: the anchors in this spec drifted twice during implementation (once across 315 commits of `main`, once across 45), and a number that is wrong reads as authoritative.

Each task is one focused, verifiable step that builds on the previous ones. A task is not
complete until its verification passes and each new behavior has been revert-verified
(break it, watch the test fail, restore).

Backend gates at CI parity from the repo root: `isort --check-only src/kiro_crew test`,
`flake8 src/kiro_crew test`, `mypy src/kiro_crew/`. Frontend gates from `website/`:
`npx tsc -b`, `npx vitest run --no-coverage`, and after `git fetch origin`,
`I18N_BASE_REF=origin/main npm run i18n:check` plus the render gate with the same base ref.

## Phase 1 — Lexical core

- [x] 1. Create `src/kiro_crew/variables.py` as a leaf module
  - `RESERVED_TOKENS`, `NAME_RE`, `TOKEN_RE`, `MAX_VALUE_LEN`.
  - `validate_pair(key, value)` returning `(key, coerced)` or `(None, reason)`, covering invalid name, reserved name, non-coercible type, oversize, and control characters other than tab.
  - `expand(text, values) -> tuple[str, frozenset[str]]` using a single `TOKEN_RE.sub` with a replacement **callable**, returning the input object unchanged when `values` is empty.
  - Import nothing from `kiro_crew`, so `config/loader.py`, `context.py`, `dashboard/chat_runner.py`, `cron.py` and the autonudge handler can all import it without a cycle.
  - _Requirements: 1.5, 1.6, 1.7, 2.5, 4.1, 4.7, 6.1, 7.1, 7.2, 7.3, 7.5, 14.1, 14.2_

- [x] 2. Unit-test the lexical core
  - Grammar: valid names, whitespace inside braces, and `{{ }}` / `{{1abc}}` / `{{a-b}}` left byte-identical and not reported unresolved.
  - Single pass: a value containing `{{other}}` where `other` is also defined stays literal.
  - Replacement safety: values containing `\1` and `\g<0>` inserted verbatim.
  - Empty mapping returns the identical object (assert with `is`).
  - One test per `validate_pair` rejection reason.
  - A test scanning `src/kiro_crew/context.py`, `src/kiro_crew/dashboard/handlers/autonudge.py` and `src/kiro_crew/slack_manifest.py` for `{{...}}` literals, asserting every name found is in `RESERVED_TOKENS`.
  - _Requirements: 6.4, 7.5, 14.1_

## Phase 2 — Schema and resolution

- [x] 3. Add the three-scope schema
  - `variables: dict[str, str]` on `KiroCrewConfig`; `variables: dict[str, str]` on `WorkspaceConfig` (`config/loader.py`) and on `KiroCrewAgentConfig` (`config/loader.py`), each with `_meta` metadata.
  - Route every pair at every scope through `variables.validate_pair` from one shared code path, dropping a rejected pair with a WARNING naming key, scope and reason while retaining the rest.
  - Teach `_migrate_workspaces` (`config/loader.py`) to carry `variables` — it currently reads only `dir` and drops everything else on the next save.
  - Parse a pre-existing top-level `variables` key that `_extra_sections` was preserving verbatim, reporting failing pairs rather than discarding them.
  - Regenerate `config-baseline.json` and confirm the new `_meta` entries appear.
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.8, 1.9, 14.4, 14.5_

- [x] 4. Implement layered resolution
  - `VariableResolution` (`values`, `winning_scope`, `shadowed`, `rejected`) and `resolve_variables(config, agent=None, session_overrides=None)`.
  - Merge global → workspace → crew → session, keyed on **key presence, not truthiness**, so an empty string at a narrow scope wins over a non-empty broad one.
  - Take the workspace layer from the workspace the session actually resolved to, reusing `resolve_agent_bindings` (`config/loader.py`) including its existing warn-and-fall-back for a crew naming a missing workspace.
  - Extend `resolve_agent_bindings` to report the resolution alongside workspace and memory store.
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.6, 3.3, 14.1_

- [x] 5. Test resolution
  - Each layer alone; each adjacent pair overriding; a stack where all layers define the same key.
  - Empty string at crew scope beating a non-empty global.
  - Missing-workspace fallback with the warning asserted.
  - Empty config resolves empty and leaves strings byte-identical.
  - A save round-trip asserting `workspaces.<n>.variables` survives `_migrate_workspaces`.
  - Derive every path from `tmp_path`; never hard-code a bare absolute path such as `/x`, which resolves to a different drive on Windows CI.
  - _Requirements: 1.4, 2.1–2.6, 14.4_

- [ ] 6. Implement and test provenance
  - `variable_sources()` re-reading the raw `config.json` and `config.local.json` dicts to report the supplying file per key and scope; winning-scope and shadowing come from the merge itself.
  - Call it only from CLI, HTTP and doctor paths, never per message.
  - Test attribution for a key defined in each file, in both, and in neither; test shadowing lists; test a Reserved_Token is rejected identically whichever file defines it.
  - _Requirements: 3.1, 3.2, 3.4, 3.5, 9.1, 9.2, 9.4, 9.5_

## Phase 3 — Expansion at each boundary

- [x] 7. Expand the agent system prompt
  - Apply expansion in `src/kiro_crew/context.py` where both prompt branches converge (after `_load_agent_prompt`, at the existing `_resolve_prompt_templates` / `_substitute_bot_name` call site), so a custom agent's prompt is covered too.
  - Order it after every Reserved_Token pass.
  - Test: a variable expands for both a built-in and a custom agent; a variable named after a Reserved_Token cannot change that token's value.
  - _Requirements: 4.2, 6.3_

- [x] 8. Refactor `chat_runner` to caller-controlled assembly
  - Split `_expand_prompt_mention` (`dashboard/chat_runner.py`) and `_expand_dollar_skills` (`:2743-2812`) into parts-returning forms — `(authored, imported_blocks, status_or_count)` — leaving their resolution logic and existing gates unchanged.
  - Assemble at the call site: resolve `@prompt`, then `$skill`, then expand variables over the authored segment only, then join.
  - Preserve the existing `prompt_expanded` / `is_slash` / `_prompt_depth` gating and the SEL `skill_dollar_expansion` audit call.
  - Test that the assembled message is byte-identical to today's output when no variable is defined, proving the refactor behavior-preserving.
  - _Requirements: 4.3, 4.8, 5.1, 5.2, 5.6, 8.3_

- [x] 9. The three security tests
  - A value is absent from the assembled prompt when the only reference is inside a `SKILL.md` body.
  - A value of `$<a real installed skill>` does not cause that skill to load.
  - A value of `@<a real prompt file>` does not cause that file to be inlined.
  - Each asserts on the assembled text, not an internal flag. Revert-verify all three.
  - _Requirements: 5.1, 5.2, 5.7, 8.1, 8.2, 8.4, 8.5_

- [x] 10. Expand inbound channel messages
  - Apply expansion to the authored inbound text in `slack/transport_dispatch.py`, `discord/transport_dispatch.py`, `telegram/transport_dispatch.py` and `messaging/dispatch.py`, before the text becomes `build_message`'s `text` argument.
  - Test one transport end to end and assert the others call the expander.
  - _Requirements: 4.4_

- [x] 11. Expand cron messages and monitor instructions
  - Expand a cron job's `message` at dispatch in `cron.py` using that job's crew's Effective_Map, leaving the stored job unchanged.
  - Expand a monitor loop instruction in `dashboard/handlers/autonudge.py` before the `{{STOP_FILE}}` replace.
  - Test: editing a variable changes what the next cron run receives; the stored `message` still contains the token; `{{STOP_FILE}}` still resolves.
  - _Requirements: 4.5, 4.6, 6.3, 9.6_

- [x] 12. Add the source guard
  - A test enumerating the expansion boundaries that fails when a new `build_message` caller or transport dispatch appears without one, following the countable-guard pattern already used for armed-resource release paths.
  - _Requirements: 5.6, 13.5_

- [x] 13. Add "leave Imported_Text alone" coverage
  - Assert no expansion in a steering file loaded by `_load_steering_resources`, and none in `mcpServers` `command`/`args`/`env`, an agent spec JSON, or an app manifest.
  - Assert an unresolved token in Imported_Text is not logged at INFO or above.
  - _Requirements: 5.3, 5.4, 5.5, 13.4, 13.5_

## Phase 4 — Surfaces

- [ ] 14. CLI verb group
  - `kirocrew vars list|show|set|unset` with `--workspace` / `--agent` scope selectors (global by default) and `--local`, modeled on the `workspace` group (`cli.py`, `cli_commands.py`) and routing `--local` writes the way `kirocrew config set --local` does.
  - `vars list` prints the effective map with winning scope and supplying file; `vars show KEY` lists the value at every scope and marks the winner.
  - A selector naming a nonexistent workspace or crew exits non-zero listing available names and writes nothing.
  - Test each verb, the unknown-scope paths, and that unrelated config keys survive a write.
  - _Requirements: 9.3, 10.1, 10.2, 10.3, 10.4, 10.5_

- [x] 15. HTTP routes
  - `GET /api/variables` and `PUT /api/variables` beside the existing config routes, under the same auth; validation failures return 400 naming the offending key.
  - Test each route including rejection shapes and per-scope writes.
  - _Requirements: 11.1, 11.2_

- [x] 16. Settings Environment Variables panel
  - New `website/src/pages/settings/VariablesPanel.tsx`, registered in `website/src/pages/SettingsPage.tsx` (one import, one registry entry in `GROUP_PREFERENCES` beside Skills, one switch line), leading with a **Global Environment Variables** section and also listing per-workspace pairs.
  - Show each row's winning scope and whether it shadows a broader scope.
  - State plainly in the panel that variables are not for secrets and are stored in plain text in `config.json`.
  - Use lucide icons, no emoji. Add every string to all locale catalogs, preserving each catalog's key order and appending new keys at the end.
  - Ensure command-palette reachability, adding an `EXTRA_PAGES` entry if the surface is hidden from the nav.
  - Call any new api client method defensively at mount, since many test files partially mock `../src/api/client`.
  - Test render, add/edit/delete at global and workspace scope, validation surfacing, scope indicators, keyboard operation and label association. Run the full frontend suite.
  - _Requirements: 11.3, 11.4, 11.6, 11.7, 11.8, 11.9, 13.2_

- [ ] 17. Crew-form pairs and composer hint
  - Add the crew's own pairs to the crew form in `website/src/pages/KiroCrewAgentsPage.tsx`, beside the existing workspace, memory-store and model fields.
  - Flag an unknown `{{name}}` in the composer before submission, reusing `website/src/components/composerTokens.ts`, and surface unresolved names once per submitted message.
  - Test both, including that a known variable produces no warning.
  - _Requirements: 7.4, 11.5, 12.3_

- [ ] 18. Doctor diagnostics
  - Report the Effective_Map size per configured crew and every rejected pair with key, scope and reason.
  - Report each cron job whose `message` references a name absent from that job's crew's Effective_Map, naming job and token.
  - Never print a value. Test both reports and assert no value appears in the output.
  - _Requirements: 12.1, 12.2, 12.4, 14.6_

## Phase 5 — Close out

- [ ] 19. Documentation
  - Document the three config scopes, the cascade order, the CLI verbs, and the expansion surfaces — including, explicitly, the surfaces that do not expand and why.
  - State that variables are not for secrets, name where a credential does belong today, and record that a future secret store would arrive under its own reference namespace rather than as a flag here.
  - _Requirements: 5.4, 13.1, 13.3, 13.6, 13.7_

- [ ] 20. Session layer (last, and droppable)
  - Slot-scoped override dict plus `PUT /api/chat/slots/{slot}/variables`, transient and never written to config, modeled on the per-session model override; a small chat-header control to set it.
  - Ordered last deliberately: it is the mitigation for the cascade having no one-click context flip, and cutting it removes no other layer's behavior.
  - Test that a session override wins over the crew layer and does not persist across sessions.
  - _Requirements: 2.1, 11.1_

- [ ] 21. Full gate run and revert-verification sweep
  - Run every backend and frontend gate listed at the top of this plan, at CI parity with the base-ref environment variables set, plus `HARNESS_BASE_REF=origin/main python3 scripts/check_harness_parity.py`.
  - Re-confirm each security test from task 9 and the source guard from task 12 by reverting its fix and watching it fail.
  - Verify a config predating this feature loads unchanged and leaves every assembled string byte-identical.
  - Verify no new value reaches a child process environment and no credential store is read or written.
  - _Requirements: 13.4, 14.4, and every requirement in 5 and 8_
