#define _GNU_SOURCE
/*
 * lutris-game-tune-wrapper.c  (v2.2)
 *
 * Setuid root wrapper for lutris-game-tune.sh
 *
 * Modes:
 *   PRE [profile]        → runs the tuning script as root; the optional
 *                          profile selects /etc/lutris-game-tune.d/<profile>.conf
 *   POST / STATUS        → run the tuning script as root
 *   RESTORE              → force a full restore (stuck game-mode state)
 *   DRYRUN [profile]     → show what PRE would do, change nothing
 *   RUN [nice] [--io SPEC] [--sched SPEC] [--] cmd...
 *                        → sets a negative nice value (default -5), optionally
 *                          also the I/O priority and the scheduling policy, drops
 *                          privileges PERMANENTLY BACK to the calling user,
 *                          then execs the command. Designed for Lutris's
 *                          "Command prefix" field.
 *
 * RUN mode details:
 *   - nice is optional; must be in -20..-1 (e.g. RUN -7 game.exe)
 *   - setpriority() sets the process nice value (inherited by children)
 *   - if sched_autogroup is active, process nice is only meaningful within
 *     its own autogroup, so the same value is also written to
 *     /proc/self/autogroup (best-effort)
 *   - privileges are then FULLY dropped: initgroups → setgid → setuid
 *     (to the real uid/gid); if the drop cannot be verified, the process aborts
 *   - the environment is NOT touched (the game needs its DISPLAY/WAYLAND/WINE
 *     variables; since no privilege remains, the user's environment is safe)
 *
 * Security note: RUN mode grants any local user able to execute this binary
 * the ability to start a command with a negative nice value (similar to the
 * system-wide privilege gamemode grants). Designed for a single-user game
 * machine.
 *
 * v2.1 changes:
 *   - added RUN mode (nice + autogroup nice, then a full privilege drop)
 *   - added verify_script(): the tuning script must be a regular file,
 *     owned by root, and not group/other writable before it's executed
 *   - added umask(022) before running the tuning script
 *   - real UID/GID are captured before the script's argv-shift logic runs;
 *     if a nice-looking token (e.g. "-999") is out of range, the wrapper
 *     falls back to the default nice value and still treats the token as
 *     the nice argument (not as the command) — avoids accidentally
 *     execve()-ing an out-of-range nice value as if it were a program name
 *   - an optional "--" separator between the nice value and the command is
 *     now accepted (some Lutris command-prefix templates insert one
 *     automatically)
 *
 * v2.2 changes:
 *   - PRE/DRYRUN accept ONE optional profile name ([A-Za-z0-9][A-Za-z0-9._-]*,
 *     max 64 chars — no slashes, no leading dot); POST/STATUS/RESTORE take none
 *   - RUN: --io idle | be:0-7 | rt:0-7   (ioprio_set)
 *          --sched other | batch | idle | rr:1-10 | fifo:1-10   (sched_setscheduler)
 *     Real-time classes (io rt, sched rr/fifo) are REFUSED unless the root-owned,
 *     non-group/other-writable marker file /etc/lutris-game-tune.allow-rt exists:
 *     a runaway RT thread can starve the whole machine, so it is opt-in.
 *
 * Build & install: use install.sh, or:
 *   gcc -O2 -Wall -Wextra -o lutris-game-tune-wrapper lutris-game-tune-wrapper.c
 *   sudo install -o root -g root -m 4755 lutris-game-tune-wrapper /usr/local/bin/
 *   sudo install -o root -g root -m 755  lutris-game-tune.sh      /usr/local/lib/lutris-game-tune/
 *
 * Lutris:
 *   Pre-game script:  /usr/local/bin/lutris-game-tune-wrapper PRE
 *   Post-game script: /usr/local/bin/lutris-game-tune-wrapper POST
 *   Command prefix:   /usr/local/bin/lutris-game-tune-wrapper RUN
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <limits.h>
#include <unistd.h>
#include <fcntl.h>
#include <pwd.h>
#include <grp.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <sched.h>
#include <ctype.h>

/* Exact path to the script — update here too if you move it. */
#define SCRIPT_PATH "/usr/local/lib/lutris-game-tune/lutris-game-tune.sh"

/* Default nice value used in RUN mode when none is specified. */
#define DEFAULT_GAME_NICE (-5)
/* Allowed nice range for RUN mode: negative values only (priority boost). */
#define NICE_MIN (-20)
#define NICE_MAX (-1)

/* RUN: I/O priority (linux/ioprio.h is not always installed) */
#define IOPRIO_CLASS_SHIFT 13
#define IOPRIO_WHO_PROCESS 1
#define IOPRIO_CLASS_RT    1
#define IOPRIO_CLASS_BE    2
#define IOPRIO_CLASS_IDLE  3

