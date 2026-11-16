"""Offline embedding and private Qdrant adapters; downloads require explicit setup."""

import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
import numpy as np
import onnxruntime as ort  # type: ignore[import-untyped]
from tokenizers import Tokenizer  # type: ignore[import-untyped]

from archers.domain import DomainError
from archers.ingestion.normalization import NormalizedDocument

MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
MODEL_FILES = {
    "onnx/model.onnx": "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452",
    "tokenizer.json": "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037",
    "sentence_bert_config.json": "fc1993fde0a95c24ec6c022539d41cf6e2f7c9721e5415d6fb6897472a9cd4b7",
    "README.md": "dcd602d2fd35c203a247304a06fec6654a12f7941b739f9221a064fe8dc3b7f0",
}
PIPELINE = "minilm-mean-l2-v1:block-offset-chunks-v1:hashed-log-tf-v1"
ORT_ERRORS = (
    ort.capi.onnxruntime_pybind11_state.Fail,
    ort.capi.onnxruntime_pybind11_state.InvalidArgument,
    ort.capi.onnxruntime_pybind11_state.InvalidGraph,
    ort.capi.onnxruntime_pybind11_state.InvalidProtobuf,
    ort.capi.onnxruntime_pybind11_state.NoSuchFile,
    ort.capi.onnxruntime_pybind11_state.NotImplemented,
    ort.capi.onnxruntime_pybind11_state.RuntimeException,
)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while block := file.read(65536):
            digest.update(block)
    return digest.hexdigest()


def download_model(directory: Path) -> None:
    """Explicit operator action. Fetch only pinned, checksum-verified public assets."""
    directory.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=120, trust_env=False) as client:
        for name, expected in MODEL_FILES.items():
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                if file_hash(path) != expected:
                    raise ValueError("Existing model asset failed checksum; review before replacing.")
                continue
            temporary: str | None = None
            try:
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
                    temporary = output.name
                    total = 0
                    url = f"https://huggingface.co/{MODEL_ID}/resolve/{MODEL_REVISION}/{name}"
                    with client.stream("GET", url) as response:
                        response.raise_for_status()
                        for block in response.iter_bytes(65536):
                            total += len(block)
                            if total > 100 * 1024 * 1024:
                                raise ValueError("Model asset exceeds the reviewed size limit.")
                            output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                if file_hash(Path(temporary)) != expected:
                    raise ValueError("Downloaded model asset failed checksum verification.")
                os.replace(temporary, path)
                temporary = None
            except httpx.HTTPError as exc:
                raise DomainError("MODEL_UNAVAILABLE", "Pinned model asset download failed.", 503) from exc
            finally:
                if temporary is not None:
                    Path(temporary).unlink(missing_ok=True)
    (directory / "manifest.json").write_text(json.dumps({
        "model": MODEL_ID, "revision": MODEL_REVISION, "license": "Apache-2.0",
        "files": MODEL_FILES,
    }, indent=2))


@dataclass(frozen=True)
class Chunk:
    text: str
    ordinal: int
    token_count: int
    locations: list[dict[str, Any]]


