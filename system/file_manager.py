r"""Safe file & folder operations.

This module is the single chokepoint between voice commands and the file
system. It enforces:

* **Hard-coded protected paths** — system-critical Windows directories may be
  *read* (with permission) but never written to.
* **Path normalisation** — symlinks and ``..`` traversals are resolved before
  any check so attackers cannot bypass guards via tricks like
  ``Documents\..\..\Windows``.
* **Confirmation hooks** — destructive ops (delete / overwrite / rename) ask
  via an injected callable so they can be wired to voice or GUI dialogs.
* **No shell** — every action uses :mod:`pathlib`, :mod:`shutil`, or
  :mod:`subprocess` with argv lists. Nothing is ever passed through ``cmd``
  with ``shell=True``.
"""
from __future__ import annotations

import os
import re
import shutil
import string
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

from utils.logger import get_logger
from utils.paths import user_home

_log = get_logger()

# Paths inside which **writes** are categorically forbidden.
PROTECTED_WRITE_ROOTS: tuple[Path, ...] = tuple(
    Path(p).resolve()
    for p in (
        os.environ.get("WINDIR", r"C:\Windows"),
        r"C:\Program Files",
        r"C:\Program Files (x86)",
        r"C:\ProgramData",
        os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32"),
        os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "SysWOW64"),
    )
    if p
)

# A folder hint such as "documents", "downloads", "desktop" is resolved to
# a real path here so users don't have to type full paths.
_NAMED_DIRS: dict[str, Path] = {
    "home": user_home(),
    "user": user_home(),
    "documents": user_home() / "Documents",
    "downloads": user_home() / "Downloads",
    "desktop": user_home() / "Desktop",
    "pictures": user_home() / "Pictures",
    "music": user_home() / "Music",
    "videos": user_home() / "Videos",
}


# ConfirmFn(question) -> True if the user said yes.
ConfirmFn = Callable[[str], bool]


class FileSafetyError(Exception):
    """Raised when an operation would touch a protected/illegal path."""


# ---------------------------------------------------------------------------
# Drive helpers (Windows-aware, but safe on POSIX)
# ---------------------------------------------------------------------------
def list_drives() -> List[str]:
    """Return the available local drive roots.

    On Windows that's something like ``["C:\\", "D:\\"]``. On POSIX systems
    we fall back to ``["/"]`` so search-from-root behaviour still works.
    """
    if not sys.platform.startswith("win"):
        root = Path("/")
        return [str(root)] if root.exists() else []
    return [
        f"{letter}:\\"
        for letter in string.ascii_uppercase
        if Path(f"{letter}:\\").exists()
    ]


# Match things people actually say or type for a drive:
#   "c", "c:", "c:/", "c:\\", "c drive", "drive c", "the c drive"
_DRIVE_HINT_RE = re.compile(
    r"""^\s*
        (?:the\s+)?
        (?:drive\s+)?            # "drive c"
        ([A-Za-z])                # the letter
        \s*
        (?::|\s*drive)?           # "c:", "c drive"
        [\\/]?                    # trailing slash
        \s*$
    """,
    re.VERBOSE,
)


def _count_tree(root: Path, cap: int = 10_000) -> Tuple[int, int]:
    """Return ``(files, dirs)`` inside ``root`` (capped to keep delete
    confirmations responsive even when pointed at huge trees).
    """
    files = 0
    dirs = 0
    try:
        for _current, dirnames, filenames in os.walk(root, followlinks=False):
            files += len(filenames)
            dirs += len(dirnames)
            if files + dirs >= cap:
                break
    except OSError:
        pass
    return files, dirs


def resolve_drive_hint(hint: str) -> Optional[Path]:
    """If ``hint`` names a drive (``"C"``, ``"D:"``, ``"E drive"``...) return its root.

    Returns ``None`` if the hint doesn't look like a drive reference or the
    drive isn't mounted. Always returns ``None`` on non-Windows.
    """
    if not hint or not sys.platform.startswith("win"):
        return None
    m = _DRIVE_HINT_RE.match(hint)
    if not m:
        return None
    letter = m.group(1).upper()
    root = Path(f"{letter}:\\")
    return root if root.exists() else None


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass
class FileOpResult:
    """Outcome of a file operation, suitable for spoken feedback."""

    ok: bool
    message: str
    payload: Optional[object] = None


