# FileTools

简单、快速的在线文件处理工具。核心理念：

> 上传文件 → 选择操作 → 自动处理 → 下载结果

当前进度：**第十一阶段最终收尾已完成（FINAL HARDENING PASSED）**。
这一轮**没有新增任何功能**，做的全是「让已经建好的四平台产品真的能交付」：

- 修掉 CI 上那条一直红的**损坏文档归类**：同一个损坏的 `.docx`，`soffice` 在 Windows 上
  退出码是 1、在 Linux 上是 0，而旧代码只押退出码 —— 于是 Linux 上把「文件已损坏」错报成
  「转换失败」。判据换成了与平台无关的一条（源文件主部件是否良构），并补了**反向护栏测试**，
  防止矫枉过正地把所有失败都算成 400。
- 把**版本**与**品牌**钉成唯一真源（仓库根 `VERSION` + `branding/source/`），
  四个平台的消费方全部由脚本写入、由验收脚本逐项断言相等。
- 安全防线（图片炸弹 / SVG / HTML 注入与 SSRF / 文件名）**每一条都有自动化测试**，
  不是只写在代码里。
- 新增 `scripts/verify_final.py` 作为**最终总验收入口**：环境够不着的项输出 `NOT EXECUTED`
  并写明原因，**绝不写成 PASS**。
