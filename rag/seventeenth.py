"""第十七届（2025）初赛 A/B 类的题目与官方参考解答。

这批题不在 ``papers/`` 的 1-16 届 PDF 里，来源是桌面的
《第1-17届全国大学生数学竞赛(非数类)预赛试题参考解析汇总》。
题干和解答按原卷整理成 Markdown + LaTeX，直接作为可展示文本使用，
所以记录上带 ``display_ready=True``。

A、B 卷有四道完全相同的题（平面方程、隐函数求导、微分方程、级数），
题干写成完全一致的字符串，交给 ``rag.papers.dedupe`` 合并成一条记录，
另一份试卷的题号会进入 ``aliases``，浏览 A/B 卷时都能看到。
"""

from __future__ import annotations


def _record(
    edition: int,
    category: str,
    number: int,
    question: str,
    answer: str,
    solution: str,
) -> dict:
    category_code = category.lower()
    problem_id = f"cmc-{edition:02d}-prelim-{category_code}-{number:02d}"
    return {
        "id": problem_id,
        "question": question,
        "question_layout": question,
        "stage": "初赛",
        "edition": edition,
        "year": 2025,
        "category": category,
        "supplement": "",
        "number": number,
        "source": f"第{edition}届 非数学类 初赛 {category}类 第{number}题",
        "source_file": "第1-17届全国大学生数学竞赛(非数类)预赛试题参考解析汇总.pdf",
        "answer": answer,
        "solution": solution,
        "display_ready": True,
    }


_PLANE_QUESTION = (
    "设平面过原点与点 $(6,-3,2)$，且与平面 $4x-y+2z-8=0$ 垂直，"
    "则此平面方程为 ____。"
)

_PLANE_ANSWER = "$2x+2y-3z=0$（答案不唯一，差一个非零倍数均可）。"

_PLANE_SOLUTION = r"""所求平面的法向量同时垂直于向量 $(6,-3,2)$ 和已知平面的法向量 $(4,-1,2)$，取叉积

$$
(6,-3,2)\times(4,-1,2)=(-4,-4,6),
$$

可取法向量 $(2,2,-3)$。平面过原点，所以方程为

$$
2x+2y-3z=0.
$$

注：答案不唯一，系数差一个非零倍数都对，例如 $-2x-2y+3z=0$ 或 $x+y-\frac32z=0$ 也对。"""

_IMPLICIT_QUESTION = (
    "设函数 $f(u,v)$ 具有一阶连续偏导数，$f(2,0)=3$，$f_u'(2,0)=5$，"
    "又设 $z=z(x,y)$ 是由方程 $xz=f(2x-y,xyz)$ 确定的隐函数，则 "
    r"$\left.\frac{\partial z}{\partial x}\right|_{x=1,y=0}=$ ____。"
)

_IMPLICIT_ANSWER = "$7$。"

_IMPLICIT_SOLUTION = r"""对方程两边关于 $x$ 求偏导：

$$
z+x\frac{\partial z}{\partial x}
=2f_u'+yf_v'\left(z+x\frac{\partial z}{\partial x}\right).
$$

解得

$$
\frac{\partial z}{\partial x}
=\frac{1}{x}\left(\frac{2f_u'}{1-yf_v'}-z\right).
$$

当 $x=1,\ y=0$ 时，$z=f(2,0)=3$，$f_u'(2,0)=5$，所以

$$
\left.\frac{\partial z}{\partial x}\right|_{x=1,y=0}
=2f_u'(2,0)-3=7.
$$"""

_DIFFEQ_QUESTION = (
    r"求微分方程 $y'x\ln x\sin y+(1-x\cos y)\cos y=0$ 的通解。"
)

_DIFFEQ_ANSWER = r"$\dfrac{1}{\cos y}=\dfrac{x+C}{\ln x}$。"

