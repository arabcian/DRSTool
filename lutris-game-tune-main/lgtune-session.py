#!/usr/bin/env python3
"""Unprivileged per-launch supervisor. Never execute the game through pkexec."""
import argparse
import os
import signal
import subprocess
import sys
import time
import uuid

WRAPPER = '/usr/local/bin/lutris-game-tune-wrapper'


def group_alive(pgid):
    # A launcher can exit before the game. Ignore zombie members; don't signal
    # processes outside the group created for this invocation.
    with os.scandir('/proc') as entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                with open(entry.path + '/stat') as stream:
                    tail = stream.read().rsplit(')', 1)[1].split()
                if int(tail[2]) == pgid and tail[0] not in ('Z', 'X'):
                    return True
            except (OSError, ValueError, IndexError):
                continue
    return False


def run(command, nice, *, polkit=False, wrapper=WRAPPER):
    token = 'run-' + uuid.uuid4().hex
    privileged = (['pkexec'] if polkit else []) + [wrapper]
    child = None
    pending = None
    armed = False
    previous = {}

    def forward(sig, _frame):
        nonlocal pending
        pending = sig
        if child is not None:
            try:
                os.killpg(child.pid, sig)
            except ProcessLookupError:
                pass

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous[sig] = signal.signal(sig, forward)
    rc = 1
    try:
        # No game is started when PRE fails or requests a conflicting profile.
        pre = subprocess.run([*privileged, 'PRE', '--global', token], start_new_session=True)
        if pre.returncode:
            return pre.returncode if pre.returncode > 0 else 128 - pre.returncode
        armed = True
        if pending is None:
            child = subprocess.Popen([wrapper, 'RUN', str(nice), '--', *command], start_new_session=True)
            if pending is not None:  # signal between the check and Popen
                forward(pending, None)
            rc = child.wait()
            while group_alive(child.pid):
                time.sleep(.25)
        else:
            rc = -pending
    except OSError as exc:
        print(f'Game Tune: {exc}', file=sys.stderr)
        rc = 127
    finally:
        try:
            if armed:
                # Do not erase another invocation's state, even after a delayed POST.
                try:
                    post = subprocess.run([*privileged, 'POST', token], start_new_session=True)
                    if post.returncode:
                        print('Game Tune: POST failed; inspect STATUS and recover after all games exit.', file=sys.stderr)
                        if rc == 0:
                            rc = 1
                except OSError as exc:
                    print(f'Game Tune: POST failed: {exc}', file=sys.stderr)
                    if rc == 0:
                        rc = 1
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    return 128 - rc if rc < 0 else rc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--polkit', action='store_true')
    parser.add_argument('--nice', type=int, default=-10, choices=range(-20, 0))
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or os.getuid() == 0 or os.geteuid() == 0:
        parser.error('requires a non-root user and a game command')
    return run(command, args.nice, polkit=args.polkit)


if __name__ == '__main__':
    sys.exit(main())
