# Collie narrow Codex footer override

The override recognizes a truncated coloured model field followed by a differently coloured `Pursuing goal (...)` or `Goal paused (/goal resume)` / `Goal stalled (/goal resume)` notice. Ordinary prose, menu text and a footer without the live composer remain rejected. The original generic status grammar is unchanged. The paused-goal case was captured on GBIG PAM with a wrapped unsent draft; reading it does not send the draft or resume the goal.

Maintained source: this directory. Installed durable copy: `/home/steel/.config/herdr/plugins/config/herdr.collie/local-overrides`.

The installed Collie repository has `post-checkout` and `post-merge` hooks that run `apply.py` from the durable copy. Collie's updater uses `checkout --detach --force`, or creates a staged Git worktree, before building: both execute `post-checkout`. Linked worktrees share these hooks. A rehearsal with a fresh worktree verified that the patch applies there and recognizes the captured platsec screen.

Application is idempotent. An incompatible upstream grammar or a partially modified local patch causes the hook to fail, so the current update implementation stops before building/activating an unpatched version. The hook does not edit release versions or contact the network.

Explicit deletion/reinstallation of the plugin replaces its Git repository and hooks. The durable copy survives, but the hooks must be installed into the new repository before its next update. A future upstream updater that bypasses checkout hooks also requires revisiting this integration; compatibility with arbitrary future implementations is not promised.

Regression checks: 1294 passing Codex tests (6 existing todo cases), frontend typecheck, and production frontend build. The test includes the narrow footer's exact ANSI styling, preservation of a wrapped PAM draft on goal pause, and refusal checks for unpainted text, menu-like text, a non-truncated field, shared colours and a missing composer.

Rollback: remove the two added hooks, restore `web/src/lib/harness/codex/markers.ts` from the selected upstream revision, remove `web/src/lib/harness/codex/narrow-goal.local.test.ts`, and rebuild the frontend. Keep the durable copy until rollback is verified. The pre-change deployed web bundle is backed up under the durable directory as `web-dist-before-narrow-goal`.
