import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import numpy as np
import pytest
from tokenizers import Tokenizer, models, pre_tokenizers, processors

from archers.domain import DomainError
from archers.indexing import Chunk, LocalEncoder, VectorIndex, download_model
from archers.ingestion.normalization import NormalizedBlock, NormalizedDocument, NormalizedLocation


@pytest.fixture
def encoder() -> LocalEncoder:
    model = LocalEncoder.__new__(LocalEncoder)
    vocabulary = {"[UNK]": 0, "[CLS]": 1, "[SEP]": 2}
    vocabulary.update({f"word{i}": i + 3 for i in range(1500)})
    tokenizer = Tokenizer(models.WordLevel(vocabulary, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.post_processor = processors.TemplateProcessing(
        single="[CLS] $A [SEP]", special_tokens=[("[CLS]", 1), ("[SEP]", 2)]
    )
    model.tokenizer = tokenizer
    return model


def test_chunks_preserve_all_characters_and_token_limits(encoder: LocalEncoder) -> None:
    text = "  " + " \n".join(f"word{i}" for i in range(1200)) + "  "
    document = NormalizedDocument("text", (
        NormalizedBlock(text, NormalizedLocation("line", line_start=1, line_end=1200)),
    ), ())
    chunks = encoder.chunk(document)
    assert len(chunks) > 1
    covered = set()
    last_end = 0
    for ordinal, chunk in enumerate(chunks):
        assert chunk.ordinal == ordinal
        assert chunk.token_count == len(encoder.tokenizer.encode(chunk.text).ids)
        assert chunk.token_count <= 256
        start = text.index(chunk.text)
        assert start <= last_end
        covered.update(range(start, start + len(chunk.text)))
        last_end = start + len(chunk.text)
        assert chunk.locations[0]["kind"] == "lines"
        assert chunk.locations[0]["line_end"] == 1200
    assert covered == set(range(len(text)))


def test_sparse_is_deterministic_sorted_and_shared_between_query_and_document(
    encoder: LocalEncoder,
) -> None:
    first, repeated, query = encoder.sparse(["Vacation vacation leave", "Vacation vacation leave", "vacation"])
    assert first == repeated
    assert first[0] == sorted(set(first[0]))
    assert query[0][0] in first[0]
    assert max(first[1]) > 1
    assert encoder.sparse([""])[0] == ([], [])


def test_chunk_keeps_original_case_unicode_and_locations(encoder: LocalEncoder) -> None:
    text = "Équipe POLICIES: annual leave.\n原文 retained."
    document = NormalizedDocument("pdf", (
        NormalizedBlock(text, NormalizedLocation("page", page=7)),
    ), ())
    chunk = encoder.chunk(document)[0]
    assert chunk.text == text
    assert chunk.locations[0]["page"] == 7
    assert chunk.locations[0]["kind"] == "pdf_page"


def test_no_automatic_model_download_and_no_truncation(encoder: LocalEncoder, tmp_path: Path) -> None:
    with pytest.raises(OSError):
        LocalEncoder(tmp_path)
    assert not list(tmp_path.iterdir())
    with pytest.raises(ValueError, match="truncation"):
        encoder.encode([" ".join(["word1"] * 300)])


def test_explicit_download_rejects_modified_existing_asset(tmp_path: Path) -> None:
    (tmp_path / "onnx").mkdir()
    (tmp_path / "onnx/model.onnx").write_bytes(b"synthetic tampered asset")
    with pytest.raises(ValueError, match="checksum"):
        download_model(tmp_path)


def test_qdrant_stage_checks_vectors_payloads_and_exact_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, object] = {"points": {}}
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        calls.append((request.method, request.url.path, body))
        if request.url.path.endswith("/index"):
            result = {"status": "completed"}
        elif request.method == "GET":
            result = {"config": {"params": {
                "vectors": {"dense": {"size": 2, "distance": "Cosine"}},
                "sparse_vectors": {"sparse": {}},
            }}}
        elif request.url.path.endswith("/count"):
            result = {"count": len(state["points"])}
        elif request.method == "PUT":
            state["points"] = {p["id"]: p for p in body["points"]}
            result = {"status": "completed"}
        else:
            result = list(state["points"].values())
            if state.get("corrupt"):
                result[0]["payload"]["generation_id"] = str(uuid4())
        return httpx.Response(200, json={"result": result})

    client = httpx.Client(base_url="http://qdrant", transport=httpx.MockTransport(handle))
    monkeypatch.setattr("archers.indexing.httpx.Client", lambda **kwargs: client)
    index = VectorIndex("http://qdrant", "synthetic", 2)
    tenant, document, version, generation = (uuid4() for _ in range(4))
    chunks = [Chunk("DO NOT PUT THIS TEXT IN QDRANT", 0, 10, [])]
    index.stage(tenant, document, version, generation, chunks, [[0.6, 0.8]], [([1], [1.0])])
    assert "DO NOT PUT" not in json.dumps(calls)
    assert str(tenant) in json.dumps(calls)
    state["corrupt"] = True
    with pytest.raises(DomainError, match="verification"):
        index.stage(tenant, document, version, generation, chunks, [[0.6, 0.8]], [([1], [1.0])])
    for bad in ([[float("nan"), 1.0]], [[0.0, 0.0]], [[1.0]]):
        with pytest.raises(ValueError):
            index.stage(tenant, document, version, generation, chunks, bad, [([1], [1.0])])
    index.close()


def test_qdrant_outage_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    client = httpx.Client(base_url="http://qdrant", transport=httpx.MockTransport(
        lambda request: httpx.Response(503, json={"status": "unavailable"})
    ))
    monkeypatch.setattr("archers.indexing.httpx.Client", lambda **kwargs: client)
    with pytest.raises(DomainError) as error:
        VectorIndex("http://qdrant", "synthetic", 384)
    assert error.value.code == "DEPENDENCY_UNAVAILABLE"


def test_real_model_semantic_similarity_and_local_only_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    path = os.environ.get("ARCHERS_TEST_MODEL_PATH")
    if not path:
        pytest.skip("Set ARCHERS_TEST_MODEL_PATH for real local embedding validation.")
    monkeypatch.setattr(httpx.Client, "request", lambda *a, **k: pytest.fail("Runtime network call"))
    encoder = LocalEncoder(Path(path))
    vectors = np.array(encoder.encode([
        "How much annual leave do employees receive?",
        "Employees are entitled to twenty days of paid vacation each year.",
        "The database listens on port five thousand.",
    ]))
    assert vectors.shape == (3, 384)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1)
    assert vectors[0] @ vectors[1] > vectors[0] @ vectors[2]
