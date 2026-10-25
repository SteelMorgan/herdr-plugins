#!/bin/sh
# Keep central Herdr Mirror supervised after startup. Log state transitions,
# restart attempts, and a bounded diagnostic snapshot when the daemon vanishes.
PATH=/home/steel/.local/bin:/usr/local/bin:/usr/bin:/bin
HOME=/home/steel
USER=steel
LOGNAME=steel
HERDR_ENV=1
export PATH HOME USER LOGNAME HERDR_ENV

state_dir=/home/steel/.local/state/herdr-mirror
mirror=/home/steel/.local/bin/herdr-mirror
herdr=/home/steel/.local/bin/herdr
notice_interval=60
heartbeat_interval=300
snapshot_interval=300

log() {
    printf '%s %s\n' "$(/usr/bin/date '+%Y-%m-%dT%H:%M:%S%z')" "$*"
}

now_epoch() {
    /usr/bin/date +%s
}

if [ ! -x "$mirror" ] || [ ! -x "$herdr" ]; then
    log "required Herdr CLI is missing; giving up"
    exit 1
fi

mkdir -p "$state_dir" || exit 1
if [ "${1:-}" != --locked ]; then
    # The legacy lock can remain inherited by the detached mirror daemon.
    # Keep the supervisor lock separate so that stale daemon descriptors do
    # not prevent a fresh monitor from starting.
    /usr/bin/flock -n -E 75 -o "$state_dir/boot-watchdog.supervisor.lock" "$0" --locked
    lock_status=$?
    if [ "$lock_status" -eq 75 ]; then
        log "another mirror boot watchdog is already running"
    fi
    exit "$lock_status"
fi
shift

if [ ! -x /usr/bin/docker ] || [ ! -x /usr/bin/timeout ]; then
    log "Docker CLI or timeout is missing; cannot supervise Mirror"
    exit 1
fi

diagnostic_snapshot() {
    recorded_pid=unknown
    if [ -r "$state_dir/daemon.pid" ]; then
        recorded_pid=$(/usr/bin/tr -cd '0-9' < "$state_dir/daemon.pid")
        [ -n "$recorded_pid" ] || recorded_pid=unknown
    fi
    boot_id=$(/usr/bin/cat /proc/sys/kernel/random/boot_id 2>/dev/null || printf 'unknown')
    uptime=$(/usr/bin/cut -d' ' -f1 /proc/uptime 2>/dev/null || printf 'unknown')
    log "mirror diagnostic snapshot (reason=$1, recorded_pid=$recorded_pid, boot_id=$boot_id, uptime_seconds=$uptime)"

    if [ -e "$state_dir/daemon.log" ]; then
        log "daemon.log metadata: $(/usr/bin/stat -c 'mtime=%y size=%s' "$state_dir/daemon.log" 2>/dev/null || printf 'unavailable')"
        log "last daemon.log entries follow"
        /usr/bin/tail -n 16 "$state_dir/daemon.log" 2>/dev/null | /usr/bin/tr -d '\000' | while IFS= read -r line; do
            log "daemon.log: $line"
        done
    else
        log "daemon.log is missing"
    fi

    if [ -r /proc/pressure/memory ]; then
        memory_pressure=$(/usr/bin/tr '\n' ';' < /proc/pressure/memory)
        log "memory PSI: $memory_pressure"
    fi
    if [ -r /sys/fs/cgroup/memory.events ]; then
        memory_events=$(/usr/bin/tr '\n' ';' < /sys/fs/cgroup/memory.events)
        log "current cgroup memory.events: $memory_events"
    fi

    if [ -x /usr/bin/dmesg ]; then
        kernel_log=$(/usr/bin/dmesg -T 2>&1)
        kernel_rc=$?
        if [ "$kernel_rc" -eq 0 ]; then
            oom_lines=$(printf '%s\n' "$kernel_log" | /usr/bin/grep -Ei 'out of memory|oom-kill|killed process' | /usr/bin/tail -n 12 || true)
            if [ -n "$oom_lines" ]; then
                log "matching kernel OOM entries follow"
                printf '%s\n' "$oom_lines" | while IFS= read -r line; do
                    log "kernel: $line"
                done
            else
                log "kernel OOM scan: no matching entries in the readable buffer"
            fi
        else
            first_kernel_error=$(printf '%s\n' "$kernel_log" | /usr/bin/sed -n '1p')
            log "kernel OOM scan unavailable (exit $kernel_rc): $first_kernel_error"
        fi
    fi
}

status_line() {
    /usr/bin/timeout --kill-after=1 5 "$mirror" status 2>&1
}

status_pid() {
    printf '%s\n' "$1" | /usr/bin/sed -n 's/^daemon: running (pid \([0-9][0-9]*\)).*/\1/p'
}

last_state=unknown
last_known_running=0
last_notice=0
last_retry_notice=0
last_failure_notice=0
last_snapshot_at=0
last_heartbeat=0
attempt=0
retry_delay=10
log "Mirror supervisor started; sticky manual pause will be respected"