class LocalEncoder:
    dimension = 384
    token_limit = 256
    fingerprint = hashlib.sha256(
        json.dumps([MODEL_ID, MODEL_REVISION, MODEL_FILES, PIPELINE], sort_keys=True).encode()
    ).hexdigest()

    def __init__(self, model_path: Path) -> None:
        for name, expected in MODEL_FILES.items():
            if file_hash(model_path / name) != expected:
                raise ValueError("Local model asset failed integrity verification.")
        self.tokenizer = Tokenizer.from_file(str(model_path / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        try:
            self.session = ort.InferenceSession(
                str(model_path / "onnx/model.onnx"), sess_options=options,
                providers=["CPUExecutionProvider"],
            )
        except (*ORT_ERRORS, RuntimeError) as exc:
            raise DomainError("MODEL_UNAVAILABLE", "Local embedding model failed to load.", 503) from exc

    def chunk(self, document: NormalizedDocument) -> list[Chunk]:
        chunks: list[Chunk] = []
        limit = min(450, 480, self.token_limit)
        special = self.tokenizer.num_special_tokens_to_add(False)
        capacity = limit - special
        overlap = min(60, capacity // 4)
        for block in document.blocks:
            text = block.text
            encoding = self.tokenizer.encode(text, add_special_tokens=False)
            offsets = [(start, end) for start, end in encoding.offsets if end > start]
            location = {key: value for key, value in asdict(block.location).items() if value is not None}
            location["kind"] = {
                "page": "pdf_page", "cell": "sheet_cells", "line": "lines",
                "row": "rows", "block": "block", "slide": "slide",
            }[block.location.kind]
            # Keep original characters, not decode(ids), which normalizes case and spacing.
            if not offsets:
                if text.strip():
                    raise ValueError("Text cannot be represented by the selected tokenizer.")
                continue
            first = 0
            while first < len(offsets):
                last = min(first + capacity, len(offsets))
                while True:
                    start = 0 if first == 0 else offsets[first][0]
                    end = len(text) if last == len(offsets) else offsets[last][0]
                    passage = text[start:end]
                    count = len(self.tokenizer.encode(passage).ids)
                    if count <= limit:
                        break
                    last -= 1
                    if last <= first:
                        raise ValueError("A text span exceeds the tokenizer limit.")
                chunks.append(Chunk(passage, len(chunks), count, [location]))
                if len(chunks) > 50000:
                    raise ValueError("Chunk count exceeds the configured document budget.")
                if last == len(offsets):
                    break
                first = max(first + 1, last - overlap)
        return chunks

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        try:
            for start in range(0, len(texts), 16):
                encoded = [self.tokenizer.encode(text) for text in texts[start:start + 16]]
                if any(not item.ids or len(item.ids) > self.token_limit for item in encoded):
                    raise ValueError("Embedding input exceeds the model limit; truncation is forbidden.")
                length = max(len(item.ids) for item in encoded)
                ids = np.array([item.ids + [0] * (length - len(item.ids)) for item in encoded], dtype=np.int64)
                mask = np.array([[1] * len(item.ids) + [0] * (length - len(item.ids)) for item in encoded], dtype=np.int64)
                inputs = {"input_ids": ids, "attention_mask": mask, "token_type_ids": np.zeros_like(ids)}
                output = self.session.run(None, {
                    item.name: inputs[item.name] for item in self.session.get_inputs()
                })[0]
                embeddings = np.asarray(output, dtype=np.float32)
                if embeddings.shape != (len(encoded), length, self.dimension):
                    raise ValueError("Embedding output has an unexpected shape.")
                pooled = (embeddings * mask[..., None]).sum(axis=1) / mask.sum(axis=1)[:, None]
                norms = np.linalg.norm(pooled, axis=1, keepdims=True)
                if not np.isfinite(pooled).all() or (norms <= 0).any():
                    raise ValueError("Embedding output contains invalid values.")
                vectors.extend((pooled / norms).tolist())
        except ORT_ERRORS as exc:
            raise DomainError("MODEL_UNAVAILABLE", "Local embedding execution failed.", 503) from exc
        return vectors

    def sparse(self, texts: list[str]) -> list[tuple[list[int], list[float]]]:
        """Stable hashed lexical log-TF; explicitly not corpus-trained BM25."""
        result = []
        for text in texts:
            counts = Counter(
                int.from_bytes(hashlib.sha256(term.encode()).digest()[:4], "big")
                for term in re.findall(r"\w+", text.casefold(), flags=re.UNICODE)
            )
            indices = sorted(counts)
            result.append((indices, [1 + math.log(counts[index]) for index in indices]))
        return result


class VectorIndex:
    def __init__(self, url: str, collection: str, dimension: int) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", collection) or dimension < 1:
            raise ValueError("Invalid collection configuration.")
        self.collection, self.dimension = collection, dimension
        self.client = httpx.Client(base_url=url.rstrip("/"), timeout=60, trust_env=False)
        try:
            response = self.client.get(f"/collections/{collection}")
            if response.status_code == 404:
                self._request("PUT", f"/collections/{collection}", {
                    "vectors": {"dense": {"size": dimension, "distance": "Cosine"}},
                    "sparse_vectors": {"sparse": {}},
                })
            else:
                response.raise_for_status()
            info = self._request("GET", f"/collections/{collection}")
            params = info["config"]["params"]
            if (
                params["vectors"]["dense"]["size"] != dimension
                or params["vectors"]["dense"]["distance"] != "Cosine"
                or "sparse" not in params["sparse_vectors"]
            ):
                raise ValueError("Existing collection has an incompatible vector schema.")
            for field in ("tenant_id", "document_id", "generation_id"):
                self._request("PUT", f"/collections/{collection}/index?wait=true", {
                    "field_name": field, "field_schema": "keyword",
                })
        except (httpx.HTTPError, KeyError, TypeError) as exc:
            self.client.close()
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Vector index initialization failed.", 503) from exc
        except (DomainError, ValueError):
            self.client.close()
            raise

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            response = self.client.request(method, path, json=body)
            response.raise_for_status()
            return response.json()["result"]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Vector database operation failed.", 503) from exc

    @staticmethod
    def point_id(tenant: UUID, version: UUID, generation: UUID, ordinal: int) -> str:
        return str(uuid5(NAMESPACE_URL, f"archers:{tenant}:{version}:{generation}:{ordinal}"))

    def stage(
        self, tenant_id: UUID, document_id: UUID, version_id: UUID, generation_id: UUID,
        chunks: list[Chunk], dense: list[list[float]],
        sparse: list[tuple[list[int], list[float]]],
    ) -> None:
        if not chunks or len(chunks) != len(dense) or len(chunks) != len(sparse):
            raise ValueError("Vector cardinality does not match chunks.")
        if [chunk.ordinal for chunk in chunks] != list(range(len(chunks))):
            raise ValueError("Chunk ordinals must be contiguous.")
        for vector, (indices, values) in zip(dense, sparse, strict=True):
            if (
                len(vector) != self.dimension or not all(math.isfinite(v) for v in vector)
                or not any(v != 0 for v in vector)
                or len(indices) != len(values) or indices != sorted(set(indices))
                or any(index < 0 or index >= 2**32 for index in indices)
                or not all(math.isfinite(v) and v > 0 for v in values)
            ):
                raise ValueError("Invalid dense or sparse vector output.")
        for start in range(0, len(chunks), 64):
            points: list[dict[str, Any]] = [{
                "id": self.point_id(tenant_id, version_id, generation_id, chunk.ordinal),
                "vector": {
                    "dense": dense[start + offset],
                    "sparse": {"indices": sparse[start + offset][0], "values": sparse[start + offset][1]},
                },
                "payload": {
                    "tenant_id": str(tenant_id), "document_id": str(document_id),
                    "version_id": str(version_id), "generation_id": str(generation_id),
                    "ordinal": chunk.ordinal,
                },
            } for offset, chunk in enumerate(chunks[start:start + 64])]
            self._request("PUT", f"/collections/{self.collection}/points?wait=true", {"points": points})
            saved = self._request("POST", f"/collections/{self.collection}/points", {
                "ids": [p["id"] for p in points], "with_payload": True, "with_vector": True,
            })
            self._verify_points(points, saved)
        result = self._request("POST", f"/collections/{self.collection}/points/count", {
            "exact": True, "filter": {"must": [
                {"key": "tenant_id", "match": {"value": str(tenant_id)}},
                {"key": "generation_id", "match": {"value": str(generation_id)}},
            ]},
        })
        if not isinstance(result, dict) or result.get("count") != len(chunks):
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Vector count verification failed.", 503)

    @staticmethod
    def _verify_points(points: list[dict[str, Any]], saved: Any) -> None:
        try:
            expected = {p["id"]: p for p in points}
            if not isinstance(saved, list) or len(saved) != len(points) or {p["id"] for p in saved} != set(expected):
                raise ValueError("Vector IDs do not match")
            for point in saved:
                want = expected[point["id"]]
                if (
                    point["payload"] != want["payload"]
                    or not np.allclose(point["vector"]["dense"], want["vector"]["dense"], atol=1e-5)
                    or point["vector"]["sparse"]["indices"] != want["vector"]["sparse"]["indices"]
                    or not np.allclose(point["vector"]["sparse"]["values"], want["vector"]["sparse"]["values"])
                ):
                    raise ValueError("Vector content does not match")
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Vector content verification failed.", 503) from exc

    def search(self, tenant_id: UUID, vector: list[float], limit: int = 5) -> list[dict[str, Any]]:
        """Internal diagnostics only; callers must check canonical PostgreSQL lifecycle."""
        result = self._request("POST", f"/collections/{self.collection}/points/query", {
            "query": vector, "using": "dense", "limit": limit,
            "filter": {"must": [{"key": "tenant_id", "match": {"value": str(tenant_id)}}]},
            "with_payload": True,
        })
        points: list[dict[str, Any]] = result["points"]
        return points

    def close(self) -> None:
        self.client.close()
