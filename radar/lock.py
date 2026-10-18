"""OS-managed exclusive ownership of a database's workflow runner."""
import contextlib
import os


@contextlib.contextmanager
def runner_lock(directory):
    handle = open(directory / '.runner.lock', 'a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            handle.write(b'0')
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise ValueError('Another Venture Radar runner owns this data directory. Use the running app to trigger research, or stop it first.') from None
    try:
        yield
    finally:
        handle.close()  # OS releases the lock on normal exit or process termination.
