# -*- coding: utf-8 -*-
from __future__ import annotations

import fcntl
import os
import signal
import stat
import sys
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def _purge_local_bytecode() -> None:
    """Always prefer the current audited source over stale same-size bytecode."""
    cache = BASE_DIR / "__pycache__"
    try:
        if cache.is_dir() and not cache.is_symlink():
            for path in cache.iterdir():
                try:
                    if path.suffix == ".pyc" and path.is_file() and not path.is_symlink():
                        path.unlink()
                except Exception:
                    pass
    except Exception:
        pass
    sys.dont_write_bytecode = True


_purge_local_bytecode()

from config import BOT_TOKEN, LOCK_FILE, PID_FILE, LOG_FILE


_MANAGED_PARENT_PID = 0
try:
    _MANAGED_PARENT_PID = int((os.environ.get("SHUIBEI_PARENT_PID") or "0").strip() or 0)
except Exception:
    _MANAGED_PARENT_PID = 0


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _open_regular_nofollow(path, flags: int, mode: int = 0o600):
    fd = os.open(str(path), int(flags) | getattr(os, "O_NOFOLLOW", 0), int(mode))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError("runtime path is not a regular file")
        if hasattr(os, "geteuid") and int(st.st_uid) != int(os.geteuid()):
            raise PermissionError("runtime file owner mismatch")
        os.fchmod(fd, 0o600)
        return fd
    except Exception:
        os.close(fd)
        raise


def _write_pid_file() -> None:
    fd = _open_regular_nofollow(PID_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _lock_instance():
    Path(LOCK_FILE).parent.mkdir(parents=True, exist_ok=True)
    fd = _open_regular_nofollow(LOCK_FILE, os.O_RDWR | os.O_CREAT)
    fh = os.fdopen(fd, "r+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        print("[ShuiBei] another instance is already running", flush=True)
        raise SystemExit(75)
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    os.fsync(fh.fileno())
    _write_pid_file()
    return fh


def _harden_existing_log() -> None:
    try:
        p = Path(LOG_FILE)
        if not p.exists() or p.is_symlink():
            return
        st = p.stat()
        if hasattr(os, "geteuid") and int(st.st_uid) != int(os.geteuid()):
            return
        if stat.S_ISREG(st.st_mode):
            os.chmod(p, 0o600)
    except Exception:
        pass


def _managed_parent_watch(parent_pid: int) -> None:
    """由 JiZhangBen 拉起时，父进程消失后水杯自动退出，避免留下孤儿 Bot。"""
    parent_pid = int(parent_pid or 0)
    if parent_pid <= 1:
        return
    while True:
        time.sleep(2.0)
        if not _pid_alive(parent_pid) or os.getppid() != parent_pid:
            try:
                os.kill(os.getpid(), signal.SIGTERM)
            except Exception:
                os._exit(0)
            return


def _handle_exit_signal(_signum, _frame):
    raise SystemExit(0)


def main():
    if not BOT_TOKEN or BOT_TOKEN == "PUT_SHUIBEI_BOT_TOKEN_HERE":
        print("[ShuiBei] ShuiBei Bot Token 配置无效。", flush=True)
        raise SystemExit(78)

    # SIGTERM 也走 finally，确保 PID 文件被清理；主进程退出时可以干净联动关闭。
    try:
        signal.signal(signal.SIGTERM, _handle_exit_signal)
        signal.signal(signal.SIGINT, _handle_exit_signal)
    except Exception:
        pass

    lock_fh = _lock_instance()
    _harden_existing_log()
    if _MANAGED_PARENT_PID > 1:
        threading.Thread(
            target=_managed_parent_watch,
            args=(_MANAGED_PARENT_PID,),
            daemon=True,
            name="shuibei-parent-watch",
        ).start()

    try:
        from app import run
        run()
    finally:
        try:
            Path(PID_FILE).unlink(missing_ok=True)
        except Exception:
            pass
        try:
            lock_fh.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
