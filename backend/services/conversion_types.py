"""统一转换中心的数据形状（DTO）。

## 为什么单独一个模块

这是一层**叶子**：只定义「一次转换的输入长什么样、结果长什么样」，
不含任何转换逻辑。把它从 ``services.conversion_service`` 里拆出来，
是为了让「按家族拆出去的叶子实现」（``services/text_service`` /
``services/markup_service``）能 import 它而**不反向依赖** ``conversion_service``
—— 否则 ``conversion_service`` import 叶子、叶子 import ``conversion_service``
就是一个循环 import，只能靠在函数体里 import 绕开，越绕越乱。

``services.conversion_service`` 仍然把这四个名字原样导出
（``from services.conversion_types import ...``），所以既有的
``from services.conversion_service import ConversionOptions``
一行都不用改 —— 搬迁对调用方是不可见的。

## 这里只放数据

唯一的行为是 :attr:`DetectedSource.targets` 这个派生属性。它不是逻辑，
是「这个源格式能转成什么」的另一种问法，答案在注册表里（纯数据，零 I/O）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from compressors.cropper import CropRequest
from compressors.resizer import ResizeRequest
from conversion import registry
from conversion.options import METADATA_DEFAULT
from office.txt_to_pdf import TxtOptions
from pdf.image_to_pdf import PdfLayout

__all__ = [
    "ConversionOptions",
    "ConversionOutput",
    "ConversionRequest",
    "DetectedSource",
]


@dataclass(slots=True)
class DetectedSource:
    """一个上传文件的真实身份（校验通过后才会构造出来）。"""

    source_type: str
    extension: str
    size: int
    #: 图片独有
    width: int | None = None
    height: int | None = None
    #: PDF 独有
    page_count: int | None = None

    @property
    def targets(self) -> tuple[str, ...]:
        return registry.targets_for(self.source_type)


@dataclass(slots=True)
class ConversionOptions:
    """用户在界面上选的转换参数。

    字段与 ``/api/config`` 里已经暴露给前端的那些一一对应，
    由路由层用**既有的** ``routers/params.py`` / ``routers/pdf_params.py``
    解析器填好 —— 白名单校验因此与其它十四个工具完全一致。

    第九阶段（§九）新增的四个字段由 ``routers/conversion_params.py``
    从 ``options`` 这个 JSON 表单字段里绑定，**默认值一律是「保持第七阶段的行为」**：
    老前端不传 ``options``，拿到的结果与第七阶段逐字节相同。
    """

    # 图片 → 图片
    quality_preset: str | None = None
    quality_value: int | None = None
    target_bytes: int | None = None
    resize: ResizeRequest | None = None
    # 图片 → PDF
    layout: PdfLayout = field(default_factory=PdfLayout)
    # TXT → PDF
    txt_options: TxtOptions | None = None
    # ---- 第九阶段（§九）----
    #: 顺时针旋转角度，0 表示不旋转。可以是任意角度：90 的整数倍走无损
    #: 重排，其余角度重采样（第十阶段 A §二十三）
    rotation: float = 0
    #: ``None`` 不写入密度；``"original"`` 沿用源文件的密度；正整数写入它
    dpi: int | str | None = None
    #: ``"keep"`` 保留 EXIF，``"remove"`` 丢弃。
    #:
    #: 兜底值读 ``conversion.options.METADATA_DEFAULT``，**不写字面量** ——
    #: 它同时是选项 schema 里那一项的 ``default``。调用方不传 ``options``
    #: 时按这里的兜底走，而界面把 schema 的默认值显示成「已选中」；
    #: 两处一旦各写一份，就会出现「界面写着保留、实际按清除处理」这种
    #: 不报错的分叉（第十阶段 C §三）。
    metadata: str = METADATA_DEFAULT
    # ---- 第十阶段 A（§二十–§二十四）----
    #: 裁剪参数，``None`` 表示不裁剪
    crop: CropRequest | None = None
    #: 非 90 倍数旋转时，画布是否放大到装下整张图
    rotation_expand: bool = True
    #: 翻转方向，见 ``compressors.loader.FLIP_MODES``
    flip: str = "none"


@dataclass(slots=True)
class ConversionRequest:
    """一次转换的全部输入。"""

    source: Path                 # 磁盘上的源文件（in/<n>.<ext>，重试还要用）
    filename: str                # 用户看到的原始文件名
    source_type: str
    target_type: str
    out_dir: Path                # 本项独占的输出目录；结果与下载令牌都登记在这里
    options: ConversionOptions = field(default_factory=ConversionOptions)
    #: 仅 PDF → Word 用得上：真实页码进度靠它（§十二）
    progress_id: str | None = None


@dataclass(slots=True)
class ConversionOutput:
    """一项转换的结果。字段都是给前端展示与下载用的，不含服务器路径。"""

    filename: str
    path: Path
    media_type: str
    size: int
    job_id: str
    download_url: str
    page_count: int | None = None
    width: int | None = None
    height: int | None = None
    notes: list[str] = field(default_factory=list)
    #: 源文件大小（第十阶段 A §三十一）。压缩类结果要能并排显示「原来多大 → 现在多大」。
    #: 非图片家族留 ``None`` —— 那些转换不是压缩，报一个用不上的数只会让界面
    #: 多出一块没人看的信息。
    original_size: int | None = None
    #: 用户要求的目标大小上限。
    target_size: int | None = None
    #: 实际使用的质量。无损格式与未重编码的情形是 ``None``。
    quality_used: int | None = None
    #: 有没有达到目标大小。**``None`` 表示「没有目标可谈」**，不是「没达到」。
    #:
    #: 这两个的区别是 §三十二 的全部要点：没要求目标却报 ``True``，用户会以为
    #: 服务器替他达成了什么；搜到极限仍超标却报 ``True``，那是纯粹的假话。
    #: 所以「没要求」报 ``None``、「要了但没做到」报 ``False``。
    target_reached: bool | None = None
