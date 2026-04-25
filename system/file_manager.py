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
import shutil
import string
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional

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
        """Recursively search for files whose name contains ``pattern``."""
        pattern = (pattern or "").strip().lower()
        if not pattern:
            return FileOpResult(False, "Please tell me what to search for.")

        search_roots: List[Path] = []
        if roots:
            for r in roots:
                try:
                    search_roots.append(normalise_path(r))
                except FileSafetyError:
                    continue
        if not search_roots:
            search_roots = [user_home()]

        matches: List[Path] = []
        for root in search_roots:
            if not root.exists() or not root.is_dir():
                continue
            for current, dirs, files in os.walk(root, followlinks=False):
                # Skip protected roots so we don't waste time crawling Windows/.
                cur_path = Path(current)
                if any(_is_inside(cur_path, prot) or cur_path == prot
                       for prot in PROTECTED_WRITE_ROOTS):
                    dirs.clear()
                    continue
                for name in files:
                    if pattern in name.lower():
                        matches.append(cur_path / name)
                        if len(matches) >= max_results:
                            break
                if len(matches) >= max_results:
                    break
            if len(matches) >= max_results:
                break

        if not matches:
            return FileOpResult(False, f"No files matched '{pattern}'.")
        names = ", ".join(p.name for p in matches[:5])
        msg = (
            f"Found {len(matches)} match{'es' if len(matches) != 1 else ''}. "
            f"Top results: {names}."
        )
        return FileOpResult(True, msg, payload=matches)

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
        if not self._confirm(
            f"Permanently delete {target}? This cannot be undone."
        ):
            return FileOpResult(False, "Delete cancelled.")
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            return FileOpResult(True, f"{target.name} deleted.")
        except OSError as exc:
            return FileOpResult(False, f"Could not delete: {exc}")

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


# Convenience helper: list the bare drive letters available on Windows.
def list_drives() -> List[str]:
    if not sys.platform.startswith("win"):
        return []
    return [f"{letter}:\\" for letter in string.ascii_uppercase
            if Path(f"{letter}:\\").exists()]
