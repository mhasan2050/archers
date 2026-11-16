import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from archers.domain import DomainError
from archers.parsing import parse_isolated


def test_real_isolated_parser(client: TestClient, tmp_path: Path) -> None:
    source = tmp_path / "synthetic.txt"
    source.write_text("Synthetic company policy provides twenty annual leave days.")
    document = parse_isolated(source, "text", client.app.state.settings)
    assert len(document.blocks) == 1
    assert document.blocks[0].text == source.read_text()
    assert not document.warnings


def test_invalid_file_is_not_successful_parse(client: TestClient, tmp_path: Path) -> None:
    source = tmp_path / "invalid.pdf"
    source.write_bytes(b"%PDF-1.7\nnot a real PDF")
    with pytest.raises(DomainError) as error:
        parse_isolated(source, "pdf", client.app.state.settings)
    assert error.value.code == "VALIDATION_ERROR"


def test_sandbox_denies_network_and_unrelated_files(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    if sys.platform != "darwin":
        pytest.skip("This launcher-policy test is macOS-specific; Linux installs policy in child.")
    source, unrelated = tmp_path / "source.txt", tmp_path / "unrelated.txt"
    source.write_text("Synthetic input.")
    unrelated.write_text("Synthetic unrelated data must be inaccessible.")
    run = subprocess.run

    def probe(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        marker = command.index("-m")
        output = command[-3]
        script = f"""
import json, socket
from pathlib import Path
denied = 0
try:
    with socket.socket() as connection:
        connection.settimeout(1)
        connection.connect(("127.0.0.1", 9))
except PermissionError:
    denied += 1
try:
    Path({json.dumps(str(unrelated.resolve()))}).read_text()
except PermissionError:
    denied += 1
try:
    Path({json.dumps(str(source.resolve()))}).write_text("Must not overwrite original.")
except PermissionError:
    denied += 1
assert denied == 3, denied
document = {{"format": "text", "blocks": [], "warnings": []}}
Path({json.dumps(output)}, "result.json").write_text(json.dumps({{"document": document}}))
"""
        kwargs["stderr"] = subprocess.PIPE
        result = run([*command[:marker], "-c", script], **kwargs)
        assert result.returncode == 0, result.stderr.decode()
        return result

    monkeypatch.setattr(subprocess, "run", probe)
    document = parse_isolated(source, "text", client.app.state.settings)
    assert not document.blocks