- **CI 在 2026-10-08 首次全绿**（此前连着六次红）。修掉的那条是**测试自己的判据**写错了 ——
  它把「响应里不许出现 `application/json`」当成了「接口还在」的判据，而关掉接口之后收到什么
  其实取决于**部署形态**（有没有前端产物）。详见 [CI 覆盖什么](#ci-覆盖什么)。

**四平台产品：Web / Android / iOS / Windows。** 完整的逐项矩阵见[四平台总览](#四平台总览)，
环境受限的两项（Android 真机、iOS）在那里如实标出。

本轮最终收尾的结论是 **`FINAL HARDENING PASSED`**，但它**不**等于「四个平台全部验证完毕」——
紧跟着还有一句 **`Environment-limited verification`**。逐项的结论、证据与**没做到的部分**
写在下面的[当前支持平台](#当前支持平台)与[当前真实状态](#当前真实状态)两节里。

---

上一轮：**第十一阶段 A 的补充需求已完成（PHASE 11A-DESKTOP COMPLETE）** ——
新增 **Windows 桌面版**并统一了**四平台品牌**。

- **Windows 桌面版**：基于 **Tauri 2**，直接复用 Web 前端产物（不改一行 UI），真机产出并实测
  `FileTools-Setup-x64.exe`。**桌面端不含任何转换引擎** —— 它只是第四个客户端，
  和网页、手机一样调用同一个后端。
- **统一品牌**：全平台只有**一份** Logo 真源（`branding/source/`），四个平台的图标与 favicon
  全部由它生成；**一份**版本真源（仓库根的 `VERSION`）。

此前**第十一阶段 A 已完成（PHASE 11A COMPLETE）** —— 新增 **React Native（Expo）移动 App**
（四个底部标签页、能力驱动的动态工具列表、纯本地历史）。
后端**一行没改**：移动端只是**又一个客户端**，转换仍由同一套 API 在同一套队列 / Worker 上完成，
没有第二份「文件处理真相」。

更早的**第十阶段已封板（PHASE 10 COMPLETE）**：**19 种格式、70 条转换、7 个 PDF 操作**，
覆盖图片、PDF、Office 文档三条主线，以及把它们统一起来的「统一转换中心」与底层的任务队列 / Worker 并发架构。
前十阶段的接口与界面全部保持兼容。

---

## 目录

- [环境要求](#环境要求)
- [可选组件（HEIC / OCR）](#可选组件heic--ocr)
- [快速开始](#快速开始)
- [生产模式部署](#生产模式部署)
- [已实现的功能](#已实现的功能)
- [统一转换中心](#统一转换中心)
- [批量处理与任务队列](#批量处理与任务队列)
- [移动 App（Expo / React Native）](#移动-appexpo--react-native)
- [Windows 桌面版（Tauri）](#windows-桌面版tauri)
- [品牌资产](#品牌资产)
- [四平台总览](#四平台总览)
- [技术栈](#技术栈)
- [项目结构](#项目结构)
- [压缩是怎么工作的](#压缩是怎么工作的)
- [格式转换与图片几何操作是怎么工作的](#格式转换与图片几何操作是怎么工作的)
- [PDF 工具是怎么工作的](#pdf-工具是怎么工作的)
- [文档转换是怎么工作的](#文档转换是怎么工作的)
- [PDF 转 Word 是怎么工作的](#pdf-转-word-是怎么工作的)
- [API 说明](#api-说明)
- [配置项](#配置项)
- [安全措施](#安全措施)
- [测试](#测试)
- [已知限制](#已知限制)
- [常见问题](#常见问题)
- [后续阶段](#后续阶段)
- [许可证](#许可证)

---

## 环境要求

| 组件 | 版本 | 本项目验证环境 | 必需？ |
| --- | --- | --- | --- |
| Python | 3.10 及以上 | 3.14.5 | 必需 |
| Node.js | 18 及以上 | 24.15.0 | 仅构建前端 / 跑移动 App 时需要 |
| npm | 9 及以上 | 11.12.1 | 仅构建前端 / 跑移动 App 时需要 |
| LibreOffice | 7.0 及以上 | 26.2.5 | **可选**，只有 Word / Excel / PPT 转 PDF 需要 |
| pillow-heif | 1.8.0 | 已装 | **可选**，只有 HEIC 需要 |
| rapidocr-onnxruntime | 1.2.3 | 已装 | **可选**，只有 PDF→Word 的扫描件 OCR 需要 |
| Playwright | — | 已装 | 仅验收脚本需要 |

**只有构建 Windows 桌面版才需要的组件**（跑 Web / 后端 / 移动端都不需要）：

| 组件 | 版本 | 本项目验证环境 | 说明 |
| --- | --- | --- | --- |
| Rust | 1.77 及以上 | 1.98.1 | Tauri 的编译器；host 必须是 `x86_64-pc-windows-msvc` |
| Visual Studio Build Tools | 2022 及以上 | 2026，`cl.exe` / `link.exe` 14.51.36231 | 提供 MSVC 链接器（`cl.exe` 不必在 PATH 里，rustc 自己找） |
| Windows SDK | 10 及以上 | 10.0.26100.0 | 提供 `rc.exe`（编译资源） |
| WebView2 Runtime | — | 154.0.4258.53 | 桌面版的运行时；Win11 与较新的 Win10 自带，安装包内也带了引导程序 |
| NSIS | — | 3.11 | **由 Tauri 首次打包时自动下载**，不需要手动装 |
| `@tauri-apps/cli` | ^2 | 2.12.1 | `desktop/` 的 devDependency，`npm install` 时随 npm 预编译二进制装上 |

> OCR 引擎用的是 **rapidocr-onnxruntime**（纯 pip 安装、自带中英文模型），不是 Tesseract ——
> 换引擎只需要改 `services/ocr_service.py` 里的 `_load_engine`。
>
> 桌面版**没有引入任何新的 Python 或前端运行时依赖**：前端仍然零新增 npm 包
> （Tauri 的 API 由 `withGlobalTauri` 暴露成全局对象），新增的只有 Tauri 这条构建链。

### LibreOffice 安装要求

只有 **Word / Excel / PowerPoint 转 PDF** 需要它。图片工具、PDF 工具、TXT 转 PDF 都不需要；
没装的时候这三个页面会明确提示「当前服务器缺少 Office 转换组件，请联系管理员。」
并禁用开始按钮，其余功能完全不受影响。

**为什么必须是完整安装**：转换走的是无界面模式（headless），但需要 `soffice` 可执行文件
**和**它依赖的过滤器组件（`program/` 下的 `*.so` / `*.dll`、`share/` 下的过滤器配置）。
只拷一个 `soffice` 二进制是不行的。绝大多数发行版的官方包都是完整安装，照下面装即可。

**Windows**

```powershell
winget install TheDocumentFoundation.LibreOffice
# 或者从 https://www.libreoffice.org/download/ 下载 .msi 安装
```

默认装在 `C:\Program Files\LibreOffice\program\soffice.com`，本服务会自动找到它。

**Linux（Debian / Ubuntu）**

```bash
sudo apt-get update && sudo apt-get install -y libreoffice --no-install-recommends
```

`--no-install-recommends` 会跳过一堆图形界面依赖，但**会一并跳过部分过滤器**。
如果转换报「文件转换失败」，改用完整的 `sudo apt-get install -y libreoffice`。

**Linux（RHEL / CentOS / Fedora）**

```bash
sudo dnf install -y libreoffice-headless libreoffice-writer libreoffice-calc libreoffice-impress
```

**Docker**

仓库根目录有一份可直接用的 [`Dockerfile`](Dockerfile) 和一份
[`docker-compose.yml`](docker-compose.yml)，配套的 `.dockerignore` 也在
（**别删** —— 没有它，`COPY backend/ backend/` 会把开发机上的
`backend/.venv` 整个拷进 Linux 镜像）。

```bash
# 1. 先构建前端：镜像里不装 Node，dist 必须在宿主机生成
cd frontend && npm install && npm run build && cd ..

# 2a. 用 compose
#     注意：这是整个仓库里唯一一处 .env 会真的被读取的地方 ——
#     是 compose 读了它再注入成容器环境变量，后端进程自己并不读 .env。
#     .env 里只保留你要改的那几行，其余删掉（删掉 = 用程序内置默认值），
#     别把 .env.example 整个复制过来 —— 那会把 65 个默认值全钉死。
cp .env.example .env
docker compose up --build

# 2b. 或者不用 compose
docker build -t filetools .
docker run --rm -p 8000:8000 filetools
```

> ⚠️ **Dockerfile 与 docker-compose.yml 都没有在本项目环境中实测过**
> （开发机是 Windows，没有 Docker）。包名与路径按官方文档写，请以你自己构建的结果为准。
> 特别是 **中文字体**：`python:*-slim` 镜像里一个中文字体都没有，
> 不装 `fonts-noto-cjk` 的话中文 PDF 会全是方框。

**指定路径**

自动查找的顺序是：`FILETOOLS_LIBREOFFICE_PATH` → 常见安装位置 → `PATH` 里的 `soffice` / `libreoffice`。
Windows 上优先 `soffice.com` 而不是 `soffice.exe`（两者是同一个启动器，区别只在 PE 的
console-subsystem 位：`.com` 会阻塞并把输出交给我们，`.exe` 不会）。装在非常规位置时用环境变量指定：

```bash
FILETOOLS_LIBREOFFICE_PATH=/opt/libreoffice/program/soffice uvicorn main:app --port 8000
```

可以指到**目录**（会找目录下的 `soffice`）或**可执行文件**。显式配置了却不存在时，
服务会直接判定为「缺少组件」并如实提示，**不会**偷偷改用自动找到的另一个 ——
部署时路径写错却一直用的是别处版本，比直接报错难查得多。

**验证装好了没**

```bash
curl http://127.0.0.1:8000/api/config | grep -o '"doc_conversion_available":[a-z]*'
# {"doc_conversion_available":true} 表示已就绪
```

---

## 可选组件（HEIC / OCR）

这两项能力各自依赖一个**有额外负担**的组件，因此**刻意不放进 `requirements.txt`**，
而是各自一个可选文件。不装的话服务照常启动、其余功能全部可用，只是能力矩阵里不会出现对应的格子，
界面会**如实说明原因**，不会摆一个点下去必然失败的按钮。

```bash
cd backend
pip install -r requirements-heic.txt   # HEIC
pip install -r requirements-ocr.txt    # OCR（PDF→Word 的扫描件）
```

装完**不需要改配置**：重启服务即可。

### HEIC

**为什么是可选**：HEIC 的全部能力来自 `pillow-heif` —— **Pillow 12 本体一行 HEIC 代码都没有**
（实测 `Image.OPEN` / `Image.SAVE` 里没有 HEIF，`registered_extensions()` 里没有 `.heic`，
喂一个真实的 `ftypheic` 头只会得到 `UnidentifiedImageError`）。把它排除在基础依赖外有两个理由：

1. **体积** —— wheel 自带 libheif，以及 libde265（解码，约 0.9 MB）与 libx265（编码，约 22 MB）。
2. **许可** —— 见下。基础依赖应该是「装了就一定能用、且不附带额外义务」的那一组。

**探测不是「包在不在」**。`pillow-heif` 把 libheif（容器解析）和编解码器**分开打包**，
于是存在**三种**状态而不是两种：

| 状态 | 解码 | 编码 | 矩阵里会发布什么 |
| --- | --- | --- | --- |
| 包不在 | ✗ | ✗ | 一条 HEIC 格子都没有；界面说明缺少组件 |
| 只带 libde265 | ✓ | ✗ | **只发布解码方向**（`HEIC → 其它`），不出现任何 `其它 → HEIC` |
| 两者都在 | ✓ | ✓ | 双向都发布 |

只带解码器的构建能 `HEIC → JPG`，**不能** `JPG → HEIC`。「包在不在」回答不了「能不能编码」，
所以探测会**真的各做一次**：读一张内置的 8×8 样张（测解码），再自己编一张 2×2 读回来（测编码）。
代价是微秒级（实测整轮 < 5 ms），换来的是绝**不把一个点下去必然失败的 `JPG → HEIC` 摆在界面上**。

> 探测用**真实样张**而不是「编一张再读回来」来测解码，是因为后者有个盲区：
> 只带 libde265 的构建编不出东西，会把解码也误报成不可用 —— 而它其实解得开。

**不可用时的行为**：`conversions[]` / `operations[]` / `matrix` 里一条 HEIC 都没有、`available` 如实为 `false`；
首页 FAQ 的格式清单不含 HEIC（清单是从能力 API 现算的，不是手抄的）；图片工具页不显示不可用的 HEIC 操作并明确说明；
上传 HEIC 并提交时任务**失败并给出中文说明**，不伪造成功、不产出一个改名的空文件；其余格式一个都不受影响。
用户可见的提示里不含 Traceback、模块路径或 Python 异常名。

**许可与分发（事实陈述，不是法律意见）**：`pillow-heif` 的 wheel 自带 libheif，而 libheif 在这份 wheel 里链了
**libde265**（HEVC 解码器）与 **libx265**（HEVC 编码器）。上游许可以各自项目为准，此处只记录事实：

- **libx265 是 GPLv2**。分发链接了 libx265 的二进制会触发 GPLv2 的义务。这一条对「把本服务打包分发」有影响，对「自己部署自己用」通常没有。
- **HEVC 另有专利池**（Access Advance / Via LA 等）。专利许可与软件著作权许可是两件事，前者不因为代码是开源的而消失。

本仓库自身以 **AGPL-3.0** 发布（见[许可证](#许可证)），但**这不解决上面两条** ——
「本仓库的代码用什么许可」与「它链接的第三方编解码器是什么许可」是两件独立的事：
AGPL-3.0 不会让 libx265 的 GPLv2 义务消失，也不会让 HEVC 的专利许可消失。
因此这里仍不做任何结论性判断。若要把本服务对外分发（尤其是商业分发）：

> Requires project-specific legal review before commercial redistribution.

**只想要解码方向**：**没有配置开关** —— 能力发布只看 `compressors.heif` 的 `_probe()` 返回值。
要让矩阵只保留 `HEIC → 其它`，需要改代码：把 `_probe()` 里那段编码探测
（真编一张 2×2 再读回来）去掉，让 `encode` 恒为 `False`。
改完之后矩阵、界面与前端目标列表会**自动**同步，不需要动别的地方 —— 矩阵是从探测结果派生的，不是手写的。

### OCR（PDF → Word 的扫描件）

- 由 `services/ocr_service.py` 调用，语言默认 `chi_sim` + `eng`
- **逐页串行**（约 2 秒/页），且有单飞锁与等锁上限（`FILETOOLS_PDF_TO_WORD_OCR_LOCK_WAIT`，默认 60 秒）
- 单份 PDF 最多 OCR 30 页（`FILETOOLS_PDF_TO_WORD_MAX_OCR_PAGES`），超过的部分如实说明
- 可以整个关掉：`FILETOOLS_OCR_DISABLED=1`，此时扫描件会如实提示需要 OCR 组件，而不是给一份空文档
- **取消是协作式的**：正在跑的 OCR 停不下来，界面如实显示「已请求取消，正在等待当前文件处理完」

---

## 快速开始

需要开**两个终端**，一个跑后端、一个跑前端。

### 1. 启动后端

```bash
cd backend

# 创建并激活虚拟环境
python -m venv .venv
# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1
# macOS / Linux
# source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 可选：HEIC / OCR
# pip install -r requirements-heic.txt
# pip install -r requirements-ocr.txt

# 启动服务（默认 http://127.0.0.1:8000）
uvicorn main:app --reload --port 8000
```

启动成功后会看到：

```text
INFO:filetools:FileTools API v0.1.0 已启动（上传上限 50 MB，并发 5）
INFO:     Uvicorn running on http://127.0.0.1:8000
```

- 接口文档（Swagger UI）：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/api/health>
- 能力矩阵：<http://127.0.0.1:8000/api/conversion/capabilities>

### 2. 启动前端

```bash
cd frontend
npm install
npm run dev
```

打开 <http://localhost:5173> 即可使用。

前端开发服务器会把 `/api` 请求代理到 `http://127.0.0.1:8000`，因此前后端同源，不存在跨域问题。

> 如果后端不在默认地址，启动前端前设置环境变量：
> `VITE_BACKEND_URL=http://192.168.1.10:8000 npm run dev`
> （也可以写进 `frontend/.env`，两种写法的优先级见下面配置项一节的「前端环境变量」）

### 3. 试一下

#### 统一转换中心（推荐入口）

1. 打开 <http://localhost:5173/convert>
2. 拖进任意文件 —— **源格式是自动识别的，不用你选**
3. 目标格式列表按 Recommended / Other formats 两级给出，全部来自后端能力矩阵
4. 参数面板按目标格式自动裁剪（选 PNG 就没有质量滑杆，选 JPG 才有）
5. 一次拖多个文件，结果自动打 ZIP

#### 图片压缩

1. 打开 <http://localhost:5173/image/compress>
2. 把一张几 MB 的图片拖进上传区域
3. 目标大小选「≤ 2 MB」，压缩质量选「平衡」
4. 点「开始压缩」
5. 查看压缩前后对比，点「下载文件」

#### 格式转换 / 几何操作

1. 打开 <http://localhost:5173/image/convert>，上传一张 HEIC（需装可选组件），目标格式选「JPG」
2. 打开 <http://localhost:5173/image>，可以对图片做旋转 90° / 翻转 / 裁剪，再导出

#### 尺寸调整

1. 打开 <http://localhost:5173/image/resize>
2. 上传一张 4032 × 3024 的照片，宽度改成 `1920`
3. 高度会自动变成 `1440`（保持宽高比例已默认勾选）
4. 点「开始调整」→ 下载结果

#### 批量

在上面任一页面一次选中多张图片（最多 50 张），结果会自动打包成 ZIP 下载。

#### PDF 工具

1. 打开 <http://localhost:5173/pdf>，六个 PDF 功能都在这里
2. **图片转 PDF**：一次选中多张图片，拖动抓手调整顺序（第一张就是第一页），选好纸张和页边距，点「生成 PDF」
3. **PDF 转图片**：上传一份 PDF，选 JPG 或 PNG，页面范围填 `1-3`，点「开始导出」→ 多于一页会打包成 `pdf_pages.zip`
4. **PDF 合并**：选中几个 PDF，排好顺序，点「合并 PDF」→ 下载 `merged.pdf`
5. **PDF 拆分**：上传后选「按范围拆分」，三行分别填 `1-5`、`6-10`、`11-20`，点「开始拆分」→ 得到 `part-01.pdf`、`part-02.pdf`、`part-03.pdf`
6. **PDF 页面删除 / 提取**：上传后点缩略图选中页面（红色＝删除，蓝色＝提取），点「生成新的 PDF」
7. **PDF 压缩**：上传扫描件类的 PDF，选「高压缩」，点「开始压缩」→ 看到「✓ 压缩完成」与节省百分比

#### 文档转换

1. 打开 <http://localhost:5173/doc>，四个文档转换功能都在这里
2. **TXT 转 PDF**（不需要 LibreOffice，随时可试）：上传一个 `.txt`，字号填 `14`，页面大小选「A5」，方向选「横向」，点「开始转换」→ 下载得到的 PDF 就是 14 号字、A5 横排
3. **Word 转 PDF**：上传一个 `.docx`，最大文件大小选「≤ 1 MB」，点「开始转换」→ 结果里会写明页数；如果压缩达不到 1 MB，会如实写出当前大小，**不会假装成功**
4. **Excel 转 PDF**：上传一个多工作表的 `.xlsx`，结果里会写明一共导出了几张工作表、其中几张是隐藏的
5. **PPT 转 PDF**：上传一个 `.pptx`，一页幻灯片对应 PDF 的一页

> 没装 LibreOffice 时，第 3–5 步的页面顶部会直接提示「当前服务器缺少 Office 转换组件，请联系管理员。」
> 并且开始按钮是禁用的。第 2 步不受影响。

#### PDF → Word

1. 打开 <http://localhost:5173/doc/pdf-to-word>
2. 上传一份 **文字型** PDF → 直接得到可编辑的 `.docx`（不走 OCR）
3. 换成一份 **扫描件** PDF → 页面会提示需要 OCR，并显示真实进度（约 2 秒/页）

---

## 生产模式部署

前端构建产物可以被后端直接托管，这样只需要跑一个服务：

```bash
# 1. 构建前端
cd frontend
npm install
npm run build          # 产物输出到 frontend/dist

# 2. 启动后端（检测到 frontend/dist 存在时会自动挂载）
cd ../backend
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

然后访问 <http://localhost:8000> 即可。前端路由（如 `/convert`）刷新时也能正常打开。

> 注意：`frontend/dist` 是在后端**启动时**检测的。如果后端已经在运行，构建完前端需要重启后端。
>
> `frontend/dist/` 在 `.gitignore` 里 —— 克隆仓库后要自己跑一次 `npm run build`。
>
> 要用文档转换的话，这台服务器上还得装 LibreOffice（见 [LibreOffice 安装要求](#libreoffice-安装要求)）。
> 没装也不影响其它功能，那三个页面会明确提示缺少组件。

---

## 已实现的功能

### 图片压缩（第一阶段）

- 支持 JPG、PNG、WEBP（另见第十阶段的扩展格式）
- 三种压缩质量档位：高质量、平衡、高压缩
- 可指定目标大小：≤ 500 KB / ≤ 1 MB / ≤ 2 MB / ≤ 5 MB / 自定义（KB 或 MB）
- 系统自动调整图片质量和尺寸，使结果**尽可能接近目标大小、但不超过目标大小**
- 处理完成后显示：原文件大小、压缩后大小、节省百分比、实际使用的编码质量与缩放比例
- 若原文件本来就小于目标大小，直接返回原文件，不做任何有损处理

### 图片格式转换（第二阶段，第十阶段扩容）

- **入**：JPG / PNG / WEBP / BMP / GIF / TIFF / HEIC / SVG（8 种）
- **出**：JPG / PNG / WEBP / BMP / GIF / TIFF / HEIC / ICO / PDF（9 种）
- 上传后显示文件名、原始格式、原始大小和图片预览
- 目标格式与原格式相同时直接提示「当前图片已经是 PNG 格式，无需转换」，并禁用转换按钮
- JPG / WEBP / HEIC 可选质量（1–100）；PNG / BMP / TIFF 是无损格式，界面上不显示质量设置，也不做有损量化
- 一次最多 50 张，多张时结果自动打包为 ZIP

### 图片尺寸调整与几何操作（第二阶段，第十阶段扩展）

- 显示原始尺寸，可自定义宽度与高度
- ☑ 保持宽高比例：改一个边，另一边按原图比例自动算出（4032 × 3024 输入宽度 1920 → 高度自动变 1440）
- 常用尺寸快捷选择：社交媒体（1080×1080 / 1080×1350 / 1080×1920）、视频（1920×1080 / 1280×720）、网页（1200×630）
- 可同时限制目标最大文件大小：程序**先按尺寸调整，再自动优化压缩质量**
- **第十阶段新增的几何操作**：旋转（90° / 180° / 270°，顺时针）、水平翻转、垂直翻转、裁剪（指定区域）
- 手机照片按 EXIF 方向自动转正后再做几何操作 —— 否则「旋转 90°」在竖拍照片上会得到反方向的结果
- 详见[格式转换与图片几何操作是怎么工作的](#格式转换与图片几何操作是怎么工作的)

### 图片元数据（第十阶段）

- `POST /api/image/metadata` 是一个**只读**接口：上传一张图，拿回它自带的拍摄信息（相机、拍摄时间、尺寸、GPS 等）。
  它**不产出文件、不建任务、不进队列**，所以是同步返回的 —— 用户点「查看」，界面就该立刻显示，
  让他去轮询一个「查看任务」是荒唐的。
- 元数据的**保留 / 清除**是在**转换时**选的（统一转换中心的 `metadata` 选项，默认保留），
  不在这个接口上。清除只覆盖可安全剥离的 EXIF，**不做像素级擦除**，界面上如实这么写。
- 支持 XMP（含 **TIFF 的 XMP 往返**）。
- 目标格式结构上装不下 XMP 时（BMP / GIF / ICO）**如实回报**「目标格式不支持携带 XMP 元数据，该项未能保留。」，不假装已保留。
- 读不出来的项，`value` 是 `null`、界面显示「无法读取」—— 不会用空字符串冒充一个真读到的空值。

### PDF 工具箱（第三阶段）

首页「PDF工具」页面，六个功能卡片：

#### 1. 图片转 PDF

- 一次最多 50 张图片（JPG / JPEG / PNG / WEBP），一张图片一页
- 列表里显示缩略图、文件名、大小与像素尺寸，**可拖动调整顺序**（drag handle，另配上移 / 下移 / 删除按钮，键盘和触屏也能用）
- 页面大小：自动（跟随图片本身）/ A4 / A5 / Letter / 自定义（20–2000 毫米）
- 页面方向：自动（按图片横竖）/ 纵向 / 横向
- 图片适应方式：保持比例（完整显示，四周留白）/ 填充页面（铺满整页，超出部分裁掉）
- 页边距：无 / 小（5 mm）/ 中（10 mm）/ 大（20 mm）
- 完成后显示 PDF 页数与文件大小，直接下载

#### 2. PDF 转图片

- 上传后显示文件名、大小与**真实页数**
- 输出格式：JPG / PNG / WEBP —— **选择 PNG 时不显示 JPG 那样的质量选项**（PNG 无损，质量对它没有意义，界面上会说明原因）
- 清晰度：标准 96 DPI / 高清 150 DPI / 超清 300 DPI
- 页面范围：全部、1-3、1,3,5、2-6，留空表示全部；输入框旁边有常用范围快捷按钮
- 多于一页时自动打包为 `pdf_pages.zip`，单页时直接给图片文件
- 结果页展示生成图片的缩略图

#### 3. PDF 压缩

- 显示原始大小与页数
- 压缩等级：轻度（≤200 DPI / 质量 85）、平衡（≤150 DPI / 质量 72）、高压缩（≤96 DPI / 质量 55）
- 目标最大文件大小：不限制 / 5 MB / 10 MB / 20 MB / 自定义（0.1–500 MB）
- 结果页给出「原文件 / 压缩后 / 节省百分比」对比，标题是「✓ 压缩完成」

#### 4. PDF 合并

- 一次最多 50 个 PDF，按列表顺序合并（可拖动排序，也有上移 / 下移 / 删除）
- 输出固定为 `merged.pdf`，显示合并后总页数与来源文件合计大小

#### 5. PDF 拆分

- **每页一个 PDF**：`part-01.pdf`、`part-02.pdf` …，多于一份时打包为 `<原名>_parts.zip`
- **按范围拆分**：一行一个范围（`1-5` / `6-10` / `11-20`），一行生成一个文件；同一行里可以用逗号把不连续的页面放在一起
- **自定义页面**：填 `1,3,5,8`，只保留这些页合成一份 `selected-pages.pdf`

#### 6. PDF 页面删除 / 提取

- 展示所有页面的缩略图，卡片上标着 `Page 01` 这样的页码
- 点缩略图选中：删除模式下有明显红色「将删除」标记，提取模式是蓝色「已选中」
- 缩略图与页码输入框是**同一份选择**，两边改都行；超过 60 页时只渲染前 60 张缩略图，其余直接填页码
- 删除的输出是 `<原名>_edited.pdf`，提取的输出是 `selected_pages.pdf`
- 不允许把页面删光（按钮会禁用并说明原因）

六个功能共用同一套处理状态显示：等待处理 → 正在上传 → 正在分析文件 → 正在处理 → 正在生成文件 → 处理完成 / 处理失败。**只有上传阶段有真实的百分比**，处理阶段是不确定态进度条加文案轮播，不编造进度。

### 文档转换（第五阶段）

首页「文档转换」页面，四个功能卡片：

#### 1. Word 转 PDF

- 支持 `.docx`（Word 2007+）与 `.doc`（Word 97-2003）
- 可选「最大文件大小」：不限制 / ≤ 500 KB / ≤ 1 MB / ≤ 2 MB / ≤ 5 MB / ≤ 10 MB / 自定义（0.1–500 MB）
- 结果页显示原文件大小、PDF 大小与页数

#### 2. Excel 转 PDF

- 支持 `.xlsx` 与 `.xls`
- 一个工作簿里的**全部工作表**会排进同一份 PDF，结果里会写明一共导出了几张
- 隐藏的工作表不会被导出，界面上会说明这一点
- 同样支持「最大文件大小」

#### 3. PowerPoint 转 PDF

- 支持 `.pptx` 与 `.ppt`
- 一页幻灯片对应 PDF 的一页，结果里写明幻灯片页数与实际输出的页数
- 隐藏的幻灯片不会被导出，界面上会说明这一点
- 同样支持「最大文件大小」

#### 4. TXT 转 PDF

- 纯文本排版成 PDF，四项都可调：
  - **字体** —— 列表是**运行时探测本机**装了什么字体得出的（宋体 / 黑体 / 楷体 / 仿宋 / 微软雅黑 / 等线…），一个中文字体都没探测到时只给「内置字体」一项并说明原因
  - **字体大小** —— 8–32，默认 11
  - **页面大小** —— A4 / A5 / Letter
  - **页面方向** —— 纵向 / 横向
- 自动识别文本编码（UTF-8 / UTF-8 BOM / GBK / UTF-16），Windows 记事本另存的 GBK 文件能正常读出来
- 自动分页，单个超长单词或无空格的长行有兜底，不会卡死
- **TXT 不依赖 LibreOffice**：排版由本服务用 PyMuPDF 直接完成，所以服务器没装 Office 转换组件时，这一项照样可用

> 四种格式都**不支持加密文档、宏与嵌入对象**；转换质量取决于服务器上的 LibreOffice 与字体，排版结果与 MS Office 不会完全一致。详见[已知限制](#已知限制)。

### PDF → Word（第六阶段）

- `POST /api/office/pdf-to-word`，把 PDF 转成可编辑的 `.docx`
- **文字层优先**：PDF 里本身有文字就直接抽取，不走 OCR —— 这样结果是真正可编辑、可搜索的文本
- **扫描件自动 OCR**：页面文字少于阈值（默认 16 个字符）时判定为扫描页，对该页做 OCR
  - OCR 语言：简体中文 + 英文（`chi_sim` + `eng`）
  - 最多 OCR 30 页，约 2 秒/页；超过的部分如实说明
  - OCR 需要可选组件，见[可选组件](#可选组件heic--ocr)
- 带进度查询端点 `GET /api/office/pdf-to-word/progress/{progress_id}`，界面显示真实进度
- 图片型页面会以图片形式嵌入 DOCX（150 DPI / JPEG 质量 80），保证版面不丢
- 取消是**协作式**的：正在跑的 OCR 停不下来，界面如实显示「已请求取消，正在等待当前文件处理完」，不假装已停止

### 批量处理（第二阶段起步，第四阶段升级）

- 图片压缩、格式转换、尺寸调整、PDF 转图片、统一转换中心都支持批量，一次最多 50 个文件、整批合计 300 MB
- 处理中显示真实的上传进度与每个文件的处理状态
- 单个文件失败不会中断整批，失败原因会逐条列出
- 结果页提供缩略图切换，可逐个查看处理前后对比（大小、尺寸、格式）
- 处理完成后可「下载全部」或「重新处理」

细节见 [批量处理与任务队列](#批量处理与任务队列)。

### 规划中（尚未上线）

首页上带「即将上线」标记的卡片均为未实现功能，当前不可点击，**代码中没有任何无法运行的占位实现**。

- 可选的持久化存储
- 多人协作与账号体系

---

## 统一转换中心

**第七阶段建立、第九阶段重做成 2.0**，页面在 `/convert`。它把前面各阶段散落的转换能力收进**一个入口**，
并且是**配置驱动**的：能力矩阵由后端的 `conversion/registry.py` 条目表**派生**出来，
前端不写死任何一行能力表。

### 能力是怎么算出来的

`GET /api/conversion/capabilities` 是前端**唯一**的能力来源。它返回：

| 键 | 内容 |
| --- | --- |
| `conversions[]` | 70 条 1→1 转换，每条带稳定 ID（如 `image.jpg-to-png`）、源/目标、选项 schema |
| `operations[]` | 7 个多进多出 / 页面级操作，带各自的 `endpoint` |
| `categories[]` | 3 个分类：图片 / 文档 / PDF |
| `formats[]` | 19 种格式（含扩展名与是否可作为源） |
| `matrix` | 源 → 目标的完整矩阵（**真实扩容**，不是旧投影） |
| `groups` | 4 个分组：图片（8 源）/ Office 文档（6 源）/ 文本（3 源）/ PDF（1 源） |
| `targets` | 目标的展示信息 |
| `available` / `*_available` | 可用性标志（按运行期探测得出） |
| `notes` | 需要如实告诉用户的降级说明 |
| `pdf_to_word_note` | PDF→Word 的可用性说明（不可用时给出原因，可用时为空） |

**稳定 ID 的格式**：`<分类>.<源>-to-<目标>`（如 `image.jpg-to-png`）或 `op.<名字>`（如 `op.pdf-merge`）。

### 当前能力（封板实测）

```text
转换条目        70
操作条目         7   op.pdf-merge / op.pdf-split / op.pdf-compress /
                     op.pdf-extract-pages / op.pdf-delete-pages /
                     op.image-images-to-pdf / op.image-metadata
格式总数        19   jpg png webp bmp gif tiff svg heic doc docx
                     xls xlsx ppt pptx txt html md pdf ico
矩阵源格式      18
分组             4
分类             3
其中图片转换    54   （46 条 图片↔图片 + 8 条 图片→PDF）
```

### 选项面板是「按 schema 渲染」的

参数面板由 `options_schema.items` 驱动（integer / number / boolean / enum / string + `min` / `max` / `step` / `unit` / `help` / `visible_when`），
提交时用**同一份数据**序列化成 `options` JSON。渲染与提交同源，没有硬编码的键名 ——
将来加新格式不用重做一遍参数 UI。

几条刻意的裁剪（**不发点了没反应的假控件**）：

| 选项 | 出现在哪些目标上 |
| --- | --- |
| `quality` | 只有 JPG / WEBP / HEIC —— PNG / BMP / TIFF 是无损的，给质量旋钮是假控件 |
| `metadata` | 只有能装 EXIF 的目标 —— BMP / GIF / ICO 与 `→pdf` 都没有 |
| `dpi` | 只有 JPG / PNG / TIFF |

**「小 / 中 / 大」与「宽 640 ~ 宽 2560」是两套不同语义**，这不是笔误：

- `小 / 中 / 大` = 按**长边**缩放（1024 / 1600 / 2560），**只缩不放**，小图不会被拉糊
- `宽 640 ~ 宽 2560` = 按**宽度**缩放，**宽度就是所选的值**，小图会被放大

两档都不裁剪。

### PDF 操作为什么走各自的旧接口

`op.pdf-merge` 这类是**多进多出 / 页面级**操作，与 1→1 的批量模型结构上不同，
所以在统一中心里给入口与统一的参数 UI，**执行走各自已有的接口**（`/api/pdf/merge` 等）。
`/api/conversion/tasks` 只负责真正的 1→1 转换。

### 提交一个统一任务

```text
POST /api/conversion/tasks      → 202 {"batch_id": "..."}   （multipart，字段名 files）
GET  /api/conversion/tasks/{batch_id}  → 批次快照
GET  /api/conversion/tasks/{batch_id}/progress → 轻量进度
POST /api/conversion/tasks/{batch_id}/cancel   → 取消整批
POST /api/conversion/tasks/{task_id}/retry     → 重试单项
```

批次快照的顶层键：`batch_id` / `status` / `total` / `queued` / `processing` / `completed` / `failed` / `cancelled` /
`progress` / `cancelling` / `cancelled` / `error` / `tasks`（**逐项列表在这个键下**）/ `result`。

批次级的 `result` 带打包信息：`archived` / `archive_filename`（`filetools-converted.zip`）/ `download_url`（**ZIP 在这里**）。
逐项结果在 `tasks[i].result`，字段为 `download_url` / `filename` / `width` / `height` / `media_type` /
`original_size` / `output_size` / `page_count` / `preview_url` / `quality_used` / `target_reached` / `target_size` / `notes`。

> 多文件一起提交时**自动打 ZIP**；ZIP 在**批次级**的 `download_url` 上，不在某一项的 `download_url` 上。
> 重名会自动加序号去重（`报告-page-01.png` / `报告-2-page-01.png` / `报告-2-page-02.png`）。

### 前端

- 动态目标选择器：Recommended / Other formats 两级，数据全部来自 `capabilities.conversions[]`
- 首页 Hero 主 CTA 直接进 `/convert`
- 旧的单功能页面**一个都没有删**，仍然可用

---

## 批量处理与任务队列

第四阶段把这几个功能统一到同一条流水线上：**上传 → 建任务 → 进队列 → worker 逐个处理 → 轮询进度 → 下载 → 自动删除**。
第八阶段在这条流水线上升级出**按资源分池**的 Worker Pool 架构 —— **没有推翻第四阶段**，队列 / 重试 / 超时 / 取消 / 清理全部沿用。

### 一次能处理多少

| 限制 | 默认值 | 配置项 |
| --- | --- | --- |
| 单个文件 | 50 MB | `FILETOOLS_MAX_UPLOAD_BYTES` |
| 单批文件数 | 50 个 | `FILETOOLS_MAX_BATCH_FILES` |
| 整批合计 | 300 MB | `FILETOOLS_MAX_BATCH_TOTAL_BYTES` |

超过 50 个时前端提示「一次最多处理 50 个文件。」并只保留前 50 个；服务端同样会拒绝（`400`），不依赖前端自觉。

### 五个 Worker 池（第八阶段）

不同的资源互不排队，各自有独立的并发上限：

| 池 | 默认并发 | 配置项 | 里面的活 |
| --- | --- | --- | --- |
| `image` | 2 | `FILETOOLS_IMAGE_WORKERS` | 图片压缩 / 转换 / 尺寸 / 几何 / 元数据 |
| `pdf` | 1 | `FILETOOLS_PDF_WORKERS` | PDF 页面操作 |
| `office` | 1 | `FILETOOLS_OFFICE_WORKERS` | LibreOffice 转换 |
| `ocr` | 1 | `FILETOOLS_OCR_WORKERS` | OCR |
| `default` | 2 | `FILETOOLS_QUEUE_WORKERS` | 其余 |

池子由**能力条目声明**（`worker_pool`），而不是在路由里散写；没有声明时按资源需求回退到既有映射。

每个池都有：并发上限、超时兜底、看门狗（检测卡死的 worker）、任务丢失宽限、自动重试、优先级老化。
可观测性通过两个端点暴露：`GET /api/system/workers`、`GET /api/system/metrics`。

> ⚠️ 把 `FILETOOLS_OFFICE_WORKERS` / `FILETOOLS_OCR_WORKERS` 调大**不会提升吞吐**：
> LibreOffice 是全局单飞锁，OCR 是**逐页**加锁。分池改的是「不同的资源不互相排队」，不是解开这两把锁。

### 异步任务接口

批量接口都是**异步**的：请求只负责把文件收下来、建好任务组，随即返回 `202` 与任务号，处理在后台队列里进行。

```text
POST /api/image/compress   →  202 {"group_id": "...", "status_url": "/api/tasks/...", "total": 12, ...}
GET  /api/tasks/{group_id} →  200 快照（总任务 / 已完成 / 处理中 / 等待 / 失败 + 每个文件的状态）
GET  /api/download/{job_id} → 200 结果文件（一次性，下载后立即删除）
```

任务快照长这样：

```json
{
  "group_id": "…", "tool": "image.compress", "label": "图片压缩",
  "state": "processing", "total": 50,
  "completed": 18, "processing": 2, "waiting": 30, "failed": 0, "finished": 18,
  "percent": 36.0,
  "tasks": [
    { "index": 0, "filename": "photo.jpg", "size": 901234, "state": "done",
      "state_label": "已完成", "error_code": null, "error_message": null, "result": { … } }
  ],
  "result": null,
  "error": null
}
```

`percent` 由 `(completed + failed) / total` 算出，**不编造进度**：进度条只在文件真的处理完时才前进。

### 队列是怎么跑的

- 进程内 `asyncio` 队列（`services/queue_service.py`），不引入 Redis / Celery —— 单机部署下它带来的复杂度大于收益，接口层已经按「可替换」写好
- **每个任务项只处理一个文件**；worker 协程数量决定「同时处理几个文件」
- 真正的解码 / 编码跑在线程池里（`FILETOOLS_MAX_WORKERS`，默认 5），事件循环不会被 CPU 任务卡住，轮询请求照常响应
- 处理器抛异常只会让**那一个文件**失败（记 `PROCESSING_FAILED`），worker 不会死，整批继续
- 队列机制在 `services/queue_service.py` / `worker_pool.py` / `worker.py`，业务代码在 `backend/tasks/`，路由里没有任何队列逻辑

### 文件处理状态

| 状态 | 标记 | 含义 |
| --- | --- | --- |
| 等待中 | ○ | 已进队列，还没轮到 |
| 处理中 | ⟳ | worker 正在处理这一个文件 |
| 已完成 | ✓ | 有结果，可以下载 |
| 失败 | ✕ | 这一条失败，旁边直接写出原因（如「文件已损坏或不完整」） |

面板顶部同时给出 总任务 / 已完成 / 处理中 / 等待 / 失败 五个数字与 `18 / 50` 形式的进度，任何一个文件的状态变化都会在下一次轮询（500 ms）里反映出来。

### 临时文件与自动清理

- 每个任务一个独立的临时目录（`filetools_<随机>`），路径不出现在任何响应里
- 结果下载完立刻删除；没下载的到 `FILETOOLS_JOB_TTL`（默认 30 分钟）后被定时清理线程删掉
- 任务记录本身也在 `FILETOOLS_TASK_TTL`（默认 30 分钟）后清除，过期后查询返回 `404`
- 进程被强杀留下的目录由 `sweep_orphan_dirs` 兜底清理（超过「最长保留时长 × 2」才动）
- 所有时长都是环境变量，改完重启即可生效

### 手机端与拖拽

- 375 / 390 / 414 px 三个宽度下**没有任何横向滚动**，上传区与下载按钮都放大到手指可点（≥ 44 px）
- 触屏设备（`hover: none and pointer: coarse`）上传区文案是「选择文件」，点击调起系统文件选择器；桌面端是「拖放文件到这里」，拖入时高亮
- PDF 合并与页面删除 / 提取支持**拖拽排序**，也可以点「上移 / 下移」；输出的 PDF 严格按界面上的顺序生成

### 最近处理

首页的「最近处理」只保存在浏览器 `sessionStorage` 里，字段只有 **文件名称 / 操作类型 / 处理时间 / 处理状态 / 文件大小**，最多 20 条。**不保存文件内容，也不保存下载地址**，服务器上没有任何历史记录。

### 错误提示

服务端统一返回 `{ "error": { "code": …, "message": … } }`，前端按 `code` 显示中文提示，永远不把 Python traceback 或服务器路径给用户看：

| 错误码 | HTTP | 用户看到的提示 |
| --- | --- | --- |
| `FILE_TOO_LARGE` | 413 | 文件过大，请上传更小的文件 |
| `INVALID_FILE_TYPE` | 415 | 暂不支持该文件格式 / 扩展名与真实内容不一致 |
| `CORRUPTED_FILE` | 400 | 文件已损坏或不完整 |
| `PROCESSING_TIMEOUT` | 504 | 处理超时，请换更小的文件重试 |
| `PROCESSING_FAILED` | 422 | 处理失败，请稍后重试 |
| `SERVER_ERROR` | 500 | 服务器处理失败，请稍后重试 |

第七、八阶段新增了任务队列相关的错误码（`TASK_TIMEOUT` / `WORKER_LOST` / `TEMPORARY_IO_ERROR` 等）。
`GET /api/config` 会把服务端可能返回的**全部**错误码列出来，验收脚本会拿它对一遍前端文案表，漏配一个就报错
（前端 `errorMessages.ts` 与后端 `ErrorCode` 之间有**双向相等**的测试断言）。

---

## 移动 App（Expo / React Native）

`mobile/` 是一个 **React Native（Expo SDK 57 + TypeScript）** 的移动 App，与 Web 前端**并列**，
两者都只是同一套后端 API 的客户端。

**它不是移植，也没有第二份转换逻辑。** 后端**一行代码都没改**：移动端调用的就是 Web 端在调的那些
端点（`/api/conversion/capabilities`、`/api/conversion/tasks`、任务快照与下载接口），
转换仍然发生在同一套任务队列与 Worker Pool 上。没有 `/api/mobile/*` 这种「移动专用转换接口」——
那会立刻变成第二条真理。

### 跑起来

```bash
# 1. 后端（端口随意，移动端的 .env 要指过去）
cd backend && ./.venv/Scripts/python.exe -m uvicorn main:app --host 0.0.0.0 --port 8011

# 2. 移动端
cd mobile
cp .env.example .env      # 按自己机器改 EXPO_PUBLIC_API_BASE_URL
npm install
npx expo start            # 然后按 w 开浏览器 / a 开 Android / i 开 iOS
```

| 平台 | 怎么跑 | 后端地址要填什么 |
| --- | --- | --- |
| Web（`npx expo start --web`） | 浏览器 | `http://127.0.0.1:8011` |
| Android 模拟器 | Expo Go | `http://10.0.2.2:8011`（模拟器里的 `localhost` 是模拟器自己），或 `adb reverse tcp:8011 tcp:8011` 后用 `127.0.0.1` |
| iOS 模拟器 | 与宿主机同网络 | `http://127.0.0.1:8011` |
| 真机 | Expo Go / 自带构建 | 宿主机在局域网里的 IP（如 `http://192.168.1.10:8011`），并且后端要 `--host 0.0.0.0` |

> ⚠️ `EXPO_PUBLIC_API_BASE_URL` 是 **打包时**由 `babel-preset-expo` 做**纯文本替换**写进产物的，
> 不是运行时读的。所以必须**逐字**写成 `process.env.EXPO_PUBLIC_API_BASE_URL` ——
> 写成 `process.env[name]`、解构或先赋给变量，替换都不会发生，运行时拿到 `undefined`。
> 改完要重启 dev server，热重载不会生效。

### 四个标签页与路由

路由用 **expo-router**（文件即路由），`app/` 下的目录结构就是 URL 结构：

| 路由 | 页面 | 做什么 |
| --- | --- | --- |
| `/` | 首页 | 主入口 + 「最近处理」 |
| `/tools` | 工具 | 全部工具的搜索与分类筛选 |
| `/history` | 历史 | 本机记录，可删除 / 清空 |
| `/profile` | 我的 | 外观（浅色 / 深色 / 跟随系统）、服务端自检、关于 |
| `/convert/[capabilityId]` | 转换 | 动态参数表单 + 选文件 + 提交 |
| `/task/[batchId]` | 任务 | 轮询进度、下载 / 分享结果、取消 |

### 能力驱动：移动端**没有**一张硬编码的能力表

工具列表来自 `GET /api/conversion/capabilities`，取其中 `operation_type === "conversion"` 的条目
（当前 **70 条**）。分类标签来自响应的 `categories`，一个都不写死。
**在移动端代码里搜不到任何格式名或格式矩阵** —— 这是验收脚本里的一条断言（先剥掉注释再扫，
免得把文档注释里的举例当成真代码），不是一句口头承诺。

所以后端加一种格式，移动端**不用改代码**就会出现。服务端另外还登记了 **7 条 PDF 操作**
（合并 / 拆分 / 压缩 / 提取页 / 删除页 / 多图合成 PDF / 图片元数据），本阶段移动端**不提供入口** ——
它们是「多进多出 / 页面级 / 就地改写」操作，与移动端这一版 1→1 的流程模型不同，与其做一个半吊子的入口，不如如实不做。

### 参数表单也是动态的

转换页的表单按该能力的 `options_schema.items` 渲染（`integer` / `number` / `boolean` / `enum` / `string`，
带 `min` / `max` / `step` / `unit` / `help` / `visible_when`），提交时**同一份数据**序列化成 `options` JSON。
渲染与提交同源，没有一处硬编码的键名 —— 将来后端加新选项，移动端不用重新设计表单。

只有 `options_schema` 为 `null` 的能力（70 条里有 13 条）不显示参数区，这是如实反映「这条转换没有可调参数」。

### 错误处理：只看 `code`，永远不显示堆栈

后端统一返回 `{ "error": { "code": …, "message": … } }`，移动端按 `code` 查本地文案表
（`src/utils/errorMessages.ts`）。**不做字符串匹配，也不把服务端 message 直接甩给用户**，
更不会出现 traceback、服务器路径或内部模块名。「我的」页里有一项自检，会把服务端
`/api/config` 给出的**全部 25 个错误码**与本地文案表对一遍，漏配一个就在界面上写出来。

### 上传：一个文件是怎么送出去的

这一段踩过一个坑，写下来免得下次再查一遍。

**Expo SDK 57 起，全局 `fetch` 换成了 `expo/fetch`**（`expo/src/winter/runtime.native.ts` 里装的），
它**自己**把 `FormData` 序列化成 multipart。它的编码器只认三种 part：字符串、`Blob`、
以及**带 `bytes()` 的对象**。React Native 那个流传很广的 `{uri, name, type}` 三件套**不在其中** ——
会当场抛 `Unsupported FormDataPart implementation`。

要命的是这个异常发生在**开 socket 之前**：请求根本没发出去，服务端一条日志都没有，
客户端却把它归一成「无法连接服务器」。看上去像网络不通，实际是序列化没走通 —— 很容易查错方向。

现在原生这一路给的是 `expo-file-system` 的 `File` 读出来的字节，包成 `{ name, type, bytes() }`
（见 `src/services/api/conversion.ts`）。**不能直接把 `File` 当 part 用**：编码器取文件名读的是
`part.name`，而 `File.name` 是**盘上那个文件的名字**。文档选择器落盘用的是随机名
（`DocumentPicker/<uuid>.<ext>`），直接用会把源文件名报成一串 uuid，结果文件跟着错名。

文件选择器那边还有一条：**`copyToCacheDirectory` 必须关掉**（`src/services/filePicker.ts`）。
打开时 Android 会把文件复制到 `context.cacheDir`，那是 **Expo Go 宿主 App** 的缓存目录，
而 Expo Go 给每个体验的是**受限**的文件权限，`expo-file-system` 读它会明确拒绝：

```text
Missing 'READ' permission for accessing the file.
```

关掉之后拿到的是文档提供方的 `content://` —— `expo-file-system` 对 content URI **不做路径权限检查**
（`FileSystemPath.kt::checkPermission` 里 `uri.isContentUri` 直接放行），由 `ContentProviderFile`
走 ContentResolver 读。代价是不落副本、读的是本次选择的授权，选完几秒内就上传，够用。

### 结果下载与分享

结果的下载地址是**一次性**的：下载过一次（或过了保留期），服务器上就删掉了。
移动端把这件事分成两个明确的动作：**下载**（Expo FileSystem）与**分享 / 用别的 App 打开**（Expo Sharing）。
下载过的任务再打开时，页面显示的是「结果已经被下载过或已过期」这句**实话**，
而不是一个点了会 404 的按钮。「打包下载」对多文件任务同样可用。

### 本地历史：**只有元数据**

历史存在本机（AsyncStorage；在 Web 上落到 `localStorage`），字段只有
`batchId / capabilityId / filename / sourceType / targetType / status / total / resultFilename / resultExpired / createdAt`。
**没有文件内容，没有下载地址，也没有数据库。** 验收脚本会量这个存储的实际字节数
（三次转换的记录整包 **765 字节**）并断言它没有把文件塞进去。

### 主题、状态与轮询

- 浅色 / 深色 / 跟随系统三种，切换量的是**真实渲染后的像素**（页面上占比最大的不透明底色，
  亮度 250 → 17），不是读一个 state 变量
- 空列表、加载中、网络错误都有明确的界面，不留白屏
- 任务轮询是**有界**的：有最大时长（30 分钟）、任务 settle 即停、组件卸载即停 —— 不会留下一个永远在跑的定时器

### 触控与窄屏

所有可点控件的点击区 **≥ 44 px**（`TOUCH_TARGET` 令牌，验收脚本在工具页上逐个量了 75 个控件）。
375 / 390 / 414 三个宽度下**没有任何横向滚动**。

### 平台差异：一个只在 Web 上现形的真缺陷

Expo 让同一套代码跑在 Web 上，但**原生端合法的东西在 Web 上不一定合法**。
历史页最初把整张卡片包成一个按钮、删除键嵌在里面 —— 原生端没问题，但在 Web 上那是
`<button>` 套 `<button>`：**非法 HTML**，React 报错、hydration 会失败，键盘 Tab 与屏幕阅读器
也拿不到里面那个删除键。改成卡片退化为普通容器、两个按钮并列之后才真正对。
这条是被验收脚本的「浏览器控制台必须无错误」抓出来的 —— 这类问题光看界面截图看不出来。

### 本阶段**不做**的

账号 / 登录 / 支付 / 会员 / 推送，音频与视频，离线转换（转换永远在服务器上做，本地不做第二份实现），
以及和服务端的历史同步 —— 历史**只在这台设备上**，「我的」页里如实这么写着。

---

## Windows 桌面版（Tauri）

第十一阶段 A 补充新增的**第四个客户端**。它**不是**一个新的实现，而是**把 Web 前端原样装进一个
原生窗口**：同一份 React 代码、同一个后端、同一套 API。

### 桌面端**不含**任何转换引擎

这一点是硬性的：`desktop/` 里**没有** PDF、图片、Office、OCR、压缩的任何一行实现，
Rust 侧只做两件网页做不到的事 —— **把结果文件落到磁盘**、**调用资源管理器**。
所有转换仍然发往 `http://127.0.0.1:8000`，由同一个后端、同一个队列、同一批 Worker 完成。
换句话说：桌面版和网页版**没有第二份「文件处理真相」**。

### 为什么复用 Web 前端

前端多了一条构建模式（`npm run build:desktop`），产物落到 `frontend/dist-desktop/`：

- `vite.config.ts` 里 `mode === 'desktop'` 时：`base` 改成 `'./'`（自定义协议下要相对路径）、
  `define` 把后端地址与版本号**烤进产物**
- **`frontend/dist/` 一个字节都没动**，后端托管的网页版完全不受影响
- Web 的 URL 用的是 `BrowserRouter`，桌面端换成 `HashRouter` —— 这是**构建期常量**决定的，
  不是运行时嗅探 `window.__TAURI__`（构建期确定，不用赌某个全局存不存在）
- 桌面端**只改了路由器与下载方式这两处**，页面本身一行没重写

### 桌面上的文件能力

| 能力 | 实现 |
| --- | --- |
| 选择 / 多选 / 拖拽上传 | **完全复用网页版的上传区**。`tauri.conf.json` 里把 `dragDropEnabled` 关掉了 —— 不关的话 Tauri 的原生拖放会**吃掉** HTML5 的 drop 事件，网页那套 `onDrop` 根本收不到 |
| 下载结果 | ⚠️ `<a download>` + blob 在 Tauri 的 webview 里是**静默失效**的（点了没反应）。所以桌面端改走 Rust 命令 `save_result`，用 Tauri 的**原始二进制 IPC** 传字节，直接落盘到系统下载目录 |
| 打开结果 | `explorer.exe <路径>` |
| 在文件夹中显示 | `explorer.exe /select,<路径>` |

> 中文文件名要**双重处理**才能活下来：`fetch` 的 `Headers` 只接受 ≤ 0xFF 的码点，
> 塞中文会直接抛 `TypeError`；所以 JS 侧 `encodeURIComponent`、Rust 侧再百分号解码。
> 另外 `Array.isArray(payload)` 在 Tauri 的 IPC 里**也被当成原始二进制**，
> 所以那个 payload 永远不能是数组。

### 安装体验（真机逐项核过）

安装包是 **NSIS** 的 `FileTools-Setup-x64.exe`，**按当前用户安装**（`installMode: "currentUser"`）：
装到 `%LOCALAPPDATA%\FileTools`，**不弹 UAC**。

真机上走完的向导页面（每一页都是从活动的控件树里读出来的，不是照文档抄的）：

| 页面 | 内容 |
| --- | --- |
| 欢迎 | `欢迎使用 FileTools 安装程序` |
| 安装位置 | 默认 `C:\Users\<用户>\AppData\Local\FileTools`，所需空间 7.7 MB |
| 开始菜单 | 文件夹名 `FileTools`，带「不要创建快捷方式」复选框 |
| 安装中 | `安装完成` / `安装程序成功完成安装`，带「显示详情」 |
| 结束 | `完成(F)`、**运行 FileTools(R)**、**创建桌面快捷方式** |

装完之后的实际落点：`filetools.exe`（6,174,720 字节）+ `uninstall.exe`；
**桌面**快捷方式、**开始菜单**快捷方式都在；
卸载信息写进 `HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\FileTools`，
所以 **Windows 设置 → 应用** 里能看到 `FileTools 0.1.0`。
卸载走一遍，上面这些**一样不剩**（安装目录、两个快捷方式、开始菜单文件夹、注册表项全部清除）。

> 「名称 + 版本」在向导里的位置值得说明：**首次安装**的欢迎页只显示名称 `FileTools`，
> **版本号 0.1.0 出现在两条路径上** —— 已经装过时维护页的
> `FileTools 0.1.0 已经安装了`，以及「设置 → 应用」里的卸载条目。
> 这是 NSIS 默认模板的行为，我们没有为了让它出现在欢迎页而去改模板。

### 构建

```bash
python scripts/build_windows.py     # 真构建，产出 desktop/artifacts/FileTools-Setup-x64.exe
python scripts/verify_desktop.py    # 六项检查；能构建就真的构建一次
```

两个脚本都会把 `CARGO_TARGET_DIR` 指到**纯 ASCII** 路径（默认 `D:/filetools-build/target`）：
仓库自己住在含中文的目录里，而 `makensis` 对非 ASCII 路径历史上有问题。

Tauri 的原生产物名固定是 `FileTools_<版本>_x64-setup.exe`，**没有改名配置项**，
所以 `build_windows.py` 把它**复制**成 `FileTools-Setup-x64.exe`，并把两边的 **sha256 一起打出来**
作为证据链 —— 复制过的文件必须能证明它和被复制的那份逐字节相同。

---

## 品牌资产

四平台共用**一份**设计真源，全部产物由脚本生成，**没有任何一个平台是手画的**。

```text
branding/
├── source/
│   ├── filetools-icon.svg          # 唯一真源：App 图标（正方形、无文字）
│   └── filetools-logo.svg          # 品牌锁排：[Icon] FileTools
└── generated/                      # 全部由 generate_branding.py 生成
    ├── manifest.sha256             # 源与所有输出的哈希清单
    ├── web/      favicon.svg · favicon.ico(16/24/32/48) · favicon-32/48.png
    │             apple-touch-icon.png(180) · icon-192.png · icon-512.png
    │             filetools-logo.svg · manifest.webmanifest
    ├── android/  icon.png(1024) · favicon.png(48) · android-icon-{background,foreground,monochrome}.png
    │             mipmap-{mdpi,hdpi,xhdpi,xxhdpi,xxxhdpi}/ic_launcher.png(48/72/96/144/192)
    ├── ios/      icon-1024.png     # 无 alpha：iOS 图标不许透明
    └── windows/  icon.ico(16/24/32/48/64/128/256) · icon-256.png
```

> **改图标 = 改 `source/filetools-icon.svg`，然后重跑生成脚本。**
> `generated/` 下的每一个字节都是那两个源的派生物，**只有 `scripts/generate_branding.py` 能写**。
> 直接改 `generated/` 或改各平台的产物目录没用 —— `verify_branding.py` 会把全部产物**重新渲染一遍**
> 再与盘上比对，手改的当场判红。

### App Icon 与 Brand Logo 是两样东西，不许互用

| | **App Icon** | **Brand Logo** |
| --- | --- | --- |
| 文件 | `source/filetools-icon.svg` | `source/filetools-logo.svg` |
| 形状 | 正方形，铺满画布 | 横排，约 5:1 |
| 内容 | **只有图形，没有任何文字** | 图标 + 字标 `FileTools` |
| 尺寸 | 16 px 仍可辨认 | 最小显示高度 24 px |
| 用途 | App 图标、favicon、PWA 图标、安装器图标 | 页头、关于页、启动页 |
| 不许 | **当 Logo 用** | **当 App 图标用** |

**为什么这条是硬规则**：App 图标会被系统缩到 16 px，还会被裁成圆形 / 方形 / 水滴形，
里面放文字必然糊成一团；反过来，页头放一个没有字标的方块，用户认不出这是什么产品。
`verify_branding.py` 会断言 Icon 源**不含 `<text>`**、Logo 源**含 `FileTools`**。

### 唯一真源是「沿用」，不是「重新设计」

`branding/source/filetools-icon.svg` 就是原来的 `frontend/public/favicon.svg`
（399 字节、`viewBox="0 0 32 32"`、`#4f46e5` 圆角方块 + 白纸 + 折角）——
**逐字节复制，一个字节都没改**。这一阶段只做了「把它变成四个平台都能用的一份源」，
没有另起炉灶画一个新 Logo。

`verify_branding.py` 会**全仓库扫 `*.svg`**：除了 `branding/source/` 的两个源，
只允许与它们**逐字节相同**的副本存在，出现第三份内容不同的 SVG 就判失败。

### 主色 `#4f46e5` 只此一个值

图标主色**同时**出现在下面五处，改主色要**五处一起改**，`verify_branding.py` 会逐处断言相等：

- `branding/source/filetools-icon.svg` 的底色
- `frontend/index.html` 的 `<meta name="theme-color">`
- `frontend/tailwind.config.js` 的 `brand-600`
- `mobile/app.json` 的 `android.adaptiveIcon.backgroundColor`
- `branding/generated/web/manifest.webmanifest` 的 `theme_color`

> 历史教训：移动端的 `assets/` 曾经是 **Expo 模板的默认资产**（浅蓝 `#E6F4FE`），
> 与 Web 的 indigo 根本不是一个色系 —— 「每个平台各自设计一套」正是这一节要防的事。

### 各平台产物的几条硬要求

| 平台 | 要求 | 为什么 |
| --- | --- | --- |
| Android 自适应图标 | 前景层里的图形**只占画布 66.4%**（340 / 512 px，居中） | 自适应图标是 108 dp 画布、只有内圈 72 dp 保证可见，边缘会被系统裁掉。这是刻意的留白，**不是渲染出错** |
| iOS | `icon-1024.png` **不许有 alpha 通道**（`RGB` 模式） | iOS 会拒绝带透明通道的图标，或者自己拿黑色把透明处填掉 |
| Windows | `icon.ico` 内含 **16/24/32/48/64/128/256** 七档，且 **16–128 用 BMP(DIB)、256 用 PNG** | 混合编码不是随手挑的：Windows 自己产出的 `.ico`、以及 Tauri 用来生成图标的 `ico` crate 都遵循这个分界。NSIS 要拿它当 `installerIcon`，跟惯例一致风险最低 |
| Android / iOS | `ios/icon-1024.png` 与 `android/icon.png` **内容相同** | Expo 用同一份资产供两个平台。两份都留，是为了让各平台目录自成一体 |

生成脚本对每个目标用**五种渲染方式**之一：

| 方式 | 做什么 | 用在 |
| --- | --- | --- |
| `plain` | 原样渲染，圆角外透明 | favicon、PWA 图标、ICO 母图 |
| `flat` | 合成到不透明 `#4f46e5` 上，圆角外也填满 | iOS 图标、Android `icon.png`、mipmaps |
| `padded` | 图形缩到画布 66.4% 居中，贴透明底 | Android 自适应图标前景层 |
| `mono` | 去掉底色、所有形状统一刷白 | Android 主题图标 |
| `solid` | 纯 `#4f46e5` 满幅 | Android 自适应图标背景层 |

> Pillow 自带的 ICO 编码器两条路都走不通：默认**全部**写 PNG，`bitmap_format="bmp"`
> 又**全部**写 BMP。所以 `generate_branding.py` 自己拼的容器 —— 但每一帧都验证过与
> Pillow 独立降采样的结果**逐像素相同**。

### 生成流水线：零新增依赖

本机**没有任何 SVG 光栅化工具**（无 ImageMagick / Inkscape / rsvg / cairosvg），
所以流水线用两个**本来就有**的东西拼出来，没有新增任何依赖：

1. **Playwright + Chromium**（系统 Python，验收脚本本来就要用）光栅化 SVG ——
   用 `<img>` 而不是直接打开 SVG（SVG 作顶层文档时默认尺寸不确定），
   `omit_background=True` 拿透明背景
2. **Pillow**（`backend/.venv`）把位图编码成多尺寸 ICO / 重新采样 / 转无 alpha 的 RGB

```bash
python scripts/generate_branding.py           # 生成并安装到消费方
python scripts/generate_branding.py --check   # 只校验，不写
```

### 版本也收成了一份真源

仓库根的 **`VERSION`**（一行 `0.1.0`）是全平台唯一的版本来源：

| 消费方 | 怎么拿到 |
| --- | --- |
| `backend/config.py` | **运行期读** `../VERSION`；读不到**直接报错**，不给兜底常量 |
| `frontend/vite.config.ts` | 读 `../VERSION`，`define` 注入 |
| `desktop/src-tauri/tauri.conf.json` | `"version": "../../frontend/package.json"`（Tauri 原生支持） |
| `frontend/package.json` · `mobile/package.json` · `mobile/app.json` · `mobile/src/types/index.ts` · 两份 lock | 由 `scripts/sync_version.py` 写入（幂等） |

> **故意不给兜底**：如果 `VERSION` 读不到就退回到一个写死的常量，容器里就会**静默**报出一个
> 陈旧版本号 —— 那正是这个项目最忌讳的「第二份真相」。宁可启动就报错。
>
> ⚠️ 因此 **`Dockerfile` 必须 `COPY VERSION /app/VERSION`**（只 `COPY backend/` 的话镜像里读不到），
> `verify_branding.py` 会断言这一行存在。

---

## 四平台总览

**一个后端、一套转换注册表、一个图片引擎、一条任务队列、一份品牌 Logo、多个平台客户端。**
下面的每一格都来自**真实检查**，不是设计意图。

| | **Web** | **Android** | **iOS** | **Windows** |
| --- | --- | --- | --- | --- |
| **UI** | React 18 + TS + Vite（既有实现，未重写） | React Native + Expo SDK 57 | 同一套 RN 代码 | **复用 Web 的前端产物**（Tauri 2 + WebView2），零重写 |
| **Logo** | `[Icon] FileTools` | 同一份源 | 同一份源 | 同一份源 |
| **Icon** | favicon.svg · favicon.ico(16/24/32/48) · 32 · 48 · 180 · 192 · 512 | icon.png(1024) · 5 档 mipmap(48/72/96/144/192) · adaptive icon | icon-1024.png（**无 alpha**） | icon.ico(16/24/32/48/64/128/256) · icon-256.png |
| **图标来源** | ← 全部来自 `branding/source/filetools-icon.svg` | ← 同一份 | ← 同一份 | ← 同一份 |
| **构建** | ✅ `npm run build` → `frontend/dist/`，由后端托管 | ✅ 配置与资产就位（未在 CI 里跑构建） | ❌ **未执行**（本机无 macOS） | ✅ **真的构建过**，产出 `FileTools.exe` + `FileTools-Setup-x64.exe` |
| **实机** | ✅ 真实 Chromium / Edge / WebKit，2,074 项断言 | ⚠️ 封板验收 107/107 跑在 **Expo Web**（真实浏览器，375/390/414）；Android 原生路径有实测记录，但**没有一份独立的真机验收报告** | ❌ **一行实测都没有** | ✅ 安装 → 启动 → 卸载全流程真机核过 |

**四张图标全部由同一个 SVG 生成**，`verify_branding.py` 会逐个量尺寸、查像素
（Android 背景色要求**所有像素都等于 `#4f46e5`**、foreground 四角 alpha 为 0、
iOS 要求 `mode` 里**没有 alpha 通道**），并断言四个平台的 App 名都是 `FileTools`、
版本都等于根目录 `VERSION`。

> **「实机」这一列刻意不写满。** iOS 没有 macOS 就一行都跑不了；Android 的 107/107
> 是在浏览器里的 Expo Web 上跑的，不是真设备 —— 把它写成「Android 已验证」是不诚实的。
> Windows 的 ✅ 则包含真构建、真安装、真启动、真卸载。

同一件事按**最终收尾那一轮定的九行口径**再列一遍（`PASS` = 有实测证据、
`VERIFY` = 部分证据但缺关键一环、`CONFIG` = 只有配置与资产、`N/A` = 该平台没有这个东西）：

```text
FILETOOLS PLATFORM MATRIX

                Web       Android   iOS         Windows
UI              PASS      PASS      CONFIG      PASS
Build           PASS      PASS      N/A         PASS
Real Device     PASS      VERIFY    N/A         PASS
Branding        PASS      PASS      PASS        PASS
Conversion      PASS      VERIFY    N/A         VERIFY
Download        PASS      VERIFY    N/A         VERIFY
Share           N/A       VERIFY    N/A         N/A
Install         N/A       VERIFY    N/A         PASS
Uninstall       N/A       VERIFY    N/A         PASS
```

**两处 `VERIFY` 是降级下来的，不是凑绿**：

- **Windows · Conversion / Download** —— 后端联通已实测（WebView2 的网络进程真的连上
  `:8000`、CORS 放行 `http://tauri.localhost`、`/api/health` 返回 `{"status":"ok"}`），
  落盘那三条防线（中文名 / 路径穿越 / 重名）由 **13 条 Rust 单元测试**打在生产函数上、
  已实跑通过。**但「在真实桌面窗口里从头走完一次转换并点下载」没有证据** ——
  取证要开 WebView2 的远程调试端口再用 CDP 驱动，而那个端口本机开不出来（原因见
  「已知限制」第 27 条），所以不填 `PASS`。
- **Android 的 Real Device / Conversion / Download / Share / Install / Uninstall** ——
  原生路径在 AVD 上真跑通过，但**没有物理设备**上的独立验收。其中「Install」指的是
  装上 Expo Go 之外的自家安装包，这一条没做过。

### 当前支持平台

```text
Web
Windows x64
Android
iOS configuration
```

**注意最后一行写的是 `iOS configuration` 而不是 `iOS`。** 本机是 Windows，
没有 Xcode，`expo run:ios` 一行都跑不了 —— 能确认的只有配置与资产，见下。

### 当前真实状态

这五件事在中文里都容易被一句「做好了」糊过去，所以分开说：

| 状态 | 含义 | 本项目的对应事实 |
| --- | --- | --- |
| **Implemented** | 代码写完了 | 四个平台客户端 + 全部工具（19 种格式 / 70 条转换 / 7 个 PDF 操作） |
| **Built** | 真的产出过二进制或产物 | Web `frontend/dist/` · Windows `FileTools.exe` + `FileTools-Setup-x64.exe` · Android `npx expo export --platform android` |
| **Verified** | 在真实环境跑过，且留下了读数 | Web 真实浏览器（2,074 项断言）· Windows 安装→启动→卸载全流程 · Android 原生路径（AVD，真 UI 驱动，转换/下载/分享都走通）· 后端 1,645 条 pytest |
| **Not verified** | 没有可靠证据 | **iOS 的任何运行时行为**（不是「大概没问题」，是没测过） |
| **Environment limited** | 不是没做，是本机够不着 | iOS 构建（无 macOS）· Android 独立真机验收（无物理设备）· Windows ARM64（无对应 target）· `docker build`（本机无 docker） |

**「配置存在」不等于「真机验证完成」** —— 这条是本节存在的唯一理由：

- **iOS**：`app.json` 里的 `ios.bundleIdentifier`、`supportsTablet`、1024 图标
  （**无 alpha 通道，像素已量过**）全部就位，TypeScript 0 错误，路由与代码一致。
  **但 `expo run:ios` 一次都没跑过。** 所以 iOS 是 **CONFIG**，不是 PASS。
- **Android**：封板那 107/107 是在 **Expo Web**（真实浏览器，375/390/414 三档）上跑的；
  另有 AVD `filetools11a`（Android 15 / x86_64）上的原生路径实测记录 —— 真实转换、
  真实下载、真实分享都走通了，结果文件名也是对的。**但没有一份独立的物理设备验收**，
  所以矩阵里是 ⚠️ / VERIFY，不是 ✅。

#### 最终验收结论复核（2026-10-08）

最终收尾那一轮（2026-10-07）的结论**又整取了一遍证**。之所以要重取：此后代码变过**一个**提交
（`35493be`），它只动测试判据、CI 注释与本文的条数，**生产代码一行未动** —— 下面每一格都是当场实测，
不是从上一轮的报告里抄的。

| 项 | 实测读数 | 取证时间 |
| --- | --- | --- |
| 后端 pytest | **1645 passed / 0 failed / 0 skipped** | 10-08 15:45 |
| 冻结回归（22 步严格串行，`/tmp/ft-regress-final.sh`，sha256 `e1f1a744…`） | **PASS=22 FAIL=0** | 10-08 15:45–16:04 |
| `scripts/verify_final.py` | **96 / 96，FAILED 0，4 项 NOT EXECUTED** | 10-08 16:04 |
| `scripts/verify_branding.py` | **103 / 103** | 10-08 15:48 |
| `scripts/verify_desktop.py`（含真跑一次 Tauri 构建） | **48 / 48** | 10-08 15:50 |
| Rust 单元测试 `cargo test --lib` | **13 passed / 0 failed** | 10-08 |
| **GitHub Actions CI** | **两个 job 都 `success`** —— **七次跑里首次全绿** | 10-08 16:08 |
| 卸载残留（`%LOCALAPPDATA%\FileTools` / 桌面快捷方式 / 开始菜单 / `HKCU` 卸载项） | **四项全空** | 10-08 |

**结论不变，仍是 `FINAL HARDENING PASSED` ＋ `Environment-limited verification`。**
那 4 项 `NOT EXECUTED` 到本轮为止**依然关着**，而且不是「没做」：本机没有 docker、
不是 macOS、`adb devices` 是空的（没有物理设备）、`rustup` 只装了 `x86_64-pc-windows-msvc`。
**它们不算通过**，所以上表最后不写「四个平台全部验证完毕」。

---

## 技术栈

**前端**：React 18 · TypeScript 5.6 · Tailwind CSS 3 · Vite 5.4 · React Router 6

**桌面**：Tauri 2.12 · Rust · WebView2 · NSIS（复用同一份 React 前端产物）

**移动**：React Native · Expo SDK 57

**后端**：Python 3.14 · FastAPI 0.141 · Uvicorn 0.53 · Pillow 12.3 · PyMuPDF 1.28 · python-docx 1.2 · LibreOffice headless（可选）

**存储**：无数据库。文件处理全程使用系统临时目录，处理完自动删除。

> PDF 的读取、渲染、合并、拆分与压缩全部用 **PyMuPDF** 一个库完成，没有为了单个功能再引入第二个 PDF 引擎。
> TXT 排版、HTML / Markdown → PDF 用的也是同一个 PyMuPDF，因此**没有引入新的 Python 依赖**。
>
> Office 文档转 PDF 用的是外部的 **LibreOffice**（见[环境要求](#环境要求)）。它是**可选的**：没装的时候，
> 图片与 PDF 工具、TXT 转 PDF 全部照常可用，只有 Word / Excel / PPT 三个页面会明确提示
> 「当前服务器缺少 Office 转换组件，请联系管理员。」并禁用开始按钮，不留无效按钮。
>
> ⚠️ **PyMuPDF 采用 AGPL-3.0 许可**。个人使用、内部工具、开源项目（以 AGPL 兼容方式开源）都没有问题；
> 如果要用于**闭源商业产品**，需要向 Artifex 购买商业授权，否则应改用 `pypdf` + `pdf2image`（BSD/MIT，
> 但渲染依赖外部 poppler 二进制）等替代方案。这是选型时唯一需要提前确认的法律问题。
>
> ⚠️ **HEIC 支持涉及 libx265（GPLv2）与 HEVC 专利池**，因此是**可选依赖**，见[可选组件](#可选组件heic--ocr)。

---

## 项目结构

```text
FileTools/
├── Dockerfile                      # 单镜像部署（python:3.14-slim + LibreOffice）。**未实测**
├── docker-compose.yml              # 全仓库唯一一处 .env 会被真正读取的地方（env_file）。**未实测**
├── .dockerignore                   # 防止 COPY 把 Windows 的 .venv 拖进 Linux 镜像
├── .env.example                    # 全部 65 个 FILETOOLS_* 变量清单（后端进程**不读** .env）
├── .gitattributes / .editorconfig / .nvmrc   # 换行符、缩进、Node 版本
├── .github/workflows/ci.yml        # CI：pytest + 前端构建
├── VERSION                         # ★ 四个平台唯一的版本号来源（见「品牌资产」）
├── README.md                       # 本文（漏洞上报渠道见「安全措施」一节）
├── LICENSE                         # AGPL-3.0（为什么不是 MIT，见下面「许可证」一节）
├── backend/
│   ├── main.py                     # FastAPI 入口：中间件、异常处理、路由挂载、静态资源托管
│   ├── config.py                   # 全部可调参数（支持环境变量覆盖）
│   ├── requirements.txt            # 运行依赖（6 个，全部锁版本）
│   ├── requirements-heic.txt       # 可选：HEIC 支持（含许可说明）
│   ├── requirements-ocr.txt        # 可选：OCR 支持
│   ├── requirements-dev.txt        # 测试依赖
│   ├── pytest.ini
│   ├── routers/
│   │   ├── compress.py             # POST /api/image/compress
│   │   ├── convert.py              # POST /api/image/convert
│   │   ├── resize.py               # POST /api/image/resize
│   │   ├── metadata.py             # POST /api/image/metadata（第十阶段）
│   │   ├── pdf.py                  # 第三阶段的 9 个 PDF 接口
│   │   ├── pdf_params.py           # PDF 表单参数解析（纸张 / 页边距 / 页面范围）
│   │   ├── pdf_schemas.py          # PDF 接口的响应模型
│   │   ├── office.py               # 第五、六阶段：文档转换 + PDF→Word
│   │   ├── conversion.py           # 第七/九阶段：统一转换中心的 6 个接口
│   │   ├── conversion_params.py    # 统一转换表单参数解析（逐字复用既有解析器）
│   │   ├── conversion_schemas.py   # 统一转换的响应模型
│   │   ├── tasks.py                # GET  /api/tasks/{group_id}
│   │   ├── download.py             # GET  /api/download/{job_id}、/api/preview/{job_id}/{index}
│   │   ├── system.py               # GET  /api/system/workers、/api/system/metrics（第八阶段）
│   │   ├── params.py               # 质量 / 目标大小等公共表单参数的解析
│   │   └── schemas.py              # 批量接口的响应模型
│   ├── conversion/                  # ★ 统一转换中心的能力层（第七/九阶段）
│   │   ├── capability.py           # 纯类型：Capability / OptionSpec（零 import 实现）
│   │   ├── registry.py             # ★ 条目表 —— 能力的唯一事实来源，派生所有视图（零 I/O）
│   │   └── options.py              # 纯校验 + schema 生成
│   ├── services/
│   │   ├── conversion_service.py   # CONVERTERS 表驱动分发 + 图片家族叶子
│   │   ├── conversion_types.py     # DTO 叶子（破 import 环）
│   │   ├── markup_service.py       # html / md 家族叶子
│   │   ├── text_service.py         # txt 家族叶子
│   │   ├── intake.py               # 批量收件：逐个校验并落盘，超限即整批拒绝
│   │   ├── batch_service.py        # 批量收件的共用实现（扩展名预检 + 总量累计 + 重名去重）
│   │   ├── queue_service.py        # 任务队列、任务组/任务项
│   │   ├── worker_pool.py          # ★ 第八阶段：按资源分池的 Worker Pool
│   │   ├── worker.py               # worker 协程、超时、看门狗、重试
│   │   ├── progress.py             # 进度上报
│   │   ├── queue_backend.py        # 队列后端抽象（将来换外部队列只动这里）
│   │   ├── transform_service.py    # 第二阶段的转换/缩放业务流程 + ZIP 打包
│   │   ├── pdf_service.py          # 第三阶段：PDF 输入登记簿、结果登记与 ZIP 打包
│   │   ├── pdf_tools.py            # 第三阶段：六个 PDF 功能的业务流程与超时控制
│   │   ├── pdf_to_docx.py          # 第六阶段：PDF → Word
│   │   ├── ocr_service.py          # 第六阶段：OCR（逐页串行 + 单飞锁）
│   │   ├── office_converter.py     # 第五阶段：**唯一**碰 LibreOffice 的地方（发现/锁/超时/复验产物）
│   │   ├── doc_service.py          # 第五阶段：文档转换的业务流程
│   │   └── job_store.py            # 结果登记簿 + 过期清理线程
│   ├── tasks/
│   │   ├── image_tasks.py          # 图片功能的批量任务处理器与收尾器
│   │   ├── pdf_tasks.py            # PDF 转图片的批量任务处理器
│   │   └── conversion_tasks.py     # 第七/九阶段：统一转换的任务处理器
│   ├── compressors/                # ★ 图片处理的**唯一**实现地（没有第二个图片引擎）
│   │   ├── pipeline.py             # 共用流水线：解码 → 几何变换 → 色彩模式 → 编码 → 达标搜索
│   │   ├── loader.py               # 解码、EXIF 方向校正、像素上限检查
│   │   ├── resizer.py              # 等比/拉伸缩放、最长边限制
│   │   ├── cropper.py              # 第十阶段：裁剪区域
│   │   ├── encoder.py              # 各格式编码与质量参数换算
│   │   ├── metadata.py             # 第十阶段：EXIF / XMP 读写与剥离
│   │   ├── svg.py                  # 第十阶段：SVG 安全模型（净化 → 栅格化）
│   │   ├── heif.py                 # 第十阶段：HEIC 可选组件的注册与运行期探测
│   │   └── image_compressor.py     # 第一阶段压缩核心算法（对外接口未变动）
│   ├── converters/image_converter.py   # 格式转换：同格式拒绝、透明通道处理、各格式前置准备
│   ├── pdf/                        # analyzer / loader / pages / image_to_pdf /
│   │                               # pdf_to_image / merger / splitter /
│   │                               # page_editor / compressor
│   ├── office/
│   │   ├── loader.py               # 文档校验：扩展名 → 容器魔数 → 内部结构
│   │   ├── txt_to_pdf.py           # TXT 排版：字体探测、逐段排版与自动分页
│   │   ├── docx_writer.py          # DOCX 写入
│   │   ├── document_ir.py          # 文档中间表示
│   │   ├── markup_parse.py         # HTML / Markdown 子集 → IR
│   │   ├── markup_render.py        # IR → HTML / Markdown / 纯文本
│   │   └── html_to_pdf.py          # HTML / Markdown → PDF（PyMuPDF Story）
│   ├── utils/                      # errors.py / files.py / logging.py /
│   │                               # metrics.py / validation.py
│   └── tests/                      # 46 个测试模块，见「测试」
│
├── frontend/
│   ├── index.html · vite.config.ts · tailwind.config.js
│   ├── public/                     # 由 branding/generated/web/ 安装：favicon.svg / favicon.ico /
│   │                               # apple-touch-icon.png / icon-192 / icon-512 /
│   │                               # manifest.webmanifest / filetools-logo.svg
│   ├── dist/ · dist-desktop/       # 两份产物：后端托管的网页版 / Tauri 打包用的桌面版
│   └── src/
│       ├── main.tsx · App.tsx      # 路由表
│       ├── config/tools.ts         # 工具清单（首页卡片与导航的唯一数据源）
│       ├── components/             # 见下方「组件复用」说明
│       ├── pages/                  # Home / ImageCompress / ImageConvert / ImageResize /
│       │                           # ImageTools（第十阶段）/ PdfFromImages / PdfToImages /
│       │                           # PdfCompress / PdfMerge / PdfSplit / PdfEditPages /
│       │                           # DocToPdf / PdfToWord（第六阶段）/
│       │                           # Convert（第七/九阶段：统一转换中心）/
│       │                           # ToolList / NotFound
│       ├── services/api.ts         # 所有后端调用
│       ├── services/taskRunner.ts  # 轮询任务快照（500ms）与失败重试
│       ├── hooks/                  # useServerConfig / useFilePreviews / useBatchTask / useHistory /
│       │                           # useCoarsePointer / usePageOrder / usePdfInput /
│       │                           # usePdfMultiTask / usePdfProgress / usePdfToWordProgress /
│       │                           # useConversionCapabilities / useConversionGroups
│       ├── types/index.ts          # 与后端对应的类型
│       └── utils/                  # 格式化（format.ts）、上传前校验（validation.ts）、
│                                   # 错误码文案（errorMessages.ts）、页码解析（pdf.ts）、
│                                   # 最近处理（history.ts）、统一转换（conversion.ts /
│                                   # conversionOptions.ts）、能力开关（serverFeature.ts）
│
├── mobile/                         # Expo（React Native）移动 App，见下方独立树
│
├── desktop/                        # ★ 第十一阶段 A 补充：Windows 桌面版（Tauri 2）
│   ├── package.json                # 只有 @tauri-apps/cli 一个 devDependency（**刻意不写 version**）
│   ├── artifacts/                  # FileTools-Setup-x64.exe 落这里（构建产物，不进版本库）
│   └── src-tauri/
│       ├── Cargo.toml · build.rs · tauri.conf.json · capabilities/default.json
│       ├── icons/                  # 由 branding/generated/windows/ 安装过来
│       └── src/                    # main.rs / lib.rs / result_files.rs
│                                   # ★ 只有「落盘」与「调资源管理器」，没有任何转换引擎
│
├── branding/                       # ★ 四平台品牌真源与全部产物，见下方独立树
│
└── scripts/
    ├── verify_phase2.py … verify_phase10a.py   # 各阶段的真实浏览器 / 真机验收脚本
    ├── verify_phase9a_live.py                  # 后端真机验收（并发、指标、看门狗）
    ├── verify_phase10.py                       # ★ 第十阶段封板入口（210 项断言）
    ├── verify_mobile_phase11a.py               # ★ 第十一阶段 A 封板入口（移动端 107 项断言）
    ├── sync_version.py                         # ★ 把 VERSION 写进各消费方（幂等；--check 只校验）
    ├── generate_branding.py                    # ★ 从唯一真源生成四平台全部图标（--check 只校验）
    ├── verify_branding.py                      # ★ 品牌验收：真源、四平台图标、命名、版本、Docker
    ├── build_windows.py                        # ★ 真构建：产出 FileTools.exe 与安装器（附 sha256）
    ├── verify_desktop.py                       # ★ 桌面验收：六项检查，能构建就真的构建一次
    ├── verify_final.py                         # ★ 最终总验收入口：横切四个平台，环境够不着的写 NOT EXECUTED
    ├── verify_markup_live.py / verify_text_live.py  # 标记语言 / 文本转换的真机验收
    ├── probe_ocr.py                            # 探测本机 OCR 组件是否可用
    ├── acceptance_final.py                     # 跨阶段总验收
    └── make_office_fixtures.py                 # 生成旧格式测试样张（.doc / .xls / .ppt）
```

### 品牌目录（`branding/`）

```text
branding/
├── source/                         # ★ 全仓库唯一的两个 SVG，其余全是它们的副本
│   ├── filetools-icon.svg          # App 图标：正方形、铺满、不含任何文字
│   └── filetools-logo.svg          # 品牌锁排：[Icon] + 字标 FileTools
└── generated/                      # 全部由 scripts/generate_branding.py 生成
    ├── manifest.sha256             # 源与所有输出的哈希清单
    ├── web/                        # → frontend/public/
    ├── android/                    # → mobile/assets/（含 5 档 mipmap）
    ├── ios/                        # icon-1024.png，无 alpha
    └── windows/                    # → desktop/src-tauri/icons/
```

### 移动端目录（`mobile/`）

```text
mobile/                          # Expo（React Native）+ TypeScript，独立于 Web 前端
├── app.json · tsconfig.json · .env.example
├── app/                         # expo-router 的文件式路由，文件名就是 URL
│   ├── _layout.tsx              # 根布局：主题 Provider + 能力 Provider + 错误边界
│   ├── (tabs)/                  # 底部四个标签页（分组约定，不出现在 URL 里）
│   │   ├── _layout.tsx          # 标签栏本身
│   │   ├── index.tsx · tools.tsx · history.tsx · profile.tsx
│   ├── convert/[capabilityId].tsx   # 转换页：动态参数表单 + 上传 + 提交
│   └── task/[batchId].tsx           # 任务页：有界轮询、下载 / 分享、取消
├── src/
│   ├── screens/                 # 五个页面组件（路由文件只做转发）
│   ├── components/              # AppText / Button / Card / OptionForm / ProgressBar /
│   │                            # Screen / States / ToolRow
│   ├── services/
│   │   ├── api/                 # client.ts（统一错误处理）+ capabilities / config /
│   │   │                        # conversion / tasks 四个端点封装
│   │   ├── filePicker.ts        # 选文件（DocumentPicker）
│   │   ├── download.ts          # 下载结果（FileSystem）
│   │   ├── share.ts             # 分享 / 用别的 App 打开（Sharing）
│   │   └── history.ts           # 本地历史（AsyncStorage，**只存元数据**）
│   ├── hooks/useBatchPolling.ts # 有界轮询：有上限、settle 即停、卸载即停
│   ├── state/CapabilitiesProvider.tsx  # 全 App 唯一的能力来源
│   ├── storage/kv.ts            # AsyncStorage 薄封装（Web 上落到 localStorage）
│   ├── theme/                   # 设计令牌（tokens.ts）+ 浅色/深色/跟随系统
│   ├── types/                   # 与后端响应对应的类型
│   └── utils/                   # 参数序列化（options.ts）、错误码文案（errorMessages.ts）、
│                                # 展示文案（labels.ts）
└── assets/                      # 图标与启动图
```

> `make_office_fixtures.py` 不是给部署用的，是给测试用的：它把手写的极简 flat XML
> （`.fodt` / `.fods` / `.fodp`）用本机 LibreOffice 转成**真正的**旧格式文件，
> 放进 `backend/tests/fixtures/`。这样旧格式的测试样张是真的 Office 二进制文件，
> 而不是伪造的字节串。跑一次、产物提交进版本库，之后不需要再跑。
> **它只能证明「往返样张能转」，不能证明任意一份 Word 97 文件都能转** —— 见[已知限制](#已知限制)。

### 组件复用

各阶段的页面共用同一套组件，没有各自复制一份：

| 组件 | 作用 | 用在 |
| --- | --- | --- |
| `Dropzone` | 拖拽 / 点选上传、本地预校验 | 全部页面 |
| `FileList` / `FileCard` | 待处理文件列表与缩略图 | 批量页面 / 单文件页面 |
| `SortableFileList` / `SortablePageList` | 可拖动排序的文件 / 页面列表 + 上移 / 下移 / 删除 | 图片转 PDF、PDF 合并、页面提取 |
| `PdfFileCard` | 已上传 PDF 的文件名 / 大小 / 页数 | 五个单 PDF 页面 |
| `PdfPageGrid` | 页面缩略图选择器（删除态 / 选中态） | 页面删除 / 提取 |
| `ProgressPanel` / `PdfProgressPanel` | 真实上传进度、处理步骤与「等待处理」状态 | 全部页面 |
| `TaskProgressPanel` | 总任务 / 已完成 / 处理中 / 等待 / 失败 + 进度条 + 每个文件的状态行 | 批量页面 |
| `RecentHistory` | 首页「最近处理」，读 `sessionStorage`，不涉及服务器 | 首页 |
| `PdfResultPanel` | PDF 结果的统一面板（文件名、页数、对比指标、ZIP） | PDF 页面 |
| `ImageCompare` / `BatchResultPanel` | 处理前后对比、结果列表、下载全部 | 图片页面 |
| `QualityField` | 质量档位与 1–100 滑块 | 压缩、格式转换、PDF 转图片 |
| `TargetSizeField` | 目标最大文件大小 | 图片页面 |
| `RadioGroup` | 统一的单选卡片（纸张、方向、档位…） | 全部页面 |
| `Alert` | 统一错误 / 警告 / 说明提示 | 全部页面 |
| `ToolPage` | 面包屑 + 标题 + 说明 + 安全提示的页面外壳 | 全部子页面 |
| `ConversionTargetSelector` | 动态目标格式选择器（Recommended / Other formats 两级，数据全来自能力 API） | 统一转换中心 |
| `ConversionOptions` / `ConversionOptionField` | schema 驱动的参数面板（渲染与提交同一条数据） | 统一转换中心 |
| `ConversionTaskList` / `ConversionTaskRow` | 逐项任务行与状态 | 统一转换中心 |
| `useBatchTask` | 选文件 → 上传 → 处理 → 下载的完整状态机 | 图片页面 |
| `usePdfInput` | 「先上传一份 PDF，再对它做各种操作」 | 单 PDF 页面 |
| `usePdfMultiTask` | 多文件一次请求（图片转 PDF、合并） | 图片转 PDF、PDF 合并 |
| `usePdfProgress` | 统一的处理状态文案 | PDF 页面 |
| `useHistory` | 写入 / 读取 / 清空「最近处理」 | 处理完成的页面 + 首页 |
| `useCoarsePointer` | 判断当前是不是手指操作的设备（决定上传区文案） | 上传区域 |
| `usePageOrder` | 页面提取的排序状态（拖拽 + 上移 / 下移） | 页面提取 |

单文件与批量走的是同一个 `useBatchTask`：它把文件交给后端、轮询任务快照（`services/taskRunner.ts`，500 ms）、
把每个文件的状态渲染出来。文件数大于 1 时结果自动打包成 ZIP，等于 1 时直接给单个文件 —— 这条分支在同一个 hook 里。

---

## 压缩是怎么工作的

### 指定了目标大小

1. **原文件已经达标** → 直接返回原文件，不做任何有损处理。
2. **在该质量档位的区间内二分搜索** —— 找到「体积不超过目标」的最高质量。
   每档质量对应一个区间，例如「平衡」是 50–88。
3. **质量降到区间下限仍然超标** → 把图片等比缩小 15%，回到第 2 步重新搜索。
   最多缩小 8 轮，缩到最长边 32 像素为止。
4. 若最终仍无法达标，返回能做到的最小结果，并在界面上说明原因（不会报错）。

结果始终保证 `结果体积 ≤ 目标体积`（除非触达压缩极限）。

搜索全程**在内存里做**（`data_at(quality)` 直接返回字节），磁盘上不存在候选文件，
因此没有「搜索过程中的半成品泄漏出去」这个洞；步数上有上限 `MAX_SEARCH_STEPS = 7`。

**够不到的目标如实说够不到**：比如实测出现过「6009 字节 > 目标 5120 字节」的情形，
产物会如实回报 `target_reached = false`，**不伪造成已达成**。

### 没有指定目标大小

直接使用该档位对应的质量值编码一次：高质量 88、平衡 75、高压缩 55。

### 其他细节

- 手机照片会按 EXIF 方向自动旋转到正向。
- 输出时默认**丢弃 EXIF 元数据**，既减小体积，也避免把拍摄位置等隐私信息带到结果文件里
  （统一转换中心里默认是**保留**，见[图片元数据](#图片元数据第十阶段)）。
- PNG 是无损格式，用「调色板颜色数」近似质量语义；带透明通道的 PNG 不做调色板量化，只做无损压缩。
- 如果重新编码后体积反而变大，会保留原文件并在界面上说明。

---

## 格式转换与图片几何操作是怎么工作的

所有图片功能共用 `compressors/pipeline.py` 里同一条流水线：

```text
解码 → 几何变换（旋转 / 翻转 / 裁剪 / 缩放）→ 色彩模式 → 编码 → 若指定了目标大小，则搜索达标的质量与缩放比例
```

区别只在于中间那一步做什么，所以质量搜索、超时控制、临时文件清理等逻辑只有一份。

### 转换规则

- **同格式不转换**：目标格式与原格式相同时，前端直接禁用按钮并提示「当前图片已经是 PNG 格式，无需转换」；
  即使直接调接口，后端也只会**原样返回原文件字节**（不重新编码），并附上同样的说明。
- **透明通道**：转成 JPG 时，透明区域会被压到白底上（JPEG 不支持透明）；转成 PNG / WEBP 时透明信息原样保留。
  BMP / TIFF 无 alpha 通道，转换时会按各自的格式要求前置处理。
- **GIF 需要 P 模式 + 调色板**；**ICO 需要方形缩放到 16/32/48/256 之一**。
- **多帧 GIF / 多页 TIFF 只处理第一帧**，在图片→图片与图片→PDF **两条路**上都如实写明，不假装转了全部。
- **PNG 目标不做有损压缩**：界面不显示质量设置，后端也不会用量化去换体积。只有在用户**明确指定了目标大小**时，
  才会动用调色板量化——因为那是 PNG 唯一可用的质量杠杆。
- **PNG 短边 < 16 px 会被拒掉**：Pillow 凑不出任何一档图标时**不报错**，会写出一个 6 字节的零帧空壳并报「已完成」。
  现在改抛校验错误，并加了 ICONDIR 兜底 —— 宁可明确拒绝，也不给一个打不开的文件。

### 几何操作

- **旋转** `90` / `180` / `270`，方向是**顺时针**（API 的语义）。
  实现上要转成 PIL 的 `ROTATE_90/180/270` —— 那几个是**逆时针**，所以映射表是刻意的：
  `{90: ROTATE_270, 180: ROTATE_180, 270: ROTATE_90}`。
- **翻转**：水平用 `ImageOps.mirror`，垂直用 `ImageOps.flip`。
- **裁剪**：指定区域，边界会被夹到图片范围内。
- **顺序很重要**：先按 EXIF 方向**转正**，再做几何操作。反过来的话，竖拍照片上的「旋转 90°」会得到反方向的结果。
  转正后 orientation 标记会被移除（否则查看器会再转一次，等于转了两下）。

### 缩放规则

- **保持宽高比例（默认勾选）**：只填一个边时，后端按每张图片**自身的原始比例**算出另一边。批量处理时不会拿第一张的比例去套其他图片。
- **两个边都填 + 保持比例**：图片会被**完整放进**该尺寸框内（`min(宽比, 高比)`），不裁剪、不变形；界面上会实时显示「保持比例后实际输出：1080 × 810 px」。
- **取消保持比例**：宽高各自生效，图片会被拉伸到指定尺寸。
- **放大与缩小都支持**，最长边受 `MAX_IMAGE_EDGE` 限制。

### 预览能看哪些格式

结果预览只对浏览器**原生就能解**的格式开放：`bmp` / `gif` / `jpg` / `png` / `webp`。
TIFF / HEIC / ICO 的结果**不产生** `preview_url`（浏览器解不了，给了也是白给），
界面上不会出现一个点了没反应的预览按钮。

预览**不消耗**下载令牌，可以反复看；但下载之后预览必然 404 —— 因为两者读的是同一份产物文件，
下载完它就按规则删掉了。这是预期行为，不是 bug。

### 批量规则

- 一次最多 50 张（`MAX_BATCH_FILES`），整批合计不超过 300 MB（`MAX_BATCH_TOTAL_BYTES`）。
- 采用「先全部收件校验，再统一处理」：任一文件不合法会在处理开始前就整批拒绝，不会出现「处理了一半才报错」。
- 单个文件在处理阶段失败（例如解码异常）不会中断整批，其余文件照常输出，失败原因在结果页逐条列出。
- 只有一张图片时直接返回图片本身，多张时打包为 ZIP。ZIP 使用 `ZIP_STORED`
  （图片本身已压缩，再套一层 deflate 只是白费 CPU）。
- 结果文件名统一加后缀（`_converted` / `_resized` / `_compressed`），重名时自动加序号。

### 图片炸弹防线

| 常量 | 默认值 | 触发时的文案 | 返回 |
| --- | --- | --- | --- |
| `MAX_IMAGE_PIXELS` | 80,000,000 | 图片像素总量过大，超过服务器处理上限 | `400 INVALID_REQUEST` |
| `MAX_IMAGE_EDGE` | 12,000 | 图片尺寸过大，超过服务器处理上限 | `400 INVALID_REQUEST` |

两个带都是**业务错误（4xx）而不是 500**，旧接口与统一接口返回的是**同一句话**。
同时验过「没有误伤」：一张合法的 6000 × 1000 图片仍然正常转换成功 ——
阈值卡在真实的资源上限上，不是调到谁都不敢用。

### 请求参数

`target_bytes` 与 `quality_value` 是图片接口共用的可选参数：

| 参数 | 说明 |
| --- | --- |
| `quality_value` | 1–100 的整数。不传时用服务端默认值（`DEFAULT_QUALITY_VALUE`，默认 85）；PNG 等无损格式走无损编码 |
| `target_bytes` | 目标最大文件大小（字节）。不传或传 `0` 表示不限制 |

---

## PDF 工具是怎么工作的

六个功能都跑在同一个线程池里（`services/intake.py` 的 `run_in_pool`），每个操作有自己的超时与中文超时提示：

| 操作 | 超时 | 超时提示 |
| --- | --- | --- |
| 图片转 PDF | 180 s | 生成 PDF 超时，请减少图片数量后重试 |
| PDF 转图片 | 300 s | 导出图片超时，请减少导出页数后重试 |
| 合并 | 180 s | 合并超时，请减少文件数量后重试 |
| 拆分 | 240 s | 拆分超时，请减少拆分的份数后重试 |
| 删除 / 提取 | 120 s | 删除 / 提取页面超时，请减少页数后重试 |
| 压缩 | 300 s | 压缩超时，请换一个更小的文件或更低的等级 |

「单文件上传 → 反复操作」的五个功能（转图片、拆分、删除、提取、压缩）走的是同一套流程：
上传一次拿到 `input_id` 与服务端**真实识别出来的页数**，之后每次操作只传 `input_id` 与参数，不必重新上传。
上传的原始文件在服务端保留 30 分钟（`PDF_INPUT_TTL`），超时后再点处理会收到「文件已过期，请重新上传」，
界面会自动退回上传步骤。

### 校验顺序

`pdf/loader.py` 里的判断顺序是刻意安排的，为的是让提示准确：

```text
扩展名白名单 → 文件头是不是 %PDF → PyMuPDF 能不能打开 → 是否加密 → 有没有页面 → 页数是否超限
```

拿一个 Word 文档改扩展名成 `.pdf` 过来，用户看到的是「暂不支持该文件格式，请上传 PDF 文件。」，而不是含糊的「文件损坏」。
文件是从**内存流**打开而不是按路径打开的：这样 PyMuPDF 不持有文件句柄，Windows 上不会出现
「损坏文件删不掉、清理时的报错盖住真正的报错」。

### 图片转 PDF 的页面尺寸

- **自动**：页面 = 图片像素尺寸（1 像素 = 1 点），方向参数此时无意义
- **A4 / A5 / Letter**：页面固定为该纸张，方向按「自动 / 纵向 / 横向」决定；自动时看图片本身是横是竖
- **自定义**：填写的毫米数 × 72/25.4 换算成点
- **页边距**在最终页面上四边各留出对应点数（无 0 / 小 5 mm / 中 10 mm / 大 20 mm，
  即 0 / 14.17 / 28.35 / 56.69 点）
- **适应方式**：「保持比例」把图片完整放进内容区（不裁剪、不变形）；「填充页面」铺满内容区，超出部分裁掉

### PDF 转图片

按选定的 DPI 渲染页面（标准 96 / 高清 150 / 超清 300，上限 300），再编码成 JPG / PNG / WEBP。
渲染前会先算这张图会有多少像素，超过 4000 万像素就拒绝，避免一份超大页面把内存吃满。
PNG 不做有损压缩，所以界面上不显示质量选项。

### 拆分与输出的文件名

| 功能 | 输出 |
| --- | --- |
| 图片转 PDF | 一个 PDF，文件名由第一张图片的名字生成 |
| PDF 转图片 | 每页 `page-01.jpg` 这样的文件；多于一页打包为 `pdf_pages.zip` |
| 合并 | `merged.pdf` |
| 拆分 · 每页一个 | `part-01.pdf` …（编号位数按份数取 2 位起），打包为 `<原名>_parts.zip` |
| 拆分 · 按范围 | 同样是一份一个 `part-NN.pdf`，多于一份时打包 |
| 拆分 · 自定义页面 | `selected-pages.pdf`（连字符） |
| 页面删除 | `<原名>_edited.pdf` |
| 页面提取 | `selected_pages.pdf`（下划线） |
| 压缩 | `<原名>_compressed.pdf` |

> 两处「选中页面」的文件名不一样：拆分用 `selected-pages.pdf`、提取用 `selected_pages.pdf`，
> 这是照需求文档的字面要求分别实现的，不是笔误。

### 压缩是怎么做到的

和图片压缩同一条思路：**PDF 的体积绝大部分来自图片，所以压缩就是重新编码页面里的图片**。

- 等级决定「图片分辨率上限」与「重编码质量」：轻度 200 DPI / 85，平衡 150 DPI / 72，高压缩 96 DPI / 55。
- 关于 PyMuPDF 的 `rewrite_images`：它的 `dpi_target` 不是「缩放到这个 DPI」，而是**半衰的下限** ——
  一张 290 DPI 的图在 `dpi_target=150` 时一像素都不会动。所以这里传的是 `dpi_threshold=档位`、
  `dpi_target=档位 / 2`，等价于一句可以写进文档的保证：**结果里没有图片超过所选档位的 DPI**。
- 指定了目标大小时，从所选等级开始**逐档加强**（110/60 → 80/45 → 60/38 → 48/35），哪一档先达标就停；
  全都达不到就给出最小的一档，并在结果页**如实说明没有达标**。
- 如果压缩后的结果反而比原文件更大，直接保留原文件并在界面上说明 —— 用户点「压缩」却拿到一个更大的文件是最糟的结果。
- 实测：一份三页的图片型 PDF（2000 × 1500 的 JPEG）用「高压缩」从 187911 字节降到 46566 字节（约 75%）。

### 页面范围怎么填

1 开始计数，支持 `all`、`1-3`、`1,3,5`、`2-6`，分隔符可以用中文逗号、分号或空格。
前端 `utils/pdf.ts` 与服务端 `pdf/pages.py` 用的是同一套规则与同一批提示文案：
前端让用户在输入框旁边**立刻**看到问题（例如「第 9 页不存在，该 PDF 共 3 页」），
服务端仍会重新校验一遍，前端不是唯一防线。

---

## 文档转换是怎么工作的

### 校验：三层，比扩展名严

上传的文档要过三层（`office/loader.py`），三层的失败对应三种**不同的用户动作**：

| 层 | 看什么 | 失败时用户该做什么 |
| --- | --- | --- |
| 1. 扩展名 | 是否在白名单里 | 换成支持的格式 |
| 2. 容器魔数 | OOXML 是 `PK\x03\x04`（zip），旧格式是 OLE2 的 `D0CF11E0A1B11AE1` | 文件根本不是这个格式 —— 改名改错了 |
| 3. 内部结构 | OOXML：zip 里有没有 `[Content_Types].xml`，声明的类型对不对，主部件在不在<br>旧格式：OLE2 目录项里有没有该类必有的流名<br>TXT：能不能按 utf-8-sig / utf-8 / gbk / utf-16 解出文字 | 文件坏了，重新拿一份 / 重新导出一份 |

第三层的旧格式部分值得说明：**OLE2 的头三种格式长得一模一样**（`.doc` / `.xls` / `.ppt` / `.msi` 都是它），
所以只看魔数挡不住改名。这里往下看了一层 —— 容器里的**流名**：
Word 必有 `WordDocument`，Excel 必有 `Workbook` 或 `Book`（Excel 5.0/95 用的是 `Book`，只认前者会误杀老文件），
PowerPoint 必有 `PowerPoint Document`。这样一份 `.doc` 改名叫 `.xls` 会得到
「文件扩展名（.xls）与真实内容（.doc）不一致，已拒绝处理」，而不是一路走到转换器再报一句「转换失败」。

TXT 没有魔数可用，走的是**编码探测**：头部含 NUL 字节且不是 UTF-16 BOM → 判定为二进制文件拒掉
（一个 `.exe` 改名成 `.txt` 在这里被挡下）；然后按 `utf-8-sig → utf-8 → gbk → utf-16` 依次尝试，
全失败才算损坏。顺序是有讲究的：`utf-16` 放最后，因为它没有 BOM 时能把任意字节对「成功」解成一片汉字，
排前面会把 GBK 文件解成乱码。同一份 `decode_text()` 被校验层和排版层共用，避免出现「校验说能解、排版却解不开」的裂缝。

### Office 三种：LibreOffice headless

调用全部隔离在 `services/office_converter.py` 一个模块里，**接口路由里没有一行 `subprocess`**。
对外是四个函数，签名统一为「源文件路径 → 产出 PDF 路径」：

```python
convert_word_to_pdf(source, work_dir) -> Path
convert_excel_to_pdf(source, work_dir) -> Path
convert_powerpoint_to_pdf(source, work_dir) -> Path
convert_txt_to_pdf(source, work_dir, *, options) -> tuple[Path, list[str]]
```

这个模块里踩过并写进注释的坑：

- **必须单飞**。两个 `soffice` 进程共用同一份 profile 时，**第二个会静默失败**：`exit=1`、无输出、无日志。
  所以有一把模块级 `threading.Lock()`。锁有等待上限（`FILETOOLS_OFFICE_LOCK_WAIT`，默认 60 秒），
  超时报「服务器正忙」—— 无限等会把线程池耗光。
- **两层超时，内层严格小于外层**。内层 `subprocess.run(timeout=...)` 超时后杀进程并抛超时错，
  保证正常路径下锁是在锁内先释放的；外层兜底。
- **优先 `soffice.com` 而不是 `soffice.exe`**，理由见[指定路径](#libreoffice-安装要求)。
- **每次转换用独立的输出目录**。同名输入进同一个 outdir 会互相覆盖（日志里写着 "Overwriting"）。
- **profile 目录名是 `office-converter-profile`，一个 `filetools` 字样都没有**。孤儿目录清理
  （`sweep_orphan_dirs`）是按 `filetools_` 前缀扫的，靠 `-` 和 `_` 的差别去躲太脆了。
  另外加了**孤儿 profile 恢复**：转换无输出且 profile 里残留 `.lock`（soffice 被强杀的痕迹）时，
  删掉 profile 重试一次 —— 否则一次强杀会让这个功能永久坏掉。
- **产物必须复验**。转出来的 PDF 会用 `pdf/loader.py::open_pdf` 再打开一次，
  打不开或页数 ≤ 0 就报「转换结果无法读取，请重新上传文件。」，**绝不把坏 PDF 交给用户**。

失败如实翻译，`stdout` / `stderr` 只进日志、绝不外传：

| 情况 | 返回 |
| --- | --- |
| 找不到 LibreOffice | `503 CONVERTER_UNAVAILABLE`「当前服务器缺少 Office 转换组件，请联系管理员。」 |
| 没有产物，且判为**源文件读不了** | `400 CORRUPTED_FILE`「无法读取该 Office 文件，请检查文件是否损坏。」 |
| 没有产物，但不是上面那种 | `422 PROCESSING_FAILED`「文件转换失败，请尝试重新上传文件。」 |
| 超时 | `504 PROCESSING_TIMEOUT` |

> 缺组件的检查在**落盘之前**做：不为一个注定失败的请求把 50 MB 写进磁盘。

**「源文件读不了」不是一个退出码就能判的。** 同一个损坏的 `.docx`，本机（Windows）上
`soffice` 退出码是 **1**，而 CI 的 Linux runner 上实测是 **0**。只押退出码，Linux 上就会把
「文件已损坏」（该做的是重新拿一份文件）说成「转换失败」（该做的是重试），
用户于是反复重传一份永远传不好的文件。

所以 `_source_could_not_be_loaded()` 用**三个并列的信号**，任一成立即判为读不了：

1. **进程结果** —— 退出码非 0；
2. **输入校验** —— OOXML 的主部件（`word/document.xml`、`xl/workbook.xml`…）在不在、
   是不是**良构的 XML**（`office/loader.py::ooxml_main_part_is_broken`；流式 expat，不建树，
   遇到实体声明直接判不良构，顺手挡住 billion laughs 与 XXE）。**这一条与平台无关**，
   补上的正是第 1 条在不同平台上不一致的那条缝；
3. **进程输出** —— `stderr` 里那句 `could not be loaded`。这只是**补充**信号：
   它是 LibreOffice 的英文原样输出，换个本地化版本可能就变语言了，所以不当主判据。

三条都**只在没有任何产物时**才被问到；有产物就走上面那条产物复验的路。
所以一个能转出可用 PDF 的文件永远不会被它们误伤 —— 有一条反向护栏测试钉着这件事：
**结构完全正常**的 `.docx`、退出码 0、却没有产出，必须仍然是 `422`（可以重试），
不许被顺手改判成 `400`。

### TXT：本服务自己排版

TXT 不走 LibreOffice，用 PyMuPDF 直接排版（`office/txt_to_pdf.py`）：

1. 按段落切分，用 `insert_textbox` 逐段排版。放得下返回空列表；放不下返回
   `[(未排下的片段, 剩余高度), …]` —— 把这些片段拼回去当下一页的开头，如此翻页。
   单个超长单词或无空格的长行有兜底，**不会死循环**。
2. `doc.subset_fonts()` 是**必须的，不是优化**：一页中文 TXT 不做字体子集化是 **1.62 MB**，
   做了是 **9.9 KB**（0.6%）。漏掉这一步结果会大到荒唐。

**字体是运行时探测的，不是写死的。** 这一点值得展开：PyMuPDF 的内置 CJK 码
（`china-s` / `china-ss` / `china-t` / `china-ts` / `japan` / `korea`）实测
**全部解析成同一个 `Droid Sans Fallback`** —— 拿它们做「宋体 / 黑体」的选择器，
就是一个看着能点、实际毫无效果的**假控件**。所以字体列表改为在系统字体目录里
按候选文件名探测（`simsun.ttc` → 宋体，`simhei.ttf` → 黑体，……），
**探测到哪个就列哪个**，一个中文字体都没探测到就只给「内置字体」一项并说明原因
（标签刻意写「内置字体」而不是「内置黑体」：内置码都指向 Droid Sans Fallback，标成宋体或黑体都是假话）。

排版参数：字号默认 11（正文常用 10–12）、行距 1.5 倍、页边距 20 毫米（不暴露给用户）。
另有两条兜底上限：单文件 50 万字符、单份最多 500 页 —— 几十万字的 txt 会排成几百页，
用户等不到结果，服务器也在白烧 CPU，与其拖死不如明确拒绝。

### 「最大文件大小」是尽力而为

LibreOffice **无法指定输出体积**，只能转完再压。所以流程是：转换 → 如果结果超过目标，
用已有的 PDF 压缩器（`pdf/compressor.py`）从默认档位开始逐档加强，压到了就停。

压不到时如实说明，绝不假装成功：

- 本来就比目标小 → 「转换结果已经只有 X，不超过目标大小，因此没有再做压缩。」
- 压到了 → 「已达到目标大小（不超过 X）。」
- 压不到 → 「已用到最高压缩档位，仍未达到 X（当前 Y）。这份 PDF 的内容已经高度压缩，无法再明显缩小。」
- 压完反而更大 → 保留原文件并对用户说明（**绝不返回一个更大的文件**）

> **实测结论**：LibreOffice 在导出 PDF 时**已经对图片做过很强的压缩**
>（2134 KB 的 JPEG 进去，892 KB 的 PDF 出来），所以我们再压往往反而更大、于是保留原文件。
> 因此在 Office 这条链路上，用户最常看到的是「没有再做压缩」或「仍未达到」+「保留了原文件」。
> 纯文字 PDF 更是几乎压不动（实测 643 KB → 631 KB，体积在字体和文字流上）。

### Excel 与 PPT 的页数

- **Excel**：一个工作簿里的全部工作表会导出进同一份 PDF（一页工作表一页 PDF），
  结果里如实写明「已导出 N 个工作表」。**隐藏的工作表不会被导出** —— 这句话必须说，
  因为隐藏表里往往正是用户不想给人看的东西。
- **PPT**：一页幻灯片对应 PDF 的一页，结果里写明幻灯片总页数与实际输出页数。隐藏的幻灯片同样不导出。

这两条都用真样张测过（做一份带隐藏表/隐藏页的样张，断言隐藏内容**不在** PDF 里）。

### 旧格式（.doc / .xls / .ppt）

旧格式是 OLE2 二进制容器，手写不出来，所以测试样张是**用本机 LibreOffice 生成的真文件**：
`scripts/make_office_fixtures.py` 把手写的极简 flat XML（`.fodt` / `.fods` / `.fodp`，
单文件 XML，比 zip 包好写太多）用 `--convert-to doc:"MS Word 97"` 等转成真的 `.doc` / `.xls` / `.ppt`，
校验输出以 OLE2 魔数开头后放进 `backend/tests/fixtures/`。

测试断言的不是「页数 ≥ 1」（LibreOffice 对打不开的文件也会给出一页空白），
而是**从转换出来的 PDF 里读出样张正文的标记文字**。

> 这只能证明**往返样张**能转，**证明不了**任意一份真实的 Word 97 文件都没问题 ——
> 真实的旧文件里可能有宏、有嵌入对象、有 OLE 链接。见[已知限制](#已知限制)，不假装覆盖到了。

---

## PDF 转 Word 是怎么工作的

`services/pdf_to_docx.py`，第六阶段。

### 文字层优先，OCR 兜底

对每一页分别判断：

1. 用 PyMuPDF 抽出该页的文字。**字符数 ≥ 阈值**（`FILETOOLS_PDF_TO_WORD_MIN_CHARS`，默认 16）
   → 判定为**文字页**，直接走文字层，结果是真正可编辑、可搜索的文本。
2. 否则 → 判定为**扫描页**，对该页做 OCR。

这样一份「前几页是文字、后几页是扫描件」的混合 PDF 也能得到合理结果，
而不是整份文件一起走 OCR（那样会把本来清晰的文字层也糊掉）。

### OCR 的代价与边界

| 项 | 值 | 配置项 |
| --- | --- | --- |
| 语言 | `chi_sim` + `eng` | `config.OCR_LANGUAGES` |
| 单页耗时 | 约 2 秒 | — |
| OCR 页数上限 | 30 页 | `FILETOOLS_PDF_TO_WORD_MAX_OCR_PAGES` |
| OCR DPI | 200（重试 300） | `FILETOOLS_PDF_TO_WORD_OCR_DPI` / `_RETRY_DPI` |
| 等锁上限 | 60 秒 | `FILETOOLS_PDF_TO_WORD_OCR_LOCK_WAIT` |
| 整个转换超时 | 300 秒 | `FILETOOLS_PDF_TO_WORD_TIMEOUT` |

**OCR 是逐页串行的，且有单飞锁** —— 同时跑两个 OCR 不会更快，只会互相拖慢并把内存吃满。
超过 30 页的部分会**如实说明没有做 OCR**，不会静默跳过。

### 版面

- 文字按抽取到的位置写进 DOCX，保留段落结构
- 图片型页面以图片形式嵌入（150 DPI / JPEG 质量 80），保证版面不丢
- 中文字体默认宋体（`FILETOOLS_PDF_TO_WORD_FONT`）
- 嵌入图片总量有上限 `FILETOOLS_PDF_TO_WORD_MAX_EMBED_BYTES`（默认 60 MB），防止一份超大 PDF 把内存吃满

### 取消是协作式的

与 Office 转换同理：正在跑的 OCR **停不下来**。请求取消后界面如实显示
「已请求取消，正在等待当前文件处理完」，等当前这页跑完才真正停止 —— **不假装已经停了**。

---

## API 说明

全部 **32 条**路由。错误响应统一为 `{ "error": { "code", "message" } }`，`message` 是可直接展示给用户的中文。

### 统一转换中心（第七 / 九阶段）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/conversion/capabilities` | **前端唯一的能力来源**：能力矩阵、转换列表、操作列表、格式、选项 schema |
| POST | `/api/conversion/tasks` | 提交统一转换任务（multipart，字段名 `files`），返回 `202` + `batch_id` |
| GET | `/api/conversion/tasks/{batch_id}` | 批次快照（逐项列表在 `tasks` 键下） |
| GET | `/api/conversion/tasks/{batch_id}/progress` | 轻量进度 |
| POST | `/api/conversion/tasks/{batch_id}/cancel` | 取消整批 |
| POST | `/api/conversion/tasks/{task_id}/retry` | 重试单项 |

支持查询参数过滤：`?source_type=&target_type=&category=&operation_type=`。

`POST /api/conversion/tasks` 的表单字段：

| 字段 | 说明 |
| --- | --- |
| `files` | 待转换的文件（可多个） |
| `target_type` | 目标格式，如 `png` / `pdf` / `docx` |
| `options` | 可选的 JSON 字符串（≤ 8 KB），扁平点号键，如 `{"quality":90,"resize.mode":"medium"}` |
| `capability_id` | 可选，显式指定能力 ID |

外加**七个**从第一到第五阶段冻结至今、一字未改的扁平字段：
`quality_preset` / `quality_value` / `target_bytes` / `width` / `height` / `keep_aspect` / `progress_ids[]` ——
旧调用方不需要改任何东西。

`options` 的校验是五层的，每层归属明确：

1. **语法层**：≤ 8 KB / 合法 JSON / 必须是对象 / 键名合法
2. **白名单层**：键必须在该目标下所有能力的 schema **并集**里（提交时还不知道源格式），未知键 400
3. **覆盖层**：`options` 里的同名键覆盖扁平字段
4. **类型与边界层**：纯函数校验，零 I/O
5. **绑定层**：**逐字复用既有解析器** —— 边界与中文错误文案不可能漂移，资源上限天然绕不过

### 图片接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/image/compress` | 压缩（异步：`202` + `group_id`） |
| POST | `/api/image/convert` | 格式转换（异步） |
| POST | `/api/image/resize` | 尺寸调整（异步） |
| POST | `/api/image/metadata` | **只读**查看元数据（第十阶段）。同步返回，不产出文件、不进队列 |

`POST /api/image/compress` 的表单字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `files` | File[] | 是 | 待压缩的图片，最多 `max_batch_files` 个 |
| `quality` | string | 否 | `high` / `balanced` / `strong`，默认 `balanced` |
| `target_bytes` | string | 否 | 目标大小上限（字节）。留空或传 `0` 表示不限制 |

响应 `202`：

```json
{
  "group_id": "8f3c1a...",
  "tool": "image.compress",
  "label": "图片压缩",
  "total": 2,
  "status_url": "/api/tasks/8f3c1a..."
}
```

之后轮询 `GET /api/tasks/{group_id}`，完成后从快照的 `result.download_url` 下载。
单个文件的结果字段（`saved_bytes`、`target_met`、`quality_used`、`scale`、`untouched`、`note` 等）
与第一阶段完全一致，只是从「响应顶层」挪到了 `tasks[i].result` 与 `result.items[i]` 里。

`POST /api/image/convert` / `resize` 的表单字段：

| 接口 | 字段 |
| --- | --- |
| `/convert` | `files`、`target_format`（见能力矩阵）、`quality_value` / `quality_preset`、`target_bytes` |
| `/resize` | `files`、`width`、`height`、`keep_aspect`（默认 `true`）、`quality_value`、`target_bytes` |

任务快照里的 `result` 结构：

```json
{
  "download_url": "/api/download/8f3c1a...",
  "items": [
    {
      "index": 0,
      "original": { "filename": "a.jpg", "size": 901234, "width": 4000, "height": 3000, "format": "jpeg" },
      "result":   { "filename": "a.png", "size": 2312345, "width": 4000, "height": 3000, "format": "png" },
      "preview_url": "/api/preview/8f3c1a.../0",
      "saved_bytes": -1411411,
      "saved_percent": -15.7,
      "target_met": true,
      "quality_used": 80,
      "scale": 1.0,
      "untouched": false,
      "note": null
    }
  ],
  "original_total": 1802468,
  "result_total": 4624690,
  "archived": false,
  "archive_filename": null,
  "expired": false
}
```

| 字段 | 含义 |
| --- | --- |
| `items` | 每个成功文件的处理结果，`index` 对应上传顺序 |
| `preview_url` | 该结果图片的预览地址，反复查看也不会消耗下载令牌；不可预览的格式为 `null` |
| `archived` | 结果是否打包成了 ZIP（多张时为 `true`） |
| `archive_filename` | ZIP 的文件名 |
| `saved_percent` | 为负数表示结果比原图更大（例如 JPG 转 PNG 无损输出） |
| `expired` | 结果文件已被下载或已过期删除，此时 `download_url` 为 `null` |

失败的文件不在 `items` 里，而是逐条出现在快照的 `tasks` 中（`state: "failed"` + `error_code` / `error_message`），
单个文件失败不影响其余文件。

### PDF 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/pdf/upload` | 上传一份 PDF，返回 `input_id` 与真实页数 |
| GET | `/api/pdf/input/{input_id}/thumb/{page}` | 页面缩略图（页码从 **0** 开始），不消耗下载令牌 |
| POST | `/api/pdf/from-images` | 图片转 PDF |
| POST | `/api/pdf/to-images` | PDF 转图片（**异步**：`202` + `group_id`） |
| POST | `/api/pdf/merge` | 合并 |
| POST | `/api/pdf/split` | 拆分 |
| POST | `/api/pdf/delete-pages` | 删除页面 |
| POST | `/api/pdf/extract-pages` | 提取页面 |
| POST | `/api/pdf/compress` | 压缩 |

除「PDF 转图片」外，其余五个处理接口共用同一个响应结构：

```json
{
  "job_id": "8f3c1a...",
  "download_url": "/api/download/8f3c1a...",
  "filename": "merged.pdf",
  "size": 5231456,
  "media_type": "application/pdf",
  "archived": false,
  "archive_filename": null,
  "page_count": 9,
  "original_size": 4139008,
  "original_pages": 9,
  "saved_bytes": null,
  "saved_percent": null,
  "files": [],
  "notes": []
}
```

各接口的表单字段：

| 接口 | 字段 |
| --- | --- |
| `/from-images` | `files`、`page_size`（`auto`/`a4`/`a5`/`letter`/`custom`）、`orientation`（`auto`/`portrait`/`landscape`）、`fit`（`contain`/`fill`）、`margin`（`none`/`small`/`medium`/`large`）、`custom_width_mm` / `custom_height_mm`（仅 custom，20–2000） |
| `/to-images` | `input_ids`（**可多个**，即一次把几份已上传的 PDF 一起导出）、`target_format`（`jpg`/`png`/`webp`）、`resolution`（`standard`/`high`/`ultra`）、`pages`（留空＝全部）、`quality`（1–100，PNG 不传） |
| `/merge` | `files`（多个 PDF，顺序即合并顺序） |
| `/split` | `input_id`、`mode`（`every`/`ranges`/`selected`）、`pages` |
| `/delete-pages` | `input_id`、`pages`（要删掉的页） |
| `/extract-pages` | `input_id`、`pages`（要保留的页） |
| `/compress` | `input_id`、`level`（`light`/`balanced`/`strong`）、`target`、`target_mb`（仅 custom，0.1–500） |

### 文档转换与 PDF → Word 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/office/word-to-pdf` | Word（.docx / .doc）→ PDF |
| POST | `/api/office/excel-to-pdf` | Excel（.xlsx / .xls）→ PDF |
| POST | `/api/office/powerpoint-to-pdf` | PowerPoint（.pptx / .ppt）→ PDF |
| POST | `/api/office/txt-to-pdf` | TXT → PDF（本服务自己排版，不走 LibreOffice） |
| POST | `/api/office/pdf-to-word` | PDF → Word（第六阶段） |
| GET | `/api/office/pdf-to-word/progress/{progress_id}` | PDF → Word 的真实进度 |

前四个都是**一发式**：直接把文件传上来，处理完返回结果，没有「先上传拿 `input_id` 再处理」那一步。
这是刻意的：文档只存在于处理它的那一次请求里，服务器上不留副本。

表单字段：

| 接口 | 字段 |
| --- | --- |
| 前三个 | `file`（单文件）、`target`（`none`/`500kb`/`1mb`/`2mb`/`5mb`/`10mb`/`custom`）、`target_mb` |
| `/txt-to-pdf` | `file`、`font`、`font_size`（8–32，默认 11）、`page_size`（`a4`/`a5`/`letter`）、`orientation` |
| `/pdf-to-word` | `file`、`progress_id`。OCR 与嵌入图片的参数**全部取自服务端配置**，不由请求指定 |

### 下载、预览与系统信息

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/download/{job_id}` | 下载结果。**只能使用一次**，下载完临时文件立即删除 |
| GET | `/api/preview/{job_id}/{index}` | 以 `inline` 返回结果图片。**不消耗下载令牌** |
| GET | `/api/tasks/{group_id}` | 旧式任务快照（第一到第五阶段的批量接口） |
| GET | `/api/system/workers` | 各 Worker 池的并发、在跑数、超时配置（第八阶段） |
| GET | `/api/system/metrics` | 任务计数、重试数、看门狗出手次数等（第八阶段） |
| GET | `/api/config` | 前端需要知道的限制配置与**全部错误码** |
| GET | `/api/health` | 健康检查 |

> `FILETOOLS_SYSTEM_API=0` 时 `/api/system/*` 会落到 SPA 兜底，**返回 `200 text/html` 而不是 404** ——
> 但路由是真的没注册（`/openapi.json` 里查不到），一个字段也不吐。查「接口关没关」要查 openapi，不要看状态码。

### 错误响应

```json
{ "error": { "code": "INVALID_FILE_TYPE", "message": "文件扩展名（PNG）与真实内容（JPEG）不一致，已拒绝处理" } }
```

服务端一共会返回 **25 个**错误码（`GET /api/config` 里的 `error_codes` 就是这一份，前端按它逐条对文案）：

| HTTP | code | 场景 | 用户该做什么 |
| --- | --- | --- | --- |
| 400 | `INVALID_REQUEST` | 参数非法、文件为空、目标档位不认识、图片炸弹 | 改参数 / 换小图 |
| 400 | `CORRUPTED_FILE` | 内容能收下但解析不了 | 重新拿一份文件 |
| 400 | `PDF_EMPTY` | 这份 PDF 一页都没有 | 换一份文件 |
| 400 | `PDF_ENCRYPTED` | PDF 需要密码 | 换一份没加密的 |
| 400 | `PDF_TOO_MANY_PAGES` | 超出单次页数 / 份数上限 | 拆开分几次 |
| 400 | `PDF_PAGE_NOT_FOUND` | 页码不存在 | 改页码 |
| 400 | `UNSUPPORTED_CONVERSION` | 文件收下了，但**这个源→目标组合**不在能力矩阵里 | 换一个目标格式 |
| 404 | `JOB_NOT_FOUND` | 下载令牌不存在或已过期（预览链接失效也是它） | 重新处理一次 |
| 404 | `TASK_NOT_FOUND` | 异步任务不存在或已过期 | 重新提交 |
| 409 | `TASK_NOT_RETRYABLE` | 只有**失败**项能重试，且源文件还在 | 重新提交整批 |
| 413 | `FILE_TOO_LARGE` | 超过上传大小上限 | 换小一点的文件 |
| 415 | `INVALID_FILE_TYPE` | 类型不在白名单，或内容与扩展名不符 | 换成支持的格式 / 传对页面 |
| 422 | `PROCESSING_FAILED` | 处理过程本身失败 | 重试 |
| 422 | `PDF_NO_TEXT` | 抽不出文字，OCR 也没认出来 | 换一份清晰些的 PDF |
| 422 | `OCR_FAILED` | OCR 组件在，但这次识别失败了 | 重试 |
| 422 | `DOCX_GENERATION_FAILED` | 文字拿到了，但 Word 文件写不出来 | 重试 |
| 500 | `WORKER_LOST` | 执行这个任务的 worker 不在了（会自动重试） | 重试 |
| 500 | `SERVER_ERROR` | 其他未预期错误 | 重试 |
| 503 | `CONVERTER_UNAVAILABLE` | 服务器缺少 Office 转换组件 | **联系管理员**（重传没用） |
| 503 | `OCR_UNAVAILABLE` | 这一页需要 OCR，但服务器没装 OCR 组件 | **联系管理员**（重传没用） |
| 503 | `SERVER_BUSY` | 线程池排满了，任务还没真正开工 | **稍等再试**（立刻重试只会再排满一次） |
| 503 | `TEMPORARY_IO_ERROR` | 读写临时文件出错（磁盘满 / 文件被占用） | 稍后重试 |
| 504 | `PROCESSING_TIMEOUT` | 处理超时 | 换小一点的文件或稍后重试 |
| 504 | `PDF_CONVERSION_TIMEOUT` | 超过 PDF→Word 这个功能自己的处理时限 | 减少页数或稍后重试 |
| 504 | `TASK_TIMEOUT` | 整个任务超过了它自己的处理时限 | 重试 |

> **为什么这几个单独成码，而不是统统复用 `PROCESSING_FAILED`**：
> 复用会让用户一直重传一份**根本没问题的**文件 —— 缺组件的三种（`CONVERTER_UNAVAILABLE` /
> `OCR_UNAVAILABLE` / `SERVER_BUSY`）前端给的是「联系管理员」或「稍等」，而不是「请重试」；
> `UNSUPPORTED_CONVERSION`（400，组合不支持）与 `INVALID_FILE_TYPE`（415，文件不对）
> 分开，用户才知道该换目标格式还是该换文件。

---

## 配置项

全部配置集中在 `backend/config.py`，可以通过环境变量覆盖。

> ⚠️ **本项目不读取 `.env` 文件**。`backend/config.py` 用的是 `os.environ.get()`，
> 依赖里也没有 `python-dotenv` —— 把 `.env` 放在仓库根目录对程序没有任何影响。
> 变量要通过 `export` / systemd 的 `Environment=` / `docker run -e` 真正注入。
> 仓库根目录的 [`.env.example`](.env.example) 是一张**清单**（列全了 65 个变量与
> 内置默认值），不是一份会被加载的配置。
>
> **唯一的例外是 `docker compose`**：[`docker-compose.yml`](docker-compose.yml)
> 里配了 `env_file: .env`，compose 会读仓库根目录的 `.env` 并把它注入成容器环境变量。
> 换句话说 `.env` 生效的从来不是后端，是 compose。

### 上传与批量

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FILETOOLS_MAX_UPLOAD_BYTES` | `52428800`（50 MB） | 单文件上传上限 |
| `FILETOOLS_MAX_BATCH_FILES` | `50` | 单次批量请求最多处理多少个文件 |
| `FILETOOLS_MAX_BATCH_TOTAL_BYTES` | `314572800`（300 MB） | 单次批量请求所有文件加起来的上限 |
| `FILETOOLS_BATCH_TIMEOUT` | `300` | 整批处理超时（秒） |
| `FILETOOLS_PROCESS_TIMEOUT` | `60` | 单个文件处理超时（秒） |

### 图片

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FILETOOLS_MAX_IMAGE_PIXELS` | `80000000` | 解码后最大像素数（防图片炸弹） |
| `FILETOOLS_MAX_IMAGE_EDGE` | `12000` | 图片最长边上限 |
| `FILETOOLS_DEFAULT_QUALITY` | `85` | 未指定质量时使用的默认质量（无损格式不受影响） |
| `FILETOOLS_MAX_SVG_BYTES` | `2097152`（2 MB） | 单个 SVG 的最大字节数 |
| `FILETOOLS_MAX_SVG_NODES` | `20000` | SVG 的最大节点数 |

### Worker 池与任务队列（第八阶段）

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FILETOOLS_MAX_WORKERS` | `5` | 全局线程池大小（CPU 密集任务的并发上限） |
| `FILETOOLS_IMAGE_WORKERS` | `2` | 图片池并发 |
| `FILETOOLS_PDF_WORKERS` | `1` | PDF 池并发 |
| `FILETOOLS_OFFICE_WORKERS` | `1` | Office 池并发 |
| `FILETOOLS_OCR_WORKERS` | `1` | OCR 池并发 |
| `FILETOOLS_QUEUE_WORKERS` | `2` | default 池并发 |
| `FILETOOLS_IMAGE_TIMEOUT` | `120` | 图片池兜底超时（秒） |
| `FILETOOLS_PDF_TIMEOUT` | `180` | PDF 池兜底超时（秒） |
| `FILETOOLS_OFFICE_POOL_TIMEOUT` | `120` | Office 池兜底超时（秒）。**生效值是 180** —— 兜底是下限，见下注 |
| `FILETOOLS_DEFAULT_ITEM_TIMEOUT` | `120` | 其余池的兜底超时（秒） |
| `FILETOOLS_TASK_TIMEOUT_AUTO_RETRY` | `0`（关） | 超时任务是否自动重试 |
| `FILETOOLS_TASK_MAX_AUTO_RETRIES` | `1` | 自动重试次数上限 |
| `FILETOOLS_WORKER_WATCHDOG_INTERVAL` | `5` | 看门狗扫描间隔（秒） |
| `FILETOOLS_WORKER_LOST_GRACE` | `30` | 判定 worker 丢失的宽限（秒） |
| `FILETOOLS_PRIORITY_AGING_SECONDS` | `60` | 优先级老化：等多久提升一档 |
| `FILETOOLS_PRIORITY_AGING_STEPS` | `2` | 优先级最多提升几档 |
| `FILETOOLS_SHUTDOWN_GRACE` | `10` | 优雅关机的等待秒数 |

> 超时的**生效值** = `max(池兜底, 池内既有操作的超时)` —— 兜底是下限，不是旋钮。

### 临时文件与生命周期

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FILETOOLS_JOB_TTL` | `1800` | 结果文件保留时长（秒） |
| `FILETOOLS_TASK_TTL` | `1800` | 任务记录保留时长（秒），过期后查询返回 404 |
| `FILETOOLS_CLEANUP_INTERVAL` | `60` | 定时清理的扫描间隔（秒） |
| `FILETOOLS_TEMP_ROOT` | 系统临时目录 | 临时文件根目录 |
| `FILETOOLS_CORS_ORIGINS` | `http://localhost:5173,...` | 允许跨域的前端地址，逗号分隔 |
| `FILETOOLS_SYSTEM_API` | `1` | 是否注册 `/api/system/*` |

### PDF

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FILETOOLS_MAX_PDF_PAGES` | `500` | 单个 PDF 最多处理多少页 |
| `FILETOOLS_PDF_INPUT_TTL` | `1800` | 上传的 PDF 保留时长（秒） |
| `FILETOOLS_PDF_THUMBNAIL_MAX_PAGES` | `60` | 页面缩略图最多渲染多少页 |
| `FILETOOLS_PDF_THUMBNAIL_WIDTH` | `220` | 缩略图宽度（像素） |
| `FILETOOLS_PDF_RENDER_MAX_DPI` | `300` | PDF 渲染 DPI 上限 |
| `FILETOOLS_PDF_RENDER_MAX_PIXELS` | `40000000` | 单页渲染的最大像素数 |
| `FILETOOLS_PDF_EXPORT_MAX_PAGES` | `100` | 一次最多把多少页导出成图片 |

### 文档转换与 PDF → Word

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FILETOOLS_LIBREOFFICE_PATH` | 空（自动探测） | LibreOffice 启动器路径，**可指文件也可指目录** |
| `FILETOOLS_OFFICE_TIMEOUT` | `180` | 单次 Office 转换的超时（秒） |
| `FILETOOLS_OFFICE_LOCK_WAIT` | `60` | 等转换锁的上限（秒），超时报「服务器正忙」 |
| `FILETOOLS_TXT_FONT_SIZE` | `11` | TXT 默认字号 |
| `FILETOOLS_TXT_TIMEOUT` | `120` | TXT 排版的超时（秒） |
| `FILETOOLS_MAX_TXT_CHARS` | `500000` | 单个 TXT 最多多少字符 |
| `FILETOOLS_MAX_TXT_PAGES` | `500` | 一份 TXT 最多排多少页 |
| `FILETOOLS_PDF_TO_WORD_TIMEOUT` | `300` | PDF→Word 超时（秒） |
| `FILETOOLS_PDF_TO_WORD_MIN_CHARS` | `16` | 少于这个字符数就判定为扫描页、走 OCR |
| `FILETOOLS_PDF_TO_WORD_MAX_OCR_PAGES` | `30` | 最多 OCR 多少页 |
| `FILETOOLS_PDF_TO_WORD_OCR_DPI` | `200` | OCR 渲染 DPI |
| `FILETOOLS_PDF_TO_WORD_OCR_RETRY_DPI` | `300` | OCR 效果不佳时的重试 DPI |
| `FILETOOLS_PDF_TO_WORD_OCR_LOCK_WAIT` | `60` | 等 OCR 锁的上限（秒） |
| `FILETOOLS_PDF_TO_WORD_FONT` | `宋体` | DOCX 正文字体 |
| `FILETOOLS_PDF_TO_WORD_EMBED_DPI` | `150` | 图片型页面嵌入 DOCX 时的 DPI |
| `FILETOOLS_PDF_TO_WORD_EMBED_QUALITY` | `80` | 嵌入图片的 JPEG 质量 |
| `FILETOOLS_PDF_TO_WORD_MAX_EMBED_BYTES` | `62914560`（60 MB） | 嵌入图片总量上限 |
| `FILETOOLS_PDF_TO_WORD_WORKER_BUDGET` | `270` | 整个 PDF→Word 任务的算力预算（秒），小于外层超时 |
| `FILETOOLS_OCR_DISABLED` | `0` | 设为 `1` 可整个关掉 OCR |
| `FILETOOLS_OCR_TIMEOUT` | `300` | OCR 超时（秒） |

### Markup（HTML / Markdown）

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FILETOOLS_MAX_MARKUP_CHARS` | `500000` | 单个标记文档最多多少字符 |
| `FILETOOLS_MAX_MARKUP_NODES` | `20000` | 最大节点数 |
| `FILETOOLS_MAX_MARKUP_IMAGES` | `50` | 最多允许多少张内联图片 |
| `FILETOOLS_MAX_MARKUP_IMAGE_BYTES` | `262144`（256 KB） | 单张内联图片上限 |
| `FILETOOLS_MARKUP_TIMEOUT` | `120` | Markup 转换超时（秒） |

> 前端的限制值全部来自 `GET /api/config`，改这里的环境变量后刷新页面即可生效，
> 不需要重新构建前端。

前端环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `VITE_API_BASE_URL` | 空（相对路径） | 后端地址。前后端分开部署时设置 |
| `VITE_BACKEND_URL` | `http://127.0.0.1:8000` | 仅供 `npm run dev` 的代理使用，不进产物 |

两个变量都写在 `frontend/.env`，但**去向完全不一样，别想当然**（已实测）：

- `VITE_API_BASE_URL` 是**给前端代码**用的。前端通过 `import.meta.env` 读它，
  Vite 在**构建时把这个值静态替换进产物** —— 所以改它必须重新 `npm run build`。
  留空 = 用相对路径，前后端同源部署时的正确选择。
- `VITE_BACKEND_URL` 是**给开发服务器**用的，只影响 `npm run dev` 时 `/api` 的
  代理目标，**不进构建产物**。它由 `vite.config.ts` 在**配置加载时**通过
  `loadEnv()` 读取，改了要重启 dev server。

`VITE_BACKEND_URL` 的优先级是 **命令行/系统环境变量 > `frontend/.env` > 兜底默认值**
（顺序不能反，否则命令行的值会被 `.env` 覆盖）。两种写法都行：

```bash
# 写在 frontend/.env 里（推荐，不用每次敲）
VITE_BACKEND_URL=http://192.168.1.10:8000

# 或者临时在命令行上指定
VITE_BACKEND_URL=http://192.168.1.10:8000 npm run dev
```

> 历史坑：早期版本这里读的是 `process.env`，而 Vite **不会**把 `.env` 的值灌进
> `process.env`（只放进 `config.env` / `import.meta.env`），于是写在
> `frontend/.env` 里的 `VITE_BACKEND_URL` 是**静默失效**的 —— 代理 target 一直
> 停在兜底值且不报任何错。已改为函数式配置 + `loadEnv()` 修掉，
> 四种情形（无 `.env` / 仅 `.env` / 仅命令行 / 两者都有）均已实测。

`frontend/` 下另有一份 [`.env.example`](frontend/.env.example)，把这两条也写在了那里
（Vite 只从**前端项目目录**读 `.env`，仓库根目录的 `.env` 与它无关）。

---

## 安全措施

> ⚠️ **先说清楚前提：本项目没有任何身份认证**（没有登录、没有 API Key、没有多租户），
> 也**没有速率限制**。任何能访问到端口的人都能使用全部功能并消耗服务器的 CPU 和磁盘。
> 默认监听 `0.0.0.0` 是为了本机/局域网调试，**不要直接暴露到公网** ——
> 要给别人用请在前端套一层带认证的反向代理。

### 报告漏洞

请用 GitHub 自带的**私有漏洞上报**，不要开公开 issue：

> 仓库页 → **Security** 标签 → **Report a vulnerability**

这条路是私密的，只有维护者能看到，也省得留邮箱 —— **本仓库刻意不写任何电子邮箱**。
如果你克隆的这份副本没有启用私有上报（仓库 Settings → Security 里可以开），
那就直接开一个 issue，但**只写「哪一类问题」**，不要贴可复现的利用细节，细节等私下沟通时再给。
请一并说明：

- 影响的是哪个版本 / 哪次提交（`/api/health` 会返回版本号）
- 部署方式：直接 `uvicorn` 跑，还是 Docker
- **服务是否对公网开放** —— 这条最关键
- 最小复现步骤，以及一个能触发问题的样本文件（如果方便）

### 部署者还必须知道：转换引擎本身就是信任边界

「没有认证」「没有速率限制」这两件写在上面那段警告里。第三件单独说：
图片、PDF、Office 文档分别交给 **Pillow、PyMuPDF、LibreOffice** 解析，
**这些库解析恶意构造的文件时，崩溃、卡死、内存暴涨都是可能的**。
本项目的上限配置能挡住大部分资源耗尽，**但挡不住底层库自身的解析漏洞**。
处理来源不明的文件时建议在容器里跑，并给容器设 CPU / 内存配额。

对应需求中的 10 条文件安全要求：

| # | 要求 | 实现方式 |
| --- | --- | --- |
| 1 | 限制上传文件大小 | `utils/files.py` 分块写入并实时累加，超限立即中止并删除；`main.py` 中间件预检 `Content-Length`。不依赖请求头，伪造也无法绕过 |
| 2 | 限制允许的文件类型 | 扩展名白名单 + MIME 白名单（`config.py`） |
| 3 | 上传文件使用随机文件名 | 落盘名为 `secrets.token_hex(16)`，完全不使用用户提供的文件名 |
| 4 | 文件处理使用临时目录 | 每次任务用 `tempfile.mkdtemp()` 创建独立随机子目录 |
| 5 | 处理完成后自动删除临时文件 | 下载响应发送完毕后由 `BackgroundTask` 立即删除；未下载的由后台线程按 TTL 清理；服务关闭时全部清空 |
| 6 | 不永久保存用户文件 | 无数据库、无持久化，结果只存在于临时目录 |
| 7 | 后端增加异常处理 | 统一异常处理器输出 `{error:{code,message}}`；未预期异常只记日志，不外泄堆栈；批量处理中单张失败也不会让整批 500 |
| 8 | 防止非法文件类型伪装 | 三层校验：扩展名 → 文件头 magic bytes → 真实解码。三者不一致直接 415 |
| 9 | 处理增加超时和资源限制 | 分池并发上限 + 单文件超时 + 整批超时 + 看门狗 + `MAX_IMAGE_PIXELS` 防图片炸弹 + 最长边限制 |
| 10 | 不允许直接访问服务器临时文件 | 临时目录不在任何静态挂载路径下；下载只能凭 32 位随机令牌，服务端不接受任何路径参数 |

### 各阶段追加的限制

#### 第二 / 四阶段（批量）

- 单次请求最多 `MAX_BATCH_FILES`（默认 50）个文件，整批合计不超过 `MAX_BATCH_TOTAL_BYTES`（默认 300 MB）。
  判断在**开始处理之前**完成，不会处理到一半才发现超限。
- ZIP 与其他结果文件一样存放在任务临时目录中，下载后随目录一起删除。
- 结果文件名由原文件名清洗后生成，重复时自动加序号；ZIP 内的归档名同样经过清洗。

#### 第三阶段（PDF）

| 限制 | 默认值 | 挡住什么 |
| --- | --- | --- |
| 单个 PDF 页数上限 | 500 页 | 一份上万页的 PDF 把 CPU 与内存吃光 |
| 加密 PDF 直接拒绝 | — | 单独给出「该 PDF 已加密」而不是含糊的「文件损坏」 |
| 单页渲染像素上限 | 4000 万像素 | 构造一个巨大的 MediaBox 在 300 DPI 下渲染出几十亿像素 |
| 渲染 DPI 上限 | 300 | 通过其他手段把 DPI 抬到失控 |
| 单次导出的页数上限 | 100 页 | 一次把 500 页渲染成 300 DPI 图片 |
| 缩略图页数上限 | 60 页 | 打开页面就渲染几百张缩略图 |
| 三层类型校验 | — | 扩展名 → `%PDF-` 文件头 → PyMuPDF 真实打开 |

#### 第五阶段（Office）

| 限制 | 实现 |
| --- | --- |
| 缺组件时在**落盘之前**拒绝 | 不为一个注定失败的请求把 50 MB 写进磁盘 |
| 转换**串行**且有排队上限 | 模块级单飞锁，等锁超时报「服务器正忙」 |
| 两层超时，内层严格小于外层 | 让正常超时路径在锁内先释放锁 |
| 转换产物必须复验 | 打不开或页数 ≤ 0 就报错并丢弃，**绝不把坏 PDF 交给用户** |
| 子进程不弹窗 | `creationflags=CREATE_NO_WINDOW` |
| 子进程的原始输出不外传 | soffice 的 stdout / stderr 只写日志，**绝不进 response** |

#### 第九 / 十阶段（SVG、图片炸弹、Markup）

| 限制 | 实现 |
| --- | --- |
| **SVG 净化后再渲染** | 一切外部引用（`http(s)://`、`file://`、`srcset`、`@import`、`url()`）与可执行节点（`<script>`、`<iframe>`、`<object>`、`foreignObject`）**先剥离再渲染**。危险元素是**删除**（连内容一起）而不是整份拒绝，并如实记进 `notes` |
| 喂 `file:///etc/passwd` 不泄漏 | 有专门的测试断言产物与响应里都不出现该字符串 |
| SVG 有量与结构上限 | `MAX_SVG_BYTES`（2 MB）+ `MAX_SVG_NODES`（20000），超限明确拒绝 |
| HTML/Markdown 只允许内联资源 | 远程图片与本地文件路径一律剥离，只允许 `data:` URI 与内联 SVG；另有字符数 / 节点数 / 图片数 / 单图体积四道上限 |
| 图片炸弹两档防线 | `MAX_IMAGE_PIXELS` / `MAX_IMAGE_EDGE`，返回**业务错误 4xx** 而不是 500 |
| 错误信息不外泄 | 验收脚本会真的去页面与响应里搜 `Traceback`、`site-packages`、`PIL`、`http://`、`file://`、`javascript:`、`../` 等字样 |

#### 补充

- 结果文件名经过清洗，去除路径分隔符和控制字符，防止路径穿越。
- 按设置剥离 EXIF / XMP，避免泄露 GPS 等隐私信息（清除只覆盖可安全剥离的部分，**不做像素级擦除**）。
- 预览接口只接受 `job_id` + 整数下标，不接受路径。
- 响应带 `X-Content-Type-Options: nosniff`。
- CORS 默认只放行本地开发地址。
- **后端不发起任何出站网络请求**：生产代码里没有 `urllib.request` / `http.client` / `requests` /
  `httpx` / `aiohttp`，唯一的 `urllib` 用法是纯字符串解析的 `urllib.parse`。测试里有一项就是
  起一个本地 HTTP 服务、断言**收不到**任何请求。

> 已知限制：超时后线程池中的任务无法被强制中断（Python 线程不可取消），但临时目录仍会被正确清理。
> 生产环境建议在前面加一层反向代理限制请求体大小和连接数。

### 明确不在防护范围内

写出来是为了不给你虚假的安全感：

1. **认证、授权、多租户** —— 完全不提供（见本节开头）。
2. **速率限制、配额、滥用防护** —— 完全不提供。单个客户端可以持续提交任务把 worker 池占满。
3. **底层解析库的漏洞** —— Pillow / PyMuPDF / LibreOffice / 各家编解码器自身的安全问题，
   本项目的上限配置**不能替代它们的补丁**。
4. **恶意的 `data:` 内嵌资源** —— 内联图片是允许的，一个精心构造的内联图片同样能消耗解析资源
   （仍受上面的图片像素上限约束）。
5. **持久化与审计** —— 任务状态是**单进程内存态，重启即丢**，没有数据库，
   也没有访问日志之外的审计能力。
6. **经过加固的公网服务** —— 本项目是自托管的本地工具，**不是**面向公网的多用户服务，
   别拿它当后者用。

### 支持的版本与依赖安全

项目处于 **0.1.x**，**只维护默认分支的最新提交** —— 旧提交不单独回补修复，请先更新到最新再复现问题。

依赖固定在 `backend/requirements.txt` 与 `frontend/package-lock.json`，
定期检查两处更新是部署者的日常工作 —— 尤其是 **PyMuPDF、Pillow、LibreOffice**
这三个直接解析不可信输入的部分。

---

## 测试

### 后端单元 / 接口测试

后端有 **1645 项测试**，覆盖十一个阶段全部功能的正常流程、边界情况和安全校验：

```bash
cd backend
pip install -r requirements-dev.txt
pytest -o addopts= -q
```

预期输出：

```text
1645 passed
```

> `pytest.ini` 里设了 `addopts = -q`，上面的 `-o addopts=` 是把它临时清掉，
> 这样能拿到每个文件的明细。不加也能跑，只是输出会简略一些。
>
> **上面的 1645 是「一个都没跳过」时的数** —— 即 LibreOffice、pillow-heif、OCR 组件都在的机器。
> 少哪个组件，`passed` 就会相应少几条、`skipped` 多几条，两者相加仍是 1645。
>
> 其中需要真正调用 LibreOffice 的用例带 `@requires_soffice` 标记：机器上没装 LibreOffice 时
> 它们会 **skip** 而不是 fail（没装组件的机器不该因为「装不了的东西」变红）。
> 旧格式用例还需要 `backend/tests/fixtures/sample.{doc,xls,ppt}`，
> 缺失时同样 skip —— 跑 `python scripts/make_office_fixtures.py` 生成。

主要测试模块（共 46 个）：

| 测试文件 | 覆盖内容 |
| --- | --- |
| `test_compress.py` / `test_convert.py` / `test_resize.py` | 第一、二阶段：档位压缩、目标大小搜索与降级、同格式原样返回、**PNG 不带质量参数时保持无损**、最长边限制、下载令牌一次性、恶意文件名清洗 |
| `test_image_formats.py` / `test_image_geometry.py` / `test_image_geometry_api.py` | 第十阶段：BMP / GIF / TIFF / ICO 的编解码与前置处理、旋转 / 翻转 / 裁剪的实际像素效果、**旋转方向是顺时针** |
| `test_image_orientation.py` | 第十阶段：EXIF 方向转正、转正后再做几何操作的顺序、orientation 标记被移除 |
| `test_image_metadata.py` | 第十阶段：只读端点的字段与顺序、读不出来的项是 `null` 而非空串、转换时的 EXIF / XMP 保留与剥离、**TIFF 的 XMP 往返**、BMP/GIF/ICO 如实回报「不支持携带 XMP」 |
| `test_image_bomb.py` | 第十阶段：两档炸弹防线的单元与接口层断言、合法大图不被误伤、返回的是 4xx 而非 500 |
| `test_heic.py` | 第十阶段：HEIC 三种状态（包不在 / 只有解码器 / 都有），后两种用**猴子补丁**模拟，断言矩阵随之变化且不伪造成功 |
| `test_svg_security.py` / `test_svg_api.py` | 第十阶段：SVG 净化的每一条规则、外部引用与可执行节点被剥离、`file:///etc/passwd` 不泄漏、量与结构上限 |
| `test_image_batch_performance.py` | 第十阶段：批量图片处理的并发与资源上限 |
| `test_batch.py` / `test_queue.py` / `test_queue_backend.py` | 第二、四阶段：批量限额、单张失败不影响整批、ZIP 打包与内容校验、结果文件名去重、任务状态与进度自洽、并发被 worker 数限制住、任务过期、孤儿临时目录被清理 |
| `test_pdf_*.py`（7 个） | 第三阶段：六个 PDF 功能、页面范围解析、损坏 / 加密 / 非 PDF / 超页数的提示文案、图片转 PDF 的纸张与页边距实际尺寸 |
| `test_pdf_filename_safety.py` | 第三阶段：结果文件名清洗与 ZIP 成员名无路径穿越 |
| `test_pdf_to_docx_units.py` / `test_pdf_to_word_api.py` | 第六阶段：文字层优先、扫描页判定、OCR 的页数上限与降级、DOCX 产物可被 python-docx 打开、**`ErrorCode` ↔ 前端 `errorMessages.ts` 双向相等** |
| `test_office_loader.py` / `test_office_converter.py` / `test_office_api.py` / `test_office_legacy.py` / `test_office_txt.py` | 第五阶段：三层校验、LibreOffice 隔离层（`.com` 优先、并发单飞、两层超时、坏产物复验）、四种格式**从下载到的 PDF 里读出正文**、隐藏工作表 / 幻灯片不导出、TXT 排版选项真的改变产物 |
| `test_conversion_registry.py` / `test_conversion_capabilities.py` | 第七、九阶段：条目表是唯一事实来源、ID 唯一 / 可往返、registry **零 I/O**（子进程里 import 后断言 `sys.modules` 里没有 `fitz`/`PIL`/`docx`）、能力矩阵与 registry 逐条一致 |
| `test_conversion_options.py` / `test_conversion_params.py` | 第七、九阶段：`options` 五层校验链、schema 枚举与实现常量逐条对账、资源上限绕不过去 |
| `test_conversion_api.py` / `test_conversion_batch.py` / `test_conversion_operations.py` / `test_conversion_preview.py` / `test_conversion_compression.py` | 第九阶段：统一接口的完整流程、批次 ZIP 与重名去重、PDF 操作端点参数契约、预览不消耗令牌且下载后失效、目标体积搜索 |
| `test_markup_conversion.py` / `test_markup_security.py` / `test_text_conversion.py` | 第九阶段：HTML / Markdown 子集互转、围栏代码块不被包成两层、外链资源被剥离、TXT↔DOCX/HTML/MD |
| `test_worker_pool.py` / `test_system_api.py` | 第八阶段：分池、超时、看门狗、重试、指标计数、`/api/system/*` 的开关与 `FILETOOLS_SYSTEM_API=0` 时的行为 |

### 前端类型检查与构建

```bash
cd frontend
npm run typecheck
npm run build
```

### CI 覆盖什么

`.github/workflows/ci.yml` 在 push 到 `main` 与每个 PR 上跑两件事：后端
`pytest -o addopts= -q`，前端 `npm run build`（脚本本身含 `tsc --noEmit`）。

CI 里**刻意不跑** `scripts/verify_phase*.py` —— 那一批要真实 Chromium、要 LibreOffice、
要一个常驻的后端进程，12 步串行跑满约 15 分钟，而且必须串行（并发跑必然假红）。
它们属于发布前的真机验收，不属于 PR 门禁。

同样地，CI runner 上没有 LibreOffice、也没有 OCR 组件，所以带 `@requires_soffice` /
`@requires_ocr` 的用例在 CI 上是 **skip**。**CI 全绿不等于 Office 转换与 OCR 被验证过** ——
那两样只有在装了组件的机器上跑完整回归才算数。

> **这份工作流已经在 GitHub 上真跑过**，不是纸面配置。跑出来的几条教训都留在提交历史里：
>
> - **不要假设「红的就是代码坏了」。** 第一轮后端 job 的 **29 条红全部来自 runner 缺组件**
>   （LibreOffice / OCR），不是代码缺陷；补装 `pillow-heif` 之后 HEIC 那 7 条也不再跳过。
>   反过来同样成立 —— 见下面最后一条。
> - **诊断块曾经把自己弄瞎**：`pytest -rs` 顶掉了默认的 `-rfE`，红了一片却**一条失败行都看不到**。
>   为了看得更清楚而加的旗标，把最该看的东西挤掉了。
> - **最值得记住的一条**：Linux runner 上 PDF 里的字被**静默换掉**、产物体积翻了上千倍 ——
>   原因是 CI 自己装的 `fonts-noto-cjk` 把系统默认字体换成了一份 **MuPDF 读不了**的
>   `NotoSansCJK-Regular.ttc`。**CI 自己装的东西把 CI 弄红了**，而且红得看起来像代码问题。
> - **同一个坏文件，退出码在两边不一样。** 「损坏文档要报 400」那条用例在本机（Windows）一直绿，
>   在 Linux runner 上是红的 —— 因为同一个损坏的 `.docx`，`soffice` 在 Windows 上退出码是 **1**、
>   在 Linux 上是 **0**。**押平台相关的信号做业务归类，等于把平台差异写进了用户看到的文案。**
>   修法的关键是换成一条与平台无关的判据（源文件的主部件是不是良构 XML），
>   并把 Linux 的那种返回**写成一条不依赖 LibreOffice 的测试**，这样在任何机器上都能复现那条缝。
> - **判据也别押在部署形态上。** 最后一条红（`test_system_api_can_be_switched_off_entirely`）
>   从**第一次跑 CI 起每次都在**，本机却永远绿 —— 它验的是「接口关掉之后收到什么」，
>   而那取决于**这台机器上有没有前端产物**：本机 `frontend/dist` 一直在，后端于是挂了 SPA 兜底，
>   未匹配路径回落到 `index.html`（`200 text/html`）；CI 只 checkout 后端、从不构建前端，
>   没有 dist 就没挂兜底，收到的是 FastAPI 自己那条通用 404（`application/json`）。
>   旧断言写的是「响应里不许出现 `application/json`」，等于把**「收到的不是我们的载荷」**
>   错写成**「响应里不许有 JSON」**。两种形态都是真部署，所以修法是**两种形状各自钉死**
>   （404 时 body 必须恰好是 `{"detail":"Not Found"}`；200 时必须确实是那份 HTML 外壳）。

**结果：这份工作流在 2026-10-08 首次全绿。** 从 `819e690`（09-29）到 `ccd322f`（10-08）
连着六次红，第七次（`35493be`）两个 job 都是 `success`
（[run 37747877362](https://github.com/yuhui200/FilesTools/actions/runs/37747877362)）。

六次红可以归成两类，**两类都不是「代码随手写错了」**：

- **运行环境差异** —— runner 缺组件（LibreOffice / OCR）、CI 自己装的 `fonts-noto-cjk`
  改变了 Linux 上的默认字体。这类红的特征是**本机复现不出来**。
- **押了平台 / 部署相关的信号做判断** —— 损坏文档押 `soffice` 退出码、接口开关押部署形态。
  这一类是**真缺陷**，而且是两条：一条在生产代码里（`_source_could_not_be_loaded`），
  一条在测试的判据里。**本机各自只看得到一半**，所以两边都长期以为对方是绿的。

诊断通道（`::error::` 注解 + 步骤摘要，见 `ci.yml` 文件头）就是被这条路上「只知道红在跑测试、
不知道红在哪一条」逼出来的 —— 日志正文要仓库管理员权限才下得到，而注解谁都读得到。

### 真实浏览器 / 真机验收

各阶段都有一份**独立于 pytest** 的验收脚本，用真实 Chromium（Playwright）或真实后端跑完整流程，
并且**真的打开产物**（用 Pillow / PyMuPDF / python-docx / zipfile 校验格式、尺寸、页数、成员名），
不是只看 HTTP 200。

| 脚本 | 结果 | 说明 |
| --- | --- | --- |
| `scripts/verify_phase2.py` | 49 / 49 | 转换、同格式提示、PNG 无损、质量 UI、批量 ZIP、等比联动、移动端布局 |
| `scripts/verify_phase3.py` | 135 / 135 | 六个 PDF 功能的完整流程、纸张与页边距的实际尺寸、ZIP 成员、负例文案、移动端 |
| `scripts/verify_phase4.py` | 137 / 137 | 批量与状态 / 跨浏览器（Chromium + Edge + WebKit）/ 生命周期（另起短 TTL 后端） |
| `scripts/verify_phase5.py` | 88 / 88 | 文档转换四种格式 + 排版选项 + 缺组件降级 + 移动端 |
| `scripts/verify_phase6.py` | 106 / 106 | PDF → Word 的文字层与 OCR 两条路径 |
| `scripts/verify_phase7.py` | 258 / 258 | 统一转换中心 1.0 |
| `scripts/verify_phase8.py` | 187 / 187 | 分池架构、并发倍数、看门狗、指标 |
| `scripts/verify_phase9.py` | 87 / 87 | 统一转换中心 2.0（375 / 390 / 414 三档视口） |
| `scripts/verify_phase9a_live.py` | 744 / 744 | 后端**真机**验收：真的提交任务、真的并发、真的看指标 |
| `scripts/verify_phase10a.py` | 73 / 73 | 高级图片引擎的浏览器端 |
| **`scripts/verify_phase10.py`** | **210 / 210** | **第十阶段封板入口**，11 个分段（A 24 / B 21 / C 13 / D 11 / E 14 / F 15 / G 15 / H 10 / I 12 / J 33 / G3 42） |
| **`scripts/verify_mobile_phase11a.py`** | **107 / 107** | **第十一阶段 A 封板入口**，5 个分段（A 反漂移 25 / B 与真服务器对账 8 / C 三条真实链路 19 / D 界面行为 36 / E 任务生命周期：取消与重试 19） |
| **`scripts/verify_branding.py`** | **103 / 103** | 品牌验收：唯一真源、四平台图标尺寸与像素、App 名、版本一致性、`Dockerfile` 的 `COPY VERSION` |
| **`scripts/verify_desktop.py`** | **48 / 48** | 桌面验收：Tauri 配置、标识符、版本、Windows 图标，**并真的跑了一次 Tauri 构建** |
| **`scripts/verify_final.py`** | **96 / 96**（另 4 项 `NOT EXECUTED`） | **最终总验收入口**：版本 / 品牌 / 单一真源 / Web / 后端 / 移动 / 桌面 / 脚本齐备的横切检查。环境够不着的项（docker build、iOS 构建、Android 真机、ARM64）输出 `NOT EXECUTED` 并写明原因，**不写成 PASS** |
| 桌面端安装包 **真机装一遍** | **通过** | 走完向导逐页核对 → 装 → 开 → 卸载，安装目录 / 两个快捷方式 / 注册表项全部清除 |

跑法（以第十阶段为例）：

```bash
# 1. 构建前端并用后端托管（验收要跑真实浏览器，别对着 dev server 跑）
cd frontend && npm run build && cd ..
cd backend && ./.venv/Scripts/python.exe -m uvicorn main:app --port 8011

# 2. 另一个终端
python scripts/verify_phase10.py
```

> ⚠️ **验收脚本必须串行跑，不能并发。** `tests/test_queue.py::test_download_releases_temp_files`
> 会去扫**共享系统临时目录**，而验收脚本各自建 `mkdtemp(prefix="filetools-verify-")` ——
> 两者并发**必然假红**。
>
> ⚠️ 封板那一轮跑的是**跑之前就冻结、执行期间一字未改**的脚本副本。bash 按字节偏移边读边执行，
> 边跑边改会让它从过期偏移量接着读、报出看起来像语法错的假故障。

可用的环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FILETOOLS_WEB_BASE` | `http://localhost:5173`（2/3 阶段）/ `http://127.0.0.1:8011`（4 阶段起） | 前端地址 |
| `FILETOOLS_BACKEND_PY` | `backend/.venv` 下的解释器 | 用来生成测试素材、解码结果文件 |
| `FILETOOLS_WORK_DIR` | 新建临时目录 | 指定后测试素材会保留复用（第二次跑快很多） |
| `FILETOOLS_API_BASE` | `http://127.0.0.1:8011` | 仅 `verify_mobile_phase11a.py`：后端地址（移动端脚本自己起请求、也用来对账能力目录） |
| `FILETOOLS_APP_BASE` | `http://localhost:8081` | 仅 `verify_mobile_phase11a.py`：Expo Web dev server 地址（移动 App 在浏览器里的运行目标） |

验收报告会写入 `scripts/*_report.txt`（**该文件在 `.gitignore` 里**，是每次跑都会重写的产物）。

---

## 已知限制

都不是未发现的缺陷，是**已知且被接受**的边界。

1. **多帧 GIF / 多页 TIFF 只处理第一帧**（界面与结果卡上都写明）。
2. **Markdown 是自写子集**，不是 CommonMark 兼容。支持标题 / 粗斜体 / 链接 / 图片 / 有序无序列表 / 引用 / 代码块与行内代码 / 分隔线 / 表格，其余如实说明。
3. **ICO 只有 PNG → ICO**；**SVG 只作为源**，不作为目标（不实现 PNG → SVG）。
4. **Office 转换是串行的**：单飞锁决定同一时刻只有一个转换在跑，第 N 个并发请求要等前面 N−1 个，每个约 8–11 秒。这不是没优化 —— 两个 soffice 共用同一份 profile 时**第二个会静默失败**。
5. **OCR 逐页串行**，约 2 秒/页，单份最多 30 页；取消是协作式的，停不下来时会如实显示。
6. **PDF 操作不进统一队列**：多进多出 / 页面级操作与 1→1 的批量模型结构上不同，走各自接口，队列指标不覆盖它们。
7. **「最大文件大小」是尽力而为**：LibreOffice 无法指定输出体积，只能转完再压，而它导出的 PDF 已经压得很狠，所以 Office 链路上常常压不动 —— 结果页如实写明实际大小。
8. **服务器缺字体时会被替换**，字宽和分页随之改变。Linux 服务器尤其要注意装中文字体（`fonts-noto-cjk`）。
9. **旧格式只经过往返样张验证**：带宏、带嵌入对象的旧文件未经验证。
10. **单进程内存态**：队列、任务组、指标都是进程内的，重启即丢；优雅关机把在跑的项如实记为失败，**不伪造持久化**。
11. **超时停不下线程**：超时只释放调度槽位并如实记 `timeout_requested`，底层线程会跑完，期间仍可能持有 Office / OCR 锁。
12. **看门狗击杀 + 自动重试路径上会重复执行一次重活**（对纯 CPU 的转换意味着浪费一次算力，不是错误结果）。
13. **`_ENGINE_LOCK` 的获取是无界的**：极端情况下一个卡死的转换会让后续排队等下去（看门狗最终会处理，但等待期间是阻塞的）。
14. **HTML → PDF 只允许内联资源**：含外链图片的 HTML 转出来会缺图（如实提示，不是 bug）。
15. **Docker 部署未经实测**：开发机是 Windows，没有 Docker。仓库根目录的 `Dockerfile` /
    `.dockerignore` 是按官方文档与本地已验证的命令写的，首次使用请以你自己的构建结果为准。
    （`verify_branding.py` 会断言 `Dockerfile` 里有 `COPY VERSION` —— 镜像只 `COPY backend/`，
    少了这一行容器里报出的版本号会是错的。）
    **CI 工作流则已经真跑过**，战况与教训见[CI 覆盖什么](#ci-覆盖什么)。
16. **图片池的聚合并发倍数在本机约 1.5×，不是 2×** —— 图片处理有相当一部分卡在 CPython 的 GIL 上。用受 GIL 限制的负载去要求并发收益，考的是解释器而不是队列。
17. **LibreOffice 的 profile 目录不会被自动回收**（`office-converter-profile-*` 刻意不含 `filetools` 前缀，免得被孤儿清理误删）。
18. **PDF 结果没有内联缩略图**（预览端点只服务浏览器原生能解的图片格式），PDF 照样能下载。
19. **移动端不做离线转换**：转换永远在服务器上做，App 只负责选文件、提交、显示进度、下载。
    没有服务器时 App 会如实报网络错误，**不会**在本地偷偷实现第二份转换逻辑。
20. **移动端的历史只在本机**：换设备、重装 App 之后记录就没了，也没有与服务端同步的打算
    （服务端本来就不存历史）。本地记录里也**只有元数据**，没有文件内容。
21. **移动端不提供那 7 个 PDF 操作的入口**：它们是「多进多出 / 页面级」操作，与这一版 1→1 的
    移动端流程模型不同 —— 与其做一个半吊子的入口，不如不做，并在这里写明。
22. **移动 App 在后端前面没有任何独有权限**：它用的就是公开的那套 API，没有移动专用的密钥或后门。
23. **Windows 桌面版只出 x64**：本机 `rustup target list --installed` 只有 `x86_64-pc-windows-msvc` 一个 target，
    **ARM64 版本没有构建过**，也不假装有。要出 ARM64 需要在 ARM 机器上或装了交叉工具链的机器上重新构建。
24. **iOS 一行实测都没有**：本机没有 macOS，iOS 只做到「配置存在 + 图标就位 + 1024 无 alpha 已实测」，
    没有跑过 Xcode 构建、没有上过真机、没有上过模拟器。
25. **安装器文件名是复制出来的**：Tauri 的原生产物名固定是 `FileTools_<版本>_x64-setup.exe`，
    **没有改名配置项**，所以由 `build_windows.py` 复制成 `FileTools-Setup-x64.exe`（报告里附两份 sha256）。
26. **桌面端的拖拽只在文件选择区生效**，不是窗口任意位置 —— 原生拖放被关掉了（不关会吃掉 HTML5 的 drop），
    这是走网页拖放的必然结果。
27. **桌面版没有开 CSP**（`csp: null`）。这不是「没顾上」，是按实测评的：
    把一份候选 CSP 加到头里、用真实浏览器加载**真的 `dist-desktop` 产物**跑过一遍 ——
    0 条 CSP 违规、界面正常渲染、对 `http://127.0.0.1:8000` 的请求也确实被放行
    （那边失败的是 CORS，不是 CSP）。**但只验到了这一半**：Tauri 的 IPC 走
    `ipc:` 协议（`tauri-2.12.1/src/manager/webview.rs` 里注册的），官方要求在
    `connect-src` 里放行 `ipc: http://ipc.localhost`，而这一半**没能在本机实测** ——
    想看它到底发了哪些请求就得挂进 WebView2 的调试端口，而那个端口**在本机打不开**。
    原因查到了，很具体：设 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9333`
    启动之后，**所有 WebView2 子进程的命令行里一个 `--remote-debugging-port` 都没有**，
    而 tauri 自己的 `--disable-features=msWebOOUI,msPdf` 在场 —— 说明 wry 走的是
    `AdditionalBrowserArguments`，把那个环境变量顶掉了。要开这个端口就得改**出厂配置**
    （`tauri.conf.json` 的 `additionalBrowserArgs`）并重新构建；为了测试去动出厂配置不值当，
    所以这条到此为止，**不假装验过**。
    既然有一半是**没验证过的**，就不为了「看起来更安全」打开它：宁可留 `null` 并写在这里。
    将来要开，`connect-src` 至少要有构建期烤进去的后端地址 **和** `ipc: http://ipc.localhost`。
28. **桌面版的后端地址是构建期烤死的**（`http://127.0.0.1:8000`，与页脚「本工具在本地部署运行」一致）。
    换服务器地址要**重新构建**，界面上没有服务器地址设置项 —— 这是刻意的，避免多出一个能填错的地方。
29. **品牌 Logo 的字标用的是 SVG `<text>`**，依赖系统字体，**没有转曲**，跨机器字形可能有细微差异。
    App 图标不含文字，不受影响。viewBox 定成 `0 0 132 32` 是**量出来的** ——
    实测「FileTools」在 17px/600 下的右边界：Segoe UI 108.3、system-ui 116.2、Arial 115.4、
    Tahoma 117.6、SimSun 121.5、Verdana 126.8，132 全部兜得住，各平台外框尺寸也就一致。
    **没有用 `textLength`**：最初写了 `textLength="84"` 想钉死宽度，实测反而是错的 ——
    Segoe UI 下自然宽只有 66.9，`textLength` 会把它**撑到** 84.7、字距拉开 27%，
    `File` 和 `Tools` 中间裂出一道明显的缝；而字体更宽时 `lengthAdjust="spacing"` 又会改成
    负字距、让字**重叠**。两个方向都是坑，所以交给字体自然排版。
30. **只出 NSIS 的 `.exe`**，没有做 WiX / MSI。
31. **首次安装的向导里不显示版本号**：这是 NSIS 默认模板的行为，没有为了让它出现在欢迎页去改模板。
    版本号在另外两处可见 —— 已经装过时维护页的 `FileTools 0.1.0 已经安装了`，
    以及「设置 → 应用」里的卸载条目（`DisplayVersion`）。
32. **Tauri 的 `identifier` 以 `.app` 结尾**（`com.filetools.app`），这在 macOS 上会与 bundle 扩展名冲突，
    构建时 Tauri 会就此告警。**故意保持原样**：这个标识符是移动端 `app.json` 里
    `ios.bundleIdentifier` 与 `android.package` 逐字相同的那一个，为了一个没有 Tauri 客户端的平台去改名，
    会破坏「四个平台同一个身份」。
33. **桌面版「在真实窗口里点一次转换」这一条没做到** —— 第 27 条那个调试端口打不开的同一个原因，
    挡住的还有这条路：本轮桌面端验到的是「安装 → 启动 → 窗口真的渲染出来（读到 107 个非空控件名）
    → WebView2 网络进程真的连上 `:8000` → `/api/health` 真的返回 `{"status":"ok"}`」，
    以及 `save_result` / `open_result` / `reveal_result` 所依赖的
    `sanitize_filename` / `unique_path` 有 **13 条 Rust 单元测试**在跑真函数。
    但**从真实窗口里驱动一次完整转换并落盘，本轮没有执行**，所以四平台矩阵里 Windows 的
    Conversion / Download 写的是 `VERIFY` 而不是 `PASS`。
34. **图标近透明像素的 RGB 会有偏差**：Chromium 在预乘 alpha 空间合成后再反预乘，会把量化误差
    放大到 alpha ≤ 4/255 的那一圈像素上 —— 实测 512×512 图里共 46 个，最坏一个合成到白底的
    误差是 **0.4/255**，不可见；alpha ≥ 44 的像素颜色都准确。这是渲染管线的固有行为，
    没有可靠的修法，如实记录。
35. **Web 的 192/512 图标是 `plain`（圆角外透明），不是 `maskable`**：将来若要上 Android 的
    PWA 安装横幅，需要再补一版 `purpose: "maskable"` 的图标。

---

## 常见问题

**打开前端提示「未能连接后端服务」**
后端没启动，或端口不是 8000。启动后端后刷新页面即可。前端此时会使用默认限制配置，不会白屏。

**提示「暂不支持该文件格式」**
上传的扩展名不在白名单里，或与真实内容不符。**先看是不是源格式真的不支持** ——
`/convert` 页面的目标列表与首页 FAQ 都是从能力 API 现算的，页面上说支持就一定支持。
PDF 工具有自己的白名单：只有 `.pdf` 能上传到 PDF 页面。

**提示「图片像素总量过大 / 图片尺寸过大，超过服务器处理上限」**
图片炸弹防线。默认上限是 8000 万像素、最长边 12000 像素。可以调大
`FILETOOLS_MAX_IMAGE_PIXELS` / `FILETOOLS_MAX_IMAGE_EDGE`，但注意服务器内存。
这是**业务错误而不是服务器错误**，换个正常尺寸的图就行。

**HEIC 图片传上去说格式不支持**
没装可选组件。见[可选组件](#可选组件heic--ocr)，`pip install -r requirements-heic.txt` 后重启即可。
**只想要「HEIC 转出去」不想要「转成 HEIC」也可以** —— 只装带解码器的构建即可，矩阵会只发布解码方向。

**SVG 传上去之后内容少了一部分**
SVG 在渲染前会**先净化**：外部引用（`http(s)://`、`file://`、`@import`、`url()`）与可执行节点
（`<script>`、`<iframe>`、`<foreignObject>` 等）会被剥离。这是刻意的安全设计，被剥离的内容会如实记进 `notes`。
因此产物与源文件可能不一致 —— 这是安全与保真的取舍，选了安全。

**转出来的图片没有预览按钮**
结果预览只对浏览器原生能解的格式开放（BMP / GIF / JPG / PNG / WEBP）。
TIFF / HEIC / ICO 的结果不产生 `preview_url`，界面上不会出现点了没反应的按钮。文件照样能下载。

**预览说「预览链接无效或已过期」**
你已经下载过它了。预览与下载读的是同一份产物文件，下载完它就按规则删掉了。
重新处理一次即可。

**旋转 90° 之后方向反了**
API 的 90 / 180 / 270 是**顺时针**的语义。手机竖拍照片会先按 EXIF 转正再做旋转 ——
如果结果不符合预期，检查一下是不是源文件的 EXIF 方向本身就标错了。

**上传大图时提示超时**
默认单次处理超时 60 秒。可以调大 `FILETOOLS_PROCESS_TIMEOUT`，或调大 `FILETOOLS_MAX_WORKERS`。

**提示「无法读取该图片，请检查文件是否损坏。」**
扩展名和文件头都对，但 Pillow 解不开 —— 通常是文件被截断或内容损坏。批量处理时只有这一个文件失败。

**PDF 页面提示「该 PDF 已加密，需要密码才能打开，暂时无法处理。」**
这份 PDF 设了打开密码。目前不提供密码输入（避免密码进入日志与临时文件），请先用其他工具解密后再上传。

**PDF 转图片时，选择 PNG 之后质量设置不见了**
预期行为。PNG 是无损格式，没有「质量」这个旋钮，界面上会说明原因并建议改用 JPG 或 WEBP。

**PDF 压缩之后体积没有变小**
少数情况会发生：PDF 里几乎没有图片（纯文字），或者图片本身已经是低质量高压缩的。这时服务端会直接保留原文件
（绝不返回一个更大的文件），并在结果页说明。

**拆分出来的文件名为什么是 `part-01.pdf`，不是原文件名**
需求要求「拆成多份时用统一编号，避免与原文件重名」。ZIP 的名字会带上原名。

**PNG 压不到指定大小 / 转成 PNG 之后文件反而变大了**
PNG 是无损格式，压缩能力天然弱于 JPEG，照片类图片转 PNG 通常比原 JPG 更大 —— 界面上会如实显示负数百分比。
想要更小的体积，请转 WEBP。

**文档转换页面提示「当前服务器缺少 Office 转换组件，请联系管理员。」**
服务器上没装 LibreOffice，或者装了但服务找不到它。装上后刷新页面即可，**不用重启服务**
（路径只在探测成功时缓存）。注意：TXT 转 PDF 不走 LibreOffice，缺组件时它照样能用。

**文档转换很慢，而且几个人一起用就要排队**
预期行为。转换是**串行**的，一份约 8–11 秒。这不是没优化，是必须的（见[已知限制](#已知限制)第 4 条）。

**Excel 转出来少了几张表 / PPT 转出来少了几页**
隐藏的工作表和隐藏的幻灯片**不会**被导出。结果里会写明「已导出 N 个工作表」，可以对照一下。

**PDF 转 Word 出来的文档里文字是图片 / 提示需要 OCR**
这份 PDF 是扫描件（每页文字少于 16 个字符）。服务会自动对扫描页做 OCR，但需要装可选组件，
而且单份最多 OCR 30 页。装组件见[可选组件](#可选组件heic--ocr)。

**PDF 转 Word 很慢**
扫描件走 OCR，约 2 秒/页，而且是**逐页串行**的。30 页就是差不多一分钟。

**「最近处理」里为什么只有文件名和大小**
记录只保存在浏览器的 `sessionStorage` 里，只有 文件名称 / 操作类型 / 处理时间 / 处理状态 / 文件大小 五个字段，
不含文件内容也不含下载地址，服务器上没有任何历史记录。关掉标签页记录就没了。

**同时开了好几个页面一起处理，会互相拖慢吗**
会，但不会失控：所有任务共用同一套 Worker 池，池大小是全局上限，不会因为多开页面而翻倍。

**下载下来的是 ZIP**
一次处理了多个文件，结果会自动打包。只处理一个时直接给文件本身。

**下载链接打开是 404**
下载链接是一次性的，已经用过就会失效。重新处理一次即可。

**处理进度条为什么有时候看起来「不动」**
进度只在文件**真的处理完**时才前进，没有插值动画。这是刻意的 —— 进度条上的数字必须等于已完成 + 失败的真实数量。

---

## 后续阶段

- ~~**第二阶段**：图片格式转换、图片尺寸调整~~ ✅ 已完成
- ~~**第三阶段**：PDF 工具箱~~ ✅ 已完成
- ~~**第四阶段**：批量与队列升级~~ ✅ 已完成
- ~~**第五阶段**：文档转换（Word / Excel / PowerPoint / TXT → PDF）~~ ✅ 已完成
- ~~**第六阶段**：PDF → Word（含自动 OCR）~~ ✅ 已完成
- ~~**第七阶段**：统一转换中心 + 批量转换~~ ✅ 已完成
- ~~**第八阶段**：任务队列与 Worker 并发架构~~ ✅ 已完成
- ~~**第九阶段**：统一转换中心 2.0（能力配置驱动、53 → 70 条转换、17 → 19 种格式）~~ ✅ 已完成
- ~~**第十阶段 A**：高级图片引擎（HEIC / SVG / 几何操作 / 元数据 / 预览 / 高级批量）~~ ✅ 已完成
- ~~**第十阶段 C**：图片引擎收尾与最终加固（HEIC 依赖拆分、图片炸弹测试、TIFF XMP、预览边界、全面审计）~~ ✅ 已完成
- ~~**第十一阶段 A**：移动 App 基础框架（Expo / React Native，能力驱动、动态参数表单、本地历史）~~ ✅ 已完成
- ~~**第十一阶段 A 补充**：Windows 桌面版（Tauri 2，复用 Web 前端产物）+ 四平台品牌统一（一份 Logo 真源、一份版本真源）~~ ✅ 已完成
- **第十阶段 B**：RAW（CR2 / NEF / ARW / DNG）与 PSD —— 尚未开始
- **第十一阶段 B**：音频（MP3 / WAV / FLAC / AAC）与视频（MP4 / AVI / MKV / MOV / WEBM）—— 尚未开始

各阶段刻意**没有引入**的东西：

- 第三阶段**没有引入 ReportLab**：六个 PDF 功能全部由 PyMuPDF 完成，少一个依赖就少一份维护与许可成本。
- 第四阶段**没有引入 Redis / Celery**：任务队列是进程内的 `asyncio` 队列，单机部署下够用；队列逻辑集中在
  `services/queue_service.py` 与 `queue_backend.py`，将来要换成外部队列只动这两个模块。
- 第五阶段**没有引入任何新的 Python 依赖**：Office 三种格式交给服务器上已有的 LibreOffice，
  TXT 排版用已有的 PyMuPDF。
- 第九、十阶段**同样没有为功能引入新的 Python 依赖**：BMP / GIF / TIFF / ICO 用 Pillow 自身的能力，
  SVG 栅格化用已有的依赖，TIFF 的 XMP 读写也没引入新库。
  **唯一一个可选依赖是 HEIC 的 `pillow-heif`，它被刻意隔离在 `requirements-heic.txt` 里**（理由见[可选组件](#可选组件heic--ocr)）。
- 第十一阶段 A 的移动 App **必须**引入 React Native / Expo 那一套 npm 包 —— 这是「做一个真实的移动 App」
  这个需求本身的前提，绕不过去。但要说清楚边界：**后端与 Web 前端 `frontend/` 的依赖一个字都没动**，
  后端也没有新增任何 Python 依赖。移动端的依赖清单完全独立在 `mobile/package.json` 里，
  不装它不影响 Web 版与后端，`npm run build` 也不会碰它。
- 第十一阶段 A 补充的 Windows 桌面版**只引入了 Tauri 这一条构建链**
  （`@tauri-apps/cli` + 四个 Rust crate + Tauri 自动下载的 NSIS）。
  **没有引入 `tauri-plugin-opener`**（「打开 / 在文件夹中显示」用 `std::process::Command` 调
  `explorer.exe` 就够了），**也没有引入 `@tauri-apps/api`**（用 `withGlobalTauri` 暴露的全局对象，
  前端 npm 依赖零新增）。
  更重要的是：**桌面端没有复制任何一份转换引擎** —— 里面没有 PDF、图片、Office、OCR、压缩的实现，
  它和网页、手机一样只是**客户端**。品牌图标那条流水线也没有引入任何新依赖：
  光栅化用本来就有的 Playwright + Chromium，编码用本来就有的 Pillow
  （本机**没有** ImageMagick / Inkscape / rsvg / cairosvg，也没有为此去装）。

**明确不做的功能**（这是需求的一部分，不是没来得及做）：

- **PDF → Excel、PDF → PowerPoint**：转换质量无法保证，而且这两条路径需要商业 SDK。
  与其做一个「点了会生成损坏文件」的按钮，不如不做。
- **OCR 独立工具箱、会员 / 支付 / 广告、数据库 / Redis / Celery、API Key / SaaS 计费**：均不在范围内。
- **EPUB / MOBI / AZW3 / DWG / DXF / STL / OBJ / FBX / PST / MSG / EML / TTF / OTF / RAR / 7Z**：
  一个都不做，首页也不假装可用。

每一阶段都保持已有功能的接口不变，并补充对应的测试。**代码中没有任何无法运行的占位实现。**

---

## 许可证

本项目以 **GNU Affero General Public License v3.0（AGPL-3.0）** 发布，全文见 [`LICENSE`](LICENSE)。

**为什么是 AGPL 而不是 MIT。** 本项目的 PDF 能力（合并 / 拆分 / 压缩 / PDF → Word /
PDF ↔ 图片 / 页面抽取与删除）全部由 **PyMuPDF** 实现，而 PyMuPDF 是**双重许可**的：

```text
Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License
```

（见 `pymupdf-1.28.2.dist-info/METADATA` 的 `License` 字段。）

也就是说，任何用到 PyMuPDF 的代码要么遵守 AGPL-3.0，要么向 Artifex 购买商业许可。
把本项目标成 MIT 会与这个约束直接冲突 —— 那等于对外授予了一项我们自己都不拥有的自由。
所以这里选 AGPL-3.0，而不是更宽松的许可。

**这对使用者分别意味着什么：**

| 场景 | 义务 |
| --- | --- |
| 自己部署、自己用 | 没有额外义务 |
| 修改后分发 | 需要以 AGPL-3.0 开放对应源码 |
| **把它当网络服务对外提供** | **AGPL 第 13 条要求向使用者提供对应源码**（这正是 AGPL 与 GPL 的区别） |
| 闭源商用 | 需向 Artifex 购买 PyMuPDF 商业许可，或替换掉 PyMuPDF |

**第三方组件。** LibreOffice（MPL-2.0）、Pillow（MIT-CMU）、python-docx（MIT）、
FastAPI（MIT）、rapidocr-onnxruntime（Apache-2.0）、onnxruntime（MIT）均为宽松许可，
不构成额外约束。

**唯一需要单独注意的是 HEIC。** 可选的 `pillow-heif` 所带的 libheif 链了 **GPLv2** 的
libx265，另有 HEVC 专利池问题 —— 详见 [`backend/requirements-heic.txt`](backend/requirements-heic.txt)
顶部的说明。该依赖**不在**基础依赖里，也**不随本仓库分发**，因此不影响本仓库自身的许可；
但如果要把装着 HEIC 组件的服务对外分发，那是另一件事，需要单独判断。
