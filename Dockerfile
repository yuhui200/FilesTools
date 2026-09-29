# FileTools —— 后端 + 已构建前端的单镜像部署
#
# ⚠️ 未实测：这份 Dockerfile **从未在本项目的开发环境里构建或运行过**
#    （开发机是 Windows，没有 Docker）。下面的包名、路径与顺序按官方文档写成，
#    请以你自己 `docker build` 的结果为准。已知最可能出问题的两点：
#      1. 中文字体 —— python:*-slim 镜像里一个中文字体都没有。不装
#         fonts-noto-cjk，中文 PDF 会整页变成方框（豆腐块）。
#      2. LibreOffice 启动器路径 —— 这里假设是 /usr/bin/soffice。基础镜像不同
#         时用 FILETOOLS_LIBREOFFICE_PATH 指过去（可指文件，也可指目录）。
#
# 基础镜像用 python:3.14-slim：本项目 requirements.txt 的版本组合是在
# Python 3.14 下实测通过的（见 README「环境要求」），不要随手降到更低的版本。
FROM python:3.14-slim

# LibreOffice 体积不小（约 400 MB），单独一层，改代码时不会重复下载。
# writer/calc/impress 三个包覆盖本项目支持的全部 Office 源格式（doc/docx、
# xls/xlsx、ppt/pptx）—— 用的正是 LibreOffice 的无头转换。
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer libreoffice-calc libreoffice-impress \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先只拷依赖清单再装：改业务代码不会让这一层缓存失效
COPY backend/requirements.txt backend/
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/

# 前端必须在宿主机先跑过 `npm run build`（镜像里不装 Node）。
# 若没有 frontend/dist，这一行会直接构建失败 —— 这是故意的，
# 总比构建成功、跑起来却是 404 好查。
COPY frontend/dist/ frontend/dist/

# LIBREOFFICE_PATH 显式指到启动器：配置了却不存在时，服务会如实报「缺少组件」，
# 不会偷偷改用自动找到的另一个版本。
# PYTHONIOENCODING 是保险：slim 镜像的 locale 可能是 POSIX，中文会乱码。
ENV FILETOOLS_LIBREOFFICE_PATH=/usr/bin/soffice \
    PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# 用 python 而不是 curl：slim 镜像里没有 curl
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "backend"]
