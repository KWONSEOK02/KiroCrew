# Requirements Document

> Code citations name a file and a symbol, never a line number: the anchors in this spec drifted twice during implementation (once across 315 commits of `main`, once across 45), and a number that is wrong reads as authoritative.

## Introduction

KiroCrew has no way to define user-supplied key/value pairs and reference them from the text it sends to an agent. Users working across several contexts — a dev API and a prod API, two ticket queues, two service endpoints — retype those values in every prompt and every cron message, or hard-code them into a skill.

Four halves of this feature already exist in the codebase and were never joined:

- A **value store with no management**: `KiroCrewConfig.load_credentials()` (`config/loader.py`) parses *every* `KEY=VALUE` line in `~/.kiro/crew/.env`, not just the 13 hard-coded credential keys, and `os.environ.setdefault`s all of them into every child process. There is no UI, CLI, API, schema, or scrub for user-added keys.
- A **`{{TOKEN}}` expansion mechanism with no user-supplied values**: `ContextBuilder._resolve_prompt_templates` (`context.py`) resolves `{{MAX_SUBAGENTS}}`, `{{VERBOSITY_BLOCK}}` and `{{WIDGET_BLOCK}}`, all gateway-computed. `render_nudge_message` (`dashboard/handlers/autonudge.py`) resolves `{{STOP_FILE}}` the same way.
- **Scope objects with nowhere to hang values**: a workspace (`WorkspaceConfig`, `config/loader.py`) is one field wide, and a crew (`KiroCrewAgentConfig`, `config/loader.py`) binds a kiro agent, workspace, memory store and model — and is switched from a live header dropdown via a route that re-derives bindings (`dashboard/chat_handlers.py`).
- A **layered-resolution precedent applied to exactly one thing**: `resolve_memory_store_config` (`config/loader.py`) merges a named store's non-empty fields over a top-level section.

This feature introduces **Crew Variables**: user-defined pairs declared at four scopes that cascade — global, workspace, crew, session — expanded as `{{name}}` in the text layer.

**Why a cascade rather than Postman-style switchable environments.** Postman invented named environments because a collection is not a context, so a switchable bag had to be bolted alongside. KiroCrew already has real scope objects, so values hang on them directly and no second selector is introduced. A named-set indirection at the crew scope can be added later without a breaking change, since a crew's layer is already a bag of pairs; it is not in v1.

**Vocabulary.** The user-facing label is "Environment Variables", with the top-level section presented as "Global Environment Variables". Config keys deliberately avoid the word `env` (`variables` at the top level, `variables` on a scope entry) because `env` already means the process environment, `mcpServers.env`, and `~/.kiro/crew/.env` in this codebase.

**Secrets are a decided non-goal for v1**, not a deferred question — see Requirement 13. The reason is measured: the agent env-scrub list `_AGENT_DENIED_ENV_KEYS` (`sandbox.py`) is a closed set of 13 literals, so a user-defined key is already readable today with a plain `printenv` inside an agent shell. Marking a variable "secure" in the UI without extending that machinery would be a false promise. Postman's own model confirms the shape of the real fix — secret-capability there is a store with an encryption key and an enforced `vault:` namespace, plus per-consumer opt-in before a script may read one, never a checkbox on an ordinary variable.

## Glossary

- **Global_Layer**: the top-level `variables` map in `config.json`.
- **Workspace_Layer**: the `variables` map on a `workspaces.<name>` entry.
- **Crew_Layer**: the `variables` map on an `agents.<name>` entry.
- **Session_Layer**: transient pairs set on one chat session, never written to config.
- **Effective_Map**: the resolved `dict[str, str]` for a session — the four layers merged per key, narrowest winning.
- **Variable_Resolver**: new logic in `config/loader.py` producing the Effective_Map.
- **Variable_Expander**: new logic performing single-pass `{{name}}` substitution.
- **Reserved_Token**: an existing built-in `{{...}}` prompt token — `MAX_SUBAGENTS`, `VERBOSITY_BLOCK`, `WIDGET_BLOCK`, `STOP_FILE`, `ALIAS` — plus the single-brace `{bot_name}`.
- **Authored_Text**: text the human user typed or authored in KiroCrew — a chat message, a cron job `message`, the configured agent system prompt.
- **Imported_Text**: text loaded from a file or registry rather than typed — a `SKILL.md` body, an `@prompt` file body, a steering file. May originate outside the user's control (a cloned repo, the public skill registry).
- **Machine_Surface**: a non-prose consumer of config values — `mcpServers` `command`/`args`/`env`, an agent spec JSON, an app manifest.

