from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import secrets
import stat
from pathlib import Path

_PRIVATE_FILE_MODE = 0o600
_PRIVATE_DIR_MODE = 0o700


class StaleWriteError(RuntimeError):
    ...


def _open_real_directory(directory: Path, *, create: bool) -> tuple[int, Path]:
    directory = Path(os.path.abspath(directory.expanduser()))
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    current_fd = os.open(directory.anchor, flags)
    try:
        for part in directory.parts[1:]:
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, _PRIVATE_DIR_MODE, dir_fd=current_fd)
                except FileExistsError:
                    pass
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except OSError as exc:
                raise OSError(
                    f"refusing non-directory path component: {part}"
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
        return current_fd, directory
    except BaseException:
        os.close(current_fd)
        raise


def ensure_real_directory(directory: Path) -> Path:
    fd, normalized = _open_real_directory(directory, create=True)
    os.close(fd)
    return normalized


def confined_path(root: Path, relative: str | Path) -> Path:
    root = Path(root).resolve()
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("path escapes configured root")

    candidate = root
    for part in rel.parts:
        if part in {"", "."}:
            continue
        candidate /= part
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("symlinks are not allowed in confined paths")

    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes configured root") from exc
    return candidate


def _target_mode_at(parent_fd: int, name: str, requested: int | None) -> int:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return requested if requested is not None else _PRIVATE_FILE_MODE
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise OSError(f"refusing to replace non-regular file: {name}")
    return stat.S_IMODE(info.st_mode)


def _new_staged_name(target_name: str) -> str:
    return f".{target_name}.{secrets.token_hex(12)}.tmp"


def _sync_directory_fd(directory_fd: int) -> None:
    try:
        os.fsync(directory_fd)
    except OSError as exc:
        unsupported = {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL)}
        if exc.errno not in unsupported:
            raise


def discard_staged(temp_path: Path) -> None:
    temp_path = Path(temp_path).expanduser()
    try:
        parent_fd, _ = _open_real_directory(temp_path.parent, create=False)
    except OSError:
        return
    try:
        try:
            os.unlink(temp_path.name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
    finally:
        os.close(parent_fd)


def stage_bytes(
    path: Path,
    data: bytes,
    *,
    mode: int | None = None,
    preserve_existing_mode: bool = True,
) -> Path:
    path = Path(path).expanduser()
    parent_fd, parent = _open_real_directory(path.parent, create=True)
    path = parent / path.name
    try:
        target_mode = (
            _target_mode_at(parent_fd, path.name, mode)
            if preserve_existing_mode
            else (mode if mode is not None else _PRIVATE_FILE_MODE)
        )
    except BaseException:
        os.close(parent_fd)
        raise
    temp_name = ""
    fd: int | None = None
    for _ in range(100):
        temp_name = _new_staged_name(path.name)
        try:
            fd = os.open(
                temp_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                target_mode,
                dir_fd=parent_fd,
            )
            break
        except FileExistsError:
            continue
        except BaseException:
            os.close(parent_fd)
            raise
    if fd is None:
        os.close(parent_fd)
        raise FileExistsError("could not allocate a unique staged filename")

    temp_path = parent / temp_name
    open_fd = True
    try:
        os.fchmod(fd, target_mode)
        with os.fdopen(fd, "wb") as handle:
            open_fd = False
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return temp_path
    except BaseException:
        if open_fd:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(temp_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(parent_fd)


def stage_text(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
    preserve_existing_mode: bool = True,
) -> Path:
    return stage_bytes(
        path,
        text.encode(encoding),
        mode=mode,
        preserve_existing_mode=preserve_existing_mode,
    )


def _commit_staged(temp_path: Path, path: Path, *, exclusive: bool) -> None:
    temp_path = Path(temp_path).expanduser()
    path = Path(path).expanduser()
    parent_fd, parent = _open_real_directory(path.parent, create=True)
    path = parent / path.name
    staged_parent = Path(os.path.abspath(temp_path.parent))
    try:
        if staged_parent != parent:
            raise OSError("staged file must be in the destination directory")
        info = os.stat(temp_path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
        ):
            raise OSError(f"invalid staged file: {temp_path}")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise PermissionError(f"staged file is not owned by this user: {temp_path}")
        if exclusive:
            os.link(
                temp_path.name,
                path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            os.unlink(temp_path.name, dir_fd=parent_fd)
        else:
            _target_mode_at(parent_fd, path.name, None)
            os.replace(
                temp_path.name,
                path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        _sync_directory_fd(parent_fd)
    finally:
        os.close(parent_fd)


def commit_staged(temp_path: Path, path: Path) -> None:
    _commit_staged(temp_path, path, exclusive=False)


def commit_staged_exclusive(temp_path: Path, path: Path) -> None:
    _commit_staged(temp_path, path, exclusive=True)


def _open_regular_file(path: Path) -> int:
    path = Path(path).expanduser()
    parent_fd, _ = _open_real_directory(path.parent, create=False)
    try:
        fd = os.open(
            path.name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
        try:
            info = os.fstat(fd)
        except BaseException:
            os.close(fd)
            raise
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            os.close(fd)
            raise OSError(f"refusing to read non-regular file: {path.name}")
        return fd
    finally:
        os.close(parent_fd)


def read_regular_text(path: Path, *, encoding: str = "utf-8") -> str:
    with os.fdopen(_open_regular_file(path), "r", encoding=encoding) as handle:
        return handle.read()


def read_regular_bytes(path: Path) -> bytes:
    with os.fdopen(_open_regular_file(path), "rb") as handle:
        return handle.read()


def regular_file_sha256(path: Path) -> str | None:
    """Hash a regular file, returning ``None`` only when it is absent."""
    try:
        fd = _open_regular_file(path)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def commit_staged_if_sha256(
    temp_path: Path, path: Path, expected_sha256: str | None
) -> None:
    try:
        lock_fd = _open_regular_file(path) if expected_sha256 is not None else None
    except FileNotFoundError as exc:
        raise StaleWriteError("destination changed after approval") from exc
    try:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise StaleWriteError("destination is busy") from exc
        if regular_file_sha256(path) != expected_sha256:
            raise StaleWriteError("destination changed after approval")
        if expected_sha256 is None:
            commit_staged_exclusive(temp_path, path)
        else:
            commit_staged(temp_path, path)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    mode: int | None = None,
    preserve_existing_mode: bool = True,
) -> None:
    temp_path = stage_bytes(
        path,
        data,
        mode=mode,
        preserve_existing_mode=preserve_existing_mode,
    )
    try:
        commit_staged(temp_path, path)
    finally:
        discard_staged(temp_path)


def atomic_write_text(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
    preserve_existing_mode: bool = True,
) -> None:
    atomic_write_bytes(
        path,
        text.encode(encoding),
        mode=mode,
        preserve_existing_mode=preserve_existing_mode,
    )


def atomic_write_text_if_sha256(
    path: Path,
    text: str,
    expected_sha256: str | None,
    *,
    encoding: str = "utf-8",
) -> None:
    temp_path = stage_text(path, text, encoding=encoding)
    try:
        commit_staged_if_sha256(temp_path, path, expected_sha256)
    finally:
        discard_staged(temp_path)


def atomic_create_text(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> None:
    temp_path = stage_text(
        path,
        text,
        encoding=encoding,
        mode=mode,
        preserve_existing_mode=False,
    )
    try:
        commit_staged_exclusive(temp_path, path)
    finally:
        discard_staged(temp_path)
