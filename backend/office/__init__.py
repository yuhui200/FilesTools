"""文档转换相关的纯逻辑层。

和 ``backend/pdf/`` 一样，这里只放不依赖 FastAPI 的东西：
校验（loader）、文本排版（text_pdf）、字体探测（fonts）、
LibreOffice 调用（libreoffice）。路由与流程编排在 ``services/`` 里。
"""
