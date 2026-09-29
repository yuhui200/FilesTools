"""图片元数据查看接口（第十阶段 A §三十三–§三十六）。

一个只读接口：**上传一张图，拿回它自带的拍摄信息**。不产出文件，
因此不建任务、不进队列、没有下载地址 —— 也正因为不产出文件，
响应是同步的：用户点「查看」，界面就该立刻显示，让他去轮询一个
「查看任务」是荒唐的。

和其余图片接口共用同一条上传通道（``services.intake``）：同一个临时目录、
同一个全局线程池、同一个超时。第十阶段 A §五十九–§六十三 明令不许为
图片再建第二个池子或第二套超时，这里就是照着那句话写的 ——
``run_in_pool`` 用的还是 ``intake`` 里那一个 ``_executor``。

## 为什么单独一个文件而不是塞进 ``routers/convert.py``

``convert.py`` / ``resize.py`` / ``compress.py`` 三个都是「提交 → 轮询」
的批量接口，返回 ``TaskCreatedResponse``。这个不是。混进去会让那个文件
同时出现两种完全不同的响应契约，读的人得先把它们分开才能看懂任何一个。
"""

from __future__ import annotations

from fastapi import APIRouter, File, UploadFile
from pydantic import BaseModel, Field

from compressors.metadata import read_metadata
from services.intake import intake_upload, run_in_pool
from utils.files import create_temp_dir, remove_dir

router = APIRouter(prefix="/api/image", tags=["image"])


class MetadataFieldResponse(BaseModel):
    """一行元数据。

    ``value`` 为 ``null`` 表示**这一项读不出来**（文件里没有，或者没有这个概念），
    由界面显示成「无法读取」。它不会是一个空字符串 —— 空串与「读不出来」
    在界面上该长得一样，服务端先把它们合成一件事。
    """

    key: str = Field(description="稳定的项标识，如 width / camera / gps")
    label: str = Field(description="中文标签，界面直接显示")
    value: str | None = Field(description="已格式化的值；null 表示读不出来")
    unit: str | None = Field(default=None, description="单位，没有就不出现")


class MetadataResponse(BaseModel):
    """一次查看的结果：一个**有序**的行列表。

    做成列表而不是具名字段，是因为界面要的就是「按这个顺序铺开」——
    顺序在两处各写一遍，迟早有一处被改漏（§三十四 给的就是这个顺序）。
    """

    filename: str = Field(description="用户上传时的文件名，原样回显")
    format: str | None = Field(default=None, description="真实容器格式，如 JPEG")
    width: int | None = None
    height: int | None = None
    fields: list[MetadataFieldResponse]


@router.post(
    "/metadata",
    response_model=MetadataResponse,
    summary="查看图片元数据",
)
async def metadata_endpoint(
    file: UploadFile = File(..., description="待查看的图片（jpg / png / webp / bmp / gif / tiff / ico）"),
) -> MetadataResponse:
    """读一张图片自带的拍摄信息，不修改、不保存、不产出新文件。

    ``format`` / ``width`` / ``height`` 既在这里出现，也在 ``fields`` 里出现
    —— **不是两份真相**：两者都由 ``read_metadata`` 那一次读取的同一个
    对象产出（``fields`` 是它的 ``to_fields()`` 视图）。所以它们不可能
    互相对不上，``test_metadata`` 里那条对账断言因此是恒真的 ——
    留着它是因为「换个人来写就可能变成两次读取」，而那时它会立刻变红。

    顶层这三个键存在的理由是方便：调用方想知道尺寸时不必遍历列表。
    """
    work_dir = create_temp_dir()
    try:
        # ``intake_upload`` 已经做完三件事：限流落盘、三层校验
        # （扩展名 / 魔数 / 真正解不打得开）、出错时删掉半成品。
        # 这里一行校验都不另写 —— 同一个规则判两遍早晚有一处被改漏。
        info, source = await intake_upload(file, work_dir)
        metadata = await run_in_pool(read_metadata, source)
    finally:
        # 与转换类接口的区别：这里**没有产物要留**，所以无论成功失败
        # 都清干净。用 ``finally`` 而不是 ``except``，是因为成功的路径
        # 同样要删 —— 只兜异常会留下一个装着用户原图的目录。
        remove_dir(work_dir)

    return MetadataResponse(
        filename=info.filename,
        format=metadata.format,
        width=metadata.width,
        height=metadata.height,
        fields=[
            MetadataFieldResponse(
                key=item.key,
                label=item.label,
                value=item.value,
                unit=item.unit,
            )
            for item in metadata.to_fields()
        ],
    )
