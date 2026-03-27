---
name: safe-upgrade
description: Safely upgrade project dependencies by testing 3 upgrade strategies in parallel — each in its own isolated git worktree — then compare results and recommend the best path. The user's working directory is never modified. Use when the user says "safely upgrade dependencies", "try upgrading packages", "test dependency upgrades", "safe upgrade", "upgrade without breaking anything", or any variation of wanting to test dependency upgrades before committing to them.
---

# safe-upgrade Skill

Test 3 dependency upgrade strategies **in parallel**, each in an isolated git worktree. Compare results and recommend the safest path forward.

## Workflow

### Step 1: Detect the Project(s)

Identify whether this is a single project or a monorepo:

1. Look for monorepo indicators at the root:
   - `workspaces` field in `package.json` (npm/yarn)
   - `pnpm-workspace.yaml`
   - `lerna.json`
   - `nx.json` or `turbo.json`
   - Multiple `go.mod` files in subdirectories
   - Multiple `Cargo.toml` files with a root `Cargo.toml` `[workspace]`
   - Multiple `pyproject.toml` files in subdirectories
2. If monorepo: list all packages/projects and their locations
3. If single project: identify the package manager and test/build commands
4. Confirm with the user what you found

### Step 2: Launch 3 Agents in Parallel

Launch all 3 agents in a **single message** using the Agent tool. Each MUST use `isolation: "worktree"` so it gets its own isolated copy of the repository.

**Pass the project discovery to each agent.** Include the list of detected projects/packages and their paths in each agent's prompt so they know what to upgrade.

**Agent 1 — `upgrade-full` agent:**
> Here are the projects detected: [list with paths].
> Try upgrading ALL dependencies to latest versions, including major bumps. For monorepos, upgrade each package and run tests per-package. Report results broken down by package.

**Agent 2 — `upgrade-safe` agent:**
> Here are the projects detected: [list with paths].
> Try upgrading dependencies to latest MINOR and PATCH versions only. For monorepos, upgrade each package and run tests per-package. Report results broken down by package.

**Agent 3 — `upgrade-security` agent:**
> Here are the projects detected: [list with paths].
> Run a security audit, then upgrade ONLY packages with known vulnerabilities. For monorepos, audit and fix each package. Report results broken down by package.

### Step 3: Synthesize Results

Once all 3 agents return, present a comparison.

**For a single project:**

```markdown
## Dependency Upgrade Report

### Strategy Comparison

| Strategy | Packages Updated | Tests | Build | Branch |
|----------|-----------------|-------|-------|--------|
| Full upgrade | 12 | FAILED | - | upgrade/full |
| Minor + patch | 7 | PASSED | PASSED | upgrade/minor-patch |
| Security only | 2 | PASSED | PASSED | upgrade/security |

### Recommendation
Based on the results, I recommend: **[best strategy]**.

### Next Steps
git merge [branch-name]
```

**For a monorepo:**

```markdown
## Dependency Upgrade Report — Monorepo

### Per-Package Results

#### packages/api
| Strategy | Updated | Tests | Build |
|----------|---------|-------|-------|
| Full | 8 | PASSED | PASSED |
| Minor+patch | 5 | PASSED | PASSED |
| Security | 1 | PASSED | PASSED |

#### packages/web
| Strategy | Updated | Tests | Build |
|----------|---------|-------|-------|
| Full | 10 | FAILED | - |
| Minor+patch | 6 | PASSED | PASSED |
| Security | 2 | PASSED | PASSED |

#### packages/shared
| Strategy | Updated | Tests | Build |
|----------|---------|-------|-------|
| Full | 3 | PASSED | PASSED |
| Minor+patch | 2 | PASSED | PASSED |
| Security | 0 | PASSED | PASSED |

### Overall Recommendation
- **packages/api** and **packages/shared**: safe to do full upgrade
- **packages/web**: recommend minor+patch only (React 19 breaks tests)
- All packages: security fixes pass across all strategies

### Next Steps
git merge [branch-name]
```

### Important

- **All 3 agents MUST run with `isolation: "worktree"`** — this is the core safety guarantee
- **Launch all 3 in a single message** — they run in parallel for speed
- **Never modify the user's working directory** — all changes happen in worktrees
- **For monorepos**: pass the detected package list to each agent so they don't re-discover
- **For monorepos**: agents must report results per-package, not as a single blob
- Successful agents leave a git branch the user can merge
- Failed agents have their worktrees cleaned up automatically
- Present honest results — failures are valuable information
