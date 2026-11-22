"""工具层：模型可以调用的函数。

本文件只包含工具函数、工具注册表和分派函数，不含 Agent 主循环。

安全提示：sympy.sympify 内部使用 eval，理论上可以执行传入字符串里的代码。
v1 阶段输入来自本地自测，暂时接受这个风险；后期接入真实用户输入前，
应改成受限命名空间的解析方式，或先做输入白名单校验。
"""

import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import sympy as sp

import config
from mathparse import parse_math


def _numeric_value(expr: Any) -> float | None:
    """把不含自由变量的表达式算成有限实数；算不出来返回 None。

    sympy 对部分积分会留下 NonElementaryIntegral，直接 evalf 不会数值积分。
    这里先把 Integral 子式转成普通 Integral 再 evalf。
    """
    try:
        evaluated = expr.replace(
            lambda e: isinstance(e, sp.Integral),
            lambda e: sp.Integral(e.function, *e.limits).evalf(15),
        )
        value = sp.N(evaluated, 15)
        if value.is_real is not True:
            return None
        number = float(value)
    except Exception:
        return None
    return number if math.isfinite(number) else None


def verify_with_sympy(expr_a: Any, expr_b: Any) -> bool | str:
    """判断两个数学表达式是否等价。

    先做符号化简；化简不归零时，再对不含自由变量的常数表达式做高精度
    数值比较。仍然无法判断时返回“无法判定：...”文本，而不是 False，
    避免模型把“工具算不出来”误当成“这个等式是错的”并反复重试。

    Args:
        expr_a: 第一个表达式，字符串，例如 "sin(x)**2 + cos(x)**2"
        expr_b: 第二个表达式，字符串，例如 "1"

    Returns:
        True: 明确等价；
        False: 明确不等价（常数差明显非零，或抽样点已出现差异）；
        str: 无法判定，调用方应换一种写法或改用其他方法验证。

    注意：
        1. 解析失败仍返回 False；这是调用方参数写错，而不是数学结论。
        2. 符号化简不检查定义域。例如 (x**2-1)/(x-1) 与 x+1 会被判为等价，
           但前者在 x=1 处无定义。
        3. simplify 是启发式算法，极复杂的表达式可能化简不到 0。
    """
    try:
        a = parse_math(expr_a)
        b = parse_math(expr_b)
    except Exception:
        # SympifyError / TypeError / ValueError 等一律归为本次验证失败
        return False

    diff = a - b
    try:
        if sp.simplify(diff) == 0:
            return True
    except Exception:
        pass

    symbols = sorted(a.free_symbols | b.free_symbols, key=str)

    # 不含自由变量：直接高精度数值比较，解决 simplify 化简不动积分的问题。
    if not symbols:
        first = _numeric_value(a)
        second = _numeric_value(b)
        if first is not None and second is not None:
            scale = max(1.0, abs(first), abs(second))
            return abs(first - second) <= 1e-9 * scale
        return "无法判定：符号化简未归零，且常数表达式无法数值求值。"

    # 含自由变量：只做抽样反证。抽样出现差异就明确返回 False；
    # 抽样都一致也不轻易判 True，避免“只在样本点相等”的假阳性。

    rng = random.Random(20261021)
    compared = 0
    for _ in range(6):
        point = {s: sp.Float(round(rng.uniform(0.3, 2.7), 6)) for s in symbols}
        try:
            left = _numeric_value(a.subs(point))
            right = _numeric_value(b.subs(point))
        except Exception:
            continue
        if left is None or right is None:
            continue
        compared += 1
        scale = max(1.0, abs(left), abs(right))
        if abs(left - right) > 1e-6 * scale:
            return False

    if compared:
        return (
            "无法判定：符号化简未归零，但 "
            f"{compared} 个数值抽样点的结果一致；请换一种等价写法、"
            "或改用极限/求导/积分等更具体的方式验证。"
        )
    return "无法判定：符号化简未归零，且无法用数值抽样比较。"


# ---------- 代码执行工具（子进程隔离） ----------