# ---------------------------------------------------------------------------
# Path resolution & validation
# ---------------------------------------------------------------------------
def resolve_named_directory(hint: str) -> Optional[Path]:
    """Map a short name (``"downloads"``) to a real directory path."""
    if not hint:
        return None
    return _NAMED_DIRS.get(hint.strip().lower())


def normalise_path(raw: str, base: Optional[Path] = None) -> Path:
    """Expand env vars / ``~`` and resolve to an absolute Path.

    Relative paths resolve against ``base`` (defaults to the user's home).
    """
    if not raw or not str(raw).strip():
        raise FileSafetyError("Empty path is not allowed.")

    text = os.path.expandvars(os.path.expanduser(str(raw).strip().strip('"').strip("'")))
    p = Path(text)

    # Quick named-dir shortcut for single tokens like "documents".
    if not p.is_absolute():
        named = resolve_named_directory(text)
        if named is not None:
            return named.resolve()
        base = base or user_home()
        p = (base / p).resolve()
    else:
        p = p.resolve()

    # Reject paths containing NUL bytes or other control chars (injection guard).
    bad = set(p.as_posix()) & set(chr(c) for c in range(0, 32) if c != 9)
    if bad:
        raise FileSafetyError("Path contains control characters.")

    return p


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def assert_writable(path: Path) -> None:
    """Raise :class:`FileSafetyError` if writes to ``path`` are forbidden."""
    for root in PROTECTED_WRITE_ROOTS:
        if path == root or _is_inside(path, root):
            raise FileSafetyError(
                f"Refusing to modify protected location: {path}"
            )


def _validate_filename(name: str) -> str:
    """Reject filenames containing path separators or illegal characters."""
    cleaned = (name or "").strip().strip('"').strip("'")
    if not cleaned:
        raise FileSafetyError("Filename is empty.")
    if any(sep in cleaned for sep in ("/", "\\")):
        raise FileSafetyError("Filename must not contain path separators.")
    illegal = set('<>:"|?*') | {chr(c) for c in range(0, 32)}
    if set(cleaned) & illegal:
        raise FileSafetyError(f"Filename contains illegal characters: {cleaned!r}")
    return cleaned