_DIFFEQ_SOLUTION = r"""将原方程变形为

$$
y'\sin y+\frac{1}{x\ln x}\cos y=\frac{1}{\ln x}\cos^2 y.
$$

令 $z=\cos y$，方程化为 $n=2$ 的 Bernoulli 方程

$$
z'-\frac{1}{x\ln x}z=-\frac{1}{\ln x}z^2.
$$

再令 $u=z^{-1}$，得非齐次线性方程

$$
u'+\frac{1}{x\ln x}u=\frac{1}{\ln x}.
$$

利用通解公式：

$$
u=e^{-\int\frac{dx}{x\ln x}}
\left(\int\frac{1}{\ln x}e^{\int\frac{dx}{x\ln x}}dx+C\right)
=\frac{x+C}{\ln x}.
$$

换回原变量 $u=z^{-1}=\frac{1}{\cos y}$，所求通解为

$$
\frac{1}{\cos y}=\frac{x+C}{\ln x}.
$$"""

_SERIES_QUESTION = (
    r"(1) 设函数 $f(x)=\sin^2 x\sin 2x$，讨论 $f(x)$ 在区间 $(0,\pi)$ 的单调性；"
    r"(2) 判断级数 $\sum_{n=1}^{\infty}a_n$ 的收敛性，其中 "
    r"$a_n=\sin^2 x\sin^2 2x\cdots\sin^2 2^n x$，$x\in(-\infty,+\infty)$。"
)

_SERIES_ANSWER = (
    r"(1) $f(x)$ 在 $\left(0,\frac{\pi}{3}\right)$ 与 "
    r"$\left(\frac{2\pi}{3},\pi\right)$ 上单调增加，在 "
    r"$\left(\frac{\pi}{3},\frac{2\pi}{3}\right)$ 上单调减少；"
    r"(2) 级数 $\sum_{n=1}^{\infty}a_n$ 收敛。"
)

_SERIES_SOLUTION = r"""(1)

$$
f'(x)=2\sin^2x(3-4\sin^2x)
=-8\sin^2x\left(\sin x-\frac{\sqrt3}{2}\right)
\left(\sin x+\frac{\sqrt3}{2}\right).
$$

令 $f'(x)=0$，在 $(0,\pi)$ 内得 $x_1=\frac{\pi}{3}$，$x_2=\frac{2\pi}{3}$。
因此 $f(x)$ 在 $\left(0,\frac{\pi}{3}\right)$ 与
$\left(\frac{2\pi}{3},\pi\right)$ 上单调增加，在
$\left(\frac{\pi}{3},\frac{2\pi}{3}\right)$ 上单调减少。

(2) $f(x)$ 的周期为 $\pi$，$f(0)=f(\pi)=0$，且

$$
f\left(\frac{\pi}{3}\right)=\frac{3\sqrt3}{8},\qquad
f\left(\frac{2\pi}{3}\right)=-\frac{3\sqrt3}{8},
$$

故 $|f(x)|\leqslant\frac{3\sqrt3}{8}$。于是

$$
a_n^{3/2}
=|\sin x|\cdot|f(x)f(2x)\cdots f(2^{n-1}x)|\cdot|\sin^2 2^n x|
\leqslant\left(\frac{3\sqrt3}{8}\right)^n,
$$

从而

$$
a_n\leqslant\left(\frac{3\sqrt3}{8}\right)^{2n/3}
=\left(\frac34\right)^n.
$$

由比较判别法，级数 $\sum_{n=1}^{\infty}a_n$ 收敛。"""


