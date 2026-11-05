"""Bound capture cycles from a separate process, including blocked native threads."""
import os
import select
import signal
import subprocess
import time


CYCLE_TIMEOUT_SECONDS = 300
SHUTDOWN_GRACE_SECONDS = 10


def supervise(command, *, timeout=CYCLE_TIMEOUT_SECONDS, grace=SHUTDOWN_GRACE_SECONDS):
    read_fd, write_fd = os.pipe()
    try:
        child = subprocess.Popen(command, start_new_session=True, pass_fds=(write_fd,),
                                 env={**os.environ, 'MAPLY_CAPTURE_PROGRESS_FD': str(write_fd)})
    except BaseException:
        os.close(read_fd)
        raise
    finally:
        os.close(write_fd)
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    deadline = time.monotonic() + timeout
    progress_open = True

    def signal_group(sig):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            pass

    try:
        while child.poll() is None and not stopping:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print(f'Capture watchdog: no completed photo or idle status check within {timeout:g}s; '
                      f'resetting worker PID {child.pid}.', flush=True)
                return 124
            ready, _, _ = select.select([read_fd] if progress_open else [], [], [], min(1, remaining))
            if ready:
                if os.read(read_fd, 4096):
                    deadline = time.monotonic() + timeout
                else:
                    progress_open = False
        return 0 if stopping else child.returncode
    finally:
        try:
            if child.poll() is None:
                signal_group(signal.SIGTERM)
                try:
                    child.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    print(f'Capture watchdog: worker PID {child.pid} ignored shutdown for {grace:g}s; '
                          'sending SIGKILL to its process group.', flush=True)
        finally:
            # Also remove helper processes that outlived their parent and held the desktop lock.
            signal_group(signal.SIGKILL)
            child.wait()
            os.close(read_fd)
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