# ---------------------------------------------------------------------------
# FileManager
# ---------------------------------------------------------------------------
class FileManager:
    """High-level, voice-friendly file & folder operations."""

    def __init__(self, confirmer: Optional[ConfirmFn] = None) -> None:
        self._confirm = confirmer or (lambda _q: False)

    # ---- read-only operations -----------------------------------------
    def open_folder(self, raw_path: str) -> FileOpResult:
        """Open ``raw_path`` in Windows Explorer (or platform equivalent)."""
        path = normalise_path(raw_path)
        if not path.exists():
            return FileOpResult(False, f"That path does not exist: {path}")
        if not path.is_dir():
            return FileOpResult(False, f"{path} is not a folder.")
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=True)
            else:
                subprocess.run(["xdg-open", str(path)], check=True)
            return FileOpResult(True, f"Opening {path.name or path}.", payload=path)
        except OSError as exc:
            return FileOpResult(False, f"Could not open the folder: {exc}")

    def list_directory(self, raw_path: str, limit: int = 50) -> FileOpResult:
        """Return a short listing of ``raw_path`` suitable for speech."""
        path = normalise_path(raw_path)
        if not path.exists() or not path.is_dir():
            return FileOpResult(False, f"That folder doesn't exist: {path}")
        try:
            entries = sorted(p.name for p in path.iterdir())
        except PermissionError:
            return FileOpResult(False, f"Permission denied reading {path}.")
        shown = entries[:limit]
        msg = (
            f"{path.name or path} contains {len(entries)} items."
            if not shown
            else f"{len(entries)} items in {path.name or path}: " + ", ".join(shown)
        )
        if len(entries) > limit:
            msg += f", and {len(entries) - limit} more."
        return FileOpResult(True, msg, payload=entries)

    def search_files(
        self,
        pattern: str,
        roots: Optional[Iterable[str]] = None,
        max_results: int = 25,
    ) -> FileOpResult:
        """Recursively search for files whose name contains ``pattern``.

        Behaviour:

        * If no ``roots`` are given, the search starts from the *root of every
          mounted drive* (e.g. ``C:\\``, ``D:\\``, ...) and one worker thread is
          spun up per drive so they crawl in parallel.
        * If ``roots`` are given, each entry is resolved. A bare drive
          reference (``"C"``, ``"C:"``, ``"D drive"``, ...) is mapped to that
          drive's root, and only those drives/folders are searched — again,
          one thread per root.
        * Search is short-circuited across all workers as soon as
          ``max_results`` matches have been collected.
        """
        pattern = (pattern or "").strip().lower()
        if not pattern:
            return FileOpResult(False, "Please tell me what to search for.")

        search_roots = self._resolve_search_roots(roots)
        if not search_roots:
            return FileOpResult(
                False,
                "I couldn't find any drive or folder to search in.",
            )

        matches: List[Path] = []
        matches_lock = threading.Lock()
        stop_event = threading.Event()

        def crawl(root: Path) -> None:
            try:
                for current, dirs, files in os.walk(root, followlinks=False):
                    if stop_event.is_set():
                        return
                    cur_path = Path(current)
                    # Skip protected system roots so we don't waste time
                    # crawling Windows/, Program Files/, etc.
                    if any(
                        _is_inside(cur_path, prot) or cur_path == prot
                        for prot in PROTECTED_WRITE_ROOTS
                    ):
                        dirs.clear()
                        continue
                    # Hide hidden/system dirs (.git, $Recycle.Bin, ...) — these
                    # are huge and almost never what the user wants.
                    dirs[:] = [d for d in dirs if not d.startswith((".", "$"))]

                    found_here: List[Path] = []
                    for name in files:
                        if pattern in name.lower():
                            found_here.append(cur_path / name)

                    if found_here:
                        with matches_lock:
                            for p in found_here:
                                if len(matches) >= max_results:
                                    stop_event.set()
                                    break
                                matches.append(p)
                            if len(matches) >= max_results:
                                stop_event.set()
                                return
            except (PermissionError, OSError) as exc:
                _log.debug("search skipped %s: %s", root, exc)

        # One worker per root — exactly what the user asked for.
        worker_count = max(1, len(search_roots))
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="jarvis-search",
        ) as pool:
            futures = [pool.submit(crawl, r) for r in search_roots]
            for _ in as_completed(futures):
                if stop_event.is_set():
                    # Other workers will notice the event on their next loop
                    # iteration and bail out promptly.
                    break

        if not matches:
            scope = (
                ", ".join(str(r) for r in search_roots)
                if len(search_roots) <= 3
                else f"{len(search_roots)} drives"
            )
            return FileOpResult(
                False, f"No files matched '{pattern}' in {scope}."
            )
        names = ", ".join(p.name for p in matches[:5])
        msg = (
            f"Found {len(matches)} match{'es' if len(matches) != 1 else ''}. "
            f"Top results: {names}."
        )
        return FileOpResult(True, msg, payload=matches)

    @staticmethod
    def _resolve_search_roots(
        roots: Optional[Iterable[str]],
    ) -> List[Path]:
        """Translate user-supplied root hints into real, existing directories.

        * ``None`` / empty ⇒ every mounted drive root.
        * Strings like ``"C"``, ``"D:"``, ``"E drive"`` ⇒ that drive's root.
        * Anything else is run through :func:`normalise_path`.
        Duplicates and non-existent entries are filtered out.
        """
        resolved: List[Path] = []
        seen: set[Path] = set()

        def add(p: Path) -> None:
            if not p.exists() or not p.is_dir():
                return
            key = Path(str(p).rstrip("\\/").lower() or str(p))
            if key in seen:
                return
            seen.add(key)
            resolved.append(p)

        if not roots:
            for drive in list_drives():
                add(Path(drive))
            return resolved

        for raw in roots:
            if not raw:
                continue
            drive_root = resolve_drive_hint(str(raw))
            if drive_root is not None:
                add(drive_root)
                continue
            try:
                add(normalise_path(str(raw)))
            except FileSafetyError:
                continue

        return resolved

    # ---- write operations (guarded) -----------------------------------
    def create_folder(self, parent_dir: str, folder_name: str) -> FileOpResult:
        parent = normalise_path(parent_dir)
        name = _validate_filename(folder_name)
        target = (parent / name).resolve()
        assert_writable(target)
        if not parent.exists():
            return FileOpResult(False, f"Parent folder does not exist: {parent}")
        if target.exists():
            return FileOpResult(False, f"'{name}' already exists in {parent}.")
        try:
            target.mkdir(parents=False, exist_ok=False)
            return FileOpResult(True, f"Folder '{name}' created in {parent.name or parent}.", payload=target)
        except OSError as exc:
            return FileOpResult(False, f"Could not create folder: {exc}")

    def create_file(
        self,
        parent_dir: str,
        file_name: str,
        contents: str = "",
    ) -> FileOpResult:
        parent = normalise_path(parent_dir)
        name = _validate_filename(file_name)
        target = (parent / name).resolve()
        assert_writable(target)
        if not parent.exists():
            return FileOpResult(False, f"Parent folder does not exist: {parent}")
        if target.exists():
            if not self._confirm(
                f"{name} already exists. Overwrite it?"
            ):
                return FileOpResult(False, "Skipped — existing file kept.")
        try:
            target.write_text(contents or "", encoding="utf-8")
            return FileOpResult(True, f"File '{name}' written in {parent.name or parent}.", payload=target)
        except OSError as exc:
            return FileOpResult(False, f"Could not write file: {exc}")

    def rename(self, raw_src: str, new_name: str) -> FileOpResult:
        src = normalise_path(raw_src)
        new = _validate_filename(new_name)
        if not src.exists():
            return FileOpResult(False, f"Source not found: {src}")
        target = (src.parent / new).resolve()
        assert_writable(src)
        assert_writable(target)
        if target.exists():
            return FileOpResult(False, f"A file named '{new}' already exists there.")
        if not self._confirm(f"Rename {src.name} to {new}?"):
            return FileOpResult(False, "Rename cancelled.")
        try:
            src.rename(target)
            return FileOpResult(True, f"Renamed to {new}.", payload=target)
        except OSError as exc:
            return FileOpResult(False, f"Could not rename: {exc}")

    def copy(self, raw_src: str, raw_dest_dir: str) -> FileOpResult:
        src = normalise_path(raw_src)
        dest_dir = normalise_path(raw_dest_dir)
        if not src.exists():
            return FileOpResult(False, f"Source not found: {src}")
        if not dest_dir.exists() or not dest_dir.is_dir():
            return FileOpResult(False, f"Destination folder doesn't exist: {dest_dir}")
        target = (dest_dir / src.name).resolve()
        assert_writable(target)
        if target.exists():
            if not self._confirm(f"{src.name} already exists in {dest_dir.name}. Overwrite?"):
                return FileOpResult(False, "Copy cancelled.")
        try:
            if src.is_dir():
                shutil.copytree(src, target, dirs_exist_ok=True)
            else:
                shutil.copy2(src, target)
            return FileOpResult(True, f"Copied {src.name} into {dest_dir.name or dest_dir}.",
                                payload=target)
        except OSError as exc:
            return FileOpResult(False, f"Could not copy: {exc}")

    def move(self, raw_src: str, raw_dest_dir: str) -> FileOpResult:
        src = normalise_path(raw_src)
        dest_dir = normalise_path(raw_dest_dir)
        if not src.exists():
            return FileOpResult(False, f"Source not found: {src}")
        if not dest_dir.exists() or not dest_dir.is_dir():
            return FileOpResult(False, f"Destination folder doesn't exist: {dest_dir}")
        target = (dest_dir / src.name).resolve()
        assert_writable(src)
        assert_writable(target)
        if target.exists():
            return FileOpResult(False, f"{src.name} already exists in {dest_dir.name}.")
        if not self._confirm(f"Move {src.name} into {dest_dir.name or dest_dir}?"):
            return FileOpResult(False, "Move cancelled.")
        try:
            shutil.move(str(src), str(target))
            return FileOpResult(True, f"Moved {src.name} into {dest_dir.name or dest_dir}.",
                                payload=target)
        except OSError as exc:
            return FileOpResult(False, f"Could not move: {exc}")

    def delete(self, raw_path: str) -> FileOpResult:
        target = normalise_path(raw_path)
        assert_writable(target)
        if not target.exists():
            return FileOpResult(False, f"Nothing to delete at {target}.")

        # Build a clear, voice-friendly confirmation. For folders we
        # surface the contents count so a "bulk" delete is never silent
        # — the user explicitly hears how many items are about to be
        # erased before they say yes.
        question = self._build_delete_question(target)
        if not self._confirm(question):
            return FileOpResult(False, "Delete cancelled.")
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            return FileOpResult(True, f"{target.name} deleted.")
        except OSError as exc:
            return FileOpResult(False, f"Could not delete: {exc}")

    def bulk_delete(self, raw_paths: Iterable[str]) -> FileOpResult:
        """Delete several files / folders after a single grouped confirm.

        The user gets ONE confirmation question listing every target
        and the total number of inner items, so they can review the
        whole batch before anything is removed.
        """
        targets: List[Path] = []
        problems: List[str] = []
        for raw in raw_paths or []:
            try:
                p = normalise_path(str(raw))
                assert_writable(p)
            except FileSafetyError as exc:
                problems.append(f"{raw}: {exc}")
                continue
            if not p.exists():
                problems.append(f"{raw}: not found")
                continue
            targets.append(p)

        if not targets:
            base = "Nothing to delete."
            if problems:
                base += " (" + "; ".join(problems[:3]) + ")"
            return FileOpResult(False, base)

        total_files = 0
        total_dirs = 0
        for t in targets:
            if t.is_dir():
                f, d = _count_tree(t)
                total_files += f
                total_dirs += d + 1  # the dir itself counts too
            else:
                total_files += 1

        sample = ", ".join(t.name for t in targets[:3])
        more = f" and {len(targets) - 3} more" if len(targets) > 3 else ""
        question = (
            f"BULK DELETE: permanently remove {len(targets)} item"
            f"{'s' if len(targets) != 1 else ''} "
            f"({sample}{more}) — totalling about "
            f"{total_files} file{'s' if total_files != 1 else ''}"
            + (f" and {total_dirs} folder{'s' if total_dirs != 1 else ''}"
               if total_dirs else "")
            + "? This cannot be undone."
        )
        if not self._confirm(question):
            return FileOpResult(False, "Bulk delete cancelled.")

        deleted: List[str] = []
        for t in targets:
            try:
                if t.is_dir():
                    shutil.rmtree(t)
                else:
                    t.unlink()
                deleted.append(t.name)
            except OSError as exc:
                problems.append(f"{t.name}: {exc}")

        msg_parts = [f"Deleted {len(deleted)} item"
                     f"{'s' if len(deleted) != 1 else ''}."]
        if problems:
            msg_parts.append(
                f"Skipped {len(problems)} ({'; '.join(problems[:3])})."
            )
        return FileOpResult(bool(deleted), " ".join(msg_parts),
                            payload=deleted)

    @staticmethod
    def _build_delete_question(target: Path) -> str:
        """Compose a confirmation prompt that always exposes bulk scope."""
        if target.is_dir():
            file_count, dir_count = _count_tree(target)
            total = file_count + dir_count
            if total == 0:
                return (
                    f"Permanently delete the empty folder '{target.name}'? "
                    "This cannot be undone."
                )
            scope = (
                f"{file_count} file{'s' if file_count != 1 else ''}"
            )
            if dir_count:
                scope += (
                    f" and {dir_count} subfolder"
                    f"{'s' if dir_count != 1 else ''}"
                )
            tag = " (BULK)" if total >= 5 else ""
            return (
                f"Permanently delete the folder '{target.name}'{tag}? "
                f"It contains {scope}. This cannot be undone."
            )
        return (
            f"Permanently delete the file '{target.name}'? "
            "This cannot be undone."
        )

    def open_cmd(self, raw_dir: str) -> FileOpResult:
        """Spawn a CMD window in ``raw_dir`` (no command injection — pure argv)."""
        path = normalise_path(raw_dir)
        if not path.exists() or not path.is_dir():
            return FileOpResult(False, f"That folder doesn't exist: {path}")
        if not sys.platform.startswith("win"):
            return FileOpResult(False, "Opening CMD is only supported on Windows.")
        try:
            subprocess.Popen(
                ["cmd.exe", "/K", "cd", "/d", str(path)],
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
            return FileOpResult(True, f"CMD opened in {path.name or path}.")
        except OSError as exc:
            return FileOpResult(False, f"Could not open CMD: {exc}")


