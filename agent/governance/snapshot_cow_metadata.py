"""Same-user read-only native metadata worker. No governance/runtime imports."""
from __future__ import annotations
import ctypes
import errno
import json
import os
import stat
import sys
from pathlib import Path

MAX_BYTES = 1024 * 1024
REFUSALS = frozenset({"symlink_component", "path_missing", "unexpected_file_type",
                     "hardlink_refused", "apfs_platform_unsupported",
                     "cow_metadata_or_clone_facility_unsupported",
                     "cow_xattr_facility_unsupported", "cow_xattr_list_drift",
                     "cow_xattr_value_drift", "cow_metadata_output_oversize"})

def _path(path: Path, *, exists: bool = True) -> Path:
    path = path.absolute()
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("symlink_component")
    if exists and not path.exists():
        raise ValueError("path_missing")
    return path

def _call(name: str, restype, argtypes, *args: Any):
    if sys.platform != "darwin":
        raise ValueError("apfs_platform_unsupported")
    library = ctypes.CDLL(None, use_errno=True)
    try:
        function = getattr(library, name)
    except AttributeError as exc:
        raise ValueError("cow_metadata_or_clone_facility_unsupported") from exc
    function.restype, function.argtypes = restype, argtypes
    ctypes.set_errno(0)
    result = function(*args)
    if result == -1 or (restype == ctypes.c_void_p and not result):
        error = ctypes.get_errno()
        raise OSError(error, name + ": " + os.strerror(error))
    return result

def _acl(path: Path, *, max_bytes=MAX_BYTES) -> str:
    try:
        pointer = _call("acl_get_link_np", ctypes.c_void_p, [ctypes.c_char_p, ctypes.c_int],
                        os.fsencode(path), 0x100)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return ""
        raise
    try:
        length = ctypes.c_ssize_t()
        text = _call("acl_to_text", ctypes.c_void_p,
                     [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)], pointer, ctypes.byref(length))
        try:
            if not 0 <= length.value <= max_bytes:
                raise ValueError("cow_metadata_output_oversize")
            return ctypes.string_at(text, length.value).decode()
        finally:
            _call("acl_free", ctypes.c_int, [ctypes.c_void_p], text)
    finally:
        _call("acl_free", ctypes.c_int, [ctypes.c_void_p], pointer)

def _xattrs(path: Path, *, max_bytes=MAX_BYTES) -> dict[str, str]:
    result = {}
    total = 2
    def account(name, size):
        nonlocal total
        total += len(json.dumps(name).encode()) + size * 2 + 5
        if total > max_bytes:
            raise ValueError("cow_metadata_output_oversize")
    if sys.platform != "darwin":
        if not hasattr(os, "listxattr"):
            raise ValueError("cow_xattr_facility_unsupported")
        for key in os.listxattr(path, follow_symlinks=False):
            raw = os.getxattr(path, key, follow_symlinks=False)
            account(key, len(raw))
            result[key] = raw.hex()
        return result
    args = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    size = _call("listxattr", ctypes.c_ssize_t, args, os.fsencode(path), None, 0, 0x41)
    if size > max_bytes:
        raise ValueError("cow_metadata_output_oversize")
    if not size:
        return {}
    buffer = ctypes.create_string_buffer(size)
    if _call("listxattr", ctypes.c_ssize_t, args, os.fsencode(path), buffer, size, 0x41) != size:
        raise ValueError("cow_xattr_list_drift")
    get_args = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                ctypes.c_uint32, ctypes.c_int]
    for name in buffer.raw.rstrip(b"\0").split(b"\0"):
        key = os.fsdecode(name)
        size = _call("getxattr", ctypes.c_ssize_t, get_args, os.fsencode(path), name, None, 0, 0, 0x41)
        account(key, size)  # Refuse the aggregate before allocating/hex-copying this value.
        value = ctypes.create_string_buffer(max(1, size))
        if _call("getxattr", ctypes.c_ssize_t, get_args, os.fsencode(path), name, value, size, 0, 0x41) != size:
            raise ValueError("cow_xattr_value_drift")
        result[key] = value.raw[:size].hex()
    return result


def _metadata(path: Path, *, directory: bool = False) -> dict:
    _path(path)
    info = path.lstat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError("unexpected_file_type")
    if not directory and info.st_nlink != 1:
        raise ValueError("hardlink_refused")
    # POSIX identity can be previewed portably; unsupported apply is refused
    # before any backup/journal effect, never by substituting a copy for clone.
    result = {"dev": info.st_dev, "ino": info.st_ino, "size": info.st_size,
            "allocated_bytes": info.st_blocks * 512, "nlink": info.st_nlink,
            "birthtime": getattr(info, "st_birthtime", None), "ctime_ns": info.st_ctime_ns,
            "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid,
            "mtime_ns": info.st_mtime_ns, "flags": getattr(info, "st_flags", 0)}
    remaining = MAX_BYTES - len(json.dumps(result).encode()) - 32
    result["xattrs"] = _xattrs(path, max_bytes=remaining)
    remaining -= len(json.dumps(result["xattrs"]).encode())
    result["acl"] = _acl(path, max_bytes=remaining) if sys.platform == "darwin" else None
    if len(json.dumps(result).encode()) > MAX_BYTES:
        raise ValueError("cow_metadata_output_oversize")
    return result

def main():
    if (len(sys.argv) != 3 or sys.argv[2] not in ("0", "1")
            or len(os.fsencode(sys.argv[1])) > 8192):
        return 2
    try:
        result = _metadata(Path(sys.argv[1]), directory=sys.argv[2] == "1")
        raw = json.dumps(result, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > MAX_BYTES:
            return 3
        sys.stdout.buffer.write(raw)
        return 0
    except (OSError, ValueError, UnicodeError) as exc:
        if isinstance(exc, OSError) and type(exc.errno) is int and 0 < exc.errno < 4096:
            error = {"type": "oserror", "errno": exc.errno, "reason": "os_metadata_error"}
        else:
            reason = str(exc) if str(exc) in REFUSALS else "cow_metadata_worker_failed"
            error = {"type": "refusal", "errno": None, "reason": reason}
        # Fixed, typed diagnostic only; no raw traceback/path/body or empty proof.
        sys.stdout.buffer.write(json.dumps({"error": error}, separators=(",", ":")).encode())
        return 4


if __name__ == "__main__":
    sys.exit(main())
