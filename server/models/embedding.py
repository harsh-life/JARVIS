"""The local embedding model (11 OD-MEM-2, PRD §41: `BAAI/bge-small-en-v1.5`).

Both persistent memory and the Knowledge Vault embed text, and neither may
import the other (16 §2: application-band siblings are independent), so the
embedder lives here, below both.

**No hidden egress (10, docs/21 §2.1, MP-T8).** The server loads the model with
`local_files_only=True`: it reads the operator-provisioned cache and never
contacts Hugging Face or any mirror. A missing model is a startup failure that
names the provisioning command, not a silent download. The *only* code that may
fetch the model is `provision_embedder`, run by the operator during controlled
setup (`python -m server.memory provision`, 15 §3 step 5).

`fastembed` runs the model on ONNX Runtime in-process: no model server, no
network listener, no torch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

# Operator-facing short names → the fastembed model id. Only models fastembed
# ships an ONNX export for are accepted; anything else is a config error.
_MODEL_ALIASES = {
    "bge-small-en-v1.5": "BAAI/bge-small-en-v1.5",
    "BAAI/bge-small-en-v1.5": "BAAI/bge-small-en-v1.5",
}


class EmbedderUnavailable(Exception):
    """The embedding model cannot be loaded offline. The message says how to
    provision it and never includes model contents or user text."""


def resolve_model_name(name: str) -> str:
    try:
        return _MODEL_ALIASES[name]
    except KeyError:
        raise EmbedderUnavailable(
            f"embedder {name!r} is not supported; supported: {sorted(set(_MODEL_ALIASES))}"
        ) from None


class LocalEmbedder:
    """Deterministic, offline text embeddings. Thread-safe for concurrent reads
    (ONNX Runtime sessions are), and never makes a network call."""

    def __init__(self, *, model: str, cache_dir: str | Path) -> None:
        self.model_name = resolve_model_name(model)
        self.cache_dir = Path(cache_dir)
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover — exercised only without the extra
            raise EmbedderUnavailable(
                "the memory stack is not installed: pip install -e '.[memory]'"
            ) from exc
        try:
            self._model = TextEmbedding(
                model_name=self.model_name,
                cache_dir=str(self.cache_dir),
                local_files_only=True,
            )
        except Exception as exc:  # noqa: BLE001 — any load failure is "not provisioned"
            raise EmbedderUnavailable(
                f"embedding model {self.model_name!r} is not provisioned under {self.cache_dir}; "
                "run `python -m server.memory provision` during setup (the server never "
                "downloads models at runtime)"
            ) from exc
        self.dimensions = int(self._model.embedding_size)

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        cleaned = [t.replace("\n", " ") for t in texts]
        return [vector.tolist() for vector in self._model.embed(cleaned)]


def provision_embedder(*, model: str, cache_dir: str | Path) -> Path:
    """Download the model into `cache_dir` — the one sanctioned network step,
    run by the operator, never by the server. Returns the cache directory."""

    from fastembed import TextEmbedding

    target = Path(cache_dir)
    target.mkdir(parents=True, exist_ok=True)
    TextEmbedding(model_name=resolve_model_name(model), cache_dir=str(target))
    # Prove the offline load path works before declaring success.
    LocalEmbedder(model=model, cache_dir=target)
    return target


__all__ = ["EmbedderUnavailable", "LocalEmbedder", "provision_embedder", "resolve_model_name"]
