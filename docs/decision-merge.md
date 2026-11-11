# Retired decision-model CI admission

Mandatory model review and decision-gated staging admission have been retired
from GitHub Actions. Owner-authored PRs now use deterministic CI and direct main
admission. External authors receive a bounded advisory review and require the
repository owner's current-head approval. See [current CI policy](ci-cd.md).

The old `decision-merge`, specialized review and model-selection labels do not
control the new pipeline. There is no bootstrap SHA setting, staged promotion,
second review-interpretation model or scheduled retry of model admission.

Standalone decision, evidence-parser and requirements-contract code remains for
runtime use and experiments, with its deterministic unit coverage. Its presence
is not an active forge merge authority. Historical architecture and failed
review evidence remain available in Git history and the original PR discussions.
