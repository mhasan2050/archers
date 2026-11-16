import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from archers.config import Settings
from archers.domain import DomainError
from archers.ingestion.normalization import NormalizedDocument


def parse_isolated(source: Path, format_key: str, settings: Settings) -> NormalizedDocument:
    with tempfile.TemporaryDirectory(prefix="archers-parser-") as directory:
        output = Path(directory).resolve()
        command = [
            sys.executable, "-m", "archers.parser_process", str(source.resolve()), format_key,
            str(output), str(settings.max_upload_bytes), str(settings.parser_timeout_seconds),
        ]
        environment = {
            "PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1", "TMPDIR": str(output), "HOME": str(output),
            "LANG": "C.UTF-8",
            "PYTHONPATH": str(Path(__file__).parent.parent),
        }
        if sys.platform == "darwin":
            # sandbox-exec is used only for local development; Linux uses Landlock/seccomp.
            readable = {
                str(Path(sys.prefix).resolve()), str(Path(sys.base_prefix).resolve()),
                str(Path(__file__).parent.parent.resolve()), str(source.resolve()),
                "/System", "/usr/lib", "/usr/share", "/dev/null", "/dev/urandom",
                "/Library", "/private/var/db/dyld", "/private/etc", "/private/preboot",
            }
            if Path("/opt/homebrew").exists():
                readable.add("/opt/homebrew")
            rules = " ".join(f"(subpath {json.dumps(path)})" for path in readable)
            profile = (
                "(version 1)(deny default)(allow process-exec)(allow process-fork)"
                "(allow sysctl-read)(allow mach-lookup)(allow file-read-metadata)"
                f'(allow file-read* (literal "/") {rules} (subpath {json.dumps(str(output))}))'
                f"(allow file-write* (subpath {json.dumps(str(output))}))"
            )
            command = ["/usr/bin/sandbox-exec", "-p", profile, *command]
            environment["ARCHERS_PARSER_SANDBOX"] = "macos"
        elif sys.platform != "linux":
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Parser sandbox unavailable.", 503)
        try:
            result = subprocess.run(
                command, env=environment, cwd=output, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=settings.parser_timeout_seconds + 5, check=False,
            )
            path = output / "result.json"
            if result.returncode != 0 or not path.exists() or path.stat().st_size > 64 * 1024 * 1024:
                raise DomainError("DEPENDENCY_UNAVAILABLE", "Isolated parser failed.", 503)
            payload = json.loads(path.read_text())
            if "error" in payload:
                code = {
                    "ENCRYPTED_DOCUMENT": "PDF_ENCRYPTED",
                    "PARSER_UNAVAILABLE": "DEPENDENCY_UNAVAILABLE",
                }.get(payload["error"], "VALIDATION_ERROR")
                raise DomainError(code, "Document could not be normalized.", 422)
            return TypeAdapter(NormalizedDocument).validate_python(payload["document"])
        except subprocess.TimeoutExpired as exc:
            raise DomainError("DEADLINE_EXCEEDED", "Parser deadline exceeded.", 503) from exc
        except (OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Parser result unavailable.", 503) from exc
