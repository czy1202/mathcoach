"""BGE 向量索引：把真题编码成向量，并提供 Top-K 语义检索。

设计边界：

1. 模型加载和编码都是懒执行。没装 sentence-transformers、模型没下载或
   缓存与题库不一致时，调用方可以捕获异常并回退 BM25。
2. 向量缓存是独立的 ``.npz`` 文件，不写回 ``problems.jsonl``，也不进 git。
3. 文档和查询用同一个模型编码；BGE 中文模型额外给短查询加检索指令，
   避免“题目”和“查询”落在不同的表示空间。
4. 所有向量在写入和读取时都做 L2 归一化，检索时用点积等价于余弦相似度。

用法：

    python -m rag.build_embeddings
    python -m rag.evaluate --misses       # 评测里会带上 BGE / RRF 两行
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

import numpy as np

import config  # 触发 .env 加载，保证 CMC_* 路径在直接使用 rag 时也生效
from rag.index import ProblemIndex


DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_EMBEDDINGS = Path("data/processed/embeddings.npz")
# 官方建议短查询加指令，但在本项目 47 条查询上不加指令的 Recall@1 更高；
# 需要复现官方设置时，把 CMC_QUERY_PREFIX 设为这句中文指令即可。
DEFAULT_QUERY_PREFIX = ""

_MODEL_LOCK = threading.RLock()
_MODEL_CACHE: dict[tuple[str, str | None], object] = {}


class VectorCacheError(RuntimeError):
    """向量缓存缺失、损坏，或与当前题库/模型不匹配。"""


def embeddings_path() -> Path:
    """向量缓存位置，可用 CMC_EMBEDDINGS_PATH 覆盖。"""
    return Path(
        os.environ.get("CMC_EMBEDDINGS_PATH", str(DEFAULT_EMBEDDINGS))
    )


def configured_model_name() -> str:
    """当前配置的嵌入模型名，可用 CMC_EMBEDDING_MODEL 覆盖。"""
    value = os.environ.get("CMC_EMBEDDING_MODEL", "").strip()
    return value or DEFAULT_MODEL


def _model_identity(name: str) -> str:
    """把模型名/路径归一成可比对的标识。

    缓存里可能记录构建机器上的绝对路径（例如
    ``D:/models/bge-small-zh-v1.5``），用户机器上的路径不同但模型相同。
    只比较最后一段目录名，避免因为盘符或目录不同而误判缓存失效。
    """
    value = str(name or "").strip().replace("\\", "/").rstrip("/")
    if not value:
        return ""
    return value.rsplit("/", 1)[-1].lower()


def model_cache_folder() -> str | None:
    """模型下载目录，可用 CMC_MODEL_CACHE 指到 D 盘等位置。"""
    value = os.environ.get("CMC_MODEL_CACHE", "").strip()
    return value or None


def query_prefix(model: str | None = None) -> str:
    """查询侧的前缀指令。

    默认不加指令：在本项目的 47 条查询上，不加指令的 BGE 召回更好。
    ``CMC_QUERY_PREFIX`` 显式设置时优先使用，可用于复现官方推荐设置
    或做消融实验；设为空串则强制关闭。
    """
    if "CMC_QUERY_PREFIX" in os.environ:
        return os.environ["CMC_QUERY_PREFIX"]
    return DEFAULT_QUERY_PREFIX


def _normalize_rows(matrix: np.ndarray) -> np.ndarray:
    """按行做 L2 归一化；全零行保持为零，避免除零。"""
    matrix = np.asarray(matrix, dtype=np.float32)
    if matrix.ndim != 2:
        raise ValueError(f"向量必须是二维矩阵，收到形状 {matrix.shape}")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def _normalize_vector(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    if norm == 0:
        return vector
    return vector / norm


def embedding_text(problem: dict) -> str:
    """参与编码的文本：题干 + 受控知识点标签。

    出处和届数没有放进编码文本：它们会让“第 16 届”这类词抢走语义权重。
    知识点标签则是题目内容的压缩摘要，能帮助短题干和公式题聚类；
    在 47 条评测查询上，加上标签后 RRF 的 Recall@1 从 76.6% 提到 83.0%。
    """
    question = str(problem.get("question") or "").strip()
    tags = [str(tag).strip() for tag in problem.get("knowledge_points") or []]
    tags = [tag for tag in tags if tag]
    if not tags:
        return question
    return f"{question} 知识点：{'、'.join(tags)}"


@dataclass
class VectorHit:
    problem_id: str
    score: float
    rank: int


@dataclass
class VectorIndex:
    ids: list[str]
    embeddings: np.ndarray
    model_name: str = ""
    path: Path | None = None
    query_encoder: Callable[[str], np.ndarray] | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        self.ids = [str(pid) for pid in self.ids]
        self.embeddings = _normalize_rows(self.embeddings)
        if len(self.ids) != self.embeddings.shape[0]:
            raise ValueError(
                f"id 数量（{len(self.ids)}）与向量行数"
                f"（{self.embeddings.shape[0]}）不一致"
            )

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        query_encoder: Callable[[str], np.ndarray] | None = None,
    ) -> "VectorIndex":
        path = Path(path) if path is not None else embeddings_path()
        if not path.exists():
            raise VectorCacheError(
                f"向量缓存不存在：{path}。请先运行 python -m rag.build_embeddings"
            )
        try:
            with np.load(path, allow_pickle=False) as data:
                ids = [str(pid) for pid in data["ids"].tolist()]
                embeddings = np.asarray(data["embeddings"], dtype=np.float32)
                model_name = (
                    str(data["model"].item()) if "model" in data else ""
                )
        except Exception as exc:
            raise VectorCacheError(f"读取向量缓存失败：{path}（{exc}）") from exc

        return cls(
            ids=ids,
            embeddings=embeddings,
            model_name=model_name,
            path=path,
            query_encoder=query_encoder,
        )

    @classmethod
    def load_for(
        cls,
        index: ProblemIndex,
        path: str | Path | None = None,
        *,
        query_encoder: Callable[[str], np.ndarray] | None = None,
        check_model: bool = True,
    ) -> "VectorIndex":
        """加载缓存，并校验它确实是当前题库和当前模型生成的。"""
        vector_index = cls.load(path, query_encoder=query_encoder)
        expected = [problem["id"] for problem in index.problems]
        if len(expected) != len(vector_index.ids) or set(expected) != set(vector_index.ids):
            raise VectorCacheError(
                "向量缓存与当前题库不一致（题数或题号不同），"
                "请重新运行 python -m rag.build_embeddings"
            )
        current_model = configured_model_name()
        if (
            check_model
            and vector_index.model_name
            and _model_identity(vector_index.model_name)
            != _model_identity(current_model)
        ):
            raise VectorCacheError(
                f"向量缓存由 {vector_index.model_name} 生成，"
                f"当前配置是 {current_model}；请重新构建或修改 CMC_EMBEDDING_MODEL"
            )
        return vector_index

    @property
    def dim(self) -> int:
        return int(self.embeddings.shape[1]) if self.embeddings.ndim == 2 else 0

    def encode_query(self, query: str) -> np.ndarray:
        if self.query_encoder is not None:
            return _normalize_vector(self.query_encoder(query))
        return encode_query(
            query,
            model_name=self.model_name or configured_model_name(),
        )

    def search(
        self,
        query: str,
        k: int = 5,
        *,
        allowed_ids: Iterable[str] | None = None,
    ) -> list[VectorHit]:
        """余弦 Top-K。``allowed_ids`` 非空时只在这些题号里排。"""
        if not isinstance(query, str) or not query.strip() or k <= 0:
            return []
        if not self.ids:
            return []

        vector = self.encode_query(query)
        if vector.size != self.dim:
            raise ValueError(
                f"查询向量维度 {vector.size} 与缓存维度 {self.dim} 不一致"
            )

        scores = self.embeddings @ vector
        if allowed_ids is not None:
            allowed = {str(pid) for pid in allowed_ids}
            mask = np.array([pid in allowed for pid in self.ids], dtype=bool)
            scores = np.where(mask, scores, -np.inf)

        # stable 让同分结果按题库原顺序排列，评测可复现
        order = np.argsort(-scores, kind="stable")
        hits: list[VectorHit] = []
        for rank, position in enumerate(order, start=1):
            score = float(scores[position])
            if not np.isfinite(score):
                break
            hits.append(
                VectorHit(
                    problem_id=self.ids[int(position)],
                    score=score,
                    rank=rank,
                )
            )
            if len(hits) >= min(int(k), len(self.ids)):
                break
        return hits


def _load_model(name: str, cache_folder: str | None):
    """懒加载并复用 SentenceTransformer，避免每次检索都读几个 GB。"""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:  # pragma: no cover - 依赖缺失时走回退分支
        raise RuntimeError(
            "未安装 sentence-transformers，请先运行 "
            "pip install -r requirements.txt"
        ) from exc

    # 让 huggingface_hub 的中间文件也落到同一个目录，而不是 C 盘用户目录
    if cache_folder:
        os.environ.setdefault("HF_HOME", cache_folder)

    key = (name, cache_folder)
    with _MODEL_LOCK:
        model = _MODEL_CACHE.get(key)
        if model is None:
            try:
                model = SentenceTransformer(
                    name,
                    cache_folder=cache_folder,
                    device="cpu",
                )
            except Exception as exc:  # pragma: no cover - 依赖网络/磁盘
                raise RuntimeError(
                    f"加载嵌入模型失败：{name}（{exc}）。"
                    "若首次使用，请检查网络或设置 HF_ENDPOINT 镜像。"
                ) from exc
            _MODEL_CACHE[key] = model
    return model


def _encode(
    texts: Sequence[str],
    *,
    query: bool,
    model_name: str | None = None,
    cache_folder: str | None = None,
    batch_size: int = 32,
    show_progress: bool = False,
) -> np.ndarray:
    name = model_name or configured_model_name()
    model = _load_model(name, cache_folder if cache_folder is not None else model_cache_folder())

    prefix = query_prefix(name) if query else ""
    if prefix:
        texts = [
            text if str(text).startswith(prefix) else f"{prefix}{text}"
            for text in texts
        ]

    with _MODEL_LOCK:
        vectors = model.encode(
            list(texts),
            batch_size=max(1, int(batch_size)),
            show_progress_bar=show_progress,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
    return _normalize_rows(np.asarray(vectors, dtype=np.float32))


def encode_documents(
    texts: Sequence[str],
    *,
    model_name: str | None = None,
    cache_folder: str | None = None,
    batch_size: int = 32,
    show_progress: bool = False,
) -> np.ndarray:
    return _encode(
        texts,
        query=False,
        model_name=model_name,
        cache_folder=cache_folder,
        batch_size=batch_size,
        show_progress=show_progress,
    )


def encode_query(
    query: str,
    *,
    model_name: str | None = None,
    cache_folder: str | None = None,
) -> np.ndarray:
    return _encode(
        [query],
        query=True,
        model_name=model_name,
        cache_folder=cache_folder,
    )[0]


def build_vector_index(
    index: ProblemIndex,
    out_path: str | Path | None = None,
    *,
    encoder: Callable[[Sequence[str]], np.ndarray] | None = None,
    model_name: str | None = None,
    cache_folder: str | None = None,
    batch_size: int = 32,
    show_progress: bool = True,
) -> Path:
    """编码整个题库并写入 ``.npz``。

    ``encoder`` 主要给测试用：传入一个假编码器就不用下载模型。
    """
    texts = [embedding_text(problem) for problem in index.problems]
    if not texts:
        raise ValueError("题库为空，无法构建向量索引")
    empty = [problem["id"] for problem, text in zip(index.problems, texts) if not text]
    if empty:
        raise ValueError(f"这些题目没有题干，无法编码：{'、'.join(empty[:5])}")

    name = model_name or configured_model_name()
    if encoder is not None:
        embeddings = np.asarray(encoder(texts), dtype=np.float32)
    else:
        embeddings = encode_documents(
            texts,
            model_name=name,
            cache_folder=cache_folder,
            batch_size=batch_size,
            show_progress=show_progress,
        )
    embeddings = _normalize_rows(embeddings)

    path = Path(out_path) if out_path is not None else embeddings_path()
    if path.suffix.lower() != ".npz":
        path = path.with_suffix(".npz")
    path.parent.mkdir(parents=True, exist_ok=True)

    ids = [problem["id"] for problem in index.problems]
    np.savez_compressed(
        path,
        ids=np.array(ids, dtype=str),
        embeddings=embeddings.astype(np.float32),
        model=np.array(name),
        dim=np.array(embeddings.shape[1], dtype=np.int32),
    )
    return path


def vector_cache_available(path: str | Path | None = None) -> bool:
    return (Path(path) if path is not None else embeddings_path()).exists()
