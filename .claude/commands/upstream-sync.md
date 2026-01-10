# Upstream Sync Analysis and Rebase Planning

This skill helps merge changes from the upstream repository (alexta69/metube) into this fork.

## Instructions

Perform the following steps in order:

### Step 1: Fetch Latest Upstream Sync Analysis

1. Use `gh issue list --repo tatoalo/metube_pot --state open --label "upstream-sync" --limit 1` to find the most recent upstream sync analysis issue
2. If no labeled issues found, list recent open issues and identify the upstream sync analysis by title pattern "Upstream Sync Analysis: *"
3. Use `gh issue view <issue_number> --repo tatoalo/metube_pot` to read the full issue body
4. Extract and summarize:
   - New features added upstream
   - Breaking changes
   - Dependency updates
   - Mergeability score and recommendations

### Step 2: Review Against Master Branch

1. Ensure the upstream remote is configured:
   ```
   git remote add upstream https://github.com/alexta69/metube.git 2>/dev/null || true
   git fetch upstream
   ```

2. Compare master with upstream/master:
   - `git log master..upstream/master --oneline` to see new commits
   - `git diff master..upstream/master --stat` to see changed files

3. Identify conflicts with fork customizations:
   - Check files modified in both fork and upstream
   - Flag any fork-specific features that might be affected by breaking changes

4. Cross-reference with the sync analysis issue to validate findings

### Step 3: Create Rebase Plan

Create a markdown file at `.claude/rebase-plans/YYYY-MM-DD-upstream-sync.md` with:

1. **Summary**: Overview of what's being merged
2. **Upstream Changes**: List of commits/features from upstream
3. **Fork Impact Assessment**:
   - Which fork customizations are affected
   - Potential conflicts identified
4. **Rebase Strategy**:
   - Recommended approach (rebase vs merge)
   - Order of operations
   - Files requiring manual attention
5. **Testing Checklist**:
   - Key functionality to verify after merge
6. **Rollback Plan**: Steps to revert if issues arise

After creating the plan, display the file path and a summary for the user to review.

## Usage

Invoke this skill with `/upstream-sync` to start the analysis and planning process.

## Arguments

- `$ARGUMENTS` - Optional: specific issue number to analyze (e.g., `/upstream-sync 8`)