/* RUN: real-time priorities are clamped to this range and need the marker */
#define RT_PRIO_MAX 10
#define RT_ALLOW_FILE "/etc/lutris-game-tune.allow-rt"

static int verify_script(const char *path)
{
    struct stat st;

    if (stat(path, &st) != 0) {
        perror("Script stat failed");
        return -1;
    }
    if (!S_ISREG(st.st_mode)) {
        fprintf(stderr, "Error: %s is not a regular file.\n", path);
        return -1;
    }
    if (st.st_uid != 0) {
        fprintf(stderr, "Error: %s is not owned by root (uid=%u).\n",
                path, (unsigned)st.st_uid);
        return -1;
    }
    if (st.st_mode & (S_IWGRP | S_IWOTH)) {
        fprintf(stderr, "Error: %s is group/other writable — refusing.\n",
                path);
        return -1;
    }
    return 0;
}

/* Does the token look like it was INTENDED as a nice value? i.e. starts
 * with '-' and is fully numeric after that. This is checked separately
 * from range validity so we can tell "not a nice arg at all" (e.g. it's the
 * command name "wine") apart from "was clearly meant as a nice arg, but out
 * of range" (e.g. "-999") — the latter must NOT be passed on as the command
 * to execute. */
static int looks_like_nice_token(const char *s)
{
    if (s == NULL || s[0] != '-' || s[1] == '\0')
        return -1;
    char *end = NULL;
    errno = 0;
    strtol(s, &end, 10);
    if (errno != 0 || *end != '\0')
        return -1;
    return 0;
}

/* Strict range check once we know the token is numeric. Returns 0 and sets
 * *out if within [NICE_MIN, NICE_MAX], -1 otherwise. */
static int parse_nice_range(const char *s, int *out)
{
    long v = strtol(s, NULL, 10);
    if (v < NICE_MIN || v > NICE_MAX)
        return -1;
    *out = (int)v;
    return 0;
}

/* PRE/POST/STATUS: escalate fully to root (script execution path) */
static int escalate_to_root(void)
{
    if (setgroups(0, NULL) != 0) { perror("setgroups() failed"); return -1; }
    if (setgid(0)          != 0) { perror("setgid(0) failed");   return -1; }
    if (setuid(0)          != 0) { perror("setuid(0) failed");   return -1; }
    return 0;
}

/* RUN: permanently drop privileges back to the calling user */
static int drop_to_caller(uid_t ruid, gid_t rgid)
{
    struct passwd *pw = getpwuid(ruid);

    if (pw != NULL) {
        if (initgroups(pw->pw_name, rgid) != 0) {
            perror("initgroups() failed");
            return -1;
        }
    } else {
        /* No passwd entry — at least reset supplementary groups */
        if (setgroups(1, &rgid) != 0) {
            perror("setgroups() failed");
            return -1;
        }
    }
    if (setgid(rgid) != 0) { perror("setgid() failed"); return -1; }
    if (setuid(ruid) != 0) { perror("setuid() failed"); return -1; }

    /* Verify the drop is permanent: for a non-root user, re-escalating
     * MUST fail */
    if (ruid != 0 && setuid(0) == 0) {
        fprintf(stderr, "Error: privilege drop could not be verified — aborting.\n");
        return -1;
    }
    return 0;
}

/* Write nice to /proc/self/autogroup (best-effort; absent if autogroup is disabled) */
static void set_autogroup_nice(int nice_val)
{
    char buf[16];
    int fd, len;

    fd = open("/proc/self/autogroup", O_WRONLY);
    if (fd < 0)
        return; /* CONFIG_SCHED_AUTOGROUP may be disabled — not an error */
    len = snprintf(buf, sizeof(buf), "%d", nice_val);
    if (len > 0)
        (void)!write(fd, buf, (size_t)len);
    close(fd);
}

static int parse_small_int(const char *s, int lo, int hi, int *out)
{
    char *end = NULL;
    long v;

    errno = 0;
    v = strtol(s, &end, 10);
    if (errno != 0 || end == s || *end != '\0' || v < lo || v > hi)
        return -1;
    *out = (int)v;
    return 0;
}