def problems() -> list[dict]:
    """返回第十七届初赛 A/B 卷的原始记录（未去重、未打标签）。"""
    return [
        # ---------- A 卷 ----------
        _record(
            17, "A", 1,
            "设函数 $f(x)=x-\\ln(1+x)-\\frac12x\\sin x$，$g(x)=ax^3$，"
            "且满足 $\\lim_{x\\to0}\\frac{f(x)}{g(x)}=1$，则 $a=$ ____。",
            "$a=-\\dfrac13$。",
            r"""由 Taylor 展开

$$
\ln(1+x)=x-\frac{x^2}{2}+\frac{x^3}{3}+o(x^3),\qquad
\sin x=x-\frac{x^3}{6}+o(x^3),
$$

得

$$
f(x)=-\frac13x^3+o(x^3).
$$

于是

$$
1=\lim_{x\to0}\frac{f(x)}{g(x)}
=\lim_{x\to0}\frac{-\frac13x^3+o(x^3)}{ax^3}
=-\frac{1}{3a},
$$

所以 $a=-\frac13$。""",
        ),
        _record(17, "A", 2, _PLANE_QUESTION, _PLANE_ANSWER, _PLANE_SOLUTION),
        _record(
            17, "A", 3,
            "极限 $\\lim_{x\\to0}\\frac{\\int_0^{\\sin^2x}\\ln(1+t)\\,dt}"
            "{(\\sqrt[3]{1+x^3}-1)\\sin x}=$ ____。",
            "$\\dfrac32$。",
            r"""当 $x\to0$ 时，

$$
\int_0^{\sin^2x}\ln(1+t)\,dt\sim\frac{(\sin^2x)^2}{2}\sim\frac{x^4}{2},
$$

且

$$
(\sqrt[3]{1+x^3}-1)\sin x\sim\frac{x^3}{3}\cdot x=\frac{x^4}{3}.
$$

所以原极限为

$$
\lim_{x\to0}\frac{x^4/2}{x^4/3}=\frac32.
$$""",
        ),
        _record(17, "A", 4, _IMPLICIT_QUESTION, _IMPLICIT_ANSWER, _IMPLICIT_SOLUTION),
        _record(
            17, "A", 5,
            "设曲线 $L$ 为球面 $x^2+y^2+z^2=4$ 与平面 $x+y+z=3$ 的交线，"
            "则曲线积分 $\\int_L(x^2+2xy)\\,ds=$ ____。",
            "$6\\pi$。",
            r"""曲线 $L$ 是球面与平面的交线，关于 $x,y,z$ 对称。由对称性，

$$
\int_L(x^2+2xy)\,ds
=\frac13\int_L(x^2+y^2+z^2)\,ds
+\frac23\int_L(xy+yz+zx)\,ds.
$$

注意到

$$
xy+yz+zx=\frac12\left((x+y+z)^2-(x^2+y^2+z^2)\right),
$$

且在 $L$ 上 $x+y+z=3$，$x^2+y^2+z^2=4$，所以

$$
\int_L(x^2+2xy)\,ds
=\frac13\int_L(x+y+z)^2\,ds
=\frac13\cdot9\cdot2\pi=6\pi.
$$

其中圆 $L$ 的半径为 $1$，周长为 $2\pi$。""",
        ),
        _record(
            17, "A", 6,
            "设 $f(x)$ 在 $(-\\infty,+\\infty)$ 上连续，在 $x=0$ 处可导，且满足 "
            "$f(x)=x^3+x^2\\lim_{x\\to0}\\frac{f(x)}{x}-x\\int_0^1f(x)\\,dx$。"
            "求函数 $f(x)$ 的表达式。",
            "$f(x)=x^3-\\dfrac{3}{22}x^2-\\dfrac{3}{22}x$，"
            "$x\\in(-\\infty,+\\infty)$。",
            r"""由 $f$ 连续且表达式含 $x^2$、$x$ 项，知 $f(0)=0$，于是

$$
a:=\lim_{x\to0}\frac{f(x)}{x}
=\lim_{x\to0}\frac{f(x)-f(0)}{x-0}=f'(0).
$$

记 $b=\int_0^1f(x)\,dx$，则

$$
f(x)=x^3+ax^2-bx.
$$

由此 $a=\lim_{x\to0}\frac{f(x)}{x}=-b$。对等式两边在 $[0,1]$ 上积分：

$$
b=\int_0^1x^3dx+a\int_0^1x^2dx-b\int_0^1x\,dx
=\frac14+\frac a3-\frac b2.
$$

与 $a=-b$ 联立，解得 $a=-\frac{3}{22}$，$b=\frac{3}{22}$。因此

$$
f(x)=x^3-\frac{3}{22}x^2-\frac{3}{22}x,\qquad -\infty<x<+\infty.
$$""",
        ),
        _record(17, "A", 7, _DIFFEQ_QUESTION, _DIFFEQ_ANSWER, _DIFFEQ_SOLUTION),
        _record(
            17, "A", 8,
            "设 $\\Omega$ 是由平面 $\\Pi_1:x=0$，$\\Pi_2:x=y$，$\\Pi_3:y=z$，"
            "$\\Pi_4:z=1$，$\\Pi_5:z=2$ 围成的空间区域，求三重积分 "
            "$\\iiint_\\Omega\\frac{dx\\,dy\\,dz}{\\sqrt{y^2+z^2}}$。",
            "$\\dfrac{3(\\sqrt2-1)}{2}$。",
            r"""采用先二后一法。固定 $1\leqslant z\leqslant2$，截面为

$$
D_z=\{(x,y)\mid 0\leqslant x\leqslant z,\ 0\leqslant y\leqslant x\}.
$$

于是

$$
\iiint_\Omega\frac{dx\,dy\,dz}{\sqrt{y^2+z^2}}
=\int_1^2dz\int_0^zdy\int_0^y\frac{dx}{\sqrt{y^2+z^2}}
=\int_1^2dz\int_0^z\frac{y}{\sqrt{y^2+z^2}}dy.
$$

计算得

$$
=\int_1^2\left.\sqrt{y^2+z^2}\right|_0^z dz
=(\sqrt2-1)\int_1^2z\,dz
=\frac{3(\sqrt2-1)}{2}.
$$""",
        ),
        _record(17, "A", 9, _SERIES_QUESTION, _SERIES_ANSWER, _SERIES_SOLUTION),
        _record(
            17, "A", 10,
            "设函数 $f(x),g(x)$ 在 $[a,b]$ 上连续可导，且满足："
            "(i) $\\int_a^bf(x)\\,dx=\\int_a^bg(x)\\,dx=0$；"
            "(ii) $f'(x)g(x)-f(x)g'(x)>0$，$x\\in[a,b]$；"
            "(iii) $g(x)$ 在 $(a,b)$ 上有唯一零点 $\\xi$。证明："
            "(1) $\\int_a^bf(x)\\left(\\int_a^xg(s)\\,ds\\right)dx"
            "=-\\int_a^bg(x)\\left(\\int_a^xf(s)\\,ds\\right)dx$；"
            "(2) $\\int_a^bf(x)\\left(\\int_a^xg(s)\\,ds\\right)dx>0$。",
            "证明见参考解答。",
            r"""(1) 记

$$
F(x)=\int_a^xf(s)\,ds,\qquad G(x)=\int_a^xg(s)\,ds.
$$

由分部积分，并利用 $F(b)=G(b)=0$，

$$
\int_a^bf(x)G(x)\,dx
=\int_a^bG(x)\,dF(x)
=-\int_a^bF(x)g(x)\,dx.
$$

(2) 由条件 (ii)，$\left(\frac{f}{g}\right)'=\frac{f'g-fg'}{g^2}>0$，
故 $\frac{f}{g}$ 在 $[a,\xi)$ 与 $(\xi,b]$ 上分别严格单调递增。

不妨设 $g'(\xi)>0$，则 $g(x)<0$ 于 $[a,\xi)$，$g(x)>0$ 于 $(\xi,b]$。
将二重积分区域

$$
D=\{(x,s)\mid a\leqslant s\leqslant x\leqslant b\}
$$

分成 $D_1=\{a\leqslant s\leqslant x\leqslant\xi\}$，
$D_2=\{a\leqslant s\leqslant\xi\leqslant x\leqslant b\}$，
$D_3=\{\xi\leqslant s\leqslant x\leqslant b\}$，则

$$
\int_a^bf(x)\left(\int_a^xg(s)\,ds\right)dx
=\frac12\iint_D\bigl(f(x)g(s)-f(s)g(x)\bigr)\,dx\,ds.
$$

在 $D_1\cup D_3$ 上，$f(x)g(s)-f(s)g(x)\geqslant0$ 且不恒为零；
在 $D_2$ 上，利用 $\int_a^bf=\int_a^bg=0$ 可算得该二重积分为 $0$。
综上，

$$
\int_a^bf(x)\left(\int_a^xg(s)\,ds\right)dx>0.
$$""",
        ),

        # ---------- B 卷 ----------
        _record(
            17, "B", 1,
            "已知函数 $y=x^2e^{\\sin x}$，则 $y''(0)=$ ____。",
            "$2$。",
            r"""$y'=e^{\sin x}(2x+x^2\cos x)$，

$$
y''=e^{\sin x}\left[(2+2x\cos x-x^2\sin x)+(2x+x^2\cos x)\cos x\right].
$$

代入 $x=0$，得 $y''(0)=2$。""",
        ),
        _record(17, "B", 2, _PLANE_QUESTION, _PLANE_ANSWER, _PLANE_SOLUTION),
        _record(
            17, "B", 3,
            "设 $f(x)$ 连续，且 $f(x)+f(-x)=2$。求 "
            "$\\int_{-1}^1\\left(f(x)+x^2\\ln(x+\\sqrt{x^2+1})\\right)dx=$ ____。",
            "$2$。",
            r"""因为 $y=\ln(x+\sqrt{x^2+1})$ 是奇函数，所以

$$
\int_{-1}^1x^2\ln(x+\sqrt{x^2+1})\,dx=0.
$$

于是原积分等于

$$
\int_{-1}^1f(x)\,dx
=\int_0^1\bigl(f(x)+f(-x)\bigr)dx
=\int_0^1 2\,dx=2.
$$""",
        ),
        _record(17, "B", 4, _IMPLICIT_QUESTION, _IMPLICIT_ANSWER, _IMPLICIT_SOLUTION),
        _record(
            17, "B", 5,
            "积分 $\\int_1^{+\\infty}\\frac{\\ln x}{(1+x)^2}\\,dx=$ ____。",
            "$\\ln2$。",
            r"""分部积分：

$$
\int_1^{+\infty}\frac{\ln x}{(1+x)^2}dx
=\int_1^{+\infty}\ln x\,d\left(-\frac{1}{1+x}\right)
=\int_1^{+\infty}\frac{dx}{x(1+x)}.
$$

而

$$
\int_1^{+\infty}\frac{dx}{x(1+x)}
=\int_1^{+\infty}\left(\frac1x-\frac1{x+1}\right)dx
=\ln2.
$$""",
        ),
        _record(
            17, "B", 6,
            "设 $a,b$ 为常数，函数 $f(x,y)=ax^2+by^2+1$ 在点 $(4,3)$ 处的所有方向导数中，"
            "沿方向 $\\mathbf l=-4\\mathbf i-3\\mathbf j$ 的方向导数最大，最大值为 $10$。"
            "(1) 求 $a,b$ 的值；(2) 求曲面 $z=f(x,y)$ 被平面 $z=0$ 所截下的有限部分的面积。",
            "(1) $a=b=-1$；(2) $\\dfrac{\\pi}{6}(5\\sqrt5-1)$。",
            r"""(1) 梯度为

$$
\operatorname{grad}f|_{(4,3)}=(8a,6b).
$$

方向导数在 $\mathbf l=(-4,-3)$ 方向最大，故 $(8a,6b)$ 与 $(-4,-3)$ 同向，且模长为 $10$：

$$
\frac{8a}{-4}=\frac{6b}{-3}>0,\qquad
\sqrt{(8a)^2+(6b)^2}=10.
$$

解得 $a=b=-1$。

(2) 曲面为 $z=1-x^2-y^2$，投影区域 $D:x^2+y^2\leqslant1$。面积为

$$
A=\iint_D\sqrt{1+4x^2+4y^2}\,dx\,dy
=\int_0^{2\pi}d\theta\int_0^1\sqrt{1+4r^2}\,r\,dr
=\frac{\pi}{6}(5\sqrt5-1).
$$""",
        ),
        _record(
            17, "B", 7,
            "求由方程 $2x^2+2y^2+z^2+8xz-z+8=0$ 所确定的函数 $z=z(x,y)$ 的极值。",
            "在 $(-2,0)$ 处取得极小值 $z=1$；在 $\\left(\\dfrac{16}{7},0\\right)$ 处取得极大值 "
            "$z=-\\dfrac87$。",
            r"""方程两边分别对 $x,y$ 求偏导：

$$
4x+2zz_x+8z+8xz_x-z_x=0,\qquad
4y+2zz_y+8xz_y-z_y=0.
$$

解得

$$
z_x=-\frac{4x+8z}{2z+8x-1},\qquad
z_y=-\frac{4y}{2z+8x-1}.
$$

令 $z_x=z_y=0$，得 $x=-2z,\ y=0$。代回原方程：

$$
7z^2+z-8=0,
$$

故 $z_1=1,\ z_2=-\frac87$，驻点为 $(-2,0)$ 与 $\left(\frac{16}{7},0\right)$。

继续求二阶偏导并代入，在两个驻点都有 $B^2-AC<0$。
在 $(-2,0)$ 处 $A=C=\frac{4}{15}>0$，取极小值 $z=1$；
在 $\left(\frac{16}{7},0\right)$ 处 $A=C=-\frac{4}{15}<0$，取极大值 $z=-\frac87$。""",
        ),
        _record(17, "B", 8, _DIFFEQ_QUESTION, _DIFFEQ_ANSWER, _DIFFEQ_SOLUTION),
        _record(17, "B", 9, _SERIES_QUESTION, _SERIES_ANSWER, _SERIES_SOLUTION),
        _record(
            17, "B", 10,
            "设 $f(x)$ 在 $[a,b]$ 上有连续的导数，记 "
            "$A=\\frac{1}{b-a}\\int_a^bf(x)\\,dx$。证明："
            "$\\int_a^b|f(x)-A|^2dx\\leqslant"
            "\\frac{(b-a)^2}{2}\\int_a^b|f'(x)|^2dx$。",
            "证明见参考解答。",
            r"""由积分中值定理，存在 $c\in[a,b]$，使

$$
A=\frac{1}{b-a}\int_a^bf(x)\,dx=f(c).
$$

由 Newton-Leibniz 公式与 Cauchy 积分不等式，

$$
|f(x)-A|^2
=|f(x)-f(c)|^2
=\left(\int_c^xf'(t)\,dt\right)^2
\leqslant|x-c|\int_a^b|f'(t)|^2dt.
$$

于是

$$
\int_a^b|f(x)-A|^2dx
\leqslant\int_a^b|x-c|\,dx\int_a^b|f'(x)|^2dx.
$$

而

$$
\int_a^b|x-c|\,dx
=\frac12(a-c)^2+\frac12(b-c)^2
=\frac12\left[(a-c)^2+(b-c)^2\right].
$$

令 $g(t)=(a-t)^2+(b-t)^2$，它在 $[a,b]$ 上的最大值为 $(a-b)^2$，故

$$
\int_a^b|x-c|\,dx\leqslant\frac{(b-a)^2}{2}.
$$

从而

$$
\int_a^b|f(x)-A|^2dx
\leqslant\frac{(b-a)^2}{2}\int_a^b|f'(x)|^2dx.
$$""",
        ),
    ]
