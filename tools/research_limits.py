"""Offline heavy research leases shared by every worktree of this repository.

This limits concurrency, not trading risk or an individual process's RSS.
The OS releases a lease if its owner exits; there is no retry or launcher.

Capacity: runtime/research_resources/capacity.json in the primary checkout, {"slots": N} (1..3); absent or
unreadable means one slot, the historical single heavy.lock. Slot 1 is always heavy.lock itself, so older code
that only knows heavy.lock still counts against the same capacity. A lease that holds any slot besides
heavy.lock (a second concurrent job, or one job taking several slots) is refused while free RAM is below the
floor (default 6 GB), so parallel slots never trade away the memory guard; the lone heavy.lock case is unchanged.
"""
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path

MAX_SLOTS = 3
RAM_FLOOR_GB = 6.0


class ResearchBusy(RuntimeError):
    pass


def shared_repository_root(repo_root):
    root = Path(repo_root).resolve()
    marker = root / ".git"
    if marker.is_dir():
        return root
    if not marker.is_file() or marker.is_symlink() or marker.stat().st_size > 8192:
        return root
    pointer = marker.read_text(encoding="utf-8").strip()
    if not pointer.startswith("gitdir: "):
        return root
    git_dir = Path(pointer[8:])
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    common_file = git_dir / "commondir"
    if not common_file.is_file() or common_file.stat().st_size > 8192:
        return root
    common = Path(common_file.read_text(encoding="utf-8").strip())
    if not common.is_absolute():
        common = git_dir / common
    common = common.resolve()
    return common.parent if common.name == ".git" else root


def capacity(repo_root):
    path = shared_repository_root(repo_root) / "runtime" / "research_resources" / "capacity.json"
    try:
        slots = int(json.loads(path.read_text(encoding="utf-8"))["slots"])
    except (OSError, ValueError, KeyError, TypeError):
        return 1
    return max(1, min(slots, MAX_SLOTS))


def free_ram_gb():
    """Available physical memory in GB, or None where it cannot be read."""
    if os.name == "nt":
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                        ("available", ctypes.c_ulonglong), ("page_total", ctypes.c_ulonglong),
                        ("page_available", ctypes.c_ulonglong), ("virtual_total", ctypes.c_ulonglong),
                        ("virtual_available", ctypes.c_ulonglong), ("extended", ctypes.c_ulonglong)]
        status = Status()
        status.length = ctypes.sizeof(Status)
        return status.available / 2**30 if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) else None
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 2**20
    except OSError:
        return None
    return None


@contextmanager
def _lock_file(path):
    """Non-blocking exclusive OS lock on one byte of path; raises ResearchBusy if another process holds it."""
    if path.is_symlink():
        raise ValueError("Research lease must be a regular local file")
    handle = path.open("a+b")
    acquired = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as error:
            raise ResearchBusy("Інший важкий дослідницький прогін уже працює. "
                               "Цей запуск не почато; автоматично повторювати його не можна.") from error
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


@contextmanager
def research_slot(repo_root, label="offline research", slots=1, ram_floor_gb=RAM_FLOOR_GB):
    """Hold `slots` of the repository's heavy-research slots (clamped to capacity) or raise ResearchBusy at once."""
    folder = shared_repository_root(repo_root) / "runtime" / "research_resources"
    folder.mkdir(parents=True, exist_ok=True)
    total = capacity(repo_root)
    need = max(1, min(int(slots), total))
    names = ["heavy.lock"] + [f"heavy.{i}.lock" for i in range(2, total + 1)]
    with ExitStack() as stack:
        held = []
        for name in names:
            if len(held) == need:
                break
            try:
                stack.enter_context(_lock_file(folder / name))
                held.append(name)
            except ResearchBusy:
                continue
        if len(held) < need:
            raise ResearchBusy("Інший важкий дослідницький прогін уже працює "
                               f"(вільно {len(held)} з {total} слотів, потрібно {need}). "
                               "Цей запуск не почато; автоматично повторювати його не можна.")
        # Holding only heavy.lock is the historical single-run case and stays unconditional; any extra slot
        # means running beside another heavy job, which is allowed only above the RAM floor.
        ram = free_ram_gb() if held != ["heavy.lock"] else None
        if ram is not None and ram < ram_floor_gb:
            raise ResearchBusy(f"Вільної RAM {ram:.1f} ГБ < {ram_floor_gb:g} ГБ для паралельного слота. "
                               "Важкий прогін не почато; автоматично повторювати його не можна.")
        yield {"label": label, "slots": need, "capacity": total, "held": held}


@contextmanager
def exclusive_research_slot(repo_root, label="offline research"):
    """One heavy slot (historical name: with capacity 1 this is the single heavy.lock)."""
    with research_slot(repo_root, label, slots=1):
        yield
