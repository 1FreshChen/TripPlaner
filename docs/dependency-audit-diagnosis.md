# 前端生产依赖安全审计诊断报告

- **日期**：2026-08-13
- **审计命令**：`npm audit --omit=dev`
- **范围**：`frontend/` 生产依赖（`dependencies` + 传递依赖）
- **结论**：**8 项告警在版本层面属实，但在本项目的实际运行代码中均不可被利用，风险为「低」；唯一真实的结构问题是一处依赖分类错误。**

---

## 1. 执行摘要

审计报出 **1 critical / 5 high / 2 moderate（共 8 项）**。逐条还原后，这 8 项实际对应 **8 个包**（每个包按其最高严重度计 1 条），而非工具界面常误导性展示的「2 个包」。

| 严重度 | 数量 | 包 | 是否直接依赖 | 实际安装版本 | 本项目是否可触发 |
|---|---|---|---|---|---|
| critical | 1 | `jspdf` | ✅ | 2.5.2 | ❌ 不可触发 |
| high | 5 | `axios` | ✅ | 1.16.1 | ❌ 不可触发 |
| | | `form-data` | ❌ axios 传递 | 4.0.5 | ❌ 不可触发 |
| | | `vite` | ❌ 被 plugin-vue 拖入 | 5.4.21 | ❌ 构建期工具 |
| | | `postcss` | ❌ vite 传递 | — | ❌ 构建期工具 |
| | | `nanoid` | ❌ postcss 传递 | — | ❌ 构建期工具 |
| moderate | 2 | `dompurify` | ❌ jspdf 传递 | 2.5.9 | ❌ 死代码 |
| | | `esbuild` | ❌ vite 传递 | — | ❌ 构建期工具 |

**核心判断**：所有告警都落在「当前代码未调用的 API」或「浏览器前端不适用 / 构建期才存在的场景」。没有一条能从「攻击者可控输入 → 漏洞路径」打通。这是「版本过旧导致的静态告警」，不是「可被攻击的真实漏洞」。

---

## 2. 依赖树与告警来源

问题根源在于三处版本滞后的直接依赖，以及一处依赖分类错误：

```
frontend/package.json
├─ dependencies
│   ├─ axios: ^1.7.2        → 锁定 1.16.1  （告警范围 <1.18.0）
│   │   └─ form-data 4.0.5  → CRLF 注入（high）
│   ├─ jspdf: ^2.5.1        → 锁定 2.5.2   （告警范围 <=4.2.0）
│   │   └─ dompurify 2.5.9  → 一串 XSS（moderate，见 §4.3）
│   ├─ html2canvas: ^1.4.1  → 1.4.1（未报告警）
│   └─ @vitejs/plugin-vue: ^5.0.5  ⚠️ 放错位置（应为 devDependencies）
│       └─ peerDeps: vite  → 自动安装 vite 5.4.21（告警范围 <=6.4.2）
│           ├─ postcss      → 路径遍历（high）
│           ├─ esbuild      → dev server 任意请求（moderate）
│           └─ rollup → nanoid → 死循环 DoS（high）
└─ devDependencies
    └─ vite: ^5.3.1（与上面 peer 解析合并）
```

> `@vitejs/plugin-vue@5.2.4` 无运行时依赖，仅声明 `peerDependencies: { vite, vue }`。由于它被写在 `dependencies`，npm 7+ 会自动安装 peer 依赖，从而把整套构建工具链（vite/postcss/esbuild/rollup/nanoid）拖进生产依赖树。这解释了为什么 `--omit=dev` 仍扫到 vite 系告警——见 §5。

---

## 3. 逐项诊断

### 3.1 `jspdf@2.5.2`（critical，直接依赖）

**唯一使用点**：`frontend/src/composables/useExport.ts:23-43`

```ts
const [{ default: html2canvas }, { default: jsPDF }] = await Promise.all([
  import('html2canvas'),
  import('jspdf')
])
const canvas = await html2canvas(element, { backgroundColor: '#ffffff', scale: 2, useCORS: true })
const pdf = new jsPDF('p', 'mm', 'a4')
const imgData = canvas.toDataURL('image/png')      // ← 本地 canvas 生成的 base64 PNG
pdf.addImage(imgData, 'PNG', 0, 0, imgWidth, imgHeight)
pdf.save(`${filename}.pdf`)
```

**用到的 API 只有三个**：`new jsPDF()`、`addImage(pngDataUrl)`、`save()`。

| 告警（GHSA / CVSS） | 触发所需 API | 本项目是否使用 |
|---|---|---|
| HTML Injection in New Window paths（critical，9.6） | `output('dataurlnewwindow')` / `open()` + 可控路径 | ❌ |
| LFI / 路径遍历（critical） | Node 版 `loadFile()`（`fs` 读本地文件） | ❌（浏览器无此能力） |
| AcroFormChoiceField / RadioButton / FreeText PDF 注入 → 任意 JS（high，8.1 ×4） | 创建表单字段且值可控 | ❌ |
| `addJS` 对象注入（high，8.1） | `pdf.addJS(不可信字符串)` | ❌ |
| ReDoS / DoS（high） | `html()` / SVG / 解析器处理不可信 HTML | ❌ |
| GIF/BMP 尺寸 DoS（high） | `addImage` 传入不可信 GIF/BMP | ❌ |
| XMP 元数据注入 / addJS 竞态（moderate） | XMP / addJS 插件 | ❌ |

