# lutris-game-tune (v4)

Pre/post-game system tuning for Lutris, plus CCD/CCX core isolation and
lower-nice game startup. All three features are provided through a single
script and a single setuid wrapper.

---

## What's new in v4

**CCD/CCX isolation was re-architected after a real incident** where an
earlier, move-based implementation broke D-Bus/PolicyKit session tracking
mid-game (see [CCD/CCX isolation architecture](#ccdccx-isolation-architecture)
below for the full story). The short version:

- **Existing cgroups are no longer swept.** The old approach moved every
  process it found (except a hand-maintained protect-list) into a
  temporary `theUgly` cgroup, then moved everything back on exit. This is
  gone. Login sessions, elogind, dbus, and every other pre-existing cgroup
  are now left completely alone — their processes never change cgroup
  membership.
- **Existing cgroups are constrained in place instead.** Each one gets its
  own `cpuset.cpus` written directly (with the original value saved and
  restored on exit), which confines every process inside it — and all of
  its descendants, since cpuset is hierarchical — to the system CCD
  without moving anyone anywhere.
- **Only genuinely homeless processes are still moved**: whatever is found
  sitting directly in the cgroup-v2 root (no session/service cgroup of its
  own) is moved into `theUgly`, with per-PID origin tracking so POST
  returns each one to the exact cgroup it came from — not to root.
- **Two independent safety nets remain for that one remaining move step**:
  a static list of daemon/session-manager process names
  (`CCD_PROTECTED_PROCS`, covering both OpenRC and systemd naming) and a
  dynamic lookup of the current login session leader PIDs straight from
  the login manager's own session records
  (`/run/systemd/sessions/` or `/run/elogind/sessions/`), refreshed on
  every PRE run.
- **`CCD_PROTECTED_CGROUPS` is now legacy** — accepted for config-file
  compatibility but not used by the current logic, since nothing needs to
  be listed to be protected any more.

This release also verified the script works unchanged across both OpenRC
and systemd (elogind and systemd-logind both write session records in the
same `LEADER=` format the dynamic lookup reads).

---

## What's new in v3

This release builds on the previous **v2** script and adds two major features:

1. **CCD/CCX core isolation integrated** (replaces the formerly separate
   `tasks-redirect` project). On PRE, it moves the launcher/game process onto
   the performance core group (CCD0/CCX0) and everything else onto the
   second group (CCD1/CCX1); on POST (once the last game exits) it reverts
   this. **Automatically and silently skipped on single-CCX/CCD processors**
   — no log lines or warnings are produced, and no cgroups are created.
2. **Added a `RUN [nice]` command-prefix mode.** Put something like
   `lutris-game-tune-wrapper RUN -5` (or just `lutris-game-tune-wrapper RUN`
   to use the default of `-5`) in Lutris's "Command prefix" field to start
   the game with a lower (higher-priority) nice value. Root privilege is
   used only to set the nice value (and, if `sched_autogroup` is active,
   the autogroup nice too); it is dropped **permanently and
   irreversibly** back to the real user immediately afterward — the game
   itself never runs as root.

Also, per request:

3. **THP (Transparent HugePage) is now always `always`.** In v2, only
   `enabled` was configurable (`THP_MODE`, default `madvise`), while
   `shmem_enabled=advise` and `defrag=never` were hardcoded. In v3, **all
   three are always `always`** — the `THP_MODE` config key has been removed
   entirely.
4. **CCD revert on exit now retries up to 25 times.** If both cgroups
   (`theGood`/`theUgly`) are fully emptied and removed before the 25th
   attempt, the script stops early. If, after 25 attempts, at least one
   group still isn't empty, the script reports an error (see
   [CCD revert behavior](#ccdccx-revert-behavior-on-exit) below).

PRE/POST behavior and all other v2 features (security hardening, reference
counting, the STATUS command, whitelist-based config) are preserved as-is.

---

## Files

| File | Description |
|---|---|
| `lutris-game-tune.sh` | Main bash script (PRE/POST/STATUS + CCD isolation) |
| `lutris-game-tune-wrapper.c` | Setuid root C wrapper (PRE/POST/STATUS/RUN) |
| `lutris-game-tune.conf` | Sample config file (installed to `/etc/lutris-game-tune.conf`) |
| `lutris-game-tune.conf.example` | Same as above, annotated for manual installs (see [Manual installation](#manual-installation)) |
| `install.sh` | Automated build + install script |
| `uninstall.sh` | Removal script — restores game state first if still active, `--purge` also removes config/logs |

---

## Installation

### Automated (recommended)

```bash
sudo ./install.sh
```

The script:
1. Builds the wrapper (`gcc -O2 -Wall -Wextra`)
2. Copies `lutris-game-tune.sh` to `/usr/local/lib/lutris-game-tune/` as
   `root:root 755`
3. Copies `lutris-game-tune.conf` to `/etc/lutris-game-tune.conf`
   (**does not overwrite an existing config**)
4. Installs the wrapper as `/usr/local/bin/lutris-game-tune-wrapper` with
   `root:root 4755` (setuid)
5. Verifies the setuid bit; warns if traces of an older, separate
   `tasks-redirect`/GameTune Suite install are found

To uninstall:

```bash
sudo ./install.sh --uninstall
# equivalent to, and delegates to:
sudo ./uninstall.sh
```

Either restores original settings first if game mode is still active, then
removes the wrapper and script. `/etc/lutris-game-tune.conf` and the log
file are kept by default; to remove those too:

```bash
sudo ./uninstall.sh --purge
```

### Manual installation

```bash
gcc -O2 -Wall -Wextra -o lutris-game-tune-wrapper lutris-game-tune-wrapper.c

sudo mkdir -p /usr/local/lib/lutris-game-tune
sudo install -o root -g root -m 755 lutris-game-tune.sh /usr/local/lib/lutris-game-tune/
sudo install -o root -g root -m 644 lutris-game-tune.conf /etc/lutris-game-tune.conf
sudo install -o root -g root -m 4755 lutris-game-tune-wrapper /usr/local/bin/
```

---

## Hooking into Lutris

Per-game (right-click → **Configure** → **System options**) or globally
(**Preferences** → **System options**):

| Field | Value |
|---|---|
| Pre-game script | `/usr/local/bin/lutris-game-tune-wrapper PRE` |
| Post-game script | `/usr/local/bin/lutris-game-tune-wrapper POST` |
| Command prefix *(optional)* | `/usr/local/bin/lutris-game-tune-wrapper RUN -5` |

Lutris automatically prepends the **Command prefix** field to the actual
launch command — you only write the prefix itself. The nice argument is
**optional**; if omitted, `-5` is used automatically (`RUN` alone works
just as well as `RUN -5`). If given, it must be between `-20` and `-1` —
this mode only supports raising priority (negative nice), not lowering it.
`-5` is a reasonable starting point for most systems — overly aggressive
values (`-15`, `-20`) can starve the rest of the system. If you pass a
value outside `-20..-1`, the wrapper prints a warning and falls back to the
default of `-5` rather than failing outright.

If `sched_autogroup_enabled` is active (this script sets it to `1` during
PRE), a plain per-process nice value is only meaningful relative to other
processes in the same autogroup. To make the priority boost effective
system-wide, the wrapper also writes the same nice value to
`/proc/self/autogroup` on a best-effort basis (silently skipped if your
kernel doesn't have `CONFIG_SCHED_AUTOGROUP` enabled).

The three fields are independent of each other: if you don't want to use
the command prefix, just fill in the Pre/Post-game script fields.

---

## Configuration: `/etc/lutris-game-tune.conf`

The file is read on a **whitelist basis** (there is no `source` inside the
script — this prevents arbitrary code execution in the setuid chain). Only
the keys below are recognized; unknown keys and malformed lines are
silently ignored (with a warning logged).

```bash
# --- CPU Governor / EPP ---
SET_CPU_GOVERNOR=1
CPU_GOVERNOR=performance

# --- PCIe ASPM ---
SET_ASPM=1
ASPM_POLICY=performance

# --- Deep C-state disabling (optional) ---
DISABLE_DEEP_CSTATES=0
CSTATE_KEEP_MAX=1

# --- VM ---
VM_SWAPPINESS=10

# --- CCD/CCX core isolation ---
CCD_ISOLATION_ENABLED=1
CCD_PROTECTED_CGROUPS=              # legacy, no longer used — see below
CCD_PROTECTED_PROCS=elogind elogind-daemon systemd-logind dbus-daemon dbus-broker polkitd openrc-user login gdm gdm3 sddm lightdm
CCD_LAUNCHER=lutris          # "steam" for Steam, "heroic" for Heroic, etc.
CCD_EXTRA_GOOD_PROCS=        # e.g.: gamescope picom
CCD_MONITOR_SECONDS=30
CCD_GOOD_PARTITION_TYPE=root
CCD_UGLY_PARTITION_TYPE=root
```

**THP settings are NOT present in the config** — `enabled`/`shmem_enabled`/
`defrag` are all hardcoded to `always` inside the script and are not
configurable.

The file must be owned by `root:root` and must not be group/other writable
(644 or 640). Otherwise the script ignores it entirely and runs with
defaults — this is a security measure, not a bug.

**For single-CCD/CCX processors**: even with `CCD_ISOLATION_ENABLED=1`, the
script detects how many distinct L3 (CCX) groups exist via
`/sys/devices/system/cpu/cpu*/cache/index3/shared_cpu_list`. If only one
group is found, the CCD section returns **without doing anything and
without any log output**. To check:
```bash
cat /sys/devices/system/cpu/cpu*/cache/index3/shared_cpu_list | sort -u
```
A single line means your system has one CCX and isolation is not applied.

---

## CCD/CCX isolation architecture

**This section explains a design change made after a real incident — read
it before changing `CCD_PROTECTED_CGROUPS`/`CCD_PROTECTED_PROCS` or
wondering why isolation "only" touches some processes.**

### The incident

An earlier version of this script isolated CCDs by **moving processes
between cgroups**: on PRE, every process found anywhere on the system
(except paths listed in `CCD_PROTECTED_CGROUPS`) was swept into a
temporary `theUgly` cgroup pinned to the system CCD; on POST, everything
was moved back.

This broke things in production. A login-session process (the OpenRC
`openrc-user` session leader, in the case that surfaced this) was found
sitting directly in the cgroup-v2 **root** at scan time — not yet inside
any protected path — so it got swept into `theUgly` along with everything
else, then correctly moved back to root on POST. Mechanically that's
exactly what the mover was designed to do. The problem is that **root
isn't a safe cgroup for a session leader to sit in**: D-Bus/PolicyKit's
(elogind's or systemd-logind's) notion of "this process belongs to an
active, local login session" depends on the process's cgroup membership.
The moment a session's processes left their cgroup — even for a split
second, even landing back in the exact same place afterward — the login
manager's session tracking got confused and the session dropped into a
`closing` state. From there, every `pkexec`-gated action (including
power-management GUIs) started failing with:

```
Error executing command as another user: Not authorized
```

In one occurrence this cascaded further: the login manager's own process
also ended up swept, its accounting broke, and the TTY/session layer got
wedged badly enough that a reboot was needed to fully clear it.

The fix was not "protect more paths" — a static or even dynamic protect
list can always miss a process that hasn't moved into its expected cgroup
yet at scan time. The fix was to **stop moving existing processes at
all.**

### The current design

- **Nothing that already has a cgroup gets moved.** On PRE, the script
  walks every top-level directory directly under `/sys/fs/cgroup/` (login
  session scopes, elogind, dbus, other service cgroups, whatever your
  init system created) and writes that cgroup's own `cpuset.cpus` to the
  system CCD (`constrain_cgroup_cpus()`). The original value is saved to
  `STATE_DIR` first. Because `cpuset` is hierarchical, every process
  inside that cgroup — and inside any of its sub-cgroups — is confined to
  those CPUs automatically. No process's cgroup membership changes, so
  nothing that depends on cgroup identity (session tracking, service
  supervision, etc.) is disturbed.
- **Only genuinely homeless processes are still moved.** Anything found
  sitting directly in the root cgroup's own `cgroup.procs` (not inside any
  sub-cgroup — mostly stray daemons and kernel threads that can't be
  moved anyway) is moved into `theUgly`, exactly as before, with each
  PID's origin recorded so POST can put it back precisely where it was
  found — not dumped into root.
- **The launcher/game process tree** is still actively moved into
  `theGood` (the performance CCD), same as always — that's the one thing
  this script's isolation is actually *for*.
- **Two independent safety nets** still exist for that one remaining move
  path (root-level stray processes):
  - `CCD_PROTECTED_PROCS` — a static, space-separated list of process
    names (matched with `pgrep -x`) that are never moved, covering common
    daemon/session-manager names across both OpenRC and systemd.
  - A **dynamic** lookup, refreshed on every PRE run, that reads the
    current login session leader PIDs directly from the login manager's
    own records (`/run/systemd/sessions/*/LEADER` or
    `/run/elogind/sessions/*/LEADER`) and protects them by PID regardless
    of name or cgroup. This is the more robust of the two — it doesn't
    need to know in advance what your session leader is called.
- **`CCD_PROTECTED_CGROUPS` no longer does anything.** It's still parsed
  from the config file (so old configs don't produce warnings) but the
  sweep it used to guard no longer exists.

### Practical effect / systemd compatibility

On a systemd system, `cpuset` may or may not be delegated to top-level
slices like `system.slice`/`user.slice` (`Delegate=` / subtree_control).
If it isn't, `constrain_cgroup_cpus()` simply can't write to those
cgroups' `cpuset.cpus` and skips them — the script logs how many cgroups
it managed to constrain vs. skipped, and warns if it constrained none at
all. This reduces how much of the **system side** actually gets confined
to the non-performance CCD, but it is never unsafe: nothing is moved, so
no session/service identity can break. The launcher/game side of
isolation (moving the game onto the performance CCD) is unaffected either
way. Both OpenRC's elogind and systemd's systemd-logind write session
records in the same `LEADER=` format, so the dynamic session-leader
protection works unchanged on either init system.

---

## CCD/CCX revert behavior on exit

When the last game exits (POST, reference count reaches 0),
`restore_ccd_isolation()` does two things, in order:

1. **Undoes the in-place cpuset constraints** on every existing cgroup
   that was constrained during PRE, restoring each one's original
   `cpuset.cpus` value from what was saved in `STATE_DIR`. This runs
   first and is a straightforward one-pass restore — there's no process
   movement involved, so there's nothing to retry.
2. **Tears down `theGood`/`theUgly`**: moves every process still inside
   them back to its recorded origin cgroup (the launcher/game tree back
   to wherever it started, root-level stray processes back to root), then
   removes both cgroups. Since a process can sometimes take a moment to
   actually leave a cgroup, this step is retried:

- **Up to 25 attempts**, roughly 0.2 seconds apart.
- **Early stop**: as soon as both `theGood` and `theUgly` are empty and
  have been successfully removed, the function returns immediately —
  it does not wait out the rest of the 25 attempts.
- **Hard failure**: if, after all 25 attempts, at least one of the two
  groups still contains processes or could not be removed, the script logs
  an explicit error and exits with a non-zero status. All *other* settings
  (VM/THP/scheduler/PCI/etc., and the cpuset restore from step 1 above)
  are still applied regardless — only the `theGood`/`theUgly` teardown is
  reported as failed.

You'll see one of these in `/var/log/lutris-game-tune.log`:

```
[INFO ] CCD/CCX isolation fully reverted after 3 attempt(s).
```
or
```
[ERROR] CCD/CCX isolation could not be fully reverted after 25 attempts.
[ERROR]   theUgly still has 1 process(es) or could not be removed.
```

---

## Manual testing

```bash
# Enter game mode (VM/THP/sched/PCI + CCD isolation, depending on your system)
sudo /usr/local/bin/lutris-game-tune-wrapper PRE

# Check status
sudo /usr/local/bin/lutris-game-tune-wrapper STATUS

# Check THP settings (all three should read "always")
cat /sys/kernel/mm/transparent_hugepage/enabled
cat /sys/kernel/mm/transparent_hugepage/shmem_enabled
cat /sys/kernel/mm/transparent_hugepage/defrag

# On a multi-CCD system, check the cgroups (if present)
cat /sys/fs/cgroup/theGood/cpuset.cpus.effective 2>/dev/null
cat /sys/fs/cgroup/theUgly/cpuset.cpus.effective 2>/dev/null

# --- WHILE still in game mode, confirm your own login session was left
# --- completely alone (this is the exact check that catches the "Not
# --- authorized" failure mode described above, if it were ever to recur):
loginctl
loginctl show-session $(loginctl | awk '/'"$USER"'/ {print $1; exit}') -p Active -p State
cat /proc/$$/cgroup    # should be your normal session cgroup, unchanged
echo '{"op":"test"}' | pkexec /bin/true 2>&1; echo "pkexec exit: $?"

# Exit game mode
sudo /usr/local/bin/lutris-game-tune-wrapper POST

# Test RUN mode (as a normal user — the wrapper itself must be setuid,
# call it directly after install, not with sudo)
lutris-game-tune-wrapper RUN -5 -- nice        # should print "-5"
lutris-game-tune-wrapper RUN -- nice           # no nice given, should also print "-5" (the default)
lutris-game-tune-wrapper RUN -5 -- whoami      # should print your own username, NOT "root"
lutris-game-tune-wrapper RUN -5 -- id          # supplementary groups (audio, video, etc.) should be intact
lutris-game-tune-wrapper RUN -999 -- nice      # out-of-range: warns and falls back to "-5", still runs `nice`
```

---

## Logs and status

| Path | Contents |
|---|---|
| `/var/log/lutris-game-tune.log` | Full PRE/POST/STATUS history (including CCD), rotated at 1 MB |
| `/run/lutris-game-tune/` | Current state — cleared on reboot. Includes: saved original parameter values, the reference count (`.refcount`), per-PID cgroup origins for any root-level stray processes moved into `theUgly` (`.ccd_pid_origin`), and the original `cpuset.cpus` values for every existing cgroup constrained in place (`.ccd_cpuset_saved`) |

```bash
tail -f /var/log/lutris-game-tune.log
sudo lutris-game-tune-wrapper STATUS
```

---

## Troubleshooting

**polkit-gated tools fail with "Not authorized" while a game is running (or after exit)**
If you're running a version of this script from before the v4 rewrite,
this is the exact incident that drove the redesign — see
[CCD/CCX isolation architecture](#ccdccx-isolation-architecture) above.
Update to the current script; the sweep that caused this no longer exists.
If you're already on the current version and still see this, check
`/var/log/lutris-game-tune.log` for `constrained` / `skipped` counts during
PRE and confirm your login session is still `active` (not `closing`):
```bash
loginctl
loginctl show-session <your-session-id> -p Active -p State
```

**Log says "No existing cgroups could be constrained"**
This means `cpuset` isn't delegated to the top-level cgroups on your
system (common on systemd if `Delegate=cpuset`/subtree_control isn't set
for `system.slice`/`user.slice`). Nothing is broken — the script simply
couldn't confine the system side to the non-performance CCD, so isolation
coverage is reduced but still safe (nothing is moved). The launcher/game
side of isolation is unaffected.

**CCD isolation doesn't seem to do anything / never shows up in the log**
Your processor most likely has a single CCX/CCD — this is silently skipped
by design (see "For single-CCD/CCX processors" above).

**RUN mode says "Error: privilege drop could not be verified — aborting"**
This is an expected safeguard for the case where the wrapper is already
being invoked as root (e.g. via `sudo`) — RUN mode is meant for normal user
commands, and "dropping" from root to root is meaningless, so the wrapper
refuses. Since Lutris runs as a normal user, you won't see this in actual
use.

**Nice value isn't applied / "setpriority() failed" error**
Verify that the wrapper's setuid bit is actually set:
```bash
ls -la /usr/local/bin/lutris-game-tune-wrapper
```
The output should show `-rwsr-xr-x` (lowercase `s`). Your `/usr/local/bin`
may be mounted `nosuid`.

**RUN mode prints "Warning: nice value ... out of range ... using default -5"**
You passed a nice value outside `-20..-1` (e.g. `RUN -999 -- game`). The
wrapper doesn't fail — it falls back to the default of `-5` and still runs
your command normally. Pass a value in range if you want something other
than the default.

**`debugfs` parameters say `WARN: Does not exist`**
Your kernel may not have `CONFIG_SCHED_DEBUG` enabled:
```bash
zcat /proc/config.gz | grep CONFIG_SCHED_DEBUG
```

**POST ran but settings weren't restored**
Use `STATUS` to confirm game mode is actually off. If multiple games are
open at once, POST defers the restore until the reference count reaches 0
(i.e. the last game exits) — that's expected behavior, not a bug.

**CCD isolation fails to revert (error after 25 attempts)**
Check `/var/log/lutris-game-tune.log` for which group (`theGood` or
`theUgly`) still has stuck processes. This usually means a process is
unkillable or in an uninterruptible state; you may need to identify and
deal with it manually (e.g. `cat /sys/fs/cgroup/theUgly/cgroup.procs`), then
re-run `sudo lutris-game-tune-wrapper POST`.

---

## Security notes

The wrapper binary:
- Only accepts `PRE`, `POST`, `STATUS`, or `RUN [nice] [--] <command>`
  arguments (whitelist)
- Clears the environment with `clearenv()` in PRE/POST/STATUS (prevents
  `LD_PRELOAD`-style attacks)
- Before running the tuning script, verifies it's a regular file, owned by
  root, and not group/other writable (`verify_script()`) — an extra line of
  defense beyond filesystem permissions alone
- **Deliberately does not** clear the environment in RUN mode (the game
  needs variables like DISPLAY, WINEPREFIX, etc.); instead it applies a
  **permanent, irreversible** privilege drop (`initgroups` + `setgid` +
  `setuid` back to the real user) and proves it by verifying that
  `setuid(0)` fails afterward
- Restores the calling user's real supplementary group memberships via
  `initgroups()` (not an empty group list) — groups like `audio` or `video`
  that games/Wine often depend on are preserved
- Strictly validates the nice value as an integer in the `-20..-1` range;
  an out-of-range or missing value falls back to the default of `-5`
  rather than failing, and is never mistaken for the command to run
- Script/config files are owned by `root:root` and not writable by normal
  users; the config is additionally checked for group/other write
  permissions

Re-run `sudo ./install.sh` whenever the wrapper or script is updated.