# 注入到用户代码之前的前导脚本：导入危险模块直接报错，并禁用 os 的进程接口。
# 这是“护栏”不是安全沙箱：本地单用户场景用于降低误用风险，公网部署应换容器。
_SANDBOX_PREAMBLE = r'''
import builtins, os, sys

# 注意：numpy/scipy 在 import 时就会用到 ctypes、subprocess、socket，
# 直接禁用模块会破坏科学计算库。因此：只禁用明确不用的网络客户端，
# 进程/网络模块允许 import，但把真正的执行接口替换成报错函数。
_BANNED_MODULES = {
    "requests", "httpx", "webbrowser", "ftplib", "smtplib",
    "telnetlib", "pty",
}

class _ModuleGuard:
    @staticmethod
    def find_spec(name, path=None, target=None):
        root = name.split(".", 1)[0]
        if root in _BANNED_MODULES:
            raise PermissionError(f"沙箱禁用模块：{name}")
        return None

sys.meta_path.insert(0, _ModuleGuard())

def _blocked(*args, **kwargs):
    raise PermissionError("沙箱禁止启动外部进程或访问网络")

for _name in ("system", "popen", "execv", "execve", "execvp", "execvpe",
              "spawnv", "spawnve", "spawnl", "spawnle", "fork", "forkpty"):
    if hasattr(os, _name):
        setattr(os, _name, _blocked)

# 网络：保留 socket 模块本身（numpy/platform 会 import，ssl 还要继承
# socket 类），但禁止建连、解析和监听。不能把 socket.socket 换成函数，
# 否则 ssl 定义 SSLSocket 时会直接报错。
try:
    import socket as _socket
    for _name in ("create_connection", "create_server", "getaddrinfo",
                  "gethostbyname", "gethostbyname_ex"):
        if hasattr(_socket, _name):
            setattr(_socket, _name, _blocked)
    for _name in ("connect", "connect_ex", "bind", "sendto"):
        if hasattr(_socket.socket, _name):
            setattr(_socket.socket, _name, _blocked)
except Exception:
    pass

# 允许 import 但禁止真正启动进程；这样 scipy 等库可以正常加载。
try:
    import subprocess as _sp
    for _name in ("Popen", "run", "call", "check_call", "check_output",
                  "getoutput", "getstatusoutput"):
        if hasattr(_sp, _name):
            setattr(_sp, _name, _blocked)
except Exception:
    pass

try:
    import multiprocessing as _mp
    for _name in ("Process", "Pool", "Pipe", "Queue", "Manager"):
        if hasattr(_mp, _name):
            setattr(_mp, _name, _blocked)
except Exception:
    pass

# 文件访问：允许读标准库/依赖包/临时目录，只允许写临时工作目录。
# 这样模型即使打印文件内容，也读不到项目里的 .env。
import io as _io
import tempfile as _tempfile

_CWD = os.path.normcase(os.path.abspath(os.getcwd()))
_READ_ROOTS = tuple(
    os.path.normcase(os.path.abspath(p))
    for p in (_CWD, sys.prefix, sys.base_prefix, _tempfile.gettempdir(),
              os.path.dirname(os.__file__))
    if p
)

def _path_allowed(path, write):
    try:
        full = os.path.normcase(os.path.abspath(os.fspath(path)))
    except (TypeError, ValueError):
        return True  # 文件描述符等非路径参数，交给原函数处理
    try:
        if write:
            return os.path.commonpath([full, _CWD]) == _CWD
        return any(
            os.path.commonpath([full, root]) == root for root in _READ_ROOTS
        )
    except ValueError:
        return False

def _guard_open(original):
    def wrapper(file, mode="r", *args, **kwargs):
        write = any(ch in mode for ch in "wax+")
        if not _path_allowed(file, write):
            raise PermissionError(f"沙箱禁止访问该路径：{file}")
        return original(file, mode, *args, **kwargs)
    return wrapper

builtins.open = _guard_open(builtins.open)
_io.open = _guard_open(_io.open)

_original_os_open = os.open

def _safe_os_open(path, flags, *args, **kwargs):
    write = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND))
    if not _path_allowed(path, write):
        raise PermissionError(f"沙箱禁止访问该路径：{path}")
    return _original_os_open(path, flags, *args, **kwargs)

os.open = _safe_os_open
'''

# 只透传运行 Python 必需的环境变量；任何 KEY/TOKEN 都不会进入子进程。
_SANDBOX_ENV_KEYS = ("PATH", "SYSTEMROOT", "WINDIR")

RUN_PYTHON_MAX_CODE = 4000


def _sandbox_env(workdir: Path) -> dict[str, str]:
    """最小环境：只有路径类变量，HOME/TEMP 都指向临时目录。"""
    env = {key: os.environ[key] for key in _SANDBOX_ENV_KEYS if key in os.environ}
    env["TEMP"] = env["TMP"] = str(workdir)
    env["HOME"] = env["USERPROFILE"] = str(workdir)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """超时后连同子进程一起结束，避免计算进程残留。"""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=5,
            )
            return
        except Exception:
            pass
    else:
        import signal

        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...（输出已截断，原长 {len(text)} 字符）"


