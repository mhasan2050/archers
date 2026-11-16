"""Private parser child entry point; no database or model credentials."""

import ctypes
import errno
import json
import os
import resource
import sys
from dataclasses import asdict
from pathlib import Path

from archers.ingestion.normalization import NormalizationError, ParseLimits, normalize


def linux_sandbox(output: Path, source: Path) -> None:
    """Unprivileged Landlock filesystem policy plus seccomp network/process deny."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise OSError("no_new_privs unavailable")
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 1:
        raise OSError("Landlock unavailable; parsing is disabled")
    handled = (1 << 13) - 1
    if abi >= 2:
        handled |= 1 << 13
    if abi >= 3:
        handled |= 1 << 14
    if abi >= 5:
        handled |= 1 << 15
    ruleset_attr = ctypes.c_uint64(handled)
    ruleset = libc.syscall(444, ctypes.byref(ruleset_attr), ctypes.sizeof(ruleset_attr), 0)
    if ruleset < 0:
        raise OSError("Landlock ruleset unavailable")

    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

    read_access = (1 << 2) | (1 << 3)
    try:
        roots = {Path(sys.prefix), Path(sys.base_prefix), Path(__file__).parent}
        roots.update(Path(p) for p in ("/usr/lib", "/lib", "/etc/ld.so.cache") if Path(p).exists())
        permissions = [(p, read_access) for p in roots]
        permissions += [(source, 1 << 2), (output, handled)]
        for path, permission in permissions:
            if path.is_file():
                permission &= ~(1 << 3)
            descriptor = os.open(path, 0x200000 | os.O_CLOEXEC)
            try:
                rule = PathRule(permission, descriptor)
                if libc.syscall(445, ruleset, 1, ctypes.byref(rule), 0) != 0:
                    raise OSError("Landlock path rule unavailable")
            finally:
                os.close(descriptor)
        if libc.syscall(446, ruleset, 0) != 0:
            raise OSError("Landlock restriction unavailable")
    finally:
        os.close(ruleset)
    seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_rule_add.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint,
    ]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    context = seccomp.seccomp_init(0x7FFF0000)
    if not context:
        raise OSError("seccomp unavailable")
    try:
        for name in (
            b"socket", b"connect", b"bind", b"listen", b"accept", b"accept4",
            b"execve", b"execveat", b"fork", b"vfork", b"clone", b"clone3",
            b"ptrace", b"mount", b"unshare", b"setns",
        ):
            number = seccomp.seccomp_syscall_resolve_name(name)
            if number >= 0 and seccomp.seccomp_rule_add(
                context, 0x00050000 | errno.EPERM, number, 0
            ) != 0:
                raise OSError("seccomp rule unavailable")
        if seccomp.seccomp_load(context) != 0:
            raise OSError("seccomp restriction unavailable")
    finally:
        seccomp.seccomp_release(context)


def main() -> None:
    source, format_key, directory, maximum, timeout = sys.argv[1:]
    output = Path(directory)
    resource.setrlimit(resource.RLIMIT_CPU, (int(timeout), int(timeout)))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))
        linux_sandbox(output, Path(source))
    elif sys.platform != "darwin" or os.environ.get("ARCHERS_PARSER_SANDBOX") != "macos":
        raise RuntimeError("Supported parser sandbox is required")
    try:
        document = normalize(Path(source), format_key, ParseLimits(max_file_bytes=int(maximum)))
        result: dict[str, object] = {"document": asdict(document)}
    except NormalizationError as exc:
        result = {"error": exc.code}
    (output / "result.json").write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    main()
