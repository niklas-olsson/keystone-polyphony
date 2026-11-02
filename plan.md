1. **Change the Runner OS**: Update `runs-on: ubuntu-latest` to `runs-on: ubuntu-24.04` in `.github/workflows/opencode.yml`.
2. **Update the Checkout Action**:
   - Modify the `actions/checkout@v4` step to check out the trusted base repository code instead of the untrusted fork PR code.
   - Remove `repository: ...` and use `ref: ${{ github.base_ref || github.event.pull_request.base.ref }}`.
3. **Extract the Diff Securely**:
   - Add a step `Extract PR diff securely` right after `Prepare Environment` (where `steps.prep.outputs.pr_number` is reliably computed).
   - Use `gh pr diff ${{ steps.prep.outputs.pr_number }} > pr_patch.diff` with `env: GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}`.
4. **Pass Diff to Agent**:
   - In the `Run Review CLI` step, append the contents of `pr_patch.diff` (if it exists) to the end of `review_prompt.txt` so the agent knows what to review. We will append the diff explicitly: `echo "\n\nReview the following patch:\n\`\`\`diff\n$(cat pr_patch.diff)\n\`\`\`" >> "$PROMPT_FILE"`.
5. **Pre-commit checks**: Run `pre_commit_instructions` to fulfill the standard pre-commit requirements.
6. **Submit**: Create a commit, submit to the requested branch, and push.
