"""Filesystem boundary checks for explicitly configured source paths."""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .errors import ConfigurationError, SnapshotError


def validate_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError("source path must be a non-empty string")
    if "\\" in value:
        raise ConfigurationError(f"source path must use POSIX separators: {value!r}")
    pure = PurePosixPath(value)
    if pure.is_absolute() or value.startswith("/"):
        raise ConfigurationError(f"absolute source paths are forbidden: {value!r}")
    if any(part in {"", ".", ".."} for part in pure.parts):
        raise ConfigurationError(f"unsafe source path: {value!r}")
    return pure.as_posix()


@contextmanager
def open_absolute_directory_no_symlinks(path: Path):
    """Open an existing absolute directory without traversing any symlink.

    ``O_NOFOLLOW`` protects only the final component when it is used with a
    pathname.  Walking from the filesystem root with held directory
    descriptors extends that protection to every ancestor.  This is used for
    authoritative output files whose parent may be substituted concurrently.
    """

    lexical = Path(os.path.abspath(os.fspath(path)))
    if not lexical.is_absolute():  # Defensive: ``abspath`` should guarantee it.
        raise SnapshotError(f"directory path must be absolute: {path}")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    descriptors: list[int] = []
    try:
        try:
            descriptor = os.open(lexical.anchor, flags)
        except OSError as exc:
            raise SnapshotError(
                f"directory root is unavailable or unsafe: {lexical.anchor}"
            ) from exc
        descriptors.append(descriptor)
        anchor_parts = Path(lexical.anchor).parts
        for component in lexical.parts[len(anchor_parts) :]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except OSError as exc:
                raise SnapshotError(
                    f"directory ancestor is unavailable or unsafe: {lexical}"
                ) from exc
            descriptors.append(child)
            descriptor = child
        yield descriptor
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@contextmanager
def open_directory_under_root(
    root: Path, relative_path: str, *, create: bool = False
):
    """Open a project-relative directory by walking held no-follow dirfds."""

    relative_path = validate_relative_path(relative_path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    descriptors: list[int] = []
    try:
        try:
            descriptor = os.open(root, flags)
        except OSError as exc:
            raise SnapshotError(f"directory root is unavailable or unsafe: {root}") from exc
        descriptors.append(descriptor)
        for component in PurePosixPath(relative_path).parts:
            if create:
                try:
                    os.mkdir(component, 0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise SnapshotError(
                        f"cannot create safe output directory component: {component}"
                    ) from exc
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except OSError as exc:
                raise SnapshotError(
                    f"directory component is unavailable or unsafe: {relative_path}"
                ) from exc
            descriptors.append(child)
            descriptor = child
        yield descriptor
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def open_child_directory(
    parent_descriptor: int, name: str, *, create: bool = False
) -> int:
    """Open one validated child directory relative to a held parent dirfd."""

    if not name or "/" in name or name in {".", ".."}:
        raise SnapshotError(f"unsafe directory name: {name!r}")
    if create:
        try:
            os.mkdir(name, 0o755, dir_fd=parent_descriptor)
        except FileExistsError:
            pass
        except OSError as exc:
            raise SnapshotError(f"cannot create safe output directory: {name}") from exc
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise SnapshotError(f"directory is unavailable or unsafe: {name}") from exc


def resolve_regular_file_under_root(root: Path, relative_path: str) -> Path:
    """Resolve an allowlisted regular file while rejecting every symlink."""

    relative_path = validate_relative_path(relative_path)
    if root.is_symlink():
        raise SnapshotError(f"source root must not be a symlink: {root}")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise SnapshotError(f"source root is unavailable: {root}") from exc
    if not resolved_root.is_dir():
        raise SnapshotError(f"source root is not a directory: {root}")

    current = resolved_root
    for part in PurePosixPath(relative_path).parts:
        current = current / part
        try:
            file_stat = os.lstat(current)
        except FileNotFoundError:
            raise
        if stat.S_ISLNK(file_stat.st_mode):
            raise SnapshotError(f"symlinks are forbidden in source paths: {current}")

    try:
        resolved_candidate = current.resolve(strict=True)
    except OSError as exc:
        raise SnapshotError(f"source is unavailable: {relative_path}") from exc
    try:
        common = Path(os.path.commonpath((resolved_root, resolved_candidate)))
    except ValueError as exc:
        raise SnapshotError(f"source path escapes root: {relative_path}") from exc
    if common != resolved_root:
        raise SnapshotError(f"source path escapes root: {relative_path}")
    if not resolved_candidate.is_file():
        raise SnapshotError(f"source is not a regular file: {relative_path}")
    return resolved_candidate


@contextmanager
def open_regular_file_under_root(root: Path, relative_path: str):
    """Open a source through directory descriptors without following symlinks.

    Holding each parent directory descriptor closes the lstat-to-open race that
    would otherwise allow a configured component to be swapped after checking.
    """

    relative_path = validate_relative_path(relative_path)
    parts = PurePosixPath(relative_path).parts
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    opened_directories: list[int] = []
    file_descriptor: int | None = None
    try:
        try:
            current_descriptor = os.open(root, directory_flags)
        except OSError as exc:
            raise SnapshotError(f"source root is unavailable or unsafe: {root}") from exc
        opened_directories.append(current_descriptor)
        for component in parts[:-1]:
            try:
                current_descriptor = os.open(
                    component,
                    directory_flags,
                    dir_fd=current_descriptor,
                )
            except FileNotFoundError:
                raise
            except OSError as exc:
                raise SnapshotError(
                    f"source parent is unavailable or a symlink: {relative_path}"
                ) from exc
            opened_directories.append(current_descriptor)
        try:
            before_open = os.stat(
                parts[-1], dir_fd=current_descriptor, follow_symlinks=False
            )
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise SnapshotError(
                f"source is unavailable or unsafe: {relative_path}"
            ) from exc
        if not stat.S_ISREG(before_open.st_mode):
            raise SnapshotError(f"source is not a regular file: {relative_path}")
        try:
            file_descriptor = os.open(
                parts[-1],
                os.O_RDONLY | os.O_NONBLOCK | nofollow,
                dir_fd=current_descriptor,
            )
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise SnapshotError(
                f"source is unavailable or a symlink: {relative_path}"
            ) from exc
        file_stat = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or (
            file_stat.st_dev,
            file_stat.st_ino,
        ) != (before_open.st_dev, before_open.st_ino):
            raise SnapshotError(f"source is not a regular file: {relative_path}")
        yield file_descriptor
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        for descriptor in reversed(opened_directories):
            os.close(descriptor)


def resolve_private_path_under_root(root: Path, relative_path: str) -> Path:
    """Resolve a generated/private path, rejecting traversal and symlinks."""

    relative_path = validate_relative_path(relative_path)
    resolved_root = root.resolve(strict=True)
    candidate = resolved_root.joinpath(*PurePosixPath(relative_path).parts)
    current = resolved_root
    for part in PurePosixPath(relative_path).parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise SnapshotError(f"symlinks are forbidden in snapshot paths: {current}")
    resolved_candidate = candidate.resolve(strict=True)
    if Path(os.path.commonpath((resolved_root, resolved_candidate))) != resolved_root:
        raise SnapshotError(f"snapshot path escapes root: {relative_path}")
    if not resolved_candidate.is_file():
        raise SnapshotError(f"snapshot entry is not a regular file: {relative_path}")
    return resolved_candidate


def ensure_directory_under_root(root: Path, relative_path: str) -> Path:
    """Create a private directory tree without traversing symlink components."""

    relative_path = validate_relative_path(relative_path)
    if root.is_symlink():
        raise SnapshotError(f"output root must not be a symlink: {root}")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise SnapshotError(f"output root is unavailable: {root}") from exc
    if not resolved_root.is_dir():
        raise SnapshotError(f"output root is not a directory: {root}")
    current = resolved_root
    for part in PurePosixPath(relative_path).parts:
        candidate = current / part
        try:
            file_stat = os.lstat(candidate)
        except FileNotFoundError:
            try:
                os.mkdir(candidate, 0o755)
            except OSError as exc:
                raise SnapshotError(f"cannot create safe output directory: {candidate}") from exc
            file_stat = os.lstat(candidate)
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISDIR(file_stat.st_mode):
            raise SnapshotError(f"output path component is unsafe: {candidate}")
        current = candidate
    if Path(os.path.commonpath((resolved_root, current.resolve(strict=True)))) != resolved_root:
        raise SnapshotError(f"output directory escapes root: {relative_path}")
    return current
