# Local Actions runners

Public local jobs request only `[factory-keystone-polyphony]`; these runners have no
GitHub default labels. Trusted non-PR jobs and same-repository PR-family jobs
whose author association is `OWNER`, `MEMBER` or `COLLABORATOR` use that label.
Forks, other author associations and missing PR/head metadata use the original
GitHub-hosted Ubuntu label instead. Routing changes only `runs-on`, not existing
job conditions, build commands, qualification gates or artifact actions.
Artifact storage/billing is separate from runner compute.

One runner executes one job at a time for this repository (including matrix
jobs). Its per-repository runner/daemon envelope is 2 CPU and 4 GiB RAM.
All repositories share a host-wide aggregate ceiling of 3 CPU and 10 GiB RAM;
per-repository limits are ceilings, not simultaneous capacity guarantees.
An offline host leaves jobs queued until its matching runner returns; GitHub's
normal queue expiry still applies.

External-contributor jobs are not skipped by routing. Normal GitHub approval
requirements still apply. Keep branch creation/write access restricted to trusted
collaborators; PR-controlled workflow YAML is not an immutable isolation boundary.
A maintainer must review workflow changes before permitting untrusted execution.

The default branch also has a manual, owner-only `local-runner-health.yml` smoke
workflow using the custom label. It is separate from normal contributor CI and
does not qualify workloads or change retained evidence.

The dispatch-driven **PR Validation** job remains entirely on its original
GitHub-hosted `ubuntu-24.04` runner, even when manually invoked from trusted main:
its selected candidate SHA may contain fork code. Its original revision preflight
and contributor admission policy are unchanged. Approved external contributions
retain their existing validation path. The trusted-main controller never checks
out candidate code; fork PR-family metadata jobs also route to hosted runners.
