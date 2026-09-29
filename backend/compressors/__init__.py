"""图片处理核心。

- ``loader``    解码、EXIF 方向、最大边长限制
- ``encoder``   编码与「按目标大小搜索质量 + 缩小尺寸」
- ``resizer``   尺寸调整的几何计算
- ``cropper``   裁剪
- ``svg``       SVG 净化与栅格化
- ``metadata``  只读的拍摄信息读取（第十阶段 A §三十四）
- ``heif``      HEIC / HEIF 的插件注册与运行期探测（§五–§八）
- ``pipeline``  完整处理流程（三个功能共用）
- ``image_compressor`` 第一阶段压缩功能的对外入口

这里刻意不做聚合导入：各模块之间存在相互引用（pipeline ↔ converter），
在包初始化时提前导入会形成循环。请直接从具体模块导入需要的名字。

下面两段是**例外**，都是必须在包导入期完成的副作用，而且都不 import
本包的其它子模块，所以不会引起上面那个循环。
"""

from __future__ import annotations

import logging

from compressors.heif import register as _register_heif

# ----------------------------------------------------------------------
# Pillow 的逐 tag 调试日志必须关掉（第十阶段 A §三十五 / §三十六）
#
# ``PIL.TiffImagePlugin`` 会在 **DEBUG** 级别把读到的**每一个 EXIF tag
# 的原值**打进日志，形如::
#
#     DEBUG PIL.TiffImagePlugin tag: GPSProcessingMethod (27) - value: b'...'
#     DEBUG PIL.TiffImagePlugin tag: BodySerialNumber (42033) - value: ...
#
# 也就是说：只要有人把日志级别调到 DEBUG，用户的 **GPS 坐标与机身序列号**
# 就会原样落进日志文件。§三十五 明令结构化日志不得包含这两样东西，
# 而「反正平时是 INFO，不会触发」不是一个保证 —— 排查线上问题时把级别
# 调到 DEBUG 恰恰是最常做的事，那正是泄露最容易发生的时刻。
#
# 钉在 INFO 上：Pillow 的 WARNING / ERROR（EXIF 损坏、字段读不动）一条不少，
# 只有那份逐 tag 的调试流水被关掉。它是纯诊断噪音，关了不损失任何排查能力。
#
# 放在包的 ``__init__`` 里而不是 ``main.py``：任何一条走图片引擎的路径
# （接口、脚本、测试）都会先导入这个包，所以这个保证跟着引擎走，
# 而不是跟着某一个启动入口走。
logging.getLogger("PIL").setLevel(logging.INFO)

# ----------------------------------------------------------------------
# HEIC 插件注册（§五–§八）
#
# 与上面那条同一个理由，而且**更强**：``pillow-heif`` 是靠往 Pillow 的插件表
# 里注册来工作的，注册必须发生在**任何一次 ``Image.open`` 之前**。
# 放到某个接口里做，就意味着「先被谁调到」决定了 HEIC 能不能用 ——
# 那是个看调用顺序的脸色行事的 bug。放在包导入期，顺序唯一。
#
# 它是幂等的，并且在没有 ``pillow-heif`` 的机器上**静默跳过**：
# HEIC 是可选能力，缺了它 JPG/PNG 一切照旧（见 ``compressors.heif``）。
_register_heif()