## Requirements

### Requirement 1: Variable Definitions at Four Scopes

**User Story:** As a KiroCrew user, I want to declare variables at the scope they actually belong to, so that a value shared by everything is written once and a value specific to one crew lives on that crew.

#### Acceptance Criteria

1. THE `KiroCrewConfig` dataclass SHALL include a top-level `variables` field of type `dict[str, str]`, defaulting to empty, with `_meta` field metadata consistent with the surrounding dataclasses.
2. THE `WorkspaceConfig` dataclass SHALL gain a `variables` field of type `dict[str, str]`, defaulting to empty.
3. THE `KiroCrewAgentConfig` dataclass SHALL gain a `variables` field of type `dict[str, str]`, defaulting to empty.
4. `_migrate_workspaces` (`config/loader.py`) currently reads only the `dir` key and silently discards any other key on the next save. THE migration SHALL be taught to carry `variables`, and a test SHALL assert a round-trip through save preserves it.
5. THE Config_Loader SHALL accept a variable name matching `^[A-Za-z][A-Za-z0-9_]*$` and SHALL reject any other name with a warning naming the offending key and its scope, without failing the load.
6. WHEN a value is not a string, THE Config_Loader SHALL coerce a bool, int or float to its string form and SHALL reject any other type with a warning naming the offending key and scope.
7. THE Config_Loader SHALL cap a single value at 4096 characters and SHALL reject a value containing an ASCII control character other than tab, in each case with a warning naming the key and scope.
8. WHEN a pair is rejected under criteria 5 through 7, THE Config_Loader SHALL omit that single pair and SHALL retain every other pair at that scope and every other scope.
9. THE same validation rules SHALL apply identically at all four scopes, from one shared code path.

### Requirement 2: Layered Resolution

**User Story:** As a KiroCrew user, I want a narrower scope to win over a broader one, so that I can set a default globally and override it for one crew without editing the default.

#### Acceptance Criteria

1. THE Variable_Resolver SHALL produce the Effective_Map by merging, in order, Global_Layer, then Workspace_Layer, then Crew_Layer, then Session_Layer, each overriding the previous per key.
2. THE merge SHALL be keyed on **key presence, not truthiness**: an empty string at a narrower scope is an intentional override to empty and SHALL NOT fall through to a broader scope. This differs deliberately from `resolve_memory_store_config` (`config/loader.py`), where an empty field means "inherit".
3. THE Workspace_Layer applied SHALL be that of the workspace the session actually resolved to, which `resolve_agent_bindings` (`config/loader.py`) already derives from the crew's binding or `default_workspace`.
4. WHEN a crew names a workspace absent from `workspaces`, THE Variable_Resolver SHALL apply the fallback workspace's layer, matching the warning-and-fall-back behavior `resolve_agent_bindings` already has for the workspace itself.
5. THE Variable_Resolver SHALL NOT recurse: a value containing `{{...}}` SHALL be carried into the Effective_Map verbatim.
6. WHEN every layer is empty, THE Variable_Resolver SHALL return an empty Effective_Map and THE Variable_Expander SHALL become a no-op leaving every string byte-identical.

### Requirement 3: Provenance

**User Story:** As a KiroCrew user, I want to see which scope supplied each effective value, so that I can tell why a value is what it is without reading three config sections.

#### Acceptance Criteria

1. THE Variable_Resolver SHALL report, per key in the Effective_Map, which scope supplied the winning value and which scopes were shadowed.
2. THE Variable_Resolver SHALL report, per key, which file supplied the value — `config.json` or `config.local.json`.
3. `resolve_agent_bindings` SHALL report the resolved Effective_Map alongside the workspace and memory store it already returns.
4. THE provenance computation MAY be a separate, colder code path than the hot resolution, since `KiroCrewConfig.load()` deep-merges `config.local.json` over `config.json` (`config/loader.py`) before dataclass parsing and per-file attribution requires re-reading the two raw dicts.
5. THE provenance path SHALL NOT be invoked per message.

