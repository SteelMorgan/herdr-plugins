# Single shared D-Bus session bus for this WSL user (no systemd user bus here).
# Without a bus address, D-Bus clients such as Docker Desktop's Linux CLI
# autolaunch a private dbus-daemon per call that never exits. On 2026-10-04
# ~27k leaked daemons exhausted WSL memory and crashed the herdr server.
# Sourced from ~/.profile and ~/.bashrc; idempotent and safe to run concurrently.

_sb_dir="${XDG_CACHE_HOME:-$HOME/.cache}/dbus-session"
_sb_addr="unix:path=$_sb_dir/bus"

if { [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] || [ "$DBUS_SESSION_BUS_ADDRESS" = "$_sb_addr" ]; } \
    && command -v dbus-daemon >/dev/null 2>&1; then
    mkdir -p "$_sb_dir" && chmod 700 "$_sb_dir"
    (
        flock -w 5 9 || exit 0
        if ! timeout 2 dbus-send --bus="$_sb_addr" --dest=org.freedesktop.DBus \
            --print-reply / org.freedesktop.DBus.GetId >/dev/null 2>&1; then
            rm -f "$_sb_dir/bus"
            # 9>&-: the forked daemon must not inherit the lock fd.
            dbus-daemon --session --address="$_sb_addr" --fork --nopidfile \
                </dev/null >/dev/null 2>&1 9>&-
        fi
    ) 9>"$_sb_dir/lock"
    DBUS_SESSION_BUS_ADDRESS="$_sb_addr"
    export DBUS_SESSION_BUS_ADDRESS
fi

unset _sb_dir _sb_addr
