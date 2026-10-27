# 前端第三方库

这些文件是从 npm 包中复制到仓库里的，前端不依赖 CDN，离线也能正常渲染。

| 目录 | 包 | 版本 | 许可证 |
| --- | --- | --- | --- |
| `katex/` | [KaTeX](https://katex.org/) | 0.19.1 | MIT |
| `markdown-it/` | [markdown-it](https://github.com/markdown-it/markdown-it) | 15.0.3 | MIT |
| `markdown-it-texmath/` | [markdown-it-texmath](https://github.com/goessner/markdown-it-texmath) | 1.0.0 | MIT |
| `dompurify/` | [DOMPurify](https://github.com/cure53/DOMPurify) | 3.4.17 | Apache-2.0 OR MPL-2.0 |

升级时替换对应目录即可；`markdown-it-texmath` 是 CommonJS 包，页面里先用
`window.module` 垫片加载，再把 `module.exports` 挂到 `window.texmath`。