### Requirement 4: Expansion in Authored Text

**User Story:** As a KiroCrew user, I want `{{baseUrl}}` in what I type to be replaced by the effective value, so that I stop retyping the same values.

#### Acceptance Criteria

1. THE Variable_Expander SHALL recognize `\{\{\s*([A-Za-z][A-Za-z0-9_]*)\s*\}\}` and SHALL replace a matched token with the Effective_Map value for that name.
2. THE SYSTEM SHALL expand tokens in the configured agent system prompt, applied where both prompt branches converge (after `_load_agent_prompt`, `context.py`) so a custom agent's prompt is covered as well as a built-in one.
3. THE SYSTEM SHALL expand tokens in a chat message the user submits from the dashboard composer.
4. THE SYSTEM SHALL expand tokens in an inbound message from Slack, Discord, Telegram, Webex, WeCom or Teams.
5. THE SYSTEM SHALL expand tokens in a cron job's `message` at dispatch time, not registration time, so changing a variable changes what the next run receives.
6. THE SYSTEM SHALL expand tokens in a `monitor_start` loop instruction before the existing `{{STOP_FILE}}` pass (`dashboard/handlers/autonudge.py`).
7. THE Variable_Expander SHALL be single-pass: a substituted value SHALL NOT be rescanned.
8. THE SYSTEM SHALL persist the user's original text with tokens intact in session history; expansion is a send-time transformation.

### Requirement 5: Refusal to Expand in Imported Text and Machine Surfaces

**User Story:** As a KiroCrew user, I want a skill I installed from the public registry to be unable to read my variables, so that a third-party skill cannot become an exfiltration primitive.

#### Acceptance Criteria

1. THE SYSTEM SHALL NOT expand tokens in a `SKILL.md` body, including one appended by `$skill` expansion (`dashboard/chat_runner.py`) or fetched by `skill_fetch`.
2. THE SYSTEM SHALL NOT expand tokens in an `@prompt` file body (`dashboard/chat_runner.py`).
3. THE SYSTEM SHALL NOT expand tokens in a steering file loaded by `_load_steering_resources` (`context.py`).
4. THE SYSTEM SHALL NOT expand tokens in any Machine_Surface, preserving the rule stated in the `_placeholder_values` docstring (`apps/bridges.py`) that rendered agent JSON must not draw values from a writable location.
5. WHEN Imported_Text contains a token, THE SYSTEM SHALL leave it byte-identical and SHALL NOT log the token's name at INFO or above.
6. THE SYSTEM SHALL enforce criteria 1 through 4 structurally, by expanding only an explicitly passed Authored_Text string, and SHALL NOT rely on scanning for markers.
7. THE repository SHALL carry a test asserting a variable's value is absent from the prompt assembled for a session whose only reference to it is inside a `SKILL.md` body.

### Requirement 6: Reserved Names

**User Story:** As a KiroCrew user, I want a clear warning when I name a variable the same as a built-in token, so that I do not silently shadow gateway behavior.

#### Acceptance Criteria

1. THE Config_Loader SHALL treat `MAX_SUBAGENTS`, `VERBOSITY_BLOCK`, `WIDGET_BLOCK`, `STOP_FILE`, `ALIAS` and `bot_name` as Reserved_Tokens.
2. WHEN any scope defines a key equal to a Reserved_Token, THE Config_Loader SHALL omit that pair with a warning naming the key, the scope, and the reason.
3. THE Variable_Expander SHALL run after every Reserved_Token pass, so a user variable can never alter a built-in substitution.
4. THE Reserved_Token list SHALL live in one module-level constant, and a test SHALL assert it covers every `{{...}}` literal present in `context.py`, `dashboard/handlers/autonudge.py` and `slack_manifest.py`.

### Requirement 7: Unresolved Tokens Stay Literal