def run_python(
    code: Any,
    timeout: float | None = None,
    max_output: int | None = None,
) -> str:
    """在隔离子进程中执行一小段 Python，用于数值计算与验证。

    为什么用子进程而不是线程：Python 无法强行终止线程，sympy 之类
    长时间计算会把整个请求卡死；子进程可以超时后直接杀掉。

    安全边界（本地护栏，不是真正的沙箱）：
    - 只透传最小环境变量，API Key 不会进入子进程；
    - 禁用网络/子进程/ctypes 等模块，覆盖 os.system 等进程接口；
    - 临时工作目录、8 秒硬超时、输出截断；
    - 公网部署应改用容器 / WASM 等真正的隔离方案。

    Args:
        code: Python 源码；必须用 print 输出需要的结果。
        timeout: 超时秒数，默认读 config.RUN_PYTHON_TIMEOUT。
        max_output: stdout/stderr 各截断到多少字符。

    Returns:
        执行结果文本；任何失败都返回可读说明，不抛异常。
    """
    if not config.RUN_PYTHON_ENABLED:
        return "错误：代码执行工具未启用（RUN_PYTHON_ENABLED=0）。"
    if not isinstance(code, str) or not code.strip():
        return "错误：code 不能为空。"
    if len(code) > RUN_PYTHON_MAX_CODE:
        return f"错误：代码过长（上限 {RUN_PYTHON_MAX_CODE} 字符）。"

    timeout = float(timeout or config.RUN_PYTHON_TIMEOUT)
    max_output = int(max_output or config.RUN_PYTHON_MAX_OUTPUT)

    tmpdir = Path(tempfile.mkdtemp(prefix="mathcoach-sandbox-"))
    script = tmpdir / "main.py"
    script.write_text(_SANDBOX_PREAMBLE + "\n" + code + "\n", encoding="utf-8")

    popen_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-X", "utf8", str(script)],
            cwd=str(tmpdir),
            env=_sandbox_env(tmpdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **popen_kwargs,
        )
    except Exception as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return f"错误：无法启动代码执行进程（{type(exc).__name__}: {exc}）。"

    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:
            out, err = proc.communicate(timeout=2)
        except Exception:
            out, err = "", ""
        timed_out = True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if timed_out:
        parts = [
            f"错误：代码执行超时（超过 {timeout:g} 秒），进程已终止。"
            "请简化计算或缩短循环。"
        ]
        if out.strip():
            parts.append("[超时前的输出]\n" + _truncate(out, max_output))
        return "\n\n".join(parts)

    if not out.strip() and not err.strip():
        return "代码执行成功，但没有输出。请用 print(...) 输出需要的结果。"

    sections: list[str] = []
    if out.strip():
        sections.append("[stdout]\n" + _truncate(out, max_output))
    if err.strip():
        sections.append("[stderr]\n" + _truncate(err, max_output))
    sections.append(f"[退出码] {proc.returncode}")
    body = "\n\n".join(sections)
    if proc.returncode != 0:
        return f"错误：代码执行出错（退出码 {proc.returncode}）：\n\n{body}"
    return body


TOOLS: dict[str, dict] = {
    "verify_with_sympy": {
        "函数": verify_with_sympy,
        "描述": (
            "判断两个数学表达式是否等价。等价返回 True，不等价返回 False；"
            "若 sympy 化简不动、但数值抽样看不出差异，返回以“无法判定”开头的说明。"
            "收到“无法判定”时不要重复同一个调用，应换一种等价写法或改用其他验证方式。"
        ),
        "参数说明": {
            "expr_a": "第一个表达式，字符串，例如 'sin(x)**2 + cos(x)**2'",
            "expr_b": "第二个表达式，字符串，例如 '1'",
        },
    },
    "run_python": {
        "函数": run_python,
        "描述": (
            "在隔离的 Python 子进程中执行一小段代码，用于数值积分、估算、"
            "批量计算和答案验证。代码必须用 print() 输出结果，否则看不到；"
            "单次限时 8 秒，禁止联网、读写项目文件和启动子进程。"
            "极限/积分数值验证时，取满足条件的特殊值代入，把数值结果"
            "和你的答案比较。注意 x**n 在 n 很大且 x>1 时会溢出，"
            "可改用 exp(n*log(x)) 或分段处理。"
        ),
        "参数说明": {
            "code": "完整的 Python 代码，字符串；必须用 print 输出结果",
        },
    },
}


