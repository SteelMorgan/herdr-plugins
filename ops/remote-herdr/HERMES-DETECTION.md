# Hermes foreground detection through sudo routing

Applied on VM200 on 2026-10-25 with explicit owner approval for the sudo PTY
setting. The route still enters the existing VPN namespace, drops to UID hermes,
and preserves provider/model/YOLO arguments. Office services are unaffected.

The old `sudo` use_pty hid Python in a separate foreground terminal, so Herdr saw
only sudo. The isolated entry script was also named launch.py rather than hermes.

## Changes

1. Install the native session integration as the Hermes user:
   `herdr integration install hermes`.
2. Create root-owned `/usr/local/libexec/gbig-herdr/` and a symlink named `hermes`
   to `/opt/hermes-admin/current/launch.py`.
3. Apply `hermes-detection.patch` to the existing root-owned route script after
   checking the matching base. This changes only the Hermes entry script path;
   Python's executable/argv[0] must remain its original venv path. Replacing
   argv[0] with bare hermes breaks venv dependency discovery.
4. Add only this command-scoped setting to the existing route sudoers file:

   ```sudoers
   Defaults!/usr/local/sbin/gbig-agent-route !use_pty
   ```

   Validate a staged file with visudo before installing as root:root 0440; then
   validate the whole /etc/sudoers. This setting reduces terminal isolation and
   must not be installed silently on another deployment. Full Admin sudo is a
   separate owner authorization, not granted by this patch.

5. Gracefully exit the interactive CLI, verify its pane is a shell, then use
   `herdr agent start <name> --kind hermes --pane <actual-id> -- --resume <id>`
   with the existing provider/model/reasoning/YOLO arguments. Never start a second
   process merely because an earlier agent-start call timed out.
6. Remove obsolete manual coordinator identity after real recognition. Confirm
   both remote and central mirror show working during a harmless test request
   and idle/done afterwards; session identity must match the resumed session.

Verified: native agent-start success, current session retained, model Sol 6.1,
real foreground Hermes in the original process group, native v5 session report,
working -> idle remote and working -> done central (unfocused completion),
VPN route/UID retained, sudo -n id -u returns 0. No gateway restart was needed.
Existing central mirror bridge and user's focus remained unchanged.

Pre-change route and sudoers copies are retained on VM200 under
`/var/backups/gbig-herdr-detection-20261025-150320/`. To roll back, gracefully
stop the CLI, restore only those two files with their original ownership/modes,
validate visudo and resume the same session. The symlink/native integration can
remain unused; no database or credentials need replacement.