**User Story:** As a KiroCrew user, I want a misspelled variable to be obvious rather than silently empty, so that I notice before the agent acts on a truncated instruction.

#### Acceptance Criteria

1. WHEN a token names a variable absent from the Effective_Map, THE Variable_Expander SHALL leave the token byte-identical.
2. THE Variable_Expander SHALL NOT substitute an empty string for an unknown name.
3. THE Variable_Expander SHALL return the set of unresolved names encountered.
4. WHEN at least one token is unresolved in a dashboard chat message, THE SYSTEM SHALL surface the unresolved names in the session, exactly once per message.
5. THE Variable_Expander SHALL leave a malformed token such as `{{ }}`, `{{1abc}}` or `{{a-b}}` byte-identical and SHALL NOT report it as unresolved.

### Requirement 8: No Token Escalation

**User Story:** As a KiroCrew user, I want a variable's value treated as plain text, so that a value cannot reach in and load a skill or prompt file I did not ask for.

#### Acceptance Criteria

1. WHEN a value contains a `$name` sequence matching `_DOLLAR_SKILL_PATTERN` (`skills.py`), THE SYSTEM SHALL NOT load the named skill as a result of the substitution.
2. WHEN a value contains an `@name` sequence, THE SYSTEM SHALL NOT inline the named prompt file as a result of the substitution.
3. THE turn pipeline SHALL resolve `$skill` and `@prompt` references against the pre-expansion text, so criteria 1 and 2 hold structurally rather than by sanitizing values.
4. THE repository SHALL carry a test for each of criteria 1 and 2 naming a real installed skill and prompt and asserting it was not loaded.
5. THE Variable_Expander SHALL NOT strip or escape characters within a value, since criteria 1 through 3 remove the need to.

### Requirement 9: Local Values That Are Not Shared

**User Story:** As a KiroCrew user working from a shared config, I want to override one variable on this machine only, so that my local endpoint does not end up in the file my team reads.

#### Acceptance Criteria

1. THE SYSTEM SHALL allow `variables`, `workspaces.<name>.variables` and `agents.<name>.variables` to be set in `config.local.json`, which `KiroCrewConfig.load()` already deep-merges over `config.json`.
2. WHEN the same variable at the same scope is defined in both files, THE SYSTEM SHALL use the `config.local.json` value.
3. THE CLI SHALL accept a `--local` flag on its writing verbs, routing the write to `config.local.json` consistent with `kirocrew config set --local` (`cli_config.py`).
4. THE SYSTEM SHALL surface the supplying file per effective value, satisfying Requirement 3.2.
5. THE Reserved_Token rejection SHALL apply identically whichever file defines the pair.
6. WHEN a cron job or monitor loop resolves variables, THE SYSTEM SHALL use the same Effective_Map a dashboard session for that crew would resolve, and any divergence between shared and local layers for unattended runs SHALL be documented rather than silently differing.

### Requirement 10: CLI Surface

**User Story:** As a KiroCrew user, I want to manage variables from the command line, so that I can script them and use them headlessly.

#### Acceptance Criteria

1. THE CLI SHALL provide `kirocrew vars list`, printing the Effective_Map with each value's winning scope and supplying file, and accepting `--workspace` / `--agent` to resolve as that context would.
2. THE CLI SHALL provide `kirocrew vars set KEY VALUE` and `kirocrew vars unset KEY`, each accepting a scope selector — global by default, `--workspace NAME` or `--agent NAME` — plus `--local`.
3. THE CLI SHALL provide `kirocrew vars show KEY`, listing the value at every scope that defines it and marking which one wins.
4. WHEN a scope selector names a workspace or crew that does not exist, THE CLI SHALL exit non-zero listing the available names and SHALL NOT write the config.
5. THE CLI SHALL preserve every unrelated key in the file it writes, using the existing atomic config writer.

### Requirement 11: HTTP and UI Surface

**User Story:** As a KiroCrew user, I want to edit variables in the dashboard and see which scope a value came from, so that I do not hand-edit JSON.

#### Acceptance Criteria