# ---------- 题库检索工具 ----------

_INDEX_LOCK = threading.Lock()
_INDEX_CACHE: Any = None
_INDEX_CACHE_PATH: Path | None = None
_INDEX_CACHE_ANSWERS_MTIME: float | None = None


def index_path() -> Path:
    """题库 JSONL 的位置，可用 CMC_INDEX_PATH 覆盖（测试和换数据时用）。"""
    return Path(os.environ.get("CMC_INDEX_PATH", "data/processed/problems.jsonl"))


def answers_path() -> Path:
    """官方答案 JSONL 的位置，可用 CMC_ANSWERS_PATH 覆盖。

    答案与题库分开存放：文件不存在时索引照常工作，只是试卷页显示"暂无"。
    """
    return Path(os.environ.get("CMC_ANSWERS_PATH", "data/processed/answers.jsonl"))


def index_available() -> bool:
    """题库是否已构建。调用方据此决定要不要开放检索工具。"""
    return index_path().exists()


def load_problem_index():
    """懒加载题库索引，避免未使用 RAG 的命令行也读一遍 JSONL。"""
    global _INDEX_CACHE, _INDEX_CACHE_PATH, _INDEX_CACHE_ANSWERS_MTIME

    path = index_path()
    answers = answers_path()
    answers_mtime = answers.stat().st_mtime if answers.exists() else None
    with _INDEX_LOCK:
        if (
            _INDEX_CACHE is not None
            and _INDEX_CACHE_PATH == path
            and _INDEX_CACHE_ANSWERS_MTIME == answers_mtime
        ):
            return _INDEX_CACHE
        if not path.exists():
            raise FileNotFoundError(
                f"题库不存在：{path}。请先运行 python -m rag.build"
            )
        from rag.index import ProblemIndex

        _INDEX_CACHE = ProblemIndex.load(
            path,
            answers_path=answers if answers.exists() else None,
        )
        _INDEX_CACHE_PATH = path
        _INDEX_CACHE_ANSWERS_MTIME = answers_mtime
        return _INDEX_CACHE


# ---------- 向量检索与混合检索 ----------

RETRIEVER_MODES = {"auto", "bm25", "vector", "hybrid"}

_VECTOR_LOCK = threading.RLock()
_VECTOR_CACHE: Any = None
_VECTOR_CACHE_PATH: Path | None = None
_VECTOR_CACHE_INDEX_PATH: Path | None = None


def embeddings_path() -> Path:
    """向量缓存位置，可用 CMC_EMBEDDINGS_PATH 覆盖。"""
    return Path(
        os.environ.get("CMC_EMBEDDINGS_PATH", "data/processed/embeddings.npz")
    )


def retriever_mode() -> str:
    """当前检索策略：auto / bm25 / vector / hybrid。"""
    value = os.environ.get("CMC_RETRIEVER", "auto").strip().lower()
    return value if value in RETRIEVER_MODES else "auto"


def vector_available() -> bool:
    """向量缓存文件是否存在（不校验模型，校验在加载时做）。"""
    return embeddings_path().exists()


def load_vector_index():
    """懒加载并缓存向量索引，供工具层和 Web 健康检查复用。"""
    global _VECTOR_CACHE, _VECTOR_CACHE_PATH, _VECTOR_CACHE_INDEX_PATH

    path = embeddings_path()
    source = index_path()
    with _VECTOR_LOCK:
        if (
            _VECTOR_CACHE is not None
            and _VECTOR_CACHE_PATH == path
            and _VECTOR_CACHE_INDEX_PATH == source
        ):
            return _VECTOR_CACHE
        if not path.exists():
            raise FileNotFoundError(
                f"向量缓存不存在：{path}。请先运行 python -m rag.build_embeddings"
            )
        from rag.vector import VectorIndex

        index = load_problem_index()
        _VECTOR_CACHE = VectorIndex.load_for(index, path)
        _VECTOR_CACHE_PATH = path
        _VECTOR_CACHE_INDEX_PATH = source
        return _VECTOR_CACHE