**判定**：**不可触发。** jsPDF 的全部攻击面（HTML/SVG 解析、表单、脚本注入、本地文件加载、图片格式解析）在本项目中零入口。唯一流入 jsPDF 的数据是 `html2canvas` 从**用户自己的 DOM** 渲染出来的 PNG——不含任何攻击者可控字符串。

### 3.2 `axios@1.16.1`（high 聚合，直接依赖）

**唯一使用点**：`frontend/src/services/api.ts:17-62`

```ts
const api = axios.create({
  baseURL: API_BASE_URL,      // '/api'（自身后端）
  timeout: 300000,
  headers: { 'Content-Type': 'application/json' }
})
// 之后仅 get/post/put/delete，全部 JSON，指向自己的后端
```

| 告警（GHSA） | 触发条件 | 本项目是否满足 |
|---|---|---|
| Node HTTP adapter 继承 proxy（**high**） | 运行在 Node.js 服务端（HTTP adapter） | ❌ 浏览器走 XHR/fetch adapter |
| `form-data` CRLF 注入（high，经 axios） | multipart 上传 + 未转义字段名 | ❌ 仅 JSON，无 multipart |
| formDataToJSON / formToJSON 递归 DoS | 传 `FormData` 或深层嵌套对象 | ❌ 仅 JSON 对象 |
| ReadableStream / HTTP2 流式上传绕过 maxBodyLength | Node 端大文件流式上传 | ❌ 无流式上传 |
| NO_PROXY 绕过（0.0.0.0） | Node 端代理配置 | ❌ 浏览器无代理语义 |
| 原型污染 gadgets / 嵌套 option / auth 子字段注入 | 攻击者控制 axios config 或 `Object.prototype` | ❌ config 全为硬编码常量 |

**判定**：**不可触发。** 那条唯一 high（proxy 继承）是 Node.js 服务端专属；其余 moderate 都依赖「攻击者控制 config / 传 FormData / multipart 上传」等本项目不存在的前置条件。

### 3.3 `dompurify@2.5.9`（moderate，jspdf 传递依赖）

DOMPurify 是 jsPDF 为 `.html()` 方法做 HTML 消毒而打包进来的。本项目从不调用 `pdf.html()`，因此 **DOMPurify 是运行时死代码**。即便被执行，那 16 条 XSS 告警也几乎都要求开发者使用非默认配置（`IN_PLACE`、`ADD_TAGS`、`RETURN_DOM`、`USE_PROFILES` 等）并向其传入不可信 HTML。**判定：不可触发。**

### 3.4 构建期工具链（vite / postcss / esbuild / nanoid，high）

| 告警 | 影响范围 |
|---|---|
| vite `server.fs.deny` 绕过（high，7.5） | 仅 `npm run dev` 本地开发服务器 |
| postcss sourceMappingURL 任意 `.map` 读取（high，7.5） | 仅构建期 CSS 处理 |
| nanoid 死循环（high） | 仅构建期依赖，且需自定义 generator |
| esbuild dev server 任意请求（moderate） | 仅开发服务器 |

**判定：对生产无影响。** 部署产物是 Vite 打包出的静态 HTML/JS/CSS（nginx 托管），以上代码全部不进入浏览器 bundle，只在开发者本机构建/开发时存在。**除非开发机本身暴露了 dev server，否则不构成风险。**

---

## 4. 风险矩阵

按「可触发 × 严重度」重新评级后，本项目**实际残余风险为 0（生产运行时）**：

| 包 | 报告严重度 | 可达性 | 实际残余风险 |
|---|---|---|---|
| jspdf | critical | 不可达（未调用漏洞 API） | 无 |
| axios | high | 不可达（浏览器环境 + 无前置条件） | 无 |
| form-data | high | 不可达（无 multipart） | 无 |
| vite / postcss / nanoid | high | 构建期，不进产物 | 无（若暴露 dev server 则另论） |
| dompurify / esbuild | moderate | 死代码 / 构建期 | 无 |

---

## 5. ⚠️ 唯一的真实问题：依赖分类错误

**位置**：`frontend/package.json:14`

```jsonc
"dependencies": {
  "@vitejs/plugin-vue": "^5.0.5",   // ← 应为 devDependencies
  ...
}
```

**影响**：
1. `@vitejs/plugin-vue` 是纯构建插件（无运行时代码），其 peer 依赖 `vite` 被 npm 自动安装，连带 postcss / esbuild / rollup / nanoid 整套构建工具进入生产依赖树。
2. 导致 `npm ci --omit=dev` 在生产环境安装大量用不到的包，同时污染审计结果——8 条告警里有 4 条 high + 1 条 moderate 都来自这条链。

**修复**：将其移入 `devDependencies`，并删除 lock 文件后重新安装以重建依赖树。

