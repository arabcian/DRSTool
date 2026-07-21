/*
 * lutris-game-tune-wrapper.c  (v2.1, merged)
 *
 * Setuid root wrapper for lutris-game-tune.sh
 *
 * Modes:
 *   PRE / POST / STATUS  → runs the tuning script as root
 *   RUN [nice] cmd...    → sets a negative nice value (default -5), drops
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

/* Exact path to the script — update here too if you move it. */
#define SCRIPT_PATH "/usr/local/lib/lutris-game-tune/lutris-game-tune.sh"

/* Default nice value used in RUN mode when none is specified. */
#define DEFAULT_GAME_NICE (-5)
/* Allowed nice range for RUN mode: negative values only (priority boost). */
#define NICE_MIN (-20)
#define NICE_MAX (-1)

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

static int do_run(int argc, char *argv[])
{
    uid_t ruid = getuid();
    gid_t rgid = getgid();
    int nice_val = DEFAULT_GAME_NICE;
    int cmd_idx = 2;

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

    /* Optional "--" separator between the nice value and the command
     * (some Lutris command-prefix templates insert it automatically). */
    if (cmd_idx < argc && strcmp(argv[cmd_idx], "--") == 0) {
        cmd_idx++;
    }

    if (cmd_idx >= argc) {
        fprintf(stderr, "Usage: %s RUN [%d..%d] [--] command [args...]\n",
                argv[0], NICE_MIN, NICE_MAX);
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

int main(int argc, char *argv[])
{
    if (argc < 2) {
        fprintf(stderr, "Usage: %s PRE|POST|STATUS | RUN [%d..%d] [--] command [args...]\n",
                argv[0], NICE_MIN, NICE_MAX);
        return 1;
    }

    const char *action = argv[1];

    /* --- RUN mode: lower nice, drop privileges, exec the game --- */
    if (strcmp(action, "RUN") == 0)
        return do_run(argc, argv);

    /* --- PRE/POST/STATUS: tuning script --- */
    if (strcmp(action, "PRE")    != 0 &&
        strcmp(action, "POST")   != 0 &&
        strcmp(action, "STATUS") != 0) {
        fprintf(stderr,
                "Error: invalid argument '%s'. Expected PRE, POST, STATUS, or RUN.\n",
                action);
        return 1;
    }
    if (argc != 2) {
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

    execl("/bin/bash", "bash", SCRIPT_PATH, action, (char *)NULL);
    perror("execl failed");
    return 1;
}