def clear_vector_cache() -> None:
    """清掉工具层缓存（测试或重建向量后调用）。"""
    global _VECTOR_CACHE, _VECTOR_CACHE_PATH, _VECTOR_CACHE_INDEX_PATH
    with _VECTOR_LOCK:
        _VECTOR_CACHE = None
        _VECTOR_CACHE_PATH = None
        _VECTOR_CACHE_INDEX_PATH = None


def retriever_status() -> dict:
    """给 /api/health 用的检索状态，不加载模型，也不返回密钥。"""
    return {
        "retriever": retriever_mode(),
        "vector_available": vector_available(),
        "embedding_model": os.environ.get(
            "CMC_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"
        ),
    }


def _search_hits(query: str, limit: int):
    """按 CMC_RETRIEVER 选择检索路径，返回 (hits, 实际使用的策略)。"""
    index = load_problem_index()
    mode = retriever_mode()
    if mode == "bm25":
        return index.search(query, k=limit), "bm25"

    try:
        vector_index = load_vector_index()
        if mode == "vector":
            from rag.hybrid import vector_search

            return vector_search(index, vector_index, query, k=limit), "vector"

        # auto 和显式 hybrid 都走 RRF；auto 在向量不可用时回退 BM25
        from rag.hybrid import hybrid_search

        return hybrid_search(index, vector_index, query, k=limit), "hybrid"
    except (FileNotFoundError, ImportError, RuntimeError, ValueError):
        # 模型未下载、依赖缺失、缓存过期都不应该让解题主流程失败
        return index.search(query, k=limit), "bm25"


def search_past_problems(query: str, k: int | str = 3) -> str:
    """检索 CMC 历年真题，返回精简 JSON。

    只返回题干和出处，不返回解答：工具结果会进模型上下文，需要控制体积；
    完整解答留给后续的 get_problem_detail（暂未实现）。

    默认 ``CMC_RETRIEVER=auto``：向量缓存可用时用 BM25 + BGE + RRF，
    否则自动退回 BM25；也可以显式设成 bm25 / vector / hybrid。
    """
    if not isinstance(query, str) or not query.strip():
        return "[]"

    try:
        limit = max(1, min(int(k), 5))
    except (TypeError, ValueError):
        limit = 3

    hits, used = _search_hits(query, limit)
    results = [
        {
            "id": hit.problem["id"],
            "source": hit.problem["source"],
            "question": hit.problem["question"][:300],
            "knowledge_points": hit.problem.get("knowledge_points") or [],
            "score": round(hit.score, 3),
        }
        for hit in hits
        # 向量余弦可能为负；只保留真正的正相关结果，BM25/RRF 本来就只返回正分
        if used == "vector" or hit.score > 0
    ]
    return json.dumps(results, ensure_ascii=False)


_CN_EDITION_DIGITS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}