/* idle | be:0-7 | rt:0-7 */
static int parse_io_spec(const char *spec, int *cls, int *lvl)
{
    if (strcmp(spec, "idle") == 0) {
        *cls = IOPRIO_CLASS_IDLE; *lvl = 0;
        return 0;
    }
    if (strncmp(spec, "be:", 3) == 0 && parse_small_int(spec + 3, 0, 7, lvl) == 0) {
        *cls = IOPRIO_CLASS_BE;
        return 0;
    }
    if (strncmp(spec, "rt:", 3) == 0 && parse_small_int(spec + 3, 0, 7, lvl) == 0) {
        *cls = IOPRIO_CLASS_RT;
        return 0;
    }
    fprintf(stderr, "Error: invalid --io spec '%s' (idle | be:0-7 | rt:0-7).\n", spec);
    return -1;
}

/* other | batch | idle | rr:1-10 | fifo:1-10 */
static int parse_sched_spec(const char *spec, int *pol, int *prio)
{
    *prio = 0;
    if (strcmp(spec, "other") == 0) { *pol = SCHED_OTHER; return 0; }
    if (strcmp(spec, "batch") == 0) { *pol = SCHED_BATCH; return 0; }
    if (strcmp(spec, "idle")  == 0) { *pol = SCHED_IDLE;  return 0; }
    if (strncmp(spec, "rr:", 3) == 0 &&
        parse_small_int(spec + 3, 1, RT_PRIO_MAX, prio) == 0) {
        *pol = SCHED_RR;
        return 0;
    }
    if (strncmp(spec, "fifo:", 5) == 0 &&
        parse_small_int(spec + 5, 1, RT_PRIO_MAX, prio) == 0) {
        *pol = SCHED_FIFO;
        return 0;
    }
    fprintf(stderr,
            "Error: invalid --sched spec '%s' (other | batch | idle | rr:1-%d | fifo:1-%d).\n",
            spec, RT_PRIO_MAX, RT_PRIO_MAX);
    return -1;
}

/* Real-time classes are opt-in: marker must be a root-owned regular file,
 * not group/other writable, and not a symlink. */
static int rt_allowed(void)
{
    struct stat st;

    if (lstat(RT_ALLOW_FILE, &st) != 0)
        return 0;
    if (!S_ISREG(st.st_mode) || st.st_uid != 0 ||
        (st.st_mode & (S_IWGRP | S_IWOTH)))
        return 0;
    return 1;
}

static void print_usage(const char *prog)
{
    fprintf(stderr,
            "Usage: %s PRE [profile] | POST | STATUS | RESTORE | DRYRUN [profile]\n"
            "       %s RUN [%d..%d] [--io SPEC] [--sched SPEC] [--] command [args...]\n",
            prog, prog, NICE_MIN, NICE_MAX);
}

static int do_run(int argc, char *argv[])
{
    uid_t ruid = getuid();
    gid_t rgid = getgid();
    int nice_val = DEFAULT_GAME_NICE;
    int cmd_idx = 2;
    int io_class = -1, io_level = 0;
    int sched_pol = -1, sched_prio = 0;

    /* Only shift past argv[2] if it was clearly intended as a nice value
     * (numeric, '-'-prefixed). This avoids the ambiguity where an
     * out-of-range or malformed nice-looking argument could otherwise be
     * silently treated as the command to execute. */
    if (argc > 2 && looks_like_nice_token(argv[2]) == 0) {
        cmd_idx = 3;
        if (parse_nice_range(argv[2], &nice_val) != 0) {
            fprintf(stderr,
                    "Warning: nice value '%s' out of range (%d..%d); using default %d.\n",
                    argv[2], NICE_MIN, NICE_MAX, DEFAULT_GAME_NICE);
            nice_val = DEFAULT_GAME_NICE;
        }
    }

    /* Options (--io / --sched), then an optional "--" separator (some Lutris
     * command-prefix templates insert it automatically). */
    while (cmd_idx < argc && strncmp(argv[cmd_idx], "--", 2) == 0) {
        if (argv[cmd_idx][2] == '\0') {
            cmd_idx++;
            break;
        }
        if (strcmp(argv[cmd_idx], "--io") == 0 && cmd_idx + 1 < argc) {
            if (parse_io_spec(argv[cmd_idx + 1], &io_class, &io_level) != 0)
                return 1;
            cmd_idx += 2;
            continue;
        }
        if (strcmp(argv[cmd_idx], "--sched") == 0 && cmd_idx + 1 < argc) {
            if (parse_sched_spec(argv[cmd_idx + 1], &sched_pol, &sched_prio) != 0)
                return 1;
            cmd_idx += 2;
            continue;
        }
        fprintf(stderr, "Error: unknown or incomplete RUN option '%s'.\n", argv[cmd_idx]);
        return 1;
    }

    if (cmd_idx >= argc) {
        print_usage(argv[0]);
        return 1;
    }

    if ((io_class == IOPRIO_CLASS_RT || sched_pol == SCHED_RR || sched_pol == SCHED_FIFO) &&
        !rt_allowed()) {
        fprintf(stderr,
                "Error: real-time I/O / scheduling classes are disabled.\n"
                "       To allow them: sudo install -o root -g root -m 644 /dev/null %s\n",
                RT_ALLOW_FILE);
        return 1;
    }

    /* Temporary root privilege (still effective here) is used to set the
     * nice value; setpriority is inherited by children. */
    if (setpriority(PRIO_PROCESS, 0, nice_val) != 0) {
        perror("setpriority() failed");
        /* not fatal — still start the game */
    }

    /* If autogroup is active, also lower the group weight (otherwise nice
     * is only effective against processes in the same session) */
    set_autogroup_nice(nice_val);

    /* I/O priority and scheduling policy: both are inherited by children. */
    if (io_class >= 0) {
        long prio = ((long)io_class << IOPRIO_CLASS_SHIFT) | io_level;
        if (syscall(SYS_ioprio_set, IOPRIO_WHO_PROCESS, 0, prio) != 0)
            perror("ioprio_set() failed");
    }
    if (sched_pol >= 0) {
        struct sched_param sp;
        memset(&sp, 0, sizeof(sp));
        sp.sched_priority = sched_prio;
        if (sched_setscheduler(0, sched_pol, &sp) != 0)
            perror("sched_setscheduler() failed");
    }

    /* Permanently drop privileges back to the calling user */
    if (drop_to_caller(ruid, rgid) != 0)
        return 1;

    /* Do not touch the environment: the game needs the user's
     * DISPLAY/WINE/Lutris environment; this is safe now that no privilege
     * remains. */
    execvp(argv[cmd_idx], &argv[cmd_idx]);
    fprintf(stderr, "execvp('%s') failed: %s\n",
            argv[cmd_idx], strerror(errno));
    return 127;
}