while :; do
    now=$(now_epoch)

    if [ -e "$state_dir/daemon.paused" ]; then
        if [ "$last_state" != paused ]; then
            log "sticky manual pause is set; waiting for an explicit resume"
            last_state=paused
            last_known_running=0
            retry_delay=10
        fi
        /bin/sleep 10
        continue
    fi

    mirror_status=$(status_line)
    mirror_status_rc=$?
    daemon_line=$(printf '%s\n' "$mirror_status" | /usr/bin/sed -n '1p')

    case "$daemon_line" in
        "daemon: running (pid "*)
            daemon_pid=$(status_pid "$daemon_line")
            last_known_running=1
            if [ "$last_state" != running ]; then
                if [ "$last_state" = stopped ] || [ "$last_state" = paused ]; then
                    log "mirror daemon is running again (pid ${daemon_pid:-unknown})"
                else
                    log "mirror daemon is running (pid ${daemon_pid:-unknown})"
                fi
                last_state=running
                last_heartbeat=$now
                last_notice=0
                last_retry_notice=0
                last_failure_notice=0
                attempt=0
                retry_delay=10
            elif [ $((now - last_heartbeat)) -ge "$heartbeat_interval" ]; then
                log "heartbeat: mirror daemon running (pid ${daemon_pid:-unknown})"
                last_heartbeat=$now
            fi
            /bin/sleep 10
            continue
            ;;
        "daemon: not running"*)
            if [ "$last_known_running" -eq 1 ]; then
                log "mirror daemon stopped unexpectedly; status confirms no live daemon"
                diagnostic_snapshot "unexpected-stop"
                last_snapshot_at=$now
                last_known_running=0
            elif [ "$last_state" != stopped ]; then
                log "mirror daemon is not running; checking whether it can be started"
            fi
            last_state=stopped
            ;;
        *)
            if [ "$last_state" != unavailable ] || [ $((now - last_notice)) -ge "$notice_interval" ]; then
                log "cannot read mirror daemon state (status exit $mirror_status_rc): ${daemon_line:-empty response}"
                if [ "$mirror_status_rc" -ne 0 ]; then
                    log "mirror status error: $(printf '%s\n' "$mirror_status" | /usr/bin/sed -n '1p')"
                fi
                last_notice=$now
            fi
            last_state=unavailable
            /bin/sleep 10
            continue
            ;;
    esac

    if ! /usr/bin/timeout --kill-after=2 7 /usr/bin/docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
        if [ $((now - last_notice)) -ge "$notice_interval" ]; then
            log "daemon remains stopped; Docker API is unavailable"
            last_notice=$now
        fi
        /bin/sleep 10
        continue
    fi

    if ! /usr/bin/timeout --kill-after=1 5 "$herdr" status server >/dev/null 2>&1; then
        if [ $((now - last_notice)) -ge "$notice_interval" ]; then
            log "daemon remains stopped; central Herdr server is unavailable"
            last_notice=$now
        fi
        /bin/sleep 10
        continue
    fi

    if [ "$last_retry_notice" -eq 0 ] || [ $((now - last_retry_notice)) -ge "$notice_interval" ]; then
        log "calling herdr-mirror ensure (attempt $((attempt + 1)))"
        last_retry_notice=$now
    fi
    attempt=$((attempt + 1))
    ensure_output=$(/usr/bin/timeout --kill-after=2 8 "$mirror" ensure 2>&1)
    ensure_rc=$?
    if [ -n "$ensure_output" ]; then
        printf '%s\n' "$ensure_output" | while IFS= read -r line; do
            log "ensure: $line"
        done
    fi

    after_status=$(status_line)
    after_status_rc=$?
    after_line=$(printf '%s\n' "$after_status" | /usr/bin/sed -n '1p')
    case "$after_line" in
        "daemon: running (pid "*)
            daemon_pid=$(status_pid "$after_line")
            log "mirror daemon recovered after ensure (pid ${daemon_pid:-unknown}, ensure exit $ensure_rc)"
            last_state=running
            last_heartbeat=$now
            last_notice=0
            last_retry_notice=0
            last_failure_notice=0
            last_snapshot_at=0
            attempt=0
            retry_delay=10
            ;;
        *)
            if [ "$last_failure_notice" -eq 0 ] || [ $((now - last_failure_notice)) -ge "$notice_interval" ]; then
                log "ensure did not start the daemon (exit $ensure_rc; status exit $after_status_rc): ${after_line:-empty response}"
                last_failure_notice=$now
            fi
            if [ "$last_snapshot_at" -eq 0 ] || [ $((now - last_snapshot_at)) -ge "$snapshot_interval" ]; then
                diagnostic_snapshot "ensure-did-not-start"
                last_snapshot_at=$now
            fi
            if [ "$retry_delay" -lt 60 ]; then
                retry_delay=$((retry_delay * 2))
                [ "$retry_delay" -le 60 ] || retry_delay=60
            fi
            /bin/sleep "$retry_delay"
            continue
            ;;
    esac
    /bin/sleep 10
done
