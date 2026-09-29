"""图片压缩（第一阶段功能的对外入口）。

核心算法已经抽到可复用的模块中，这里只负责把第一阶段的参数翻译成流水线参数：

- ``compressors/loader.py``  解码、EXIF 方向、最大边长限制
- ``compressors/encoder.py`` 编码与「按目标大小搜索质量 + 缩小尺寸」
- ``compressors/pipeline.py`` 完整处理流程

本模块的对外接口（CompressOptions / CompressResult / compress_image）保持不变，
因此第一阶段的调用方与测试无需改动。

两种工作模式：

1. **按质量档位压缩**（未指定目标大小）
   直接用档位对应的质量值编码一次。

2. **按目标大小压缩**（指定了「不超过 N MB」）
   先在该档位的质量区间内二分搜索，找到「体积不超过目标」的最高质量；
   如果连区间最低质量都超过目标，就等比缩小图片尺寸后再搜一轮，
   直到达标或触发轮次上限。最终保证结果尽可能接近目标大小且不超出。

目标模式下若原文件本身已经不超过目标，直接原样返回，不做任何有损处理。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from compressors.encoder import FORMAT_EXTENSIONS, encode_image
from compressors.loader import load_image
from compressors.pipeline import (
    PipelineOptions,
    PipelineResult,
    run_pipeline,
)
from config import settings
from utils.errors import ValidationError

__all__ = [
    "FORMAT_EXTENSIONS",
    "CompressOptions",
    "CompressResult",
    "compress_image",
    "encode_image",
    "load_image",
]

# 处理结果的字段与流水线完全一致，直接复用同一个类型
CompressResult = PipelineResult


@dataclass(slots=True)
class CompressOptions:
    """压缩参数。"""

    quality_preset: str = settings.DEFAULT_QUALITY_PRESET
    # 目标大小（字节）。None 表示只按质量档位压缩，不限制体积。
    target_bytes: int | None = None


def compress_image(source: Path, options: CompressOptions) -> CompressResult:
    """压缩图片，返回编码后的字节与元信息。"""
    # 档位必须在做任何事之前校验，避免「原文件已达标」的快速路径把非法参数放过去
    if options.quality_preset not in settings.QUALITY_PRESETS:
        raise ValidationError(f"未知的压缩质量档位：{options.quality_preset}")

    return run_pipeline(
        source,
        PipelineOptions(
            quality_preset=options.quality_preset,
            target_bytes=options.target_bytes,
        ),
    )