/* [A-Za-z0-9][A-Za-z0-9._-]{0,63}: no slash, no leading dot -> no traversal */
static int valid_profile_name(const char *s)
{
    size_t n = strlen(s), i;

    if (n == 0 || n > 64 || !isalnum((unsigned char)s[0]))
        return 0;
    for (i = 1; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        if (!isalnum(c) && c != '.' && c != '_' && c != '-')
            return 0;
    }
    return 1;
}

int main(int argc, char *argv[])
{
    if (argc < 2) {
        print_usage(argv[0]);
        return 1;
    }

    const char *action = argv[1];

    /* --- RUN mode: lower nice, drop privileges, exec the game --- */
    if (strcmp(action, "RUN") == 0)
        return do_run(argc, argv);

    /* --- PRE/POST/STATUS/RESTORE/DRYRUN: tuning script --- */
    int takes_profile = (strcmp(action, "PRE") == 0 || strcmp(action, "DRYRUN") == 0);

    if (!takes_profile &&
        strcmp(action, "POST")    != 0 &&
        strcmp(action, "STATUS")  != 0 &&
        strcmp(action, "RESTORE") != 0) {
        fprintf(stderr,
                "Error: invalid argument '%s'. Expected PRE, POST, STATUS, RESTORE, DRYRUN, or RUN.\n",
                action);
        return 1;
    }
    if (takes_profile) {
        if (argc > 3) {
            fprintf(stderr, "Error: %s accepts at most one argument (a profile name).\n", action);
            return 1;
        }
        if (argc == 3 && !valid_profile_name(argv[2])) {
            fprintf(stderr, "Error: invalid profile name '%s' ([A-Za-z0-9][A-Za-z0-9._-]*, max 64).\n",
                    argv[2]);
            return 1;
        }
    } else if (argc != 2) {
        fprintf(stderr, "Error: %s mode does not accept extra arguments.\n", action);
        return 1;
    }

    if (escalate_to_root() != 0)
        return 1;

    /*
     * Clean environment — prevents PATH injection and LD_PRELOAD-style
     * attacks. Only the minimal set the script needs.
     */
    if (clearenv() != 0) {
        perror("clearenv() failed");
        return 1;
    }
    setenv("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", 1);
    setenv("HOME", "/root", 1);

    umask(022);

    /* Script integrity check (there's a TOCTOU window, but the script
     * directory should already be root-writable-only — this is an
     * additional line of defense) */
    if (verify_script(SCRIPT_PATH) != 0)
        return 1;

    if (argc == 3)
        execl("/bin/bash", "bash", SCRIPT_PATH, action, argv[2], (char *)NULL);
    else
        execl("/bin/bash", "bash", SCRIPT_PATH, action, (char *)NULL);
    perror("execl failed");
    return 1;
}