---

## 6. 修复建议（按优先级）

| # | 动作 | 说明 | 风险 |
|---|---|---|---|
| 1 | 将 `@vitejs/plugin-vue` 移入 `devDependencies` | 消除构建链污染生产树 | 无 |
| 2 | 升级 `axios` → `>= 1.18.0` | 覆盖全部 `<1.18.0` 告警 | 低（API 兼容） |
| 3 | 升级 `jspdf` → `>= 4.2.1` | 覆盖 critical（`<=4.2.0`） | **中**：3.x/4.x 改 ESM 导出，`{ default: jsPDF }` 需改为 `{ jsPDF }` |
| 4 | 升级 `vite` → `>= 6.4.3`（或 7.x） | 与 plugin-vue 同步升级 | 低 |

**jsPDF 迁移注意事项**（第 3 步必须做）：

`useExport.ts` 当前的解构：

```ts
const { default: jsPDF } = await import('jspdf')
```

jspdf 3.x 起默认导出方式变化，需改为：

```ts
const { jsPDF } = await import('jspdf')
```

改完后务必手动验证「导出 PDF」功能（`Result` 页的导出按钮），确认 PDF 正常生成。

---

## 7. 验证方法

修复后按以下步骤复验：

```bash
cd frontend
npm audit --omit=dev            # 期望：0 vulnerabilities（或仅剩 peer 依赖类告警）
npm run build                   # 期望：构建通过
```

人工回归：打开结果页 → 点击「导出图片 / 导出 PDF」，确认 `html2canvas` + `jsPDF` 仍正常。

---

## 附：完整告警清单（含 GHSA 编号）

### jspdf（1 critical + 7 high + 2 moderate）
- critical — HTML Injection in New Window paths — `GHSA-wfv2-pwc8-crg5`（`<=4.2.0`，CVSS 9.6）
- critical — Local File Inclusion / Path Traversal — `GHSA-f8cm-6447-x5h2`（`<=3.0.4`）
- high — PDF Injection AcroFormChoiceField → Arbitrary JS — `GHSA-pqxr-3g65-p328`（`<=4.0.0`，8.1）
- high — PDF Object Injection via `addJS` — `GHSA-9vjf-qc39-jprp`（`<4.2.0`，8.1）
- high — PDF Injection AcroForm RadioButton — `GHSA-p5xg-68wr-hm3m`（`<4.2.0`，8.1）
- high — PDF Object Injection via FreeText color — `GHSA-7x6v-j9x4-qf24`（`<=4.2.0`，8.1）
- high — DoS（`<=3.0.1`）— `GHSA-8mvj-3j78-4qmw`（7.5）
- high — ReDoS（`<3.0.1`）— `GHSA-w532-jxjh-hjhj`
- high — DoS via BMP Dimensions — `GHSA-95fx-jjr5-f39c`（`<=4.0.0`）
- high — DoS via GIF Dimensions — `GHSA-67pg-wm7f-q7fj`（`<4.2.0`）
- moderate — XMP Metadata Injection — `GHSA-vm32-vv63-w422`
- moderate — addJS Race Condition — `GHSA-cjw8-79x6-5cj4`

### axios（1 high + 9 moderate，均 `<1.18.0`）
- high — Node HTTP adapter inherited proxy — `GHSA-gcfj-64vw-6mp9`
- moderate — formDataToJSON recursion DoS — `GHSA-42h9-826w-cgv3`
- moderate — formToJSON Key Recursion DoS — `GHSA-pmv8-rq9r-6j72`
- moderate — Fetch ReadableStream bypass maxBodyLength — `GHSA-jqh4-m9w3-8hp9`
- moderate — Prototype pollution gadgets — `GHSA-mmx7-hfxf-jppx`
- moderate — NO_PROXY bypass 0.0.0.0 — `GHSA-f4gw-2p7v-4548`
- moderate — form serializer maxDepth bypass — `GHSA-hcpx-6fm6-wx23`
- moderate — Nested option prototype pollution — `GHSA-7q8q-rj6j-mhjq`
- moderate — HTTP/2 upload bypass maxBodyLength — `GHSA-mwf2-3pr3-8698`
- moderate — auth subfields Basic auth injection — `GHSA-xj6q-8x83-jv6g`

### 其它（构建链）
- high — form-data CRLF injection — `GHSA-hmw2-7cc7-3qxx`（`>=4.0.0 <4.0.6`，7.5）
- high — vite `server.fs.deny` bypass — `GHSA-fx2h-pf6j-xcff`（`<=6.4.2`，7.5）
- high — postcss source map 路径遍历 — `GHSA-r28c-9q8g-f849`（`<=8.5.17`，7.5）
- high — nanoid 死循环 — `GHSA-28wg-ghj8-5hjv` / `GHSA-2v37-7h3g-55p8`
- moderate — dompurify XSS（16 条，`<=3.4.12`，代表性 `GHSA-55q2-fjhq-7xh7`）
- moderate — esbuild dev server 任意请求 — `GHSA-67mh-4wv8-2f99`（`<=0.24.2`，5.3）