def parse_edition(value: Any) -> int | None:
    """把 1 / "1" / "第一届" / "十六届" 统一成整数届数。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"无法识别届数：{value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)

    text = str(value).strip().replace("第", "").replace("届", "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)

    # 兼容“第1届初赛”这种把阶段也带进来的写法
    digits = re.search(r"\d+", text)
    if digits:
        return int(digits.group(0))

    # 兼容“第七届全国大学生数学竞赛”“第十二届初赛”这类后续文字
    numeral = re.match(r"[一二三四五六七八九十]+", text)
    if numeral:
        text = numeral.group(0)

    if text == "十":
        return 10
    if "十" in text:
        tens, ones = text.split("十", 1)
        return (_CN_EDITION_DIGITS.get(tens, 1) if tens else 1) * 10 + (
            _CN_EDITION_DIGITS.get(ones, 0) if ones else 0
        )
    if text in _CN_EDITION_DIGITS:
        return _CN_EDITION_DIGITS[text]
    raise ValueError(f"无法识别届数：{value!r}")


def _parse_year(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    match = re.search(r"(\d{4})", str(value))
    if not match:
        raise ValueError(f"无法识别年份：{value!r}")
    return int(match.group(1))


def _normalize_stage(value: Any) -> str | None:
    """把“预赛/初赛”“决赛”等说法收敛到题库里的两种阶段。"""
    if value in (None, ""):
        return None
    text = str(value).strip()
    if "初赛" in text or "预赛" in text:
        return "初赛"
    if "决赛" in text:
        return "决赛"
    return text


def _normalize_category(value: Any) -> str | None:
    """允许模型把用户原话“非数学A类”直接传进来。"""
    if value in (None, ""):
        return None
    text = str(value).strip()
    if "未分类" in text:
        return "未分类"
    upper = text.upper()
    if "A" in upper:
        return "A"
    if "B" in upper:
        return "B"
    # 题库全部是非数学类；用户只说“非数学类”时不要额外加类别过滤
    if "非数学" in text:
        return None
    return text


def _normalize_supplement(value: Any) -> str | None:
    """``None`` 表示不限；空串表示正卷；“补赛”可匹配补赛1/补赛2。"""
    if value is None:
        return None
    text = str(value).strip()
    if text in {"", "正卷", "无"}:
        return ""
    return text


def _normalize_section(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if "解答" in text:
        return "解答题"
    if "填空" in text:
        return "填空题"
    if "选择" in text:
        return "选择题"
    return text


def _filter_entries(
    entries: list[dict],
    *,
    supplement: str | None = None,
    section: str | None = None,
) -> list[dict]:
    """补做索引层没有提供的模糊过滤（补赛编号、题型简称）。"""
    if supplement is not None:
        if supplement == "":
            entries = [entry for entry in entries if not entry["supplement"]]
        else:
            entries = [
                entry for entry in entries
                if entry["supplement"].startswith(supplement)
            ]
    if section:
        entries = [
            entry for entry in entries
            if section in (entry.get("section") or "")
        ]
    return entries


def _parse_problem_number(value: Any) -> int | None:
    """把 10 / "10" / "第十题" / "第10题" 统一成正整数题号。"""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError(f"无法识别题号：{value!r}")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    else:
        text = str(value).strip()
        text = re.sub(r"^第\s*", "", text)
        text = re.sub(r"\s*题\s*$", "", text)
        number = parse_edition(text)
    if number is None:
        return None
    if number <= 0:
        raise ValueError(f"题号必须是正整数：{value!r}")
    return number


def _parse_position(value: Any) -> str | None:
    """把“压轴题/最后一题/first”解析成 first / last。"""
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    if "压轴" in text or "最后" in text or text in {
        "last", "final", "last_problem", "末题",
    }:
        return "last"
    if "第一" in text or text in {"first", "first_problem", "首题"}:
        return "first"
    raise ValueError(f"无法识别位置：{value!r}，请用 first 或 last")


def _display_question(problem: dict) -> str:
    """读构建期缓存；没有缓存时回退 PDF 原文。"""
    try:
        from rag.latex import LatexStore

        path = Path(
            os.environ.get(
                "CMC_LATEX_PATH", "data/processed/question_latex.json"
            )
        )
        if not path.exists():
            return ""
        return LatexStore.load(path).get(problem) or ""
    except Exception:
        return ""


def _group_summary(group: dict) -> dict:
    return {
        "stage": group["stage"],
        "category": group["category"],
        "supplement": group["supplement"],
        "section": group["section"],
        "label": group["label"],
        "count": group["count"],
        "numbers": group["numbers"],
        "missing": group["missing"],
        "complete": group["complete"],
        "sources": [problem["source"] for problem in group["problems"]],
    }


def _candidate_ref(group: dict, entry: dict) -> dict:
    return {
        "id": entry["id"],
        "source": entry["source"],
        "stage": entry["stage"],
        "category": entry["category"],
        "supplement": entry["supplement"],
        "section": entry["section"],
        "number": entry["number"],
        "label": group["label"],
    }


def list_past_problems(
    edition: Any = None,
    year: Any = None,
    stage: Any = None,
    category: Any = None,
    supplement: Any = None,
    section: Any = None,
) -> str:
    """列出某一届/某一年真题的全部题目（返回题号、出处和数量）。

    题干不放进工具结果：它只需要让模型知道“找到了几道、分布如何”，
    完整题干由 Web 端根据同一组参数直接读取题库并渲染，避免模型复述时
    漏题或改题，也避免把整卷塞进模型上下文。
    """
    try:
        edition_num = parse_edition(edition)
        year_num = _parse_year(year)
    except (TypeError, ValueError) as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)

    if edition_num is None and year_num is None:
        return json.dumps({
            "error": "请提供届数（例如 1）或年份（例如 2024）。",
            "available_editions": list(range(1, 17)),
        }, ensure_ascii=False)

    stage_text = _normalize_stage(stage)
    category_text = _normalize_category(category)
    supplement_text = _normalize_supplement(supplement)
    section_text = _normalize_section(section)

    index = load_problem_index()
    entries = index.list_problems(
        edition=edition_num,
        year=year_num,
        stage=stage_text,
        category=category_text,
        supplement=None,
    )
    entries = _filter_entries(
        entries, supplement=supplement_text, section=section_text
    )
    if not entries:
        return json.dumps({
            "error": "没有找到符合条件的真题。",
            "edition": edition_num,
            "year": year_num,
        }, ensure_ascii=False)

    groups = index.group_problems(entries)
    return json.dumps({
        "edition": edition_num,
        "year": year_num,
        "count": len(entries),
        "groups": [_group_summary(group) for group in groups],
    }, ensure_ascii=False)


def get_past_problem(
    edition: Any = None,
    year: Any = None,
    stage: Any = None,
    category: Any = None,
    supplement: Any = None,
    section: Any = None,
    number: Any = None,
    position: Any = None,
) -> str:
    """按届数/年份 + 场次 + 题号（或位置）取回一道题及其完整题干。

    和 ``search_past_problems`` 的区别：
    - search 用题干做语义检索，返回“相似题”；
    - get 用确定字段定位，返回用户指定的那一道题，题干完整、可直接解答。

    用户说“压轴题/最后一题”时由调用方传 ``position="last"``。若同一届
    有多份符合条件的试卷（例如初赛、决赛都有第 10 题），不替用户猜，
    返回 candidates 让模型确认。
    """
    try:
        edition_num = parse_edition(edition)
        year_num = _parse_year(year)
        number_value = _parse_problem_number(number)
        position_value = _parse_position(position)
    except (TypeError, ValueError) as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)

    if edition_num is None and year_num is None:
        return json.dumps({
            "error": "请提供届数（例如 1）或年份（例如 2024）。",
            "available_editions": list(range(1, 17)),
        }, ensure_ascii=False)
    if number_value is None and position_value is None:
        return json.dumps({
            "error": "请提供题号（number）或位置（position=first/last）。"
                     "“压轴题/最后一题”用 position=last。",
        }, ensure_ascii=False)
    if number_value is not None and position_value is not None:
        return json.dumps({
            "error": "number 和 position 只能二选一，不要同时提供。",
        }, ensure_ascii=False)

    stage_text = _normalize_stage(stage)
    category_text = _normalize_category(category)
    supplement_text = _normalize_supplement(supplement)
    section_text = _normalize_section(section)

    index = load_problem_index()
    entries = index.list_problems(
        edition=edition_num,
        year=year_num,
        stage=stage_text,
        category=category_text,
        supplement=None,
    )
    entries = _filter_entries(
        entries, supplement=supplement_text, section=section_text
    )
    groups = index.group_problems(entries)

    if not groups:
        return json.dumps({
            "error": "没有找到符合条件的真题。",
            "edition": edition_num,
            "year": year_num,
            "stage": stage_text,
            "category": category_text,
        }, ensure_ascii=False)

    candidates: list[tuple[dict, dict]] = []
    for group in groups:
        problems = group["problems"]
        if number_value is not None:
            candidates.extend(
                (group, entry) for entry in problems
                if entry["number"] == number_value
            )
        else:
            selected = problems[-1] if position_value == "last" else problems[0]
            candidates.append((group, selected))

    if not candidates:
        return json.dumps({
            "error": f"没有找到第 {number_value} 题。",
            "groups": [_group_summary(group) for group in groups],
        }, ensure_ascii=False)

    if len(candidates) > 1:
        return json.dumps({
            "error": "找到多道符合条件的题，请补充 stage / category / section。",
            "candidates": [
                _candidate_ref(group, entry) for group, entry in candidates
            ],
        }, ensure_ascii=False)

    group, entry = candidates[0]
    problem = index.get(entry["id"]) or entry
    question = _display_question(problem) or entry["question"]
    return json.dumps({
        "id": entry["id"],
        "paper_id": entry["paper_id"],
        "edition": entry["edition"],
        "year": entry["year"],
        "stage": entry["stage"],
        "category": entry["category"],
        "supplement": entry["supplement"],
        "section": entry["section"],
        "number": entry["number"],
        "source": entry["source"],
        "label": group["label"],
        "question": question,
        "knowledge_points": entry["knowledge_points"],
    }, ensure_ascii=False)


RETRIEVAL_TOOLS: dict[str, dict] = {
    "search_past_problems": {
        "函数": search_past_problems,
        "描述": (
            "在 CMC 非数学类历年真题库中检索同类题，返回题干、知识点和出处。"
            "用于推荐相关真题，不返回解答。每道新题最多调用一次。"
        ),
        "参数说明": {
            "query": "检索内容，通常传当前题目的完整题干或核心表达式",
            "k": "返回条数，字符串形式的整数，默认 3，最多 5",
        },
        "必填": ["query"],
    },
    "list_past_problems": {
        "函数": list_past_problems,
        "描述": (
            "列出某一届或某一年 CMC 真题的全部题目，返回题号、出处和数量。"
            "用户明确要求查看整套试卷时使用；不要用于查找相似题，"
            "也不要在用户只要求解答某一道题时使用。"
        ),
        "参数说明": {
            "edition": "届数，例如 1、16 或“第一届”；与 year 二选一",
            "year": "年份，例如 2024；与 edition 二选一",
            "stage": "可选，初赛 / 决赛",
            "category": "可选，A / B / 未分类（可直接传用户原话，如“非数学A类”）",
            "supplement": "可选，正卷 / 补赛 / 补赛1 / 补赛2",
            "section": "可选，选择题 / 填空题 / 解答题",
        },
        "必填": [],
    },
    "get_past_problem": {
        "函数": get_past_problem,
        "描述": (
            "按届数/年份、场次、类别和题号或位置，取回指定的一道 CMC 真题"
            "及其完整题干。用户要求解答某届/某卷的“压轴题”“最后一题”"
            "或“第 N 题”时使用；取回后直接把 question 当作本题题干解答，"
            "不要再让用户粘贴题干。"
        ),
        "参数说明": {
            "edition": "届数，例如 1、16 或“第七届”；与 year 二选一",
            "year": "年份，例如 2024；与 edition 二选一",
            "stage": "可选，初赛 / 决赛；同届两场都有该题时必填",
            "category": "可选，A / B / 未分类（可直接传“非数学A类”）",
            "supplement": "可选，正卷 / 补赛 / 补赛1 / 补赛2",
            "section": "可选，选择题 / 填空题 / 解答题",
            "number": "题号，例如 10 或“第十题”；与 position 二选一",
            "position": "题号的首尾位置：last 表示压轴题/最后一题，first 表示第一题",
        },
        "必填": [],
    },
}

ALL_TOOLS: dict[str, dict] = {**TOOLS, **RETRIEVAL_TOOLS}


TOOL_TIMEOUT = 10.0


def run_tool(
    name: str,
    args: dict,
    timeout: float = TOOL_TIMEOUT,
    cancel_event: threading.Event | None = None,
) -> Any:
    """按名字调用工具，并给执行加超时护栏。

    sympy 对某些复杂表达式会算很久（例如符号积分/化简），不设上限会让
    整个请求卡死，用户点“停止”也没有用。这里把工具放到守护线程里跑，
    主线程最多等 timeout 秒；超时返回错误文本，模型会看到并换方法。

    注意：Python 线程无法强行终止，超时的计算仍会在后台跑完；因此
    同一道题里超时后不应反复重试同一个工具调用。

    Args:
        name: 工具名，必须是 ALL_TOOLS 中的键
        args: 参数字典，会以关键字参数形式传给工具函数
        timeout: 单次工具执行的超时秒数
        cancel_event: 可选的取消信号，置位后立即返回“已停止生成。”

    Returns:
        成功时返回工具函数的原始返回值；工具不存在、参数不对、超时或
        取消时返回一段说明字符串。本函数不抛异常，便于上层把错误信息
        交回模型自行纠正。
    """
    tool = ALL_TOOLS.get(name)
    if tool is None:
        available = "、".join(ALL_TOOLS.keys())
        return f"错误：不存在名为 {name} 的工具。可用工具：{available}"

    if not isinstance(args, dict):
        return f"错误：args 必须是字典，收到的是 {type(args).__name__}"

    done = threading.Event()
    outcome: dict[str, Any] = {}

    def invoke() -> None:
        try:
            outcome["value"] = tool["函数"](**args)
        except TypeError as e:
            outcome["value"] = f"错误：参数不匹配（{e}）"
        except Exception as e:
            outcome["value"] = f"错误：工具执行失败（{type(e).__name__}: {e}）"
        finally:
            done.set()

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()

    deadline = time.monotonic() + timeout
    while not done.wait(0.2):
        if cancel_event is not None and cancel_event.is_set():
            return "已停止生成。"
        if time.monotonic() > deadline:
            return (
                f"错误：工具 {name} 执行超时（超过 {timeout:g} 秒）已中止，"
                "请换一种更简单的验证方式，或直接给出结论并注明“未工具验证”。"
            )
    return outcome.get("value")
