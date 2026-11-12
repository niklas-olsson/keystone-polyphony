> This document describes standalone/runtime model selection. GitHub Actions no longer use the automatic picker or CLI review chain. The external advisory review has one fixed free model and no retry; see [CI policy](ci-cd.md).

# Free model selection and fallback

Reviews and typed decisions default to `auto`. The shared picker reads
[OpenRouter's model catalog](https://openrouter.ai/api/v1/models) at execution
time, so newly listed and removed models do not require a workflow edit.
The catalog advertises availability; successful inference is the final service
check. No API key is sent to the public catalog.

Review eligibility requires text input/output, tool calling, at least 256,000
tokens of context, and zero prices for every advertised pricing component.
Expired entries are excluded. Eligible models sort by documented total parameter
count, newest catalog creation date, then context length, with a stable model-ID
tie break. Active MoE parameters are not used as total size. Unknown sizes rank
below disclosed sizes. This is our capacity heuristic, not a measured quality
benchmark.

Decisions use the separate `decisions` output modality and the typed Decisions
API. They use the same size/date/context ranking, without the review context or
tool minimum. The currently listed free decision models have smaller or
undisclosed context windows. Ordinary chat models cannot substitute for this API.
Equivalent catalog aliases are deduplicated, preferring an explicit `:free`
route when otherwise tied.

When catalog retrieval fails, the picker warns and uses a short vetted list of
explicit `:free` routes. A successful catalog with no eligible models fails
closed. The picker never adds a paid fallback. Free service can still be rate
limited or unavailable.

CLI reviews try at most three candidates. OpenCode JSON events distinguish a
provider failure from recoverable tool errors and preserve assistant review
text. Provider failures, timeouts, and empty output may try the next candidate.
A substantive review or any explicit verdict ends the chain. Decision requests
also use bounded attempts within a shared catalog-and-inference timeout. Valid
hold, consult, refusal, and low-confidence results stop selection; another model
is never asked merely to obtain a more favorable verdict. Exhaustion fails the
review or retains hold/Architect consultation. Review evidence, comments, and
decision summaries record the actual model used.

Explicit model overrides bypass automatic ranking and fallback. Existing review
repository variables, manual inputs, and model labels retain their precedence.
`LIMINAL_DECISION_MODEL` controls Pulse and `DECISION_MERGE_MODEL` controls staging
admission. Set either to `auto` or leave it unset for automatic selection. An
explicit override can be paid; the zero-price restriction applies to automatic
selection.

The review helper executes from the trusted base checkout. Until the initial
picker change reaches that branch, workflows keep the previous free Nemotron
bootstrap model and announce that dynamic selection is not yet active. They do
not execute privileged helper code from an untrusted pull request.
