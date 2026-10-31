"""构建 BGE 向量缓存。

用法：

    python -m rag.build_embeddings
    python -m rag.build_embeddings --out data/processed/embeddings.npz

模型默认下载到 ``CMC_MODEL_CACHE``（本项目配置为 D 盘），向量文件默认写到
``data/processed/embeddings.npz``。题库或模型变化后必须重跑，否则工具层会
检测到缓存不一致并回退 BM25。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from dotenv import load_dotenv

from rag.index import ProblemIndex
from rag.vector import (
    build_vector_index,
    configured_model_name,
    embeddings_path,
    model_cache_folder,
)


DEFAULT_INDEX = Path("data/processed/problems.jsonl")


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="为 CMC 题库构建 BGE 向量索引")
    parser.add_argument("--index", default=str(DEFAULT_INDEX))
    parser.add_argument("--out", default=None, help="输出 .npz，默认读 CMC_EMBEDDINGS_PATH")
    parser.add_argument("--model", default=None, help="嵌入模型，默认读 CMC_EMBEDDING_MODEL")
    parser.add_argument("--cache-folder", default=None, help="模型缓存目录，默认读 CMC_MODEL_CACHE")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--no-progress", action="store_true")
    args = parser.parse_args()

    index_path = Path(args.index)
    if not index_path.exists():
        raise SystemExit(
            f"题库不存在：{index_path}。请先运行 python -m rag.build"
        )

    model = args.model or configured_model_name()
    cache = args.cache_folder if args.cache_folder is not None else model_cache_folder()
    out = Path(args.out) if args.out else embeddings_path()

    index = ProblemIndex.load(index_path)
    print(f"题库：{index_path}（{len(index)} 道）")
    print(f"模型：{model}")
    print(f"模型缓存：{cache or '默认 Hugging Face 缓存'}")
    print(f"输出：{out}")

    path = build_vector_index(
        index,
        out,
        model_name=model,
        cache_folder=cache,
        batch_size=args.batch_size,
        show_progress=not args.no_progress,
    )
    import numpy as np

    with np.load(path, allow_pickle=False) as data:
        dim = int(data["dim"].item())
        count = len(data["ids"])
    print(f"完成：{count} 道 × {dim} 维 -> {path}")


if __name__ == "__main__":
    main()
