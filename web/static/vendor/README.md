# 前端第三方库

这些文件是从 npm 包中复制到仓库里的，前端不依赖 CDN，离线也能正常渲染。

| 目录 | 包 | 版本 | 许可证 |
| --- | --- | --- | --- |
| `katex/` | [KaTeX](https://katex.org/) | 0.19.1 | MIT |
| `marked/` | [marked](https://github.com/markedjs/marked) | 18.1.2 | MIT |
| `dompurify/` | [DOMPurify](https://github.com/cure53/DOMPurify) | 3.4.17 | Apache-2.0 OR MPL-2.0 |

升级时替换对应目录即可。公式渲染不用 markdown 插件：`app.js` 先把 `$...$`、
`$$...$$` 换成占位符，交给 marked 渲染正文，再用 KaTeX 还原公式，避免
markdown 把 LaTeX 里的 `_`、`*` 当成排版符号。
