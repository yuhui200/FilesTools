# FileTools

简单、快速的在线文件处理工具。核心理念：

> 上传文件 → 选择操作 → 自动处理 → 下载结果

当前进度：**第十阶段已封板（PHASE 10 COMPLETE）**。项目已实现 **19 种格式、70 条转换、7 个 PDF 操作**，
覆盖图片、PDF、Office 文档三条主线，以及把它们统一起来的「统一转换中心」与底层的任务队列 / Worker 并发架构。
前九个阶段的接口与界面全部保持兼容。

---

## 目录

- [环境要求](#环境要求)
- [可选组件（HEIC / OCR）](#可选组件heic--ocr)
- [快速开始](#快速开始)
- [生产模式部署](#生产模式部署)
- [已实现的功能](#已实现的功能)
- [统一转换中心](#统一转换中心)
- [批量处理与任务队列](#批量处理与任务队列)
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
| Node.js | 18 及以上 | 24.15.0 | 仅构建前端时需要 |
| npm | 9 及以上 | 11.12.1 | 仅构建前端时需要 |
| LibreOffice | 7.0 及以上 | 26.2.5 | **可选**，只有 Word / Excel / PPT 转 PDF 需要 |
| pillow-heif | 1.8.0 | 已装 | **可选**，只有 HEIC 需要 |
| rapidocr-onnxruntime | 1.2.3 | 已装 | **可选**，只有 PDF→Word 的扫描件 OCR 需要 |
| Playwright | — | 已装 | 仅验收脚本需要 |

> OCR 引擎用的是 **rapidocr-onnxruntime**（纯 pip 安装、自带中英文模型），不是 Tesseract ——
> 换引擎只需要改 `services/ocr_service.py` 里的 `_load_engine`。

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

本仓库**没有** LICENSE / NOTICE 体系，因此这里不做任何结论性判断。若要把本服务对外分发（尤其是商业分发）：

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
| `CORRUPTED_FILE` | 422 | 文件已损坏或不完整 |
| `PROCESSING_TIMEOUT` | 504 | 处理超时，请换更小的文件重试 |
| `PROCESSING_FAILED` | 422 | 处理失败，请稍后重试 |
| `SERVER_ERROR` | 500 | 服务器处理失败，请稍后重试 |

第七、八阶段新增了任务队列相关的错误码（`TASK_TIMEOUT` / `WORKER_LOST` / `TEMPORARY_IO_ERROR` 等）。
`GET /api/config` 会把服务端可能返回的**全部**错误码列出来，验收脚本会拿它对一遍前端文案表，漏配一个就报错
（前端 `errorMessages.ts` 与后端 `ErrorCode` 之间有**双向相等**的测试断言）。

---

## 技术栈

**前端**：React 18 · TypeScript 5.6 · Tailwind CSS 3 · Vite 5.4 · React Router 6

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
│   ├── public/favicon.svg
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
└── scripts/
    ├── verify_phase2.py … verify_phase10a.py   # 各阶段的真实浏览器 / 真机验收脚本
    ├── verify_phase9a_live.py                  # 后端真机验收（并发、指标、看门狗）
    ├── verify_phase10.py                       # ★ 第十阶段封板入口（210 项断言）
    ├── verify_markup_live.py / verify_text_live.py  # 标记语言 / 文本转换的真机验收
    ├── probe_ocr.py                            # 探测本机 OCR 组件是否可用
    ├── acceptance_final.py                     # 跨阶段总验收
    └── make_office_fixtures.py                 # 生成旧格式测试样张（.doc / .xls / .ppt）
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
| 没有输出且 exit != 0 | `400 CORRUPTED_FILE`「无法读取该 Office 文件，请检查文件是否损坏。」 |
| 其他无输出 / 输出为空 | `422 PROCESSING_FAILED`「文件转换失败，请尝试重新上传文件。」 |
| 超时 | `504 PROCESSING_TIMEOUT` |

> 缺组件的检查在**落盘之前**做：不为一个注定失败的请求把 50 MB 写进磁盘。

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
> 完整说明、已实现的防护清单、以及**明确不在防护范围内**的事项见
> [SECURITY.md](SECURITY.md)（漏洞请走该文件里写的私有上报渠道）。

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

> 已知限制：超时后线程池中的任务无法被强制中断（Python 线程不可取消），但临时目录仍会被正确清理。
> 生产环境建议在前面加一层反向代理限制请求体大小和连接数。

---

## 测试

### 后端单元 / 接口测试

后端有 **1632 项测试**，覆盖十个阶段全部功能的正常流程、边界情况和安全校验：

```bash
cd backend
pip install -r requirements-dev.txt
pytest -o addopts= -q
```

预期输出：

```text
1632 passed
```

> `pytest.ini` 里设了 `addopts = -q`，上面的 `-o addopts=` 是把它临时清掉，
> 这样能拿到每个文件的明细。不加也能跑，只是输出会简略一些。
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

> ⚠️ 这份工作流没有在 GitHub 上实测过（开发机不出网）。

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
15. **Docker 部署与 CI 工作流都未经实测**：开发机是 Windows，没有 Docker、也不出网。
    仓库根目录的 `Dockerfile` / `.dockerignore` 与 `.github/workflows/ci.yml`
    是按官方文档与本地已验证的命令写的，首次使用请以你自己的构建 / 首次 CI 运行结果为准。
16. **图片池的聚合并发倍数在本机约 1.5×，不是 2×** —— 图片处理有相当一部分卡在 CPython 的 GIL 上。用受 GIL 限制的负载去要求并发收益，考的是解释器而不是队列。
17. **LibreOffice 的 profile 目录不会被自动回收**（`office-converter-profile-*` 刻意不含 `filetools` 前缀，免得被孤儿清理误删）。
18. **PDF 结果没有内联缩略图**（预览端点只服务浏览器原生能解的图片格式），PDF 照样能下载。

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
- **第十阶段 B**：RAW（CR2 / NEF / ARW / DNG）与 PSD —— 尚未开始
- **第十一阶段**：音频（MP3 / WAV / FLAC / AAC）与视频（MP4 / AVI / MKV / MOV / WEBM）—— 尚未开始

各阶段刻意**没有引入**的东西：

- 第三阶段**没有引入 ReportLab**：六个 PDF 功能全部由 PyMuPDF 完成，少一个依赖就少一份维护与许可成本。
- 第四阶段**没有引入 Redis / Celery**：任务队列是进程内的 `asyncio` 队列，单机部署下够用；队列逻辑集中在
  `services/queue_service.py` 与 `queue_backend.py`，将来要换成外部队列只动这两个模块。
- 第五阶段**没有引入任何新的 Python 依赖**：Office 三种格式交给服务器上已有的 LibreOffice，
  TXT 排版用已有的 PyMuPDF。
- 第九、十阶段**同样没有为功能引入新的 Python 依赖**：BMP / GIF / TIFF / ICO 用 Pillow 自身的能力，
  SVG 栅格化用已有的依赖，TIFF 的 XMP 读写也没引入新库。
  **唯一一个可选依赖是 HEIC 的 `pillow-heif`，它被刻意隔离在 `requirements-heic.txt` 里**（理由见[可选组件](#可选组件heic--ocr)）。

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