1. THE dashboard SHALL expose `GET /api/variables` returning every scope's pairs, the resolved Effective_Map for a requested context, and per-key winning scope and supplying file.
2. THE dashboard SHALL expose `PUT /api/variables` accepting a create, update or delete of pairs at a named scope, validated by the Requirement 1 rules, returning 400 with the offending key named on rejection.
3. THE Settings UI SHALL provide an **Environment Variables** panel whose primary section is **Global Environment Variables**, with an editable pair table and add/delete affordances.
4. THE Settings panel SHALL also present per-workspace pairs, so a user can manage a workspace's layer without visiting another page.
5. THE crew form on the Agents page SHALL provide the crew's own pairs beside the existing workspace, memory-store and model fields.
6. THE UI SHALL show, per effective value, the scope that supplied it and whether it is shadowing a broader scope.
7. Every new user-visible string SHALL be added to all locale catalogs and SHALL pass `I18N_BASE_REF=origin/main npm run i18n:check` and the render gate.
8. THE panel SHALL be reachable from the command palette, and any surface hidden from the nav SHALL carry the matching `EXTRA_PAGES` entry.
9. THE panel SHALL be keyboard operable, SHALL associate every input with a visible label, and SHALL use lucide icons rather than emoji.

### Requirement 12: Diagnostics

**User Story:** As a KiroCrew user, I want KiroCrew to tell me when my variables are misconfigured, so that I find out before a cron job runs with a literal token in it.

#### Acceptance Criteria

1. `kirocrew doctor` SHALL report the Effective_Map size per configured crew and every pair rejected under Requirements 1 and 6, naming key, scope and reason.
2. `kirocrew doctor` SHALL report each cron job whose `message` references a name absent from that job's crew's Effective_Map, naming the job and the token.
3. THE dashboard composer SHALL indicate an unknown `{{name}}` before submission, reusing the token-scanning approach of `website/src/components/composerTokens.ts`.
4. THE SYSTEM SHALL NOT include any variable's value in diagnostic output; it SHALL name keys and scopes only.

### Requirement 13: Secrets Are Out of Scope for v1

**User Story:** As a KiroCrew user, I want KiroCrew to be honest that this feature is not for secrets, so that I do not put a credential somewhere it is not protected.

#### Acceptance Criteria

1. THE feature SHALL NOT provide a secret, secure, masked or encrypted variable type.
2. THE UI SHALL state plainly, in the Environment Variables panel, that variables are not for secrets and are stored in plain text in `config.json`.
3. THE documentation SHALL state the same, and SHALL name where a credential does belong today.
4. THE feature SHALL NOT introduce any new value into a child process environment, and SHALL NOT read from or write to `~/.kiro/crew/.env` or any credential store.
5. THE feature SHALL NOT expand a token in any Machine_Surface, so a variable cannot become an `mcpServers` credential by another route.
6. THE schema SHALL remain forward-compatible with a future secret store: because a secret would arrive under its own reference namespace and its own store, no field added by this feature needs to change to accommodate one.
7. No requirement in this document SHALL be satisfied by presenting a variable as more protected than plain config text.

### Requirement 14: Non-Functional Requirements

#### Acceptance Criteria

1. **Performance.** THE Variable_Resolver SHALL NOT read config from disk per message; it SHALL resolve from the already-loaded config object on the existing per-session path. THE Variable_Expander SHALL compile its pattern once at module level.
2. **Performance.** WHEN the Effective_Map is empty, THE Variable_Expander SHALL return the input string object without scanning it.
3. **Security.** THE threat model SHALL be that a value is equivalent in trust to text the user typed themselves — which holds only because Requirement 5 confines expansion to Authored_Text and Requirement 8 prevents escalation.
4. **Backward compatibility.** WHEN a config predates this feature, THE Config_Loader SHALL load it unchanged, resolve an empty Effective_Map, and leave every string byte-identical.
5. **Backward compatibility.** WHEN a config already carries a top-level `variables` key preserved verbatim by `_extra_sections` (`config/loader.py`), THE Config_Loader SHALL parse it as the Global_Layer and SHALL report any pair that fails validation rather than discarding it silently.
6. **Observability.** THE SYSTEM SHALL log at DEBUG, once per message, the count of tokens expanded and unresolved, and SHALL NOT log values.
