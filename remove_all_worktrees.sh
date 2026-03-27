for wt in ./worktrees/agent-*; do
  git worktree remove -f "$wt"
done

for wt in ./.claude/worktrees/agent-*; do
  git worktree remove -f "$wt"
done