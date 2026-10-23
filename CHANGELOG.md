## [1.57.1](https://github.com/fullsend-ai/agent-eval-harness/compare/v1.57.0...v1.57.1) (2026-10-21)


### Bug Fixes

* **deps:** keep litellm below 1.107 on the harbor path ([#267](https://github.com/fullsend-ai/agent-eval-harness/issues/267)) ([34610e3](https://github.com/fullsend-ai/agent-eval-harness/commit/34610e39989d0fafcfb9ac552928bae3a001d78b))

# [1.57.0](https://github.com/fullsend-ai/agent-eval-harness/compare/v1.56.4...v1.57.0) (2026-10-21)


### Features

* **judge:** carry the verdict as a structured output, with the forced tool call as the fallback ([#251](https://github.com/fullsend-ai/agent-eval-harness/issues/251)) ([f098dd2](https://github.com/fullsend-ai/agent-eval-harness/commit/f098dd294f067234dacac2f411b52bcaba2acc8f)), closes [#250](https://github.com/fullsend-ai/agent-eval-harness/issues/250)

## [1.56.4](https://github.com/fullsend-ai/agent-eval-harness/compare/v1.56.3...v1.56.4) (2026-10-20)


### Bug Fixes

* **score:** confine agent judges deny-by-default ([#254](https://github.com/fullsend-ai/agent-eval-harness/issues/254)) ([3a5d3a9](https://github.com/fullsend-ai/agent-eval-harness/commit/3a5d3a9dabc0ec2807d9088750069e049ddf30f5)), closes [#253](https://github.com/fullsend-ai/agent-eval-harness/issues/253) [#240](https://github.com/fullsend-ai/agent-eval-harness/issues/240) [fullsend-ai/agents#1622](https://github.com/fullsend-ai/agents/issues/1622) [#1622](https://github.com/fullsend-ai/agent-eval-harness/issues/1622) [#1622](https://github.com/fullsend-ai/agent-eval-harness/issues/1622) [fullsend-ai/agents#1622](https://github.com/fullsend-ai/agents/issues/1622)

## [1.56.3](https://github.com/fullsend-ai/agent-eval-harness/compare/v1.56.2...v1.56.3) (2026-10-15)


### Bug Fixes

* **score:** copy judge context, validate tool lists, honor scoped allows ([fc3c7c0](https://github.com/fullsend-ai/agent-eval-harness/commit/fc3c7c0e1df48a449c4e55d4e02d0cdc3ceef3c2)), closes [#240](https://github.com/fullsend-ai/agent-eval-harness/issues/240)
* **score:** deny shell, network and sub-agent tools to judges run through Claude Code ([d0e6271](https://github.com/fullsend-ai/agent-eval-harness/commit/d0e6271425ca00d8f67cc6240840acca0ab8e798))
* **score:** resolve the open review on the judge deny list ([3282936](https://github.com/fullsend-ai/agent-eval-harness/commit/3282936d47136ba8133e32d9d5cb34737f29bcb0)), closes [#240](https://github.com/fullsend-ai/agent-eval-harness/issues/240)

## [1.56.2](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.56.1...v1.56.2) (2026-10-14)


### Bug Fixes

* **judge:** step the tool_choice ladder down on a 400 that names tool_choice ([ed062fe](https://github.com/opendatahub-io/agent-eval-harness/commit/ed062fecc3bce5f2a15c375ca0ccbd287f9a9967))

## [1.56.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.56.0...v1.56.1) (2026-10-12)


### Bug Fixes

* **judges:** correct the stale untrusted-data guard guidance and drop legacy JSON boilerplate from shipped prompts ([9fe4cdd](https://github.com/opendatahub-io/agent-eval-harness/commit/9fe4cddaed7c8b11296b1572ffdc476cdf0456b6))
* **judges:** let the comparison judge call a dimension tied instead of forcing a winner ([6845314](https://github.com/opendatahub-io/agent-eval-harness/commit/684531446cc1bd5a30ab04eafebfc46a73cf8d81))
* **scoring:** fence the whole outputs proxy under tojson and bare outputs.files ([6fecbbb](https://github.com/opendatahub-io/agent-eval-harness/commit/6fecbbbb194970d27f4249b484827528b5f96eb1))
* **scoring:** neutralize fence markers that appear inside evaluated material ([56b00a9](https://github.com/opendatahub-io/agent-eval-harness/commit/56b00a954d86f2e55d9fe24ab4412aa93af2ce99))

# [1.56.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.55.0...v1.56.0) (2026-10-09)


### Bug Fixes

* **anova:** escape markdown table cells in the deep report; correct the contrasts provenance wording ([ce31d15](https://github.com/opendatahub-io/agent-eval-harness/commit/ce31d15390231f683ca314fc477478541f3ab28e))
* **anova:** label wholesale pairwise-test failure as systemic, not per-pair degeneracy ([a3de7ca](https://github.com/opendatahub-io/agent-eval-harness/commit/a3de7caf32428c55699c9f7d33ac8904db487e80))
* **anova:** read the mixedlm design spec via model_spec on statsmodels 0.15 for contrasts ([9e8a183](https://github.com/opendatahub-io/agent-eval-harness/commit/9e8a1830a70f44694f21262a071a9fe3714d0af9))


### Features

* **anova:** Holm-corrected pairwise level contrasts ([a591225](https://github.com/opendatahub-io/agent-eval-harness/commit/a591225854ed2217d989c3b638b9645855b938c5))

# [1.55.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.54.0...v1.55.0) (2026-10-08)


### Bug Fixes

* **anova:** accept scalar judge_results in the per-judge fan-out and tighten honesty details ([a872c39](https://github.com/opendatahub-io/agent-eval-harness/commit/a872c39081cf2e4d4b60cabe93028d6e9b9baab1))
* **anova:** map integers beyond float range to a missing observation instead of raising ([ab4397b](https://github.com/opendatahub-io/agent-eval-harness/commit/ab4397b64934bd83d9273087e18454f770cc4224))
* **anova:** treat non-finite judge values as missing observations, and keep unobserved judges visible ([fce31b9](https://github.com/opendatahub-io/agent-eval-harness/commit/fce31b9a80ef4dbe888dbf09ab59041ca1859d01))


### Features

* **anova:** opt-in per-judge ANOVA fan-out with Benjamini-Hochberg correction ([1f71e12](https://github.com/opendatahub-io/agent-eval-harness/commit/1f71e12ce1d5fdf0e4fe60b6ee38a1985a0b671c))

# [1.54.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.53.4...v1.54.0) (2026-10-08)


### Bug Fixes

* **anova:** headline p matches the corrected verdict; teach interpretation prompt adjusted-p semantics ([77bd2f8](https://github.com/opendatahub-io/agent-eval-harness/commit/77bd2f867e3de5dc0e2a2838a9d9a043f3b6febc))
* **anova:** review follow-ups — reject non-string correction values, report excluded_terms on degenerate single-factor paths, tighten guide wording ([d7cfa2e](https://github.com/opendatahub-io/agent-eval-harness/commit/d7cfa2ea02b7d1c36d18a9c0bb1dad85b17423f6))
* **anova:** support statsmodels 0.15's model_spec in the term-slice fallback ([ce05e6c](https://github.com/opendatahub-io/agent-eval-harness/commit/ce05e6c3ff1b951746e1c23f480a6f166042f02a))


### Features

* **anova:** per-term Wald tests with Holm/BH multiplicity correction ([49332cb](https://github.com/opendatahub-io/agent-eval-harness/commit/49332cbde142574cd8565383477b355652a57fb1))

## [1.53.4](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.53.3...v1.53.4) (2026-10-07)


### Bug Fixes

* **eval-compare:** address the CodeRabbit review on [#238](https://github.com/opendatahub-io/agent-eval-harness/issues/238) ([4b112b6](https://github.com/opendatahub-io/agent-eval-harness/commit/4b112b67b5a1fcb1f43157fb6e221f6b3a70b331))
* **eval-compare:** report a wall clock for duration_s runs, and stop rounding per-turn costs to $0.00 ([a7aa586](https://github.com/opendatahub-io/agent-eval-harness/commit/a7aa5865697686b1b54c3215a985c89beb3d470b))

## [1.53.3](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.53.2...v1.53.3) (2026-10-02)


### Bug Fixes

* **claude-code:** forward OpenShell's Vertex placeholder switches, and let the judges use the placeholder token ([60f12fc](https://github.com/opendatahub-io/agent-eval-harness/commit/60f12fc727b28b2f0d5f2a66db04e14d9aabef80))

## [1.53.2](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.53.1...v1.53.2) (2026-09-29)


### Bug Fixes

* **eval-run:** compile and merge both eval.yaml permission lists in every workspace builder ([7aae678](https://github.com/opendatahub-io/agent-eval-harness/commit/7aae6789b323691ae22fe3961c418df7f4b345e2))

## [1.53.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.53.0...v1.53.1) (2026-09-29)


### Bug Fixes

* **claude-code:** stop forwarding the operator's GCP credential locations under an OpenRouter plan ([e86b788](https://github.com/opendatahub-io/agent-eval-harness/commit/e86b788510755e51997938c8ba967e26a7d73dfa)), closes [#234](https://github.com/opendatahub-io/agent-eval-harness/issues/234)
* **eval-run:** add absolute-workspace twins for relative-path Bash permission rules ([8bd409c](https://github.com/opendatahub-io/agent-eval-harness/commit/8bd409c58db06e89c152084e72b3fc6008063306)), closes [#233](https://github.com/opendatahub-io/agent-eval-harness/issues/233)

# [1.53.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.52.0...v1.53.0) (2026-09-28)


### Features

* **providers:** direct OpenRouter transport on Kubernetes and EvalHub, provider docs (spec 014, PR-7) ([f7a204e](https://github.com/opendatahub-io/agent-eval-harness/commit/f7a204e6dbf2c033ab89c45bc09ac0379aae70c1))

# [1.52.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.51.0...v1.52.0) (2026-09-28)


### Features

* **workspace:** carry the project's settings hooks into the run settings ([8cb2ea6](https://github.com/opendatahub-io/agent-eval-harness/commit/8cb2ea62e376067b63660156dd72ac7cd3db0972))

# [1.51.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.50.0...v1.51.0) (2026-09-28)


### Features

* **providers:** full preflight, audit-aware pooling and the key guardrail (spec 014, PR-6) ([e31c7c9](https://github.com/opendatahub-io/agent-eval-harness/commit/e31c7c9b4147023dde932465b1739a5beabc38bf)), closes [#26](https://github.com/opendatahub-io/agent-eval-harness/issues/26)

# [1.50.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.49.2...v1.50.0) (2026-09-25)


### Features

* **providers:** direct OpenRouter transport for claude-code and Harbor podman (spec 014, PR-5) ([1ae1bf4](https://github.com/opendatahub-io/agent-eval-harness/commit/1ae1bf4ce9f5abe6022a4077f313ab0a36153c4a))

## [1.49.2](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.49.1...v1.49.2) (2026-09-22)


### Bug Fixes

* **eval-analyze:** address review — scope stdout-only rule to output judges, clarify bare {{ outputs }} ([fa851b9](https://github.com/opendatahub-io/agent-eval-harness/commit/fa851b9c9f5fd1a321119c7b70f91c39c3542fda))
* **eval-analyze:** detect stdout-only skills and use {{ conversation }} in judge prompts ([c71d684](https://github.com/opendatahub-io/agent-eval-harness/commit/c71d6844cae0f97da5e94389356cd4546a5281e1))

## [1.49.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.49.0...v1.49.1) (2026-09-15)


### Bug Fixes

* **judges:** fence structured file access in judge prompts (CWE-1427) ([669ebe7](https://github.com/opendatahub-io/agent-eval-harness/commit/669ebe782e6727de42ed3a9a92b0ac050eaa9bcf))
* **judges:** re-fence tojson; fence file values via finalize, not __str__ ([34d08fa](https://github.com/opendatahub-io/agent-eval-harness/commit/34d08fae5a202239d870417740b2fd794a50bafd))

# [1.49.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.48.0...v1.49.0) (2026-09-15)


### Bug Fixes

* **judges:** address CodeRabbit review on OpenAI judge path ([216cf98](https://github.com/opendatahub-io/agent-eval-harness/commit/216cf981f7e535ab53f4762e2b0667141729301e))
* **judges:** address CodeRabbit review on the rebased PR ([041fb65](https://github.com/opendatahub-io/agent-eval-harness/commit/041fb655aa43dd1504620576f21316addbef9ace))
* **judges:** strip provider prefix in all judge-model consumers; tighten validation ([a20b82a](https://github.com/opendatahub-io/agent-eval-harness/commit/a20b82ace986afb947332649a869a79c54a39235))


### Features

* **judges:** route judge backend by model provider, independent of runner ([03c0588](https://github.com/opendatahub-io/agent-eval-harness/commit/03c0588ef9c2e839e0ce14615f479ede9653ac6f)), closes [#213](https://github.com/opendatahub-io/agent-eval-harness/issues/213) [#213](https://github.com/opendatahub-io/agent-eval-harness/issues/213) [#1](https://github.com/opendatahub-io/agent-eval-harness/issues/1) [#2](https://github.com/opendatahub-io/agent-eval-harness/issues/2) [#5](https://github.com/opendatahub-io/agent-eval-harness/issues/5)

# [1.48.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.47.2...v1.48.0) (2026-09-14)


### Bug Fixes

* address CodeRabbit follow-up review on the Cursor runner ([5d61c08](https://github.com/opendatahub-io/agent-eval-harness/commit/5d61c08cd7ce74cf4fe32a8ce1227fcf28270345))
* address review findings on the Cursor runner + judge paths ([5339c01](https://github.com/opendatahub-io/agent-eval-harness/commit/5339c010a4b95ce35e35942d4bc3589f53cd98e1))
* **lint+docs:** trim SKILL.md under skillsaw budget; address CodeRabbit doc notes ([49f6fa9](https://github.com/opendatahub-io/agent-eval-harness/commit/49f6fa949dd157ed354a78fb6e6f79417fe077d4))


### Features

* add Cursor as an eval runner ([45a2377](https://github.com/opendatahub-io/agent-eval-harness/commit/45a237722086ac774c0b05392f9b97819787928a))

## [1.47.2](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.47.1...v1.47.2) (2026-09-02)


### Bug Fixes

* **report:** clarify the analysis author model is the orchestrator's own, not the subject ([03806f4](https://github.com/opendatahub-io/agent-eval-harness/commit/03806f463643156d4470b018d5f0f5c34c4b7e2f))

## [1.47.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.47.0...v1.47.1) (2026-09-02)


### Bug Fixes

* **eval-analyze:** derive eval.md name from the config so flat layouts don't collide ([fa9fa6e](https://github.com/opendatahub-io/agent-eval-harness/commit/fa9fa6e8384396d4e28dec5a6b095532857e98fa))

# [1.47.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.46.0...v1.47.0) (2026-09-01)


### Features

* **scoring:** fence evaluated material and target the guard at the boundary ([a4938dc](https://github.com/opendatahub-io/agent-eval-harness/commit/a4938dc343799f616afbcee22536cb9866ce7772))
* **scoring:** mark graded material as untrusted in LLM judge system prompts ([a637e18](https://github.com/opendatahub-io/agent-eval-harness/commit/a637e18a8f05987e652057fc32cc260b6582e83c))

# [1.46.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.45.0...v1.46.0) (2026-09-01)


### Bug Fixes

* **judges:** guard injected exemplars as untrusted and defer excerpt I/O ([ffa8e0a](https://github.com/opendatahub-io/agent-eval-harness/commit/ffa8e0ae91e3da76398e07f232424a64b225b8bc))
* **judges:** reject examples on the reserved pairwise judge and cap excerpt reads ([828b4ca](https://github.com/opendatahub-io/agent-eval-harness/commit/828b4cac08f4244d3f75d44052ebd996ad8d0ac6))


### Features

* **judges:** few-shot examples harvested from human review labels ([c0219fa](https://github.com/opendatahub-io/agent-eval-harness/commit/c0219fa4fa07b471d2dc372137c5863a7689972c))

# [1.45.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.44.0...v1.45.0) (2026-08-31)


### Features

* **judges:** binary-first judge authoring guidance and prompt template ([6c0a22b](https://github.com/opendatahub-io/agent-eval-harness/commit/6c0a22b351be5c9b081456b7609e2090695e3b71))

# [1.44.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.43.2...v1.44.0) (2026-08-31)


### Features

* **scoring:** elicit judge rationale before verdict in structured outputs ([17ddd70](https://github.com/opendatahub-io/agent-eval-harness/commit/17ddd704b53e3f2eede3ae98030f459d9598404b))

## [1.43.2](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.43.1...v1.43.2) (2026-08-31)


### Bug Fixes

* **config:** skip hidden files and dirs in eval-config discovery ([9e59428](https://github.com/opendatahub-io/agent-eval-harness/commit/9e59428a34da00054c0022f4890632bb2e49faef))

## [1.43.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.43.0...v1.43.1) (2026-08-27)


### Bug Fixes

* **staging:** copy plugin-root .mcp.json into isolated eval workspaces ([76b63b6](https://github.com/opendatahub-io/agent-eval-harness/commit/76b63b629759c83f24fb5b2d697b82a00c5a94f9))
* **staging:** document isolated MCP coverage limits ([e82bff4](https://github.com/opendatahub-io/agent-eval-harness/commit/e82bff462a527e8a6ebd55eae8e8c67ca3ef22dc))
* **staging:** keep optional-file comment generic ([e437376](https://github.com/opendatahub-io/agent-eval-harness/commit/e437376eaa880b42b9d2d7110318da250ddcb411))

# [1.43.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.42.1...v1.43.0) (2026-08-27)


### Features

* **workspace:** symlink-free {dest, source} shared files, portable across substrates ([ea61ddf](https://github.com/opendatahub-io/agent-eval-harness/commit/ea61ddfb1d5e738eb5459998bb9aba849b8e084f)), closes [#203](https://github.com/opendatahub-io/agent-eval-harness/issues/203)

## [1.42.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.42.0...v1.42.1) (2026-08-26)


### Bug Fixes

* **workspace:** materialize listed file symlinks in workspace.files ([1d2e296](https://github.com/opendatahub-io/agent-eval-harness/commit/1d2e296676729a29e7e42691d5e43acc6dedfde2))
* **workspace:** tighten symlink allowlist and warn on nested skips ([2683913](https://github.com/opendatahub-io/agent-eval-harness/commit/26839136acd3937ab4297e6d9104548f0f983d04))

# [1.42.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.41.2...v1.42.0) (2026-08-26)


### Bug Fixes

* **hermetic:** reject the reserved "*" key at the registry boundary ([703543d](https://github.com/opendatahub-io/agent-eval-harness/commit/703543d44e04194abe8aaa41a0f7c5fdaa3027e2))


### Features

* **workspace:** hermetic-by-default plugin isolation via enabledPlugins ([8227eba](https://github.com/opendatahub-io/agent-eval-harness/commit/8227eba186d33480550ecf4e0328f998f4c98ca7))

## [1.41.2](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.41.1...v1.41.2) (2026-08-25)


### Bug Fixes

* **evalhub:** resolve _run_with_client names from its own scope ([58d54a0](https://github.com/opendatahub-io/agent-eval-harness/commit/58d54a054dbda16479c567fba2e5a9cdb631f820))

## [1.41.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.41.0...v1.41.1) (2026-08-24)


### Bug Fixes

* **config:** stop warning that the pairwise judge has no score_range ([d2ba106](https://github.com/opendatahub-io/agent-eval-harness/commit/d2ba106480c7bf5eb9113095740a0f446a4c43f6)), closes [#182](https://github.com/opendatahub-io/agent-eval-harness/issues/182)

# [1.41.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.40.2...v1.41.0) (2026-08-21)


### Bug Fixes

* **hooks:** close the pidfile launch race and bound the Stop-guard scan ([6941771](https://github.com/opendatahub-io/agent-eval-harness/commit/6941771869155d43790cc7a0cbe3c31cb13b258e))
* **hooks:** detect live executor via pidfile, not transcript parsing ([5ce1983](https://github.com/opendatahub-io/agent-eval-harness/commit/5ce1983c26b004b4a524b53751da75b9609e6c0b))
* **hooks:** harden Stop guard state and transcript handling ([df06586](https://github.com/opendatahub-io/agent-eval-harness/commit/df0658618e16aa85b8e7dfea71344d129e7524de))
* **stop-guard:** harden pid handling, escape hatch, and pidfile writes ([f7658b9](https://github.com/opendatahub-io/agent-eval-harness/commit/f7658b9afdb47a82fe7da5f5861173bcf8729c1c))


### Features

* **hooks:** add Stop guard to prevent background-kill empty-green evals ([8f97ba3](https://github.com/opendatahub-io/agent-eval-harness/commit/8f97ba310880a31279d45391a11ff65b59d1629f))

## [1.40.2](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.40.1...v1.40.2) (2026-08-21)


### Bug Fixes

* **execute:** persist permission_denials into run_result.json ([e7f48cf](https://github.com/opendatahub-io/agent-eval-harness/commit/e7f48cf0bf16c3631d9b8fd8392db84a724a68cd))
* **execute:** surface denials in aggregates; broaden the contract test ([f7db702](https://github.com/opendatahub-io/agent-eval-harness/commit/f7db702973aba2ef63c8cc7b4c4389e65bde28ba))

## [1.40.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.40.0...v1.40.1) (2026-08-21)


### Bug Fixes

* **claude-code:** apply the billed-cost rule on the timeout path too ([7446181](https://github.com/opendatahub-io/agent-eval-harness/commit/74461819fa36141807d06f5eb00c84b99e4f23f7))
* **claude-code:** stop mislabeling cases whose background tasks were killed ([e037a19](https://github.com/opendatahub-io/agent-eval-harness/commit/e037a196ce02607768b6dfe6c3b36afad81aeb23))

# [1.40.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.39.5...v1.40.0) (2026-08-21)


### Bug Fixes

* **claude-code:** contain symlinked copy roots; survive symlink loops ([0e2118b](https://github.com/opendatahub-io/agent-eval-harness/commit/0e2118b5c0b679fd1a2040e2326a5a06418ab69a))
* **claude-code:** copy staged roots from the checked canonical path ([7482473](https://github.com/opendatahub-io/agent-eval-harness/commit/7482473e0a2434a10f1b97dae2cfaf9a8c521bed))
* **claude-code:** keep staging out of repo mode and out of collected artifacts ([b89b4fa](https://github.com/opendatahub-io/agent-eval-harness/commit/b89b4fa3f0b96cd1593385ac4f437b04772cd5aa))
* **claude-code:** propagate broken plugin configs; refuse escaping symlinks ([a8713db](https://github.com/opendatahub-io/agent-eval-harness/commit/a8713db8430223166322291040c6ccbf231daa53))


### Features

* **claude-code:** optionally stage plugin dirs inside the workspace ([4e4e26e](https://github.com/opendatahub-io/agent-eval-harness/commit/4e4e26e01bc33421ae2e74fb1627204feabde9d0))
* **claude-code:** stage plugin dirs into the workspace unconditionally ([e23e2c8](https://github.com/opendatahub-io/agent-eval-harness/commit/e23e2c895b9e892b27bca921119f9a799144ecd8))

## [1.39.5](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.39.4...v1.39.5) (2026-08-20)


### Bug Fixes

* **claude-code:** report only the confirmed import failure; force bootstrap failure in the hook test ([6f28584](https://github.com/opendatahub-io/agent-eval-harness/commit/6f2858438c128d9c29d6f9ca4808a04d89e096cd))
* **claude-code:** stop losing interception evidence across the hook and telemetry path ([a9f403b](https://github.com/opendatahub-io/agent-eval-harness/commit/a9f403bedf03f0d5afbd213fa631220a4265a3e8))

## [1.39.4](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.39.3...v1.39.4) (2026-08-18)


### Bug Fixes

* **claude-code:** fail the run when the agent does not recognise the skill ([d541dc6](https://github.com/opendatahub-io/agent-eval-harness/commit/d541dc6b8a546a5d124bf90d57360f3299fce800))
* **claude-code:** suppress only on positive evidence of work ([b1a8ade](https://github.com/opendatahub-io/agent-eval-harness/commit/b1a8ade3b949d32c4537bdbfc5bdad82f749fde8))
* **validate:** require plugin_dirs when the skill is not auto-discoverable ([61f144c](https://github.com/opendatahub-io/agent-eval-harness/commit/61f144caf945abc79693a70479429546cb1c50f0))
* **validate:** verify plugin_dirs actually export the skill ([aa1be78](https://github.com/opendatahub-io/agent-eval-harness/commit/aa1be78be65dd73e1d06bf4a286cc30184562d4c))

## [1.39.3](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.39.2...v1.39.3) (2026-08-17)


### Bug Fixes

* **hooks:** survive a hook model that rejects `temperature` ([ffbc493](https://github.com/opendatahub-io/agent-eval-harness/commit/ffbc493b79b61c59e8ab853519e5f41a9310d439))

## [1.39.2](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.39.1...v1.39.2) (2026-08-14)


### Bug Fixes

* **score:** anchor the frontmatter delimiters to line boundaries ([553df7e](https://github.com/opendatahub-io/agent-eval-harness/commit/553df7ee4fde055e6ab5b7c962f272734d459dd1))
* **score:** judge the drift on evidence, not on case-001 alone ([19f215c](https://github.com/opendatahub-io/agent-eval-harness/commit/19f215c14b77cb9a7cf7de2f4447e32e50bd1048))
* **score:** only warn about fields the judge actually requires ([7aa063a](https://github.com/opendatahub-io/agent-eval-harness/commit/7aa063a65dd4b04ce6009c1aa1ddda6f5c571d75))
* **score:** report drift on evidence instead of guessing intent ([5d9c8f9](https://github.com/opendatahub-io/agent-eval-harness/commit/5d9c8f96358d0946fa7d5eb14b0ab1172194de85))
* **score:** the stale-field probe must never abort a run ([adcd83e](https://github.com/opendatahub-io/agent-eval-harness/commit/adcd83ec0d04c43481629c36fcb8682ed1087aa5))
* warn on stale inline frontmatter fields ([4451ad8](https://github.com/opendatahub-io/agent-eval-harness/commit/4451ad8564bf86d89509263f11dbf2cf8afe5e0c))

## [1.39.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.39.0...v1.39.1) (2026-08-14)


### Bug Fixes

* **report,mlflow:** a breach the table can't display is still a breach ([b0b6d38](https://github.com/opendatahub-io/agent-eval-harness/commit/b0b6d38769a40b39b3af2dbf94ded540adb41fe1))
* **report,mlflow:** agree with the CLI on what counts as a regression ([3fd316f](https://github.com/opendatahub-io/agent-eval-harness/commit/3fd316f8b5c2cd20aeb3502c3a677dcff2dd3f5b))

# [1.39.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.38.0...v1.39.0) (2026-08-14)


### Bug Fixes

* **config:** reject a non-finite score_range, and say what clamping does ([6d383a3](https://github.com/opendatahub-io/agent-eval-harness/commit/6d383a366cee9da58518bfa1e7d016a1244f1c1b))
* **config:** warn when a clamped reward judge is scored off [0, 1] ([624ffd5](https://github.com/opendatahub-io/agent-eval-harness/commit/624ffd57c739c741fbe52084e8d9040acc99ad92))
* **reward:** normalize each composed judge over its own score_range ([c3fae09](https://github.com/opendatahub-io/agent-eval-harness/commit/c3fae09215ec802a4f280d74b51acb298594bfd6))


### Features

* **config:** make reward.score_range an explicit, deprecated fallback ([129ba16](https://github.com/opendatahub-io/agent-eval-harness/commit/129ba16e2d5a6f921bdd826575d0154fd1639b58))

# [1.38.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.37.4...v1.38.0) (2026-08-14)


### Bug Fixes

* address Codex and Harbor review feedback ([a00ac06](https://github.com/opendatahub-io/agent-eval-harness/commit/a00ac06a1d2b07d5867edc2265aab6ef77c58b50))
* address external review-panel findings (rounds 1-2) ([970f3e0](https://github.com/opendatahub-io/agent-eval-harness/commit/970f3e043d19c0d29fd33568e0a79a445721454f))
* address final review round and scrub review residue ([c4771a5](https://github.com/opendatahub-io/agent-eval-harness/commit/c4771a59db965a6b1839c84eaaf32be68b1324b4))
* address independent deep-review round (events, codex, tasks, results) ([71cd2ce](https://github.com/opendatahub-io/agent-eval-harness/commit/71cd2cedafeb1dbbee510b5e03f97ba7ef1c80a4))
* address remaining CodeRabbit review threads ([0ce8c55](https://github.com/opendatahub-io/agent-eval-harness/commit/0ce8c55968cd667e64e0518bf1a8e61f0ea54171))
* **container:** install Codex platform binary ([b404f6e](https://github.com/opendatahub-io/agent-eval-harness/commit/b404f6e055c2f4c59278ced829161c29fe3b77b1))
* guard direct-store result-event fields against malformed values ([e2e7d98](https://github.com/opendatahub-io/agent-eval-harness/commit/e2e7d98d9c1d9a9b58258fec24060f423118088b))
* **harbor:** address Codex review findings ([f18e7e2](https://github.com/opendatahub-io/agent-eval-harness/commit/f18e7e2a9105382edd0d1273446cc291a917ff6b))
* **harbor:** address deep-review panel findings ([60886b7](https://github.com/opendatahub-io/agent-eval-harness/commit/60886b78090645a1b61d4c52a70efb50d89e3850))
* **harbor:** carry runner.system_prompt into task instructions ([7d718fa](https://github.com/opendatahub-io/agent-eval-harness/commit/7d718facfedf8125568fba1ef60398c084873f8e))
* **harbor:** forward plugin skill roots to every agent, not just Codex ([c5a96af](https://github.com/opendatahub-io/agent-eval-harness/commit/c5a96af795931488383a620ce44d89972e216942))
* **harbor:** prefer the interpreter's own harbor CLI over PATH ([5134b24](https://github.com/opendatahub-io/agent-eval-harness/commit/5134b2477dd159f32969530a6313f6aee9d1330c))
* **harbor:** record the effort Harbor's Codex agent actually applied ([f663853](https://github.com/opendatahub-io/agent-eval-harness/commit/f6638534890ab9a0cf1799210cd865487c33a606))
* **harbor:** surface unjudged steps in the run output ([4cd4109](https://github.com/opendatahub-io/agent-eval-harness/commit/4cd4109c80a7267ed697c15fea361bf3448bc6ad))
* harden plugin manifest handling and marker reads ([b0fe76d](https://github.com/opendatahub-io/agent-eval-harness/commit/b0fe76de215f45c45196da0a04303709f029c797))
* per-case console lines no longer print $0.00 for unknown cost ([6b3bca6](https://github.com/opendatahub-io/agent-eval-harness/commit/6b3bca6ab1de45dce71e30e255f7bdd716f616b0))
* polish Codex event parsing and Harbor task-dir guard messages ([cf1bc9e](https://github.com/opendatahub-io/agent-eval-harness/commit/cf1bc9e0a4bc1c89137785a7328ee1215a7f4631))
* track explicit Codex document reads ([62152de](https://github.com/opendatahub-io/agent-eval-harness/commit/62152ded7f03049fe21b363515bc61081c1f560f))


### Features

* **codex:** estimate local run cost via LiteLLM pricing ([f63157d](https://github.com/opendatahub-io/agent-eval-harness/commit/f63157d186c9cb1e814cadc1f2ceb29003a0e21d))
* **harbor:** add Codex runner and data mounts ([03271c6](https://github.com/opendatahub-io/agent-eval-harness/commit/03271c677a46bb280a3a6395422bcab85c545acf))

## [1.37.4](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.37.3...v1.37.4) (2026-08-13)


### Bug Fixes

* **config:** validate judge score scales at load ([1e9c4bd](https://github.com/opendatahub-io/agent-eval-harness/commit/1e9c4bd724f9cd14b0b1b898a9a9d820ae37e296)), closes [#182](https://github.com/opendatahub-io/agent-eval-harness/issues/182)
* **config:** warn the judge that actually runs unbounded ([0e58c2b](https://github.com/opendatahub-io/agent-eval-harness/commit/0e58c2b63f1f9d42fc9f2d6bc04b45d50306b4f8))
* **eval-analyze:** stop the validator rejecting score_range ([b67c53f](https://github.com/opendatahub-io/agent-eval-harness/commit/b67c53fc88eb63f3fdaac3bc7d0f7ac5beefaea0))
* **judges:** close the gaps an adversarial pass found in this series ([feb1db7](https://github.com/opendatahub-io/agent-eval-harness/commit/feb1db79fcc27e96c4e990162d919987375c6a2f))
* **judges:** honor score_range in LLM judge prompt, schema, and scoring ([baffec3](https://github.com/opendatahub-io/agent-eval-harness/commit/baffec39d8002fad06c25165361f5fd8a79e5b43)), closes [#182](https://github.com/opendatahub-io/agent-eval-harness/issues/182)
* **judges:** keep the sign when the scale goes below zero ([66f587b](https://github.com/opendatahub-io/agent-eval-harness/commit/66f587bcd9a03d2465445928ed3d8f6ad563430d))
* **judges:** read integer-ness off the declared bounds ([326c62f](https://github.com/opendatahub-io/agent-eval-harness/commit/326c62fba8cd5a2aaf118e87cbf2946491872e6d))
* **judges:** reject a non-finite judge value ([add4b12](https://github.com/opendatahub-io/agent-eval-harness/commit/add4b12969dbe57a37c03c8d0e89c2e16e1f3ae0))
* **judges:** round an agent verdict on the scale its contract states ([b9996a4](https://github.com/opendatahub-io/agent-eval-harness/commit/b9996a40e21a62b6ef3f81668cb5fbdbc7b6674d))
* **judges:** tell the MLflow fallback judge its scale ([61394b2](https://github.com/opendatahub-io/agent-eval-harness/commit/61394b208d390af17661da2328dd397049cbedda))
* **judges:** validate the score range without rewriting the value ([0e8e6c8](https://github.com/opendatahub-io/agent-eval-harness/commit/0e8e6c815e6c6ac64f547b0ce001ee33a3435d09))
* **report:** keep the fractional part of a declared score_range ([3e3e52b](https://github.com/opendatahub-io/agent-eval-harness/commit/3e3e52b2310a45a5ddb10b9da7d4c8e9571cc092)), closes [#182](https://github.com/opendatahub-io/agent-eval-harness/issues/182)
* **report:** pass judge_ranges through the reward overview ([e3aaceb](https://github.com/opendatahub-io/agent-eval-harness/commit/e3aaceb674c6334b03c728f5bd2dd83f08c42eef))
* **report:** size the histogram axis before truncating to bins ([4b6743c](https://github.com/opendatahub-io/agent-eval-harness/commit/4b6743ca4ff89b29ec0c05b87afeaa9befb47773))
* **reward:** an unscored trial is not a perfect trial ([37f3a29](https://github.com/opendatahub-io/agent-eval-harness/commit/37f3a293befc88c7973c440948dc5e3ab6fbe5ee))
* **score:** keep max_error_rate working on a persisted summary ([cd8b40d](https://github.com/opendatahub-io/agent-eval-harness/commit/cd8b40dca84d25130fc74f145b8bbb0f1b0c2532))
* **score:** name the real cause of an unavailable metric, and gate coverage ([3585140](https://github.com/opendatahub-io/agent-eval-harness/commit/35851406168893009821cbe11fb6ea37900ae996))

## [1.37.3](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.37.2...v1.37.3) (2026-08-12)


### Bug Fixes

* **anova:** make the missing-[anova]-extra error reachable and actionable ([018636e](https://github.com/opendatahub-io/agent-eval-harness/commit/018636e629418476764eba13909484d65426e641))
* **anova:** point the install hint at .eval-venv, keep the missing module name ([ac704db](https://github.com/opendatahub-io/agent-eval-harness/commit/ac704db14e88085d8e53da191d0c6ff579261307))
* **anova:** shell-quote the generated install command ([0360453](https://github.com/opendatahub-io/agent-eval-harness/commit/036045371bba72b82f78f5fdcec8f0d72a90c284))
* **venv:** activate the venv in eval-anova/check/compare scripts ([be4ad8e](https://github.com/opendatahub-io/agent-eval-harness/commit/be4ad8ee66685888d7aecae10038149f13ed2bef))

## [1.37.2](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.37.1...v1.37.2) (2026-08-11)


### Bug Fixes

* **deps:** report eval.yaml discovery failures instead of silently degrading ([bf26404](https://github.com/opendatahub-io/agent-eval-harness/commit/bf264040993ddbdeb93762b38cc7fcee8ded0207))
* **deps:** resolve venv deps from every eval.yaml, not just the first ([52d8a28](https://github.com/opendatahub-io/agent-eval-harness/commit/52d8a28ab2fe075ea6ee283b74fe0a94de324fd4))

## [1.37.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.37.0...v1.37.1) (2026-08-11)


### Bug Fixes

* **eval-analyze:** drop phantom `events` var; cover validate_eval; tighten test ([c6358f5](https://github.com/opendatahub-io/agent-eval-harness/commit/c6358f55664fb3d653c56c5f071a61fba64114a2)), closes [#179](https://github.com/opendatahub-io/agent-eval-harness/issues/179)
* resolve bootstrap venv via realpath; sync validate_eval Jinja vars ([1d3ac2b](https://github.com/opendatahub-io/agent-eval-harness/commit/1d3ac2b40c79a685ca86c37fe96905245447f472))
* **tests:** replace vacuous bootstrap test with sys.path check ([fa2227f](https://github.com/opendatahub-io/agent-eval-harness/commit/fa2227f67b4be8b575441974ca24732584d3d0e0))

# [1.37.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.36.0...v1.37.0) (2026-08-10)


### Bug Fixes

* address CodeRabbit review feedback ([2aef4d3](https://github.com/opendatahub-io/agent-eval-harness/commit/2aef4d375d6e1b146255e8ac7853b6cd9b3e837f))
* address CodeRabbit round-2 feedback ([7ebb34d](https://github.com/opendatahub-io/agent-eval-harness/commit/7ebb34dc9ef2f81d0317a3dc64a18e0e06fc1656))
* address re-review feedback from astefanutti ([82b4027](https://github.com/opendatahub-io/agent-eval-harness/commit/82b4027e5ed5d58ac9bd93c43c3dbfc82ef6189c))
* **eval-check:** address CodeRabbit findings on the previous commit ([2baa972](https://github.com/opendatahub-io/agent-eval-harness/commit/2baa972514174a93201ddb9e6db965f89150b2ea))
* **eval-check:** reduce reference-checker false positives ([a30a89e](https://github.com/opendatahub-io/agent-eval-harness/commit/a30a89eba79bab433f89bb548293295e23ea818f))


### Features

* add cross-component reference validation to eval-check ([d2899a2](https://github.com/opendatahub-io/agent-eval-harness/commit/d2899a21a50f0cea1b7de7ce903a840d73847da2))
* add inline check syntax validation (ast.parse) ([520bcc1](https://github.com/opendatahub-io/agent-eval-harness/commit/520bcc18d5ee8f969c2e101596c31271a501d531))

# [1.36.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.35.1...v1.36.0) (2026-08-07)


### Bug Fixes

* **report:** reject path traversal + symlinks in dataset case/input resolution ([2d378ad](https://github.com/opendatahub-io/agent-eval-harness/commit/2d378ad08b40065a92955e1ef26b0c256b0f80bd))


### Features

* **report:** rename default title to "Agent Eval Report" + make it configurable ([9a63db3](https://github.com/opendatahub-io/agent-eval-harness/commit/9a63db3a20ea2b2f0dbc038ccff400a22707883b))
* **report:** scannable, diff-safe per-case Input/Output sections ([2b8c74d](https://github.com/opendatahub-io/agent-eval-harness/commit/2b8c74d3f351c649c04d2a1d1caeab46239cbf59))

## [1.35.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.35.0...v1.35.1) (2026-08-06)


### Bug Fixes

* **report:** band reward-overview cells by score_range, drop (0,1) guess ([bfa6f15](https://github.com/opendatahub-io/agent-eval-harness/commit/bfa6f159ba2a3b677280de367591c5acb91ab427))
* **report:** color per-case cells by score_range, not aggregate min_mean ([d014d1a](https://github.com/opendatahub-io/agent-eval-harness/commit/d014d1acfd1567431d9822fbba913dd42116a5f0))
* **report:** use each judge's score_range for per-case histograms ([79d2681](https://github.com/opendatahub-io/agent-eval-harness/commit/79d268119d8358188a8534902cbec6fb5498d750))
* **report:** validate score_range from raw config; cap histogram bins ([b515fdc](https://github.com/opendatahub-io/agent-eval-harness/commit/b515fdc383b85d2d96bf3008cb288722b331f2d9))

# [1.35.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.34.0...v1.35.0) (2026-08-06)


### Bug Fixes

* **runner:** validate permission_mode by type, not truthiness ([fa917f5](https://github.com/opendatahub-io/agent-eval-harness/commit/fa917f5113976c38ee60ca8fb73120f30a8aa269))


### Features

* **runner:** add runner.permission_mode for Claude Code ([3d8a479](https://github.com/opendatahub-io/agent-eval-harness/commit/3d8a479183dacdf4f7ba08cdbd522fa3aedb0dba))

# [1.34.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.33.0...v1.34.0) (2026-08-05)


### Bug Fixes

* **mlflow:** harden harness-snapshot load and MLflow fetch ([f12a568](https://github.com/opendatahub-io/agent-eval-harness/commit/f12a5683d5857eac20a7ae6b2c2bab33292306ed))
* **mlflow:** keep mlflow.runName immutable on tag merge ([14b77df](https://github.com/opendatahub-io/agent-eval-harness/commit/14b77df0169eae29eb0bf92a10dd3047959b813d))


### Features

* **mlflow:** load harness-snapshot from disk and MLflow ([3ee0025](https://github.com/opendatahub-io/agent-eval-harness/commit/3ee002569b2a3aea4479f7d8918543db35f147cb))

# [1.33.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.32.0...v1.33.0) (2026-08-05)


### Bug Fixes

* **harbor:** emit multi_step_reward_strategy as a root-level key ([45156dc](https://github.com/opendatahub-io/agent-eval-harness/commit/45156dcde8164c0a89c3c201e64b19d3a7a35a69))
* **harbor:** share TOML serializer + UTF-8 writes for multi-step tasks ([39d1e73](https://github.com/opendatahub-io/agent-eval-harness/commit/39d1e73496e91b7b4c90215fc930612b6e546d20)), closes [#148](https://github.com/opendatahub-io/agent-eval-harness/issues/148)
* **multi-step:** address CodeRabbit review on [#172](https://github.com/opendatahub-io/agent-eval-harness/issues/172) ([4a08221](https://github.com/opendatahub-io/agent-eval-harness/commit/4a082217481607e12da166d615511591900dfebe))
* **multi-step:** surface non-zero step exit codes; strengthen symlink test ([8aa2233](https://github.com/opendatahub-io/agent-eval-harness/commit/8aa2233cd97b08fb21af918cb5f5f7410f7621ba))


### Features

* **config:** multi-step execution.steps + per-step hooks + step-scoped judges ([8e94fa3](https://github.com/opendatahub-io/agent-eval-harness/commit/8e94fa389e40d0d098fea083f6d18252625381a4))
* **execute:** sequential multi-step execution + per-step hooks ([a428f86](https://github.com/opendatahub-io/agent-eval-harness/commit/a428f86daf45c7f1503fca35d0e390a421f8b748))
* **harbor:** generate multi-step [[steps]] task packages (schema 1.4) ([e8bd271](https://github.com/opendatahub-io/agent-eval-harness/commit/e8bd271348329854537f387e463b1af88826383e))
* **score:** per-step judge scoping (JudgeConfig.step) ([6d2ce3f](https://github.com/opendatahub-io/agent-eval-harness/commit/6d2ce3f0af85210c741889f45d347b331154f389))

# [1.32.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.31.0...v1.32.0) (2026-08-05)


### Bug Fixes

* **agent-judge:** address CodeRabbit review ([caa1728](https://github.com/opendatahub-io/agent-eval-harness/commit/caa172850e89a3b42fa28fdc9a57f0d6f32f9da7))
* **agent-judge:** address CodeRabbit round 2 ([bba2959](https://github.com/opendatahub-io/agent-eval-harness/commit/bba2959999a217665a3b4c3cae7104c82b31fb8c))
* **agent-judge:** chain the --no-llm-judges registry-failure error (B904) ([c779f5d](https://github.com/opendatahub-io/agent-eval-harness/commit/c779f5d7efb864d782065b15703b4959991cec86))
* **agent-judge:** copy context for write-capable judges; fix blocking guidance ([6c5567a](https://github.com/opendatahub-io/agent-eval-harness/commit/6c5567ab6ca72100bda2176135e140794c2ddd88))
* **skill:** trim eval-run SKILL.md under the skillsaw context budget ([ca41ff3](https://github.com/opendatahub-io/agent-eval-harness/commit/ca41ff3475173ce583e00828a865a3beea0919f4))


### Features

* **judges:** add first-class agent judge type ([8e80378](https://github.com/opendatahub-io/agent-eval-harness/commit/8e8037885c17fb4dfefb2e3495dcd1130c27836b))
* **report:** group agent judges with LLM judges in the HTML report ([8debd27](https://github.com/opendatahub-io/agent-eval-harness/commit/8debd2783f286c5eddb17580a2b857692cc22637))

# [1.31.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.30.2...v1.31.0) (2026-08-05)


### Features

* pass GCP_SA_ACCESS_TOKEN to AnthropicVertex when available ([9bad5fb](https://github.com/opendatahub-io/agent-eval-harness/commit/9bad5fb4e222106e8dc2a8af183c10ac0648c86a))

## [1.30.2](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.30.1...v1.30.2) (2026-08-05)


### Bug Fixes

* **harbor:** mark task.description truncation with an ellipsis ([2e0714f](https://github.com/opendatahub-io/agent-eval-harness/commit/2e0714f7d63f2f61ce4edb776454c85c2c2a4f19))
* **harbor:** serialize all task.toml string fields, escape U+007F ([d6ee63c](https://github.com/opendatahub-io/agent-eval-harness/commit/d6ee63cb4c7d03e1f467b29ff3bbf8ae364a27d7)), closes [#148](https://github.com/opendatahub-io/agent-eval-harness/issues/148)
* **harbor:** write generated task files as UTF-8 ([c720c35](https://github.com/opendatahub-io/agent-eval-harness/commit/c720c35f248dca9dfb3aaae4e145a99a9d87e13e))

## [1.30.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.30.0...v1.30.1) (2026-07-30)


### Bug Fixes

* **website:** remove the sticky sidebar-header drop shadow ([dffc0c5](https://github.com/opendatahub-io/agent-eval-harness/commit/dffc0c57533506bbe1a6776542f2e3a17fbe6ff7))

# [1.30.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.29.0...v1.30.0) (2026-07-30)


### Bug Fixes

* **eval-anova:** address CodeRabbit findings on kept code ([fb0a68c](https://github.com/opendatahub-io/agent-eval-harness/commit/fb0a68c88f969a14c87baabd5968723dfbef9f34))
* **eval-anova:** clamp report heat colours and bar widths to range ([c76e203](https://github.com/opendatahub-io/agent-eval-harness/commit/c76e2034476cf19b883dde8c33770268254ad52c)), closes [#report](https://github.com/opendatahub-io/agent-eval-harness/issues/report)
* **eval-anova:** derive report labels from data, not a hardcoded roster ([e3bf683](https://github.com/opendatahub-io/agent-eval-harness/commit/e3bf68374d9291de799628143095f6cffa3001c6)), closes [#11](https://github.com/opendatahub-io/agent-eval-harness/issues/11) [#18](https://github.com/opendatahub-io/agent-eval-harness/issues/18)
* **eval-anova:** exclude cases missing from any condition and report them ([2adfa90](https://github.com/opendatahub-io/agent-eval-harness/commit/2adfa90402afd103f919b08f46681c8afba3c246))
* **eval-anova:** HTML-escape user-controlled values in reports (XSS) ([6fab8a0](https://github.com/opendatahub-io/agent-eval-harness/commit/6fab8a006d3610b8e234baacc61ef6162157ddac))
* **eval-anova:** ignore single-level factors when selecting the ANOVA ([b177428](https://github.com/opendatahub-io/agent-eval-harness/commit/b177428aaab5ce6b7999d7f79750f8b211f764ee))
* **eval-anova:** report actual sample size in the non-significant callout ([906c73e](https://github.com/opendatahub-io/agent-eval-harness/commit/906c73e6e688ec94b2e48a438cfbbb13cdfd9bcc)), closes [#10](https://github.com/opendatahub-io/agent-eval-harness/issues/10)
* **eval-anova:** stop misreporting fractional composites as failures ([09a4515](https://github.com/opendatahub-io/agent-eval-harness/commit/09a4515f10bce06a47983840f793a4a8c0d850ac))
* **eval-anova:** treat non-finite/negative F as a degenerate design ([fd9feeb](https://github.com/opendatahub-io/agent-eval-harness/commit/fd9feebd1d0ce5301baae08a765b6ee4fb1be078))


### Features

* **eval-anova:** add eval/anova-example — real maas tasks on the generic path ([9902237](https://github.com/opendatahub-io/agent-eval-harness/commit/9902237e19288115b4e0e8802f560781a84da59f)), closes [#17](https://github.com/opendatahub-io/agent-eval-harness/issues/17)
* **eval-anova:** analyze stats over a directory of standard eval-run runs ([e12f100](https://github.com/opendatahub-io/agent-eval-harness/commit/e12f10088053eb2947bd33108e67770d89915577))
* **eval-anova:** DoE/ANOVA matrix testing skill with harbor-maas-v1 benchmark ([53fb1ce](https://github.com/opendatahub-io/agent-eval-harness/commit/53fb1cef5303eca4b3d34d3550fa2fe64543f9b1)), closes [#104](https://github.com/opendatahub-io/agent-eval-harness/issues/104)
* **eval-anova:** prefer Greenhouse-Geisser corrected p in rm-ANOVA ([32ddc20](https://github.com/opendatahub-io/agent-eval-harness/commit/32ddc20ed6748f3097c1b1f78c0226b45998db2f))
* **eval-anova:** restore tests_pass gate + match original context A/B; add Harbor docs ([33fcb42](https://github.com/opendatahub-io/agent-eval-harness/commit/33fcb4259ba5ed7c7db4cdb5bf5cb07ea8216a64))
* **eval-anova:** runnable matrix orchestrator that fans out over eval-run ([b9ebca5](https://github.com/opendatahub-io/agent-eval-harness/commit/b9ebca52e8b32b32f5874eca2fb4b8f6432f1046)), closes [#5](https://github.com/opendatahub-io/agent-eval-harness/issues/5) [#14](https://github.com/opendatahub-io/agent-eval-harness/issues/14) [#15](https://github.com/opendatahub-io/agent-eval-harness/issues/15) [#17](https://github.com/opendatahub-io/agent-eval-harness/issues/17)
* **eval-compare:** render eval-anova statistics section when present ([efa3306](https://github.com/opendatahub-io/agent-eval-harness/commit/efa330671d874af1b9da118ad0cabb004abead92))
* **eval-run:** --input-override to inject run-level values into case inputs ([#17](https://github.com/opendatahub-io/agent-eval-harness/issues/17)) ([4474605](https://github.com/opendatahub-io/agent-eval-harness/commit/447460565ba928dae8106ad7e77e139f1c320098))
* **eval-run:** add {config_dir} placeholder to the cli runner ([3e0a9f1](https://github.com/opendatahub-io/agent-eval-harness/commit/3e0a9f172e91c7b5f095b686a00eb64b4ee8537f))

# [1.29.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.28.0...v1.29.0) (2026-07-30)


### Features

* **eval-run:** mirror execute console to console.log + no-redirect launch ([9c759a2](https://github.com/opendatahub-io/agent-eval-harness/commit/9c759a21822462a12f6772be028658aeca7702f0))
* **workspace:** auto-set EVAL_RUN_HEADER from --run-id ([fb1576a](https://github.com/opendatahub-io/agent-eval-harness/commit/fb1576a638833b8cb71c391311414929331797aa))

# [1.28.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.27.0...v1.28.0) (2026-07-30)


### Bug Fixes

* address CodeRabbit review feedback ([c379d0a](https://github.com/opendatahub-io/agent-eval-harness/commit/c379d0ae4225de4e60c63fa8472e461393932e27))
* address CodeRabbit review feedback ([55a8fed](https://github.com/opendatahub-io/agent-eval-harness/commit/55a8fedada64365b3da180d1ebb03726e09a63c5))
* **eval-compare:** address review — correctness, robustness, report accuracy ([74f5d1e](https://github.com/opendatahub-io/agent-eval-harness/commit/74f5d1e8fb1808e25ab98149c79c75b8eb449681))
* remove Best Quality badge, strengthen Best Value criteria ([a6d26f8](https://github.com/opendatahub-io/agent-eval-harness/commit/a6d26f85858c73e07579354b27c86152d3828d77))
* remove hardcoded judge/report names from eval-compare ([e8a61b3](https://github.com/opendatahub-io/agent-eval-harness/commit/e8a61b3299fa81264a424a2ad6acee3520e20026))
* simplify Best Value badge — best model considering quality and cost ([b2eaeaf](https://github.com/opendatahub-io/agent-eval-harness/commit/b2eaeaf12ffcf6274e59ff60e8e68c4ce27abb3c))
* use double quotes in f-strings for Python 3.11 compat ([e066dab](https://github.com/opendatahub-io/agent-eval-harness/commit/e066dab9345e846b6a59b2dbda9198a5fa08f06f))


### Features

* add eval-compare skill for cross-model comparison reports ([4c5568e](https://github.com/opendatahub-io/agent-eval-harness/commit/4c5568ef54f490b12ef6776dc70e4065043e4e64))
* **eval-compare:** light/dark theme matching the per-run reports ([2e95cdd](https://github.com/opendatahub-io/agent-eval-harness/commit/2e95cdd8e438592af501910de08a0026d94c5e50))
* **eval-compare:** make report fully generic + add LLM analysis sections ([9ea4e47](https://github.com/opendatahub-io/agent-eval-harness/commit/9ea4e47e0e44220e33dbd0b264fd743a54a489a7))

# [1.27.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.26.0...v1.27.0) (2026-07-30)


### Bug Fixes

* **stream-capture:** exclude placeholder models from per-model accounting ([2378499](https://github.com/opendatahub-io/agent-eval-harness/commit/2378499cf97d189af27045246630bcccd0a74e31))


### Features

* **harbor:** per-model usage/cost breakdown ([70f79aa](https://github.com/opendatahub-io/agent-eval-harness/commit/70f79aa2955d8e2306eac8b3d46b01a8f1f3fde1))

# [1.26.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.25.0...v1.26.0) (2026-07-30)


### Bug Fixes

* **hooks:** register before_report in the standalone hooks CLI --phase choices ([edd37b7](https://github.com/opendatahub-io/agent-eval-harness/commit/edd37b7e264a83916790abaea83d9c17fe75fe71))
* **report-hooks:** address CodeRabbit [#165](https://github.com/opendatahub-io/agent-eval-harness/issues/165) review ([ad956b4](https://github.com/opendatahub-io/agent-eval-harness/commit/ad956b4ec331d4e5e2fcf29eb71957138767c8aa))


### Features

* **hooks:** add before_report lifecycle phase + gate reward section for judge-only evals ([cbbe9ad](https://github.com/opendatahub-io/agent-eval-harness/commit/cbbe9ad5a7531c9c61fdce17adcd9d6c3edd301d))

# [1.25.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.24.0...v1.25.0) (2026-07-30)


### Bug Fixes

* **eval-analyze:** address review feedback on assess_skills ([6dab5d7](https://github.com/opendatahub-io/agent-eval-harness/commit/6dab5d73a7ac2bd4c24215ddce442885757e0e00))
* **eval-analyze:** address review feedback on scoped tools, EXISTS detection, excerpt fences, and symlinks ([5793cd3](https://github.com/opendatahub-io/agent-eval-harness/commit/5793cd39085acbb42322d32bd365ffb098ed48ed))
* **eval-analyze:** clean up stale --assess guard and Modes block placement ([9270432](https://github.com/opendatahub-io/agent-eval-harness/commit/92704329f2734201fe62db5d0837871fd723e8d8))
* **eval-analyze:** correct EXISTS detection and tool/excerpt handling in assess ([205e4ef](https://github.com/opendatahub-io/agent-eval-harness/commit/205e4ef1f1f8cc99da1ab0842601f04d0947cdc1))
* **eval-analyze:** guard glob results against symlink path traversal ([46b5452](https://github.com/opendatahub-io/agent-eval-harness/commit/46b545267066deedd1079e05416a86fc09cb3e69))
* **eval-analyze:** make out-of-project skill skip actionable ([e51698f](https://github.com/opendatahub-io/agent-eval-harness/commit/e51698fea0a4d9d70356d49e91926730a52584db))
* **eval-analyze:** treat skill_body_excerpt as untrusted data ([220b9a4](https://github.com/opendatahub-io/agent-eval-harness/commit/220b9a430168410d1f258b6f4ce76ef48bb5f6bf))
* **eval-analyze:** trim SKILL.md below 4500 token warn limit ([1b9bd3c](https://github.com/opendatahub-io/agent-eval-harness/commit/1b9bd3c3e9c2a362e95f59cccd9e113059c661b3))
* **eval-analyze:** use correct eval/ directory layout in _has_existing_eval ([73c99d8](https://github.com/opendatahub-io/agent-eval-harness/commit/73c99d80f588379c2b5129842103e20d0172a9ce))


### Features

* **eval-analyze:** add --assess flag to score skills for eval-worthiness ([6aa8163](https://github.com/opendatahub-io/agent-eval-harness/commit/6aa81630b57ea085ce65d7a216ec84fbbf8b3408)), closes [#144](https://github.com/opendatahub-io/agent-eval-harness/issues/144)

# [1.24.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.23.0...v1.24.0) (2026-07-30)


### Bug Fixes

* **collect:** route per-case subagent traces in batch mode ([1150370](https://github.com/opendatahub-io/agent-eval-harness/commit/115037027308c0f2f362abc9bd3cea64a6b0c2e7))
* **eval-run:** warn on silent trace-routing gaps (addresses CodeRabbit [#163](https://github.com/opendatahub-io/agent-eval-harness/issues/163)) ([efb10d6](https://github.com/opendatahub-io/agent-eval-harness/commit/efb10d67f7fc4586c37e9c2ad7dcaf7e628f9902))


### Features

* **events:** preserve extended-thinking (chain-of-thought) in parsed traces ([187f386](https://github.com/opendatahub-io/agent-eval-harness/commit/187f3865266d33dd3dc6640793dc9be536892420))
* **score:** CoT-inclusive {{ reasoning }} judge var + warn on undefined template vars ([70d676d](https://github.com/opendatahub-io/agent-eval-harness/commit/70d676dee3d84112e3077660bd6da8580a024ee0))

# [1.23.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.22.1...v1.23.0) (2026-07-28)


### Bug Fixes

* **mlflow:** attribute thinking to its own turn's step, not the next one ([6f88fd1](https://github.com/opendatahub-io/agent-eval-harness/commit/6f88fd11def7f15742002edcda56c99ad37efd20))
* **mlflow:** clamp trajectory timestamps and backfill reasoning_content ([59a76cf](https://github.com/opendatahub-io/agent-eval-harness/commit/59a76cf6d2f007805e0615cacc0a0998307e8b0b))
* **mlflow:** drop unused imports and use _resolve_skill for Harbor trace names ([a9add3b](https://github.com/opendatahub-io/agent-eval-harness/commit/a9add3b1037897465d272f478f43f4e2cce9dfad))
* **mlflow:** isolate tool-only thinking steps and tighten import skips ([08f0855](https://github.com/opendatahub-io/agent-eval-harness/commit/08f0855e23d5cebb212f11cacb3031c4da3bc110))
* **mlflow:** resolve CodeRabbit findings on trajectory-enriched traces ([be73c30](https://github.com/opendatahub-io/agent-eval-harness/commit/be73c307dbb3985c78dcd866f4d085de84602fa8))


### Features

* **mlflow:** enrich Harbor traces with tool content and step detail ([336799a](https://github.com/opendatahub-io/agent-eval-harness/commit/336799a2bd64e798ed2c12bf9a173af56a6357e1))
* **mlflow:** richer Harbor traces with user turns and thinking ([6141895](https://github.com/opendatahub-io/agent-eval-harness/commit/6141895c4f75cb3125acb858830f22a736e84bcb))

## [1.22.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.22.0...v1.22.1) (2026-07-27)


### Bug Fixes

* cover interactive idle-SIGTERM in session lifecycle warning ([00e8bb9](https://github.com/opendatahub-io/agent-eval-harness/commit/00e8bb978ff298061f82e851941080ded4d334bb))
* require polling after backgrounded execute.py to prevent false-green CI ([b4cf178](https://github.com/opendatahub-io/agent-eval-harness/commit/b4cf178d633cb2861851b3c4bd2a4520ef66b650)), closes [#155](https://github.com/opendatahub-io/agent-eval-harness/issues/155)

# [1.22.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.21.0...v1.22.0) (2026-07-10)


### Bug Fixes

* **agent,mlflow:** forward extra_env in run_skill shim; correct settings env key ([acb3317](https://github.com/opendatahub-io/agent-eval-harness/commit/acb33173e942a17f9c915e428f443915f94293ca))
* **config:** resolve skill/prompt target consistently across substrates ([cfc845b](https://github.com/opendatahub-io/agent-eval-harness/commit/cfc845b44f455158dc499d6b9738c82a818d222c))
* **docs,examples:** migrate example configs to execution.skill; fix broken README example ([20a894c](https://github.com/opendatahub-io/agent-eval-harness/commit/20a894cf98f00d4f5b94b845d11426179b90dfe8))
* **eval-analyze:** eliminate validate_eval false positives ([56551c5](https://github.com/opendatahub-io/agent-eval-harness/commit/56551c5d4c2632e1fdfbf5660b05f63927b04dbe))
* **eval-analyze:** move $ARGUMENTS to end, wire reorganize.py, fix bullets ([c3edc5d](https://github.com/opendatahub-io/agent-eval-harness/commit/c3edc5de8cd4eeefcaa4dfa72ee87d6dcf6ca4be))
* **eval-dataset:** fix setext-heading rule + clarify provenance-independent steps ([ab17539](https://github.com/opendatahub-io/agent-eval-harness/commit/ab175398170332695a5f7ff4c2fdb54782cc2419))
* **eval-run:** honor deny rules, isolate in-repo failures, strict args ([8c1637f](https://github.com/opendatahub-io/agent-eval-harness/commit/8c1637f9c1dc9f3120cdc241a359de7089409a2f))
* **eval:** complete prompt-mode implementation with eval_name() and Jinja2 support ([399ba9b](https://github.com/opendatahub-io/agent-eval-harness/commit/399ba9b9cbcb92a5c42766650191e38a58c24054))
* **eval:** include subagent reads in consulted_docs judge by default ([c86f4f9](https://github.com/opendatahub-io/agent-eval-harness/commit/c86f4f96f3e3aac36751ddc5b2c2d558dce8a941))
* **permissions:** correct + share the path-based deny compiler across substrates ([3d6b60a](https://github.com/opendatahub-io/agent-eval-harness/commit/3d6b60abae8e22b69ba089cd6fec9fe826b50a17))
* **scoring:** annotations text, doc coverage, regression + conversation ([908ce4c](https://github.com/opendatahub-io/agent-eval-harness/commit/908ce4c3d089b30fda8a12580edc6f9e85999f14))
* **skills:** clear skillsaw lint warnings ([c21eb94](https://github.com/opendatahub-io/agent-eval-harness/commit/c21eb948b59ac9e8e1bedd35039670720478caa9))


### Features

* **evaluation:** add prompt-based evaluation mode for direct agent capability testing ([936897a](https://github.com/opendatahub-io/agent-eval-harness/commit/936897ab2779eb6e716f5c26a5e81d6716dda47e))
* **judges:** add tool_trace template variable and enhance consulted_docs ([9374524](https://github.com/opendatahub-io/agent-eval-harness/commit/93745242d94a8d1ee7e04f83e7b23fef2047a985))

# [1.21.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.20.0...v1.21.0) (2026-07-08)


### Bug Fixes

* address [@astefanutti](https://github.com/astefanutti) review findings on PR [#146](https://github.com/opendatahub-io/agent-eval-harness/issues/146) ([2ba452e](https://github.com/opendatahub-io/agent-eval-harness/commit/2ba452e9b85db4cf8af55c9d48856eca316dc45d))
* address CodeRabbit review findings on PR [#146](https://github.com/opendatahub-io/agent-eval-harness/issues/146) ([36c038c](https://github.com/opendatahub-io/agent-eval-harness/commit/36c038c84652af76306517c989f1630bef9cd39d))
* address second review cycle ([a5dc960](https://github.com/opendatahub-io/agent-eval-harness/commit/a5dc9606fa1e0b0370e05a67851a5565d0ea1283))
* **report:** classify builtin judges by kind, not by category ([8206c22](https://github.com/opendatahub-io/agent-eval-harness/commit/8206c2273f1d167c6a63667affc2059498b8bc2b))
* **report:** thread reward_cfg so the report matches the trained reward ([0ae3691](https://github.com/opendatahub-io/agent-eval-harness/commit/0ae36915a70d1e2dd77cede4d3b775a8d595b2c9))
* **score:** normalize raw .jsonl events into the flat schema consumers expect ([4a33038](https://github.com/opendatahub-io/agent-eval-harness/commit/4a33038ef72fb90138319fc4c5f144cfb14cf549))
* **score:** sharpen evidence extraction across runners and shell args ([c1f948c](https://github.com/opendatahub-io/agent-eval-harness/commit/c1f948cf3131d92f09d1a7ffbfb312a7317f35e6))


### Features

* **config:** add per-judge score_range, close report/schema drift ([0a2c3a1](https://github.com/opendatahub-io/agent-eval-harness/commit/0a2c3a1c6daab822226eb28aa250f7e8bb58aa4a))
* **report:** add per-case reward overview table and Harbor scoring improvements ([725842c](https://github.com/opendatahub-io/agent-eval-harness/commit/725842cae6568cf6bc9ee66fdbc269bc629e398c))

# [1.20.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.19.0...v1.20.0) (2026-06-30)


### Features

* **mlflow:** break out cache tokens in trace token usage ([4b9ac26](https://github.com/opendatahub-io/agent-eval-harness/commit/4b9ac2628c61c9fb5c3196192d3b1c3a4df8c1db))

# [1.19.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.18.0...v1.19.0) (2026-06-29)


### Bug Fixes

* **bootstrap:** address CodeRabbit review on OS trust store ([617f62f](https://github.com/opendatahub-io/agent-eval-harness/commit/617f62f99e1efc81bb7b7d0f387b3166288414b0))


### Features

* **bootstrap:** verify TLS against OS trust store when no CA bundle set ([df134af](https://github.com/opendatahub-io/agent-eval-harness/commit/df134af7e4231e1e9553647482bd4f67a9504d71))

# [1.18.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.17.1...v1.18.0) (2026-06-29)


### Bug Fixes

* **mlflow:** address CodeRabbit review on harbor traces ([1c9e0b7](https://github.com/opendatahub-io/agent-eval-harness/commit/1c9e0b75f947b9b38d85fe7fca8b67a60b00de2f))
* **mlflow:** source harbor trace cost/tokens from the transcript ([be78da3](https://github.com/opendatahub-io/agent-eval-harness/commit/be78da3c74a8801560468350f9ccda951806fcaf))


### Features

* **mlflow:** build per-step MLflow traces for harbor runs ([815c615](https://github.com/opendatahub-io/agent-eval-harness/commit/815c6153f8049f1e0d65101a3e02deb04ebeca33))

## [1.17.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.17.0...v1.17.1) (2026-06-26)


### Bug Fixes

* **harbor:** upload files in chunks to avoid E2BIG on large dirs ([aa395ed](https://github.com/opendatahub-io/agent-eval-harness/commit/aa395ed6d4a9e9bfb630f675aedc269a6603a236))

# [1.17.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.16.1...v1.17.0) (2026-06-26)


### Bug Fixes

* **harbor:** address review on exec retry and infra-error surfacing ([54db1e3](https://github.com/opendatahub-io/agent-eval-harness/commit/54db1e3ffbf006c42aaba669de2ee08bc0a1ab82))


### Features

* **harbor:** don't score a missing verifier reward as 0 ([1b41578](https://github.com/opendatahub-io/agent-eval-harness/commit/1b415783ffe4b7c2bbf038cfae299753beeeb3c0))
* **harbor:** retry transient k8s exec establishment failures ([1b121aa](https://github.com/opendatahub-io/agent-eval-harness/commit/1b121aa61d63ec93cbe5e3cbb55c3b7f1e062d68)), closes [hi#parallelism](https://github.com/hi/issues/parallelism)
* **harbor:** surface trials that failed before producing any reward ([e750281](https://github.com/opendatahub-io/agent-eval-harness/commit/e750281566f60b3db9d39e6a5f6b742b1d9db8c7))

## [1.16.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.16.0...v1.16.1) (2026-06-26)


### Bug Fixes

* **harbor:** address review on the venv-activation guard ([526ba2c](https://github.com/opendatahub-io/agent-eval-harness/commit/526ba2c3ba399439c5883d7c80cb32a3015a5533))
* **harbor:** prevent duplicate pod creation from mid-run venv re-exec ([9d0fca6](https://github.com/opendatahub-io/agent-eval-harness/commit/9d0fca65b22bcb9df5cf2908e085636f9dfc17d8))

# [1.16.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.15.0...v1.16.0) (2026-06-25)


### Features

* **harbor:** add reward.judge single-judge mode ([f4c2ec7](https://github.com/opendatahub-io/agent-eval-harness/commit/f4c2ec737556d5707b272abfc592e39827a2c128))

# [1.15.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.14.2...v1.15.0) (2026-06-25)


### Features

* **harbor:** add grpo_reward judge and configurable ([633a8b3](https://github.com/opendatahub-io/agent-eval-harness/commit/633a8b36d1b4204c9ac69897b00eb0361eb4c484))
* **harbor:** bound reward-formula sandbox; fix reward doc accuracy ([0a252a7](https://github.com/opendatahub-io/agent-eval-harness/commit/0a252a783514918622d5183b4b2104666dd67169))
* **harbor:** harden reward config — normalize single-judge, validate formulas ([fcf6a9a](https://github.com/opendatahub-io/agent-eval-harness/commit/fcf6a9ac0cfe29a4180e2517a09ad70726c3365f))

## [1.14.2](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.14.1...v1.14.2) (2026-06-24)


### Bug Fixes

* use explicit None checks so empty strings are not skipped ([5e518e7](https://github.com/opendatahub-io/agent-eval-harness/commit/5e518e798f1da08b350ac3121e514815f2a53c3e))
* validate run_id and baseline as single path segments before path construction (CWE-22) ([017b66a](https://github.com/opendatahub-io/agent-eval-harness/commit/017b66a94194c85ebf9afd9054d2498dac6786ec))

## [1.14.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.14.0...v1.14.1) (2026-06-19)


### Bug Fixes

* **harbor:** restore project ConfigMap into workspace directory tree ([051ae16](https://github.com/opendatahub-io/agent-eval-harness/commit/051ae16abf16a9b7882a842c07840ee3972fdd29))

# [1.14.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.13.2...v1.14.0) (2026-06-18)


### Bug Fixes

* address CodeRabbit review findings on Harbor integration ([c02b05f](https://github.com/opendatahub-io/agent-eval-harness/commit/c02b05f3838faf346f897af1893384e1b2e3b54c))
* address second-round CodeRabbit findings ([7a87c55](https://github.com/opendatahub-io/agent-eval-harness/commit/7a87c550130d3c5b055668eb7cf4d9b483b149ea))
* **containerfile:** Harbor compatibility fixes ([31064d7](https://github.com/opendatahub-io/agent-eval-harness/commit/31064d72c0e0009353e1444d9adf49a65d254fd9))
* **harbor/k8s:** fix tar upload permissions for non-root pods ([eaf4981](https://github.com/opendatahub-io/agent-eval-harness/commit/eaf498122163836341a928aba1942218c3b98d64))
* **harbor/k8s:** match claude.ai/install.sh in skip-pkg-installs regex ([37cc074](https://github.com/opendatahub-io/agent-eval-harness/commit/37cc074c98699287bd05bd2462db83951a3c82a9))
* **harbor:** only copy declared output paths in verifier test.sh ([3ac0b2f](https://github.com/opendatahub-io/agent-eval-harness/commit/3ac0b2f873797a1216c3c1c767e7048e4f027366))
* **harbor:** propagate SIGTERM/SIGINT to harbor run subprocess ([d83e76b](https://github.com/opendatahub-io/agent-eval-harness/commit/d83e76b70619f8f01c8500f8f9d2e64b46f59a8f))
* **harbor:** use wall-clock duration from Harbor job timestamps ([544d54e](https://github.com/opendatahub-io/agent-eval-harness/commit/544d54ef2b795aea9854907569133d3d859022d7))
* **report:** show judge type for multi-step and non-config judges ([965b2e1](https://github.com/opendatahub-io/agent-eval-harness/commit/965b2e15b7eb03b1c067286f83ad78399460ee2e))
* update test for simplified instruction.md and fix zero-cost bug ([5e3694f](https://github.com/opendatahub-io/agent-eval-harness/commit/5e3694fe750569140496298f7ce250ac26fc54d6))


### Features

* add Harbor integration for containerized eval execution ([96c9fdd](https://github.com/opendatahub-io/agent-eval-harness/commit/96c9fddd1de195b8064a96f4349359cb705e99af))
* **eval-dataset:** add --harbor flag for task package generation ([63f2c59](https://github.com/opendatahub-io/agent-eval-harness/commit/63f2c599a6980e4cc3f1c60d8eb3c41e5e76139b))
* **eval-setup:** add --harbor flag for Harbor dependency installation ([abbe3f3](https://github.com/opendatahub-io/agent-eval-harness/commit/abbe3f3d6afb6138da86629c6b99924b351e3ef0))
* **harbor/k8s:** stream agent logs to pod stdout ([029e1a4](https://github.com/opendatahub-io/agent-eval-harness/commit/029e1a471785149e5dd1a02ec107609b28e932b8))
* **harbor:** add --env flag and default skip-pkg-installs for K8s ([f79e0c1](https://github.com/opendatahub-io/agent-eval-harness/commit/f79e0c12b73a5d97a78a86487249e3719a2245c4))
* **harbor:** add multi-step trial parsing ([d0605f5](https://github.com/opendatahub-io/agent-eval-harness/commit/d0605f583ca51f373e636bf41eb5ba75cd7ec6ea))
* **harbor:** extract turns, duration, version from agent transcripts ([5910342](https://github.com/opendatahub-io/agent-eval-harness/commit/591034229f2fd1557e6c156aa34ba12cc28004b4))
* **harbor:** generate per-case batch.yaml for batch-mode evals ([75c38c8](https://github.com/opendatahub-io/agent-eval-harness/commit/75c38c8acdec9b2946f29127755852455992616f))
* **harbor:** merge judge engine results into multi-step trials ([2a25ad9](https://github.com/opendatahub-io/agent-eval-harness/commit/2a25ad9a0a1ddc032969bf72b305a4b679d6ee23))
* **harbor:** simplify instruction.md template ([3920939](https://github.com/opendatahub-io/agent-eval-harness/commit/3920939c10f01ec6e0f4b632d9702d854ae57d52))
* **score:** support ANTHROPIC_AUTH_TOKEN and ANTHROPIC_BASE_URL for LLM judges ([5b4ce15](https://github.com/opendatahub-io/agent-eval-harness/commit/5b4ce15ab901fa8984cac79c95a9b5f1b8618605))

## [1.13.2](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.13.1...v1.13.2) (2026-06-09)


### Bug Fixes

* **trace_builder:** return server trace ID from _log_trace to fix FK failures ([db004f0](https://github.com/opendatahub-io/agent-eval-harness/commit/db004f0c763aa5949e8f52dae3d5634fd2a4409b)), closes [#95](https://github.com/opendatahub-io/agent-eval-harness/issues/95)
* warn when _log_trace returns no backend ID ([e877d96](https://github.com/opendatahub-io/agent-eval-harness/commit/e877d9625a3f36e0b119b0b3375a0d7734a239a7))

## [1.13.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.13.0...v1.13.1) (2026-06-09)


### Bug Fixes

* don't sample deterministic judges even with CLI --samples ([cbef112](https://github.com/opendatahub-io/agent-eval-harness/commit/cbef112080e449f0786a90a4f29a1d24341a9f98))
* let --samples 1 override per-judge config ([0346683](https://github.com/opendatahub-io/agent-eval-harness/commit/03466835a8b5cc1aad00ddae0ac762c412a84f03))
* preserve numbered list continuity across blank lines in rationales ([17baf24](https://github.com/opendatahub-io/agent-eval-harness/commit/17baf24374336795769b58a2efa707521fc77951))

# [1.13.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.12.0...v1.13.0) (2026-06-09)


### Bug Fixes

* add extra_env parameter to ResponsesAPIRunner.run_skill ([4e0360e](https://github.com/opendatahub-io/agent-eval-harness/commit/4e0360e25e7ff4b30478dc049e74351fe0912deb))
* address CodeRabbit review feedback ([bd90b5f](https://github.com/opendatahub-io/agent-eval-harness/commit/bd90b5f669812a72bcf28d068e893c5c2e3b22cf))
* address PR [#88](https://github.com/opendatahub-io/agent-eval-harness/issues/88) review feedback from astefanutti ([04e9cec](https://github.com/opendatahub-io/agent-eval-harness/commit/04e9cec89704c702d71b83b229584628c1979e07)), closes [#70](https://github.com/opendatahub-io/agent-eval-harness/issues/70)
* guarantee after_each hooks run and flow hook env to CLI runner ([122bab7](https://github.com/opendatahub-io/agent-eval-harness/commit/122bab70e8c4b171939dc76231f5d50887067309))
* move before_each inside try/finally so after_each runs on setup failure ([b114c02](https://github.com/opendatahub-io/agent-eval-harness/commit/b114c02cf9185f261f1b20c8fb4676229308738e))
* return synthetic failed result instead of None on case errors ([5f8887f](https://github.com/opendatahub-io/agent-eval-harness/commit/5f8887ffdf8c37be31287470af69b6e3c36790d7))


### Features

* add execution lifecycle hooks to eval pipeline ([5bc1e05](https://github.com/opendatahub-io/agent-eval-harness/commit/5bc1e052aa1948d0d6e88ea3449a4942327009ec))
* **hooks:** implement hook outputs for passing state from hooks to runners and judges ([bb21862](https://github.com/opendatahub-io/agent-eval-harness/commit/bb21862a060d2572e18667c99ae1b90b93569089))

# [1.12.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.11.0...v1.12.0) (2026-06-08)


### Features

* tabbed rationale view for non-stable sampled judges ([e23f9c6](https://github.com/opendatahub-io/agent-eval-harness/commit/e23f9c6711ebe908c0d8e987a9ed0b6a6d1fd0eb))

# [1.11.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.10.0...v1.11.0) (2026-06-08)


### Bug Fixes

* address CodeRabbit review findings on sampling stability ([4704c3f](https://github.com/opendatahub-io/agent-eval-harness/commit/4704c3f7cd19898f3d47fc905ab9a5bd4c87999c))
* force structured output for score/bool LLM judges ([e1bc1eb](https://github.com/opendatahub-io/agent-eval-harness/commit/e1bc1eb7b010359dce669cbbcc87c947dc444607))
* force structured output for the pairwise judge ([ba1d521](https://github.com/opendatahub-io/agent-eval-harness/commit/ba1d5214bedf8ce7e1538e496550511496af392c))
* pass 1-5 scale bounds to score histogram in the report ([9cf507f](https://github.com/opendatahub-io/agent-eval-harness/commit/9cf507ff2d8d2c90d9800aaff05b9117d025c942))
* stop truncating score/bool judge rationales to 200 chars ([c922532](https://github.com/opendatahub-io/agent-eval-harness/commit/c9225321bd2c20ac7a105d6de9e7ed678eff02bd))


### Features

* add --repeat to pairwise for verdict-stability measurement ([796b614](https://github.com/opendatahub-io/agent-eval-harness/commit/796b614cbbdde0ca6744583ee4010bf93c7fca52))
* annotate judge scores with sampling stability in the report ([1ae938b](https://github.com/opendatahub-io/agent-eval-harness/commit/1ae938bea8ace06be703419c9f22d5757fd94c75))
* per-judge `samples` config in eval.yaml ([f478aaf](https://github.com/opendatahub-io/agent-eval-harness/commit/f478aafdff5c46030ac47d73ddf0ddbac82f41cb))
* render pairwise stability section in the report ([19f1704](https://github.com/opendatahub-io/agent-eval-harness/commit/19f1704dd27e090a763795a55f4083cd82e5de61))
* sample LLM judges N times for score stability (judges --repeat) ([6b3677d](https://github.com/opendatahub-io/agent-eval-harness/commit/6b3677d6d25d5cf2f1067ba50438f6756435bf63))
* visualise judge sampling stability in the report ([c6b955f](https://github.com/opendatahub-io/agent-eval-harness/commit/c6b955f7fcbe5c19450b27857f36d1684e2e36cd))

# [1.10.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.9.1...v1.10.0) (2026-06-05)


### Bug Fixes

* pairwise comparison sends full artifacts and captures judge reasoning ([288bd3b](https://github.com/opendatahub-io/agent-eval-harness/commit/288bd3be7701a7f09eb0444abb20ab6b9a3ef163))


### Features

* show pairwise verdict and reasoning per case in the report ([f28b689](https://github.com/opendatahub-io/agent-eval-harness/commit/f28b689c1c68a183b94e67a96df4440b0984cc47))

## [1.9.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.9.0...v1.9.1) (2026-06-05)


### Bug Fixes

* skip workspace.files in batch mode to prevent silent overwrites ([#112](https://github.com/opendatahub-io/agent-eval-harness/issues/112)) ([bb7e8bd](https://github.com/opendatahub-io/agent-eval-harness/commit/bb7e8bd48f4638ca536a95dc6e9a68028d4d0e8d)), closes [#111](https://github.com/opendatahub-io/agent-eval-harness/issues/111)

# [1.9.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.8.0...v1.9.0) (2026-06-05)


### Features

* add dataset.workspace.files for provisioning case source files ([#70](https://github.com/opendatahub-io/agent-eval-harness/issues/70)) ([f687a3c](https://github.com/opendatahub-io/agent-eval-harness/commit/f687a3ca7792616ae48ed5f62e88c9f31d1dc752))

# [1.8.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.7.2...v1.8.0) (2026-06-04)


### Features

* change runner.env from list to dict with $VAR resolution ([#108](https://github.com/opendatahub-io/agent-eval-harness/issues/108)) ([b1abc0b](https://github.com/opendatahub-io/agent-eval-harness/commit/b1abc0b83c614ec94406f82b799ea61c57eeec14))

## [1.7.2](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.7.1...v1.7.2) (2026-06-04)


### Bug Fixes

* **eval-run:** include cache tokens in input token metric ([#107](https://github.com/opendatahub-io/agent-eval-harness/issues/107)) ([4ef40fd](https://github.com/opendatahub-io/agent-eval-harness/commit/4ef40fd705e24e9687bb612ec8a3b7d030cc52fd))

## [1.7.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.7.0...v1.7.1) (2026-06-04)


### Bug Fixes

* **eval-run:** validate baseline run-id exists in preflight check ([#106](https://github.com/opendatahub-io/agent-eval-harness/issues/106)) ([2a8ac0d](https://github.com/opendatahub-io/agent-eval-harness/commit/2a8ac0de10a398132b3c6d51e0edf4f6bd002fc6))

# [1.7.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.6.0...v1.7.0) (2026-06-04)


### Features

* replace runner.env_strip with runner.env for additive env forwarding ([#105](https://github.com/opendatahub-io/agent-eval-harness/issues/105)) ([b052fe1](https://github.com/opendatahub-io/agent-eval-harness/commit/b052fe1c9f5ea5bbeb954cf23b6aa66979b512f4)), closes [#103](https://github.com/opendatahub-io/agent-eval-harness/issues/103)

# [1.6.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.5.0...v1.6.0) (2026-06-03)


### Features

* add /eval-check for harness-level context and skills scanning / checking ([#74](https://github.com/opendatahub-io/agent-eval-harness/issues/74)) ([de0ca7c](https://github.com/opendatahub-io/agent-eval-harness/commit/de0ca7cf0e7697047bfd8200e9cc8f00995e44ee))

# [1.5.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.4.1...v1.5.0) (2026-06-02)


### Features

* Flexible Eval Directory Layout [Spec + Impl] ([#85](https://github.com/opendatahub-io/agent-eval-harness/issues/85)) ([c978627](https://github.com/opendatahub-io/agent-eval-harness/commit/c9786277f6e3053a6359c4776f473042b91963f8)), closes [#86](https://github.com/opendatahub-io/agent-eval-harness/issues/86) [#77](https://github.com/opendatahub-io/agent-eval-harness/issues/77) [#70](https://github.com/opendatahub-io/agent-eval-harness/issues/70) [#70](https://github.com/opendatahub-io/agent-eval-harness/issues/70)

## [1.4.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.4.0...v1.4.1) (2026-06-02)


### Bug Fixes

* **eval-run:** reflow soft-wrapped paragraphs in HTML report ([#92](https://github.com/opendatahub-io/agent-eval-harness/issues/92)) ([7a6fd41](https://github.com/opendatahub-io/agent-eval-harness/commit/7a6fd41053c0941f1d97c21af557eff01b8041e4))

# [1.4.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.3.0...v1.4.0) (2026-05-29)


### Features

* **eval-dataset:** builtin judges, conditional coverage, run-aware expansion ([#84](https://github.com/opendatahub-io/agent-eval-harness/issues/84)) ([9c3995d](https://github.com/opendatahub-io/agent-eval-harness/commit/9c3995dae7737e73fcd72f47f49a21cffbc67794))

# [1.3.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.2.3...v1.3.0) (2026-05-29)


### Features

* **eval-optimize:** add judge type awareness, targeted re-runs, smarter analysis ([#83](https://github.com/opendatahub-io/agent-eval-harness/issues/83)) ([ce89bc5](https://github.com/opendatahub-io/agent-eval-harness/commit/ce89bc531f9be2369a0d91c29beaed60412ac54c))

## [1.2.3](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.2.2...v1.2.3) (2026-05-29)


### Bug Fixes

* **eval-review:** update for v1.2 judge types, exact case matching ([#82](https://github.com/opendatahub-io/agent-eval-harness/issues/82)) ([2f7c0ea](https://github.com/opendatahub-io/agent-eval-harness/commit/2f7c0ea8a47ce83ed3d8baf315a3b376fa4a598c))

## [1.2.2](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.2.1...v1.2.2) (2026-05-29)


### Bug Fixes

* **eval-analyze:** replace {{ stdout }} with {{ conversation }} in template ([#81](https://github.com/opendatahub-io/agent-eval-harness/issues/81)) ([50ab3cb](https://github.com/opendatahub-io/agent-eval-harness/commit/50ab3cb1e6bf2877ccbdc98501793a4454f8b466))

## [1.2.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.2.0...v1.2.1) (2026-05-29)


### Bug Fixes

* **eval-run:** rename --no-judge, --case-filter, exact case matching ([#80](https://github.com/opendatahub-io/agent-eval-harness/issues/80)) ([06a3d0c](https://github.com/opendatahub-io/agent-eval-harness/commit/06a3d0cf43190099ae611c8481c5ef048a4068c5))

# [1.2.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.1.0...v1.2.0) (2026-05-29)


### Features

* **eval-analyze:** update skills for builtin judges and add list_builtins script ([#79](https://github.com/opendatahub-io/agent-eval-harness/issues/79)) ([c2aff38](https://github.com/opendatahub-io/agent-eval-harness/commit/c2aff380716da6054ea406edb8678642ca70f0a3))

# [1.1.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.0.0...v1.1.0) (2026-05-29)


### Features

* add reusable judges library with builtin registry ([#66](https://github.com/opendatahub-io/agent-eval-harness/issues/66)) ([1e51b41](https://github.com/opendatahub-io/agent-eval-harness/commit/1e51b411392bda8fd3d404733f21ad2b62aaa55b))

# 1.0.0 (2026-05-27)


### Bug Fixes

* address CodeRabbit findings in ensure_deps.py ([f17cd72](https://github.com/opendatahub-io/agent-eval-harness/commit/f17cd72701763daf5c08290ba23b5c30074bdb07))
* address CodeRabbit findings on PR [#25](https://github.com/opendatahub-io/agent-eval-harness/issues/25) ([4d4064f](https://github.com/opendatahub-io/agent-eval-harness/commit/4d4064fa5d4e6ad8bd91bc7d2b25142326bb4fe4))
* address CodeRabbit review feedback on CLI runner PR ([2d90738](https://github.com/opendatahub-io/agent-eval-harness/commit/2d90738e7b9d0ed097a8fc5f9422179161642ea7))
* address CodeRabbit review feedback on EvalHub PR ([b86f754](https://github.com/opendatahub-io/agent-eval-harness/commit/b86f754c54c65b403ac1f51d370d4360c2a0ffdd))
* address CodeRabbit review feedback on release pipeline ([7938e61](https://github.com/opendatahub-io/agent-eval-harness/commit/7938e61ba36728b798904ea19c912c7b86915ce3))
* address CodeRabbit review findings ([0719088](https://github.com/opendatahub-io/agent-eval-harness/commit/071908887386ab2c3ebc9f5799269215a6dc10a3))
* address CodeRabbit review findings on report.py ([0992b80](https://github.com/opendatahub-io/agent-eval-harness/commit/0992b80961139550268f5a634667b3da05c43ac5))
* address eval-analyze skill review findings ([183e606](https://github.com/opendatahub-io/agent-eval-harness/commit/183e606ac4dca0a03b72067f1681900f9bfea1bd))
* address remaining CodeRabbit review items on CLI runner ([94cbcb1](https://github.com/opendatahub-io/agent-eval-harness/commit/94cbcb1c32d366725da71b97e1323ffdf7946c17))
* apply Rui's EvalHub provider registration corrections ([628a85e](https://github.com/opendatahub-io/agent-eval-harness/commit/628a85e90751c2b5ea9bd608bf0d3e6f1063837a))
* bootstrap pyyaml before parsing eval.yaml in ensure_deps ([aec5acf](https://github.com/opendatahub-io/agent-eval-harness/commit/aec5acf024fc8dae3f543f05389989a20e9e728c))
* bump plugin.json and marketplace.json versions during release ([0b73d63](https://github.com/opendatahub-io/agent-eval-harness/commit/0b73d6330f4c97c1addbc52c536a8a6b8adf33ae))
* **ci:** bump Node.js to 22 for semantic-release ([0b409ff](https://github.com/opendatahub-io/agent-eval-harness/commit/0b409ff4bc982a969aea1e34f3d6013c64fb7c71))
* default model examples to claude-opus-4-6 for skill/judge, sonnet for hook ([5fac006](https://github.com/opendatahub-io/agent-eval-harness/commit/5fac006847fa2c58cd961cc0a4a25d93b01ed7d4))
* detect and surface permission denials during eval-run execution ([ba9b9a0](https://github.com/opendatahub-io/agent-eval-harness/commit/ba9b9a0c4e6d9c2712d62b4153c9c38860386136)), closes [#34](https://github.com/opendatahub-io/agent-eval-harness/issues/34)
* disable persist-credentials in tests.yml checkout ([b5ab1ec](https://github.com/opendatahub-io/agent-eval-harness/commit/b5ab1ecb9decf0a74c734cb89f5df63e9e134d5a))
* handle multiple MLflow runs per eval_run_id in from_traces.py ([b6cf4ef](https://github.com/opendatahub-io/agent-eval-harness/commit/b6cf4efa9f1e1c48066c7c013f46ec8048d16daf))
* improve execution mode detection in eval-analyze ([42b62a2](https://github.com/opendatahub-io/agent-eval-harness/commit/42b62a2d1d7d0887ea199bcd9604bd2a48b8ac27))
* improve report badge rendering for regression and markdown tables ([2a55174](https://github.com/opendatahub-io/agent-eval-harness/commit/2a551747e7094053b994ec6304735122033aab61))
* initialize git repos in eval workspaces for settings discovery ([801a255](https://github.com/opendatahub-io/agent-eval-harness/commit/801a255f17a19ac86c89b3f97ce91fdbcfe5e83c))
* merge eval.yaml permissions.allow into workspace settings.json ([8810451](https://github.com/opendatahub-io/agent-eval-harness/commit/88104519df8a926be77f71a037edad6fe9b6c45c))
* remove beads data, gitignore .beads/, fix plugin order ([9fbc1c7](https://github.com/opendatahub-io/agent-eval-harness/commit/9fbc1c736faec85084724679bbd0e3968ff50d48))
* remove unused RunnerConfig.plugins field ([7177d73](https://github.com/opendatahub-io/agent-eval-harness/commit/7177d73e0481a6d46823ee9b2252471a68505e45))
* resolve merge conflict with main in report.py ([1da05f8](https://github.com/opendatahub-io/agent-eval-harness/commit/1da05f81bbd39e82570c2812c4ec799eb0ebf370))
* revert dev marketplace.json to local source reference ([f7077b3](https://github.com/opendatahub-io/agent-eval-harness/commit/f7077b380d8b7d810282cfd49a8d8e71752895cf))
* **score:** restore stdout loading and add batch-mode fallbacks ([1634d6d](https://github.com/opendatahub-io/agent-eval-harness/commit/1634d6d2a35b98fa7c01454918e863f4965c7888))
* tighten permission-denial matcher and e2e assertion ([b4d3852](https://github.com/opendatahub-io/agent-eval-harness/commit/b4d3852a7712f76a9830dcfe1ad0fec9341845ba))
* update remaining 4-7 model IDs to 4-6 in eval.yaml ([3c8b1a8](https://github.com/opendatahub-io/agent-eval-harness/commit/3c8b1a8b042cc6de772fce097ffe120256ae8d1b))
* use GitHub source reference in dev marketplace.json ([c61eb04](https://github.com/opendatahub-io/agent-eval-harness/commit/c61eb04a6a00c3aff35c661479acc4e2c3e4baf5))
* use jq for JSON version bumps instead of sed ([e2ee502](https://github.com/opendatahub-io/agent-eval-harness/commit/e2ee502d7f43c8f23fd990e5b679a24ea2208f83))
* validate thresholds is a mapping before iterating ([a9e8aa5](https://github.com/opendatahub-io/agent-eval-harness/commit/a9e8aa5ee81159834878380bf4a2c95df72c40cc))


### Features

* add [EXTERNAL] convention for external-state fields in dataset schema ([b44c268](https://github.com/opendatahub-io/agent-eval-harness/commit/b44c26841426a973dfe15e2bbe48ddb55a612ceb)), closes [#34](https://github.com/opendatahub-io/agent-eval-harness/issues/34)
* add opaque CLI runner for arbitrary agent commands ([6879853](https://github.com/opendatahub-io/agent-eval-harness/commit/68798536d3552466367c37662277f81f09cb1468))
* add parallel case execution for eval-run ([b920489](https://github.com/opendatahub-io/agent-eval-harness/commit/b920489b2fd64ae6fc32cd9e435e6254eefe693b))
* add semantic-release pipeline for automated versioning ([c05128e](https://github.com/opendatahub-io/agent-eval-harness/commit/c05128e47bc6820bb97c43b3f345201bf5690b15))
* attach batch.yaml/input.yaml as MLflow run artifacts for from-traces ([a20961f](https://github.com/opendatahub-io/agent-eval-harness/commit/a20961fcf1a741bfad8c146f29f81d3d4b635ab8))
* auto-install Python dependencies via SessionStart hook ([403b442](https://github.com/opendatahub-io/agent-eval-harness/commit/403b44208d3e3b7b3b8a9de7012284eac0a0fcd4))
* **collect:** generate events.json from batch-mode stdout ([6a9db37](https://github.com/opendatahub-io/agent-eval-harness/commit/6a9db37e7c6ff3a22aa56cda891e692f412b63e8))
* container image, rfe-assess benchmark, and provider config ([624125f](https://github.com/opendatahub-io/agent-eval-harness/commit/624125f370c6c2726d992821f47fad0ad0ef5e62))
* EvalHub provider for agent skill evaluation ([798756b](https://github.com/opendatahub-io/agent-eval-harness/commit/798756b309fc70fe64825ca8f1b3781f2e12b12d))
* use structured permission_denials from CLI result event ([8b939d8](https://github.com/opendatahub-io/agent-eval-harness/commit/8b939d83575487d92719005dfc38da618413918a))

# Changelog
