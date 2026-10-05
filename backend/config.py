"""全局配置。

所有可调参数集中在这里，支持通过环境变量覆盖，方便本地开发和部署时调整。
"""

from __future__ import annotations

import os


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _positive_int_env(name: str, default: int) -> int:
    """读一个必须为**正**整数的配置项；非正数或非法值一律回退默认值。

    第八阶段的并发数与超时直接喂给 ``range()`` / ``asyncio.wait_for`` /
    ``ThreadPoolExecutor``，一个 0 或负数会让进程在 import 期就崩掉
    （``ThreadPoolExecutor(max_workers=0)`` 直接抛 ValueError），
    而环境变量写错本来就是常事。范围检查放在读取处，不靠调用方自觉。

    只用在第八阶段新增的数值上；既有配置项的语义一个字都没动。
    """
    value = _int_env(name, default)
    return value if value > 0 else default


def _list_env(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


def _bool_env(name: str, default: bool) -> bool:
    """读一个「是 / 否」环境变量。

    只认这几种写法，其余一律回退到默认值 —— 把 ``FILETOOLS_OCR_DISABLED=0``
    当成「真」是最容易踩的一类坑（非空字符串在 Python 里恒为真）。
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    """运行期配置。"""

    APP_NAME: str = "FileTools API"
    APP_VERSION: str = "0.1.0"

    # ------------------------------------------------------------------
    # 上传限制
    # ------------------------------------------------------------------
    # 单个上传文件的最大字节数，默认 50 MB
    MAX_UPLOAD_BYTES: int = _int_env("FILETOOLS_MAX_UPLOAD_BYTES", 50 * 1024 * 1024)

    # 允许的图片扩展名（小写，含点）
    #
    # 第九阶段（§七）把可读的图片格式从三种扩到六种。``.ico`` **不在其中**：
    # §七 只要求 ``PNG → ICO`` 这一个方向，ICO 不是输入格式。
    # 这份集合与 ``conversion.registry.EXTENSIONS_BY_SOURCE`` 里那几个图片源
    # 必须逐项相等，由 ``test_image_extensions_agree_with_settings`` 钉住 ——
    # 对不上的后果很具体：文件在上传白名单里过了，却走不到转换实现。
    #
    # 第十阶段 A（§五–§八）加进 HEIC / HEIF。它**照旧在这份集合里**，即使
    # 服务器没装 ``pillow-heif``：那样一个 .heic 会走到校验的最后一层，
    # 拿到一句「服务器未安装 HEIC 组件」的真话。把它从这里摘掉的话，
    # 用户看到的会是「暂不支持该文件格式」—— 而那是假话，HEIC 在支持范围内。
    # 能力发布由注册表的 ``heif_decode`` / ``heif_encode`` 把关，不靠这份白名单。
    ALLOWED_IMAGE_EXTENSIONS: set[str] = {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".bmp",
        ".gif",
        ".tif",
        ".tiff",
        ".heic",
        ".heif",
    }

    # 允许的图片 MIME
    ALLOWED_IMAGE_MIMES: set[str] = {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/bmp",
        "image/gif",
        "image/tiff",
        # 手机与相机导出的 HEIC 常见这两种 MIME。``image/heif-sequence``
        # 不收：那是多帧 HEIF 的连拍序列，本阶段只处理单帧（决策 C 同理）。
        "image/heic",
        "image/heif",
    }

    # ------------------------------------------------------------------
    # 图片资源限制（防解压炸弹 / 超大图拖垮服务）
    # ------------------------------------------------------------------
    # 解码后最大像素数，超过直接拒绝
    MAX_IMAGE_PIXELS: int = _int_env("FILETOOLS_MAX_IMAGE_PIXELS", 80_000_000)

    # 单张图片最长边上限，超过则等比缩到该值
    MAX_IMAGE_EDGE: int = _int_env("FILETOOLS_MAX_IMAGE_EDGE", 12_000)

    # ------------------------------------------------------------------
    # 批量处理限制（第四阶段把单批上限提到 50 个文件）
    # ------------------------------------------------------------------
    # 单次批量请求最多处理多少个文件
    MAX_BATCH_FILES: int = _int_env("FILETOOLS_MAX_BATCH_FILES", 50)

    # 单次批量请求所有文件加起来的大小上限。
    # 上传是**流式落盘**的（见 utils/files.save_upload_limited），
    # 这个上限只约束磁盘占用和上传耗时，不随文件变大而吃内存。
    MAX_BATCH_TOTAL_BYTES: int = _int_env(
        "FILETOOLS_MAX_BATCH_TOTAL_BYTES", 300 * 1024 * 1024
    )

    # 整批处理的超时（秒）。单个文件仍受 PROCESS_TIMEOUT_SECONDS 约束。
    BATCH_TIMEOUT_SECONDS: int = _int_env("FILETOOLS_BATCH_TIMEOUT", 300)

    # ------------------------------------------------------------------
    # 处理超时与并发（秒 / 个）
    # ------------------------------------------------------------------
    PROCESS_TIMEOUT_SECONDS: int = _int_env("FILETOOLS_PROCESS_TIMEOUT", 60)

    # ------------------------------------------------------------------
    # 资源池大小（第八阶段 §十四）
    #
    # 四类资源各一个池：图片（Pillow / PyMuPDF）、PDF 工具、Office（LibreOffice）、
    # OCR（含 PDF → Word）。池是**逻辑并发上限**，真正的 CPU 线程仍由
    # MAX_WORKERS 兜住 —— 「不要把所有任务简单扔进 ThreadPoolExecutor」
    # 不是靠多开线程池实现的，而是靠按资源排队、互不阻塞。
    #
    # Office 默认 1 是**配置而不是硬编码**：调成 2 不需要动转换器、注册表、
    # 路由或 worker 一行代码。但要说清楚 —— 它不会真的并行跑两个 LibreOffice，
    # 因为 services/office_converter.py 里那道进程级单飞锁才是真闸门
    # （两个 soffice 共用一个 profile 目录并不安全）。详见交付报告。
    # ------------------------------------------------------------------
    IMAGE_WORKERS: int = _positive_int_env("FILETOOLS_IMAGE_WORKERS", 2)
    PDF_WORKERS: int = _positive_int_env("FILETOOLS_PDF_WORKERS", 1)
    OFFICE_WORKERS: int = _positive_int_env("FILETOOLS_OFFICE_WORKERS", 1)
    OCR_WORKERS: int = _positive_int_env("FILETOOLS_OCR_WORKERS", 1)

    # 同时进行的 CPU 任务数上限，避免大量请求耗尽 CPU 和内存。
    #
    # 第八阶段把默认值从 4 提到 **5 = 四个资源池之和**。这不是「为了跑得快
    # 而加线程」，而是为了不制造假超时：内层 run_in_pool 的计时把
    # **线程池排队时间**也算进去，池槽位一旦多于线程，某个只在排队、
    # 根本没开工的任务就会被判成「处理超时」。两者相等时不会有这种挤压。
    # 未分类批次另走 default 池（QUEUE_WORKERS 个槽位），最坏情况是 7 个
    # 逻辑槽位争 5 条线程 —— 那种情况下 run_in_pool 会如实报 SERVER_BUSY，
    # 而不是谎称超时。可用 FILETOOLS_MAX_WORKERS 调回。
    MAX_WORKERS: int = _positive_int_env(
        "FILETOOLS_MAX_WORKERS",
        IMAGE_WORKERS + PDF_WORKERS + OFFICE_WORKERS + OCR_WORKERS,
    )

    # ------------------------------------------------------------------
    # 每个池的兜底超时（秒，第八阶段 §十六）
    #
    # **生效值是它和池内既有操作超时的较大者**，见
    # services/worker_pool.py::effective_timeout_seconds()。
    # 所以这几个数是**下限，不是上限** —— 把 OFFICE_TIMEOUT_SECONDS 调到 60
    # 不会让一次正常的 Office 转换提前被判死，它仍然有自己那 180 秒。
    #
    # 为什么必须这样：外层 asyncio.wait_for 只能取消协程、杀不掉线程。
    # 池超时若抢在操作自己的限额之前触发，那条线程会攥着 LibreOffice /
    # OCR 锁继续跑完，排队的人越积越多 —— 这正是 OFFICE_CONVERT_TIMEOUT_SECONDS
    # 那条注释在第五阶段就写下的理由，第八阶段沿用同一条规则。
    # ------------------------------------------------------------------
    IMAGE_TIMEOUT_SECONDS: int = _positive_int_env("FILETOOLS_IMAGE_TIMEOUT", 120)
    PDF_TIMEOUT_SECONDS: int = _positive_int_env("FILETOOLS_PDF_TIMEOUT", 180)
    OFFICE_TIMEOUT_SECONDS: int = _positive_int_env("FILETOOLS_OFFICE_POOL_TIMEOUT", 120)
    OCR_TIMEOUT_SECONDS: int = _positive_int_env("FILETOOLS_OCR_TIMEOUT", 300)
    # 未分类批次（既有的四个批量工具、测试直接构造的批次）走 default 池。
    # 它的兜底值要和这些批次真正会用到的最长操作对齐：BATCH_TIMEOUT_SECONDS
    # 与 PDF_RENDER_TIMEOUT_SECONDS 都是 300，所以生效值就是 300。
    DEFAULT_ITEM_TIMEOUT_SECONDS: int = _positive_int_env(
        "FILETOOLS_DEFAULT_ITEM_TIMEOUT", 120
    )

    # ------------------------------------------------------------------
    # 任务队列（第四阶段）
    # ------------------------------------------------------------------
    # 队列 worker 数。每个 worker 一次只处理**一个文件**（§7），
    # 真正的 CPU 并发上限仍由共享线程池的 MAX_WORKERS 兜住 ——
    # 这里留出余量，好让队列忙的时候单文件的交互式操作（如 PDF 压缩）
    # 还能抢到线程，不至于排在 50 个批量文件后面。
    QUEUE_WORKERS: int = _int_env("FILETOOLS_QUEUE_WORKERS", 2)

    # 任务记录保留时长（秒）。结果文件下载后即删，这条只兜底清理
    # 无人认领的任务记录（用户传完就关掉页面）。
    TASK_TTL_SECONDS: int = _int_env("FILETOOLS_TASK_TTL", 30 * 60)

    # 清理线程的扫描间隔（秒）
    CLEANUP_INTERVAL_SECONDS: int = _int_env("FILETOOLS_CLEANUP_INTERVAL", 60)

    # ------------------------------------------------------------------
    # 任务队列的调度策略（第八阶段 §七 / §十九 / §二十一 / §三十）
    # ------------------------------------------------------------------

    # 优先级老化（§七）：等待时间每满这么多秒，优先级上浮一档，最多上浮
    # PRIORITY_AGING_MAX_STEPS 档。这保证一个最低优先级的任务最多等
    # 「秒数 × 档数」就会被当成最高优先级，而同档内再按提交顺序排队 ——
    # 于是**饿死是不可能的**，不是「大概不会」。
    PRIORITY_AGING_SECONDS: int = _positive_int_env("FILETOOLS_PRIORITY_AGING_SECONDS", 60)
    PRIORITY_AGING_MAX_STEPS: int = _int_env("FILETOOLS_PRIORITY_AGING_STEPS", 2)

    # 看门狗的扫描间隔（秒）。它做两件事：拉起已经死掉的 worker 协程，
    # 以及回收「连自己的超时都没能收敛」的 worker（§十九）。
    WORKER_WATCHDOG_INTERVAL_SECONDS: int = _positive_int_env(
        "FILETOOLS_WORKER_WATCHDOG_INTERVAL", 5
    )
    # 项被认领之后，超过「自身超时 + 这个宽限」还没结束，就认定这个 worker
    # 卡死了。宽限是留给「超时已经触发、线程还在收尾」的余地：进程内的
    # LibreOffice / OCR 线程杀不掉，只能等它自己退出。
    WORKER_LOST_GRACE_SECONDS: int = _int_env("FILETOOLS_WORKER_LOST_GRACE", 30)

    # 优雅关机时留给在跑任务的宽限（秒）。**空载时零等待** —— 没有在跑的
    # 项就立刻返回，不会让每次收尾都白等这十秒。宽限到期仍未结束的项会被
    # 如实记为失败（WORKER_LOST），绝不会留在 processing 状态。
    SHUTDOWN_GRACE_SECONDS: int = _int_env("FILETOOLS_SHUTDOWN_GRACE", 10)

    # 自动重试的上限（§二十一）。与手动重试的 MAX_ITEM_RETRIES **分开计数**：
    # 后者是两个对外接口都在暴露的字段，自动重试碰它会让前端「已重试过」
    # 的文案说错话。
    TASK_MAX_AUTO_RETRIES: int = _int_env("FILETOOLS_TASK_MAX_AUTO_RETRIES", 1)

    # 超时**默认不自动重试**（§二十一 原话）。TASK_TIMEOUT 本身属于
    # 「可重试类」（服务器侧的偶发问题），但由这个开关挡住 ——
    # 一个已经跑满 300 秒的任务，再跑一次多半还是超时，白白再占一轮资源。
    TASK_TIMEOUT_AUTO_RETRY: bool = _bool_env("FILETOOLS_TASK_TIMEOUT_AUTO_RETRY", False)

    # ------------------------------------------------------------------
    # 运维观测接口（第八阶段 §二十七）
    #
    # 默认开启：验收脚本跑在已经起好的后端实例上，改不了那个进程的环境变量。
    # 两个接口都刻意做成「无身份信息」的（只有池名、数量、耗时、状态词，
    # 没有文件名、路径、令牌、任务号），所以默认开着也不构成泄露。
    # 不需要时设 FILETOOLS_SYSTEM_API=0，路由**不注册**（404，而不是 403）。
    # ------------------------------------------------------------------
    SYSTEM_API_ENABLED: bool = _bool_env("FILETOOLS_SYSTEM_API", True)

    # ------------------------------------------------------------------
    # 结果文件保存时间（秒）。下载后立即删除，未下载的到期由清理线程删除。
    # §14：默认文件生命周期 30 分钟，可用环境变量覆盖。
    # ------------------------------------------------------------------
    JOB_TTL_SECONDS: int = _int_env("FILETOOLS_JOB_TTL", 30 * 60)

    # 结果临时目录的父目录，None 表示使用系统临时目录
    TEMP_ROOT: str | None = os.environ.get("FILETOOLS_TEMP_ROOT") or None

    # ------------------------------------------------------------------
    # CORS：允许访问后端的前端地址
    # ------------------------------------------------------------------
    CORS_ORIGINS: list[str] = _list_env(
        "FILETOOLS_CORS_ORIGINS",
        [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
    )

    # ------------------------------------------------------------------
    # 压缩参数
    # ------------------------------------------------------------------
    # 质量档位 -> (最低质量, 最高质量)，用于目标大小搜索
    QUALITY_PRESETS: dict[str, tuple[int, int]] = {
        "high": (75, 95),      # 高质量：尽量保留画质
        "balanced": (50, 88),  # 平衡：默认
        "strong": (25, 70),    # 高压缩：体积优先
    }
    DEFAULT_QUALITY_PRESET: str = "balanced"

    # 未指定目标大小时，各档位直接使用的质量值
    QUALITY_PRESET_POINTS: dict[str, int] = {
        "high": 88,
        "balanced": 75,
        "strong": 55,
    }

    #: 界面上给出的质量快捷档（第十阶段 A §二十七 字面）。
    #:
    #: 这是**唯一一份**质量档位表：图片转换、压缩页、尺寸调整页都从它取。
    #: 在此之前，压缩页用的是 ``high/balanced/strong`` 三个名字、
    #: 尺寸调整页用的是裸的 1–100，转换中心又是另一份 —— 同一件事三套说法，
    #: 用户在三个页面看到的质量选项对不上，改一处还得记住另外两处。
    #: 现在三个页面报的是同一组数：100 / 90 / 85 / 80 / 70 / 60 / 50。
    #:
    #: 上面的 ``QUALITY_PRESETS`` / ``QUALITY_PRESET_POINTS`` **不是**同一个概念，
    #: 它们描述的是「按目标大小搜索时该在哪个区间里找」，是实现细节，
    #: 不是给用户看的档位。两者都留着，但只有这一份会出网。
    QUALITY_PRESET_STOPS: tuple[int, ...] = (100, 90, 85, 80, 70, 60, 50)

    #: 各档位在界面上的名字。**与 ``QUALITY_PRESET_STOPS`` 一一对应**，
    #: 由一条测试逐项钉死 —— 漏一个就会在界面上出现一个没有名字的档。
    QUALITY_PRESET_LABELS: dict[int, str] = {
        100: "最高",
        90: "很高",
        85: "推荐",
        80: "较高",
        70: "中等",
        60: "较低",
        50: "最小",
    }

    #: 目标大小的快捷档（第十阶段 A §二十九 字面：500KB / 1MB / 2MB / 5MB / 10MB）。
    #: 规格里的第六档是「自定义」—— 那不是预设，是**允许填任意字节数**，
    #: 由 ``target_bytes`` 本身的 min/max 承担，不在这里编一个假的值。
    TARGET_SIZE_PRESET_BYTES: tuple[int, ...] = (
        500 * 1024,
        1024 * 1024,
        2 * 1024 * 1024,
        5 * 1024 * 1024,
        10 * 1024 * 1024,
    )

    #: 目标大小的上下界。
    #:
    #: **从 ``routers/params.py`` 搬到这里**：选项词表（``conversion/options.py``）
    #: 要拿它当 schema 的 min/max，而那个模块有一条第 十三 阶段定下的机械约束 ——
    #: 只 import ``config``，不许 import 路由层（注册表 import 它，注册表又不许
    #: 拉起 FastAPI 那一串）。边界留在路由层，词表就只能再抄一份，
    #: 而两份边界早晚会有一份先改。搬过来之后两边读的是同一个数。
    MIN_TARGET_BYTES: int = 5 * 1024
    MAX_TARGET_BYTES: int = 200 * 1024 * 1024

    # 格式转换 / 尺寸调整时，用户未指定质量也没有档位时的默认质量。
    #
    # 第十阶段 A §二十七 把这个数定死在 85，并把它推广到**所有**路径：
    # 在此之前转换中心用 85、老转换页与尺寸调整页用 80，同一张图在两个页面
    # 不选质量会得到两份不同大小的结果。**这是本阶段有意改变的行为**，
    # 已如实写进验收报告（与 §十 的页边距改动同一性质）。
    DEFAULT_QUALITY_VALUE: int = _int_env("FILETOOLS_DEFAULT_QUALITY", 85)

    # 目标大小不达标时，每轮等比缩小的系数
    DOWNSCALE_FACTOR: float = 0.85
    # 最多缩小几轮
    MAX_DOWNSCALE_ROUNDS: int = 8

    # ------------------------------------------------------------------
    # PDF 处理限制（第三阶段）
    # ------------------------------------------------------------------
    # 单个 PDF 的页数上限。超长文档会让渲染、拆分开销成倍增长，
    # 与其把服务器拖死，不如明确拒绝。
    MAX_PDF_PAGES: int = _int_env("FILETOOLS_MAX_PDF_PAGES", 500)

    # 生成缩略图的页数上限。几百页的 PDF 全部渲染缩略图要几十秒，
    # 因此只渲染前 N 页，超出的部分前端提示「仅显示前 N 页」。
    PDF_THUMBNAIL_MAX_PAGES: int = _int_env("FILETOOLS_PDF_THUMBNAIL_MAX_PAGES", 60)
    # 缩略图宽度（像素），高度按页面比例自动计算
    PDF_THUMBNAIL_WIDTH: int = _int_env("FILETOOLS_PDF_THUMBNAIL_WIDTH", 220)

    # PDF 转图片：单页渲染的 DPI 与像素上限
    PDF_RENDER_MIN_DPI: int = 36
    PDF_RENDER_MAX_DPI: int = _int_env("FILETOOLS_PDF_RENDER_MAX_DPI", 300)
    PDF_RENDER_DEFAULT_DPI: int = 110
    # 单页渲染后的像素上限，防超大页面（如 A0 图纸）撑爆内存
    PDF_RENDER_MAX_PIXELS: int = _int_env("FILETOOLS_PDF_RENDER_MAX_PIXELS", 40_000_000)

    # 一次最多把多少页导出成图片。渲染是逐页落盘的，内存不会堆积，
    # 但几百张图打包 ZIP 依然会占用大量磁盘和 CPU，所以给一个明确上限。
    PDF_EXPORT_MAX_PAGES: int = _int_env("FILETOOLS_PDF_EXPORT_MAX_PAGES", 100)

    # 「清晰度」档位 -> DPI。前端只暴露这三档，避免用户填出荒唐的值。
    PDF_RESOLUTION_PRESETS: dict[str, int] = {
        "standard": 96,
        "high": 150,
        "ultra": 300,
    }
    DEFAULT_PDF_RESOLUTION: str = "high"

    # 上传的原始 PDF 保留时长（秒）。比结果文件长一些，
    # 因为用户往往会拿同一份 PDF 连续做几个操作。
    PDF_INPUT_TTL_SECONDS: int = _int_env("FILETOOLS_PDF_INPUT_TTL", 30 * 60)

    # PDF 压缩等级 -> (图片分辨率上限 DPI, 重编码质量)
    # DPI 是**上限**：压缩后没有图片的分辨率超过它（见 pdf/compressor.py）
    PDF_COMPRESS_LEVELS: dict[str, tuple[int, int]] = {
        "light": (200, 85),     # 轻度：分辨率上限 200 DPI，质量 85
        "balanced": (150, 72),  # 平衡：默认
        "strong": (96, 55),     # 高压缩：体积优先
    }
    DEFAULT_PDF_COMPRESS_LEVEL: str = "balanced"
    # 「压缩到指定大小」的自定义区间（MB）。放在这里而不是解析器里，
    # 是因为界面上的滑杆范围也要用它 —— 一份界限，两个消费者。
    PDF_MIN_TARGET_MB: float = 0.1
    PDF_MAX_TARGET_MB: float = 500.0

    # 图片转 PDF 的默认参数
    PDF_PAGE_SIZES: dict[str, tuple[float, float]] = {
        # A4/A5/Letter，单位是 PDF 点（1 pt = 1/72 英寸）
        "a4": (595.276, 841.890),
        "a5": (419.528, 595.276),
        "letter": (612.0, 792.0),
    }
    # 「自定义页面尺寸」的区间（毫米）。原先在 ``routers/pdf_params``、
    # ``pdf/image_to_pdf`` 与 ``conversion/options`` 各存了一份同样的
    # 20 / 2000 —— 第九阶段把参数面板接上端点后，三份里任何一份改了值，
    # 界面上的滑杆就会与真正的校验各说各话。
    PDF_MIN_CUSTOM_MM: float = 20.0
    PDF_MAX_CUSTOM_MM: float = 2000.0

    # 页边距档位（点）。第九阶段按 §十 改成整毫米：5 / 10 / 20 毫米。
    # 键名不变（前端与既有接口传的还是 none/small/medium/large），
    # 只有数值变了 —— 界面上标的是毫米，配置里存的必须是同一组毫米，
    # 否则「10 毫米」会办出 12.7 毫米的事。
    PDF_MARGINS: dict[str, float] = {
        "none": 0.0,
        "small": 5 * 72.0 / 25.4,    # 5 毫米  = 14.17 点
        "medium": 10 * 72.0 / 25.4,  # 10 毫米 = 28.35 点
        "large": 20 * 72.0 / 25.4,   # 20 毫米 = 56.69 点
    }

    # 允许上传的 PDF 扩展名。PDF 没有可靠的「内容指纹」，
    # 真实校验靠 pdf.loader 里的文件头检查 + 真正打开一次。
    ALLOWED_PDF_EXTENSIONS: set[str] = {".pdf"}

    # ------------------------------------------------------------------
    # 文档转换（第五阶段：Office / TXT → PDF，靠 LibreOffice headless）
    # ------------------------------------------------------------------
    # LibreOffice 可执行文件路径。留空表示自动查找（常见安装位置 → PATH）。
    # 显式配置了却不存在时会**直接判定为缺少组件**，不会偷偷改用自动找到的
    # 另一个 —— 部署时路径写错却一直用的是别处版本，比直接报错更难查。
    LIBREOFFICE_PATH: str = os.environ.get("FILETOOLS_LIBREOFFICE_PATH", "")

    # 允许上传的文档扩展名。分开放是为了让「上传错了格式」能给出对得上的提示
    #（上传 Excel 却提示「请上传 Word 文档」会很莫名其妙）。
    # 校验不只认扩展名：先看容器魔数，再看内部结构，见 office/loader.py。
    ALLOWED_WORD_EXTENSIONS: set[str] = {".docx", ".doc"}
    ALLOWED_EXCEL_EXTENSIONS: set[str] = {".xlsx", ".xls"}
    ALLOWED_POWERPOINT_EXTENSIONS: set[str] = {".pptx", ".ppt"}
    ALLOWED_TEXT_EXTENSIONS: set[str] = {".txt"}

    # 单次文档转换的等待上限（秒）。LibreOffice 实测预热后约 7.9 秒、
    # 冷启动约 10.7 秒一份，这里给复杂文档留足余量。必须**大于**内层
    # services/office_converter.py 里那个「本值 − _TIMEOUT_MARGIN_SECONDS」：外层是 asyncio.wait_for，
    # 它只取消协程、不会杀线程，若外层先超时，线程会攥着转换锁不放，
    # 排队的人越积越多，最后连图片和 PDF 工具都抢不到线程。
    OFFICE_CONVERT_TIMEOUT_SECONDS: int = _int_env("FILETOOLS_OFFICE_TIMEOUT", 180)

    # 等转换锁的上限（秒）。超过就报「服务器正忙」，而不是让请求无限期占着线程。
    OFFICE_LOCK_WAIT_SECONDS: int = _int_env("FILETOOLS_OFFICE_LOCK_WAIT", 60)

    # 文本转 PDF 的默认排版。界面上可以改，这里只定缺省值与范围。
    TXT_DEFAULT_FONT_SIZE: int = _int_env("FILETOOLS_TXT_FONT_SIZE", 11)
    TXT_MIN_FONT_SIZE: int = 8
    TXT_MAX_FONT_SIZE: int = 32
    # 只给标准纸张：图片转 PDF 的「自动」是「跟随图片尺寸」，纯文本没有这个概念
    TXT_PAGE_SIZES: tuple[str, ...] = ("a4", "a5", "letter")
    TXT_ORIENTATIONS: tuple[str, ...] = ("portrait", "landscape")
    # 行距倍数。中文正文 1.5 倍最易读。
    TXT_LINE_HEIGHT: float = 1.5
    # 页边距（点）。20 毫米，中文文档的常用值，不暴露给用户。
    TXT_MARGIN_PT: float = 56.7
    # 候选字体：字体键 -> (界面显示名, 候选文件名)。
    # 按顺序在系统字体目录里找**第一个存在的文件**，一个都没有就说明这台机器
    # 没装这个字体，界面上不列出来（详见 office/txt_to_pdf.py::available_fonts）。
    # 之所以要按文件探测，而不是直接用 PyMuPDF 的内置 CJK 码：实测 china-s /
    # china-ss / china-t / china-ts / japan / korea **全部解析成同一个
    # Droid Sans Fallback**，拿它们做「宋体 / 黑体」的选择器就是个不生效的假控件。
    # 键用 ASCII 是为了让它当表单值传，界面显示名可以随便改而不影响接口。
    TXT_FONT_CANDIDATES: dict[str, tuple[str, tuple[str, ...]]] = {
        "song": ("宋体", ("simsun.ttc", "SimSun.ttf")),
        "hei": ("黑体", ("simhei.ttf", "SimHei.ttf")),
        "kai": ("楷体", ("simkai.ttf", "KaiTi.ttf")),
        "fangsong": ("仿宋", ("simfang.ttf", "FangSong.ttf")),
        "yahei": ("微软雅黑", ("msyh.ttc", "msyh.ttf")),
        "dengxian": ("等线", ("Deng.ttf", "DengXian.ttf")),
        "noto": ("Noto Sans CJK", ("NotoSansCJK-Regular.ttc", "NotoSansCJKsc-Regular.otf")),
        "wqy": ("文泉驿正黑", ("wqy-zenhei.ttc",)),
    }
    # PyMuPDF **读不动**的字体文件：探测时跳过该文件名，继续试这个字体键的
    # 下一个候选（见 office/txt_to_pdf.py::available_fonts）。
    #
    # 这不是「Noto 不好」，是 **MuPDF 读不了这个集合**。NotoSansCJK-Regular.ttc
    # 也正是 Ubuntu 上 fonts-noto-cjk 装的那一个（/usr/share/fonts/opentype/noto/），
    # 所以 Linux 机器基本都会踩到。拿了真字体在本机实测，MuPDF 会报：
    #     MuPDF error: format error: Index bounds
    # 后果两条，**都是静默的** —— 用户那边看不到任何报错：
    #   1. ``doc.subset_fonts()`` 失效：一页中文 **13.7 MB** 而不是 10 KB。
    #      实测 13,726,876 → 13,726,876 字节，纹丝不动（换 fontbuffer 也一样）。
    #   2. 抽出来的字被换掉：U+0020 → U+00A0（不换行空格）、U+002D → U+2011
    #      （非断字连字符）。因为该字体把这两对字符映射到**同一个字形**
    #      （``Font.has_glyph`` 对两者返回同一字形 id：空格与 NBSP 都是 1，
    #      连字符与非断连字符都是 14），反查 cmap 建 ToUnicode 时挑中了
    #      不换行的那个。复制出去的字看着一样，却搜不到、比对不上。
    #   3. 附带一处更隐蔽的：HTML/Markdown 里的引用块文字**整段消失**。
    #
    # 跳过之后这台机器会落到 TXT_FALLBACK_FONT 上，而内置字体实测三条都正常
    # （1,703,416 → 10,046 字节，抽出文字与输入逐字相同，引用块也在），
    # 中文覆盖靠 Droid Sans Fallback，够用。
    #
    # 只在**文件名**上拦、不做运行期试排：试排要给每个候选字体各建一份临时
    # PDF，启动开销不值当。候选表里第二个文件名 NotoSansCJKsc-Regular.otf
    # **不在**这个表里 —— 它不是集合，实测 CFF 单体子集化正常。
    TXT_FONT_FILES_PYMUPDF_CANNOT_READ: frozenset[str] = frozenset(
        {"notosanscjk-regular.ttc"}
    )

    # 内置回退字体（PyMuPDF 自带，不需要任何字体文件）。
    # 标签只写「内置字体」，不写成「内置黑体」之类：内置的几个中文码实测都指向
    # Droid Sans Fallback，标成宋体或黑体都是假话。
    TXT_FALLBACK_FONT: str = "china-s"
    TXT_FALLBACK_FONT_LABEL: str = "内置字体"

    # 单个文本文件的字符数上限。几十万字的 txt 会排成几百页，
    # 用户等不到结果，服务器也在白烧 CPU —— 与其拖死，不如明确拒绝。
    MAX_TXT_CHARS: int = _int_env("FILETOOLS_MAX_TXT_CHARS", 500_000)
    # 一份文本最多生成多少页（换行极多的文件靠这条兜住）
    MAX_TXT_PAGES: int = _int_env("FILETOOLS_MAX_TXT_PAGES", 500)

    # TXT 排版的等待上限（秒）。排版是纯 CPU 计算，实测 20 万字约 2 秒，
    # 5000 字约 0.2 秒；给到 120 秒是给慢机器和上限字数留的余量。
    # 和 OFFICE_CONVERT_TIMEOUT_SECONDS 分开：那个数是为 LibreOffice 的
    # 冷启动定的，拿它当文本排版的超时会掩盖真正的问题。
    TXT_CONVERT_TIMEOUT_SECONDS: int = _int_env("FILETOOLS_TXT_TIMEOUT", 120)

    # ------------------------------------------------------------------
    # HTML / Markdown（第九阶段）
    # ------------------------------------------------------------------
    # 允许上传的标记文档扩展名。第三层校验没有魔数可用（HTML/Markdown 都是
    # 纯文本），靠的是「能不能解码成文本」+ 顶层的标签/结构是否认得出来，
    # 见 office/loader.py::_check_text 与 office/markup_parse.py。
    ALLOWED_MARKUP_EXTENSIONS: set[str] = {".html", ".htm", ".md", ".markdown"}

    # 单份标记文档的字符数上限。和 MAX_TXT_CHARS 分开：HTML 里光标签就能占掉
    # 一大半，「20 万个字符的 HTML」实际的正文远少于 20 万个字符的 txt，
    # 沿用同一个数会让两边都别扭。
    MAX_MARKUP_CHARS: int = _int_env("FILETOOLS_MAX_MARKUP_CHARS", 500_000)
    # 最多处理多少个标签。嵌套炸弹（<div> 套十万层）靠这条挡住 ——
    # 字符数上限挡不住它：十万个 <div> 只有 60 万字符。
    MAX_MARKUP_NODES: int = _int_env("FILETOOLS_MAX_MARKUP_NODES", 20_000)
    # 一份文档里最多内嵌几张图。图片是 base64 内联的，每张都会真的解码一次，
    # 不设上限的话一份 500 KB 的 HTML 能塞进几千张小图。
    MAX_MARKUP_IMAGES: int = _int_env("FILETOOLS_MAX_MARKUP_IMAGES", 50)
    # 单张**解码后**的内嵌图片字节上限。注意是解码后 —— base64 的体积是
    # 原始字节的 4/3，只看 data: URI 的字符串长度会让上限形同虚设。
    #
    # 这个值必须**小于** MAX_MARKUP_CHARS 换算出来的图片上限（500000 字符
    # 的 base64 约合 375 KB 原始字节），否则它永远不会生效：一张超大的图会
    # 先把整份文档顶穿长度上限，用户看到的是「文件太长」，而不是「这张图太大」。
    # 256 KB 落在下面，所以「一张大图」只会丢掉那张图并如实说明，不会毁掉整次转换。
    MAX_MARKUP_IMAGE_BYTES: int = _int_env(
        "FILETOOLS_MAX_MARKUP_IMAGE_BYTES", 256 * 1024
    )

    # HTML / Markdown → PDF 的等待上限（秒）。和 TXT 排版一样是纯 CPU 计算，
    # 但多了一次 HTML 解析与 Story 排版，给同样的余量。
    MARKUP_CONVERT_TIMEOUT_SECONDS: int = _int_env("FILETOOLS_MARKUP_TIMEOUT", 120)

    # ------------------------------------------------------------------
    # SVG（第十阶段 A §九–§十五）
    # ------------------------------------------------------------------
    # SVG 源文件的字节上限。比 MAX_UPLOAD_BYTES（50 MB）小得多，因为
    # SVG 是**矢量文本**：1 MB 的 SVG 已经能描述出几亿像素的图形，
    # 体积小绝不等于开销小 —— 真正的防线是下面两条。
    #
    # 2 MB 的取值依据：实测 20000 条路径指令的 SVG 约 200 KB，
    # 正常设计稿（Figma/Illustrator 导出）普遍在几十到几百 KB。
    MAX_SVG_BYTES: int = _int_env("FILETOOLS_MAX_SVG_BYTES", 2 * 1024 * 1024)

    # 最多允许多少个 XML 元素。嵌套炸弹（<g> 套十万层）与路径爆炸
    # （一条 path 里几十万条指令）靠这条挡住 —— 字节数上限挡不住前者：
    # 十万个 <g> 只有 60 万字节。取 20000 与 MAX_MARKUP_NODES 对齐，
    # 同一个理由，同一量级。
    MAX_SVG_NODES: int = _int_env("FILETOOLS_MAX_SVG_NODES", 20_000)

    # 认哪些扩展名是 SVG。
    #
    # **单独一份，不并进 ALLOWED_IMAGE_EXTENSIONS**：那份集合的含义是
    # 「Pillow 能解码的图片格式」，``test_image_extensions_agree_with_settings``
    # 把它与注册表里六个图片源逐项钉死。SVG 不是 Pillow 格式 ——
    # 它由 ``compressors.svg`` 用 PyMuPDF 渲染，校验函数也完全是另一条
    # （``validate_svg_upload`` 而不是 ``validate_image_upload``）。
    # 混进同一份集合，只会让「图片格式」这个概念失去边界。
    #
    # ``.svgz``（gzip 过的 SVG）**有意不收**：它是另一种容器，
    # 要先解压再走同一条净化链路，而解压本身又是一个资源放大面
    # （几 KB 能膨胀成几百 MB）。收进来就得配一套压缩比上限与测试，
    # 而用户几乎不会拿 .svgz 来转换 —— 不做，也不假装支持。
    ALLOWED_SVG_EXTENSIONS: set[str] = {".svg"}

    # ------------------------------------------------------------------
    # PDF → Word（第六阶段 A）
    # ------------------------------------------------------------------
    # 一页算不算「有文字层」。低于这个字符数就按扫描页处理、送去 OCR。
    # 取 16 而不是 0：很多扫描件会带一层由 OCR 软件塞进去的、只认出页眉页码的
    # 残缺文字层；阈值太低会让这些页被当成文字页直接抽出来，结果比 OCR 还差。
    PDF_TO_WORD_MIN_TEXT_CHARS: int = _int_env("FILETOOLS_PDF_TO_WORD_MIN_CHARS", 16)

    # 逐页 OCR 的渲染 DPI。实测 A4 @200 DPI = 1653×2339 像素、单页 2.0 秒；
    # 调到 300 会让耗时和内存都翻一倍多，而中文识别率提升有限。
    PDF_TO_WORD_OCR_DPI: int = _int_env("FILETOOLS_PDF_TO_WORD_OCR_DPI", 200)
    # 一页什么都没认出来时的重试 DPI。只重试这一页，不是整份重来。
    PDF_TO_WORD_OCR_RETRY_DPI: int = _int_env("FILETOOLS_PDF_TO_WORD_OCR_RETRY_DPI", 300)

    # 一份 PDF 最多 OCR 多少页。按实测 2 秒/页算，30 页约 1 分钟；
    # 不设这条的话 MAX_PDF_PAGES 的 500 页会变成十几分钟，请求早断了、线程还在烧。
    PDF_TO_WORD_MAX_OCR_PAGES: int = _int_env("FILETOOLS_PDF_TO_WORD_MAX_OCR_PAGES", 30)

    # 等 OCR 锁的上限（秒）。和 OFFICE_LOCK_WAIT_SECONDS 同一个理由：
    # run_in_pool 的 wait_for 取消不了线程，一个被取消却还活着的 OCR 线程
    # 会攥着锁不放，无限期等待会把整个线程池拖死。
    PDF_TO_WORD_OCR_LOCK_WAIT_SECONDS: int = _int_env(
        "FILETOOLS_PDF_TO_WORD_OCR_LOCK_WAIT", 60
    )

    # 整个转换的等待上限（秒），以及 worker 自己的预算。
    # 后者必须**严格小于**前者：外层 asyncio.wait_for 只能取消协程，
    # OCR 又是在进程内跑的、没有子进程可杀，所以只能靠 worker 自己
    # 在每页开工前检查是否超预算，主动抛超时先退出。
    PDF_TO_WORD_TIMEOUT_SECONDS: int = _int_env("FILETOOLS_PDF_TO_WORD_TIMEOUT", 300)
    PDF_TO_WORD_WORKER_BUDGET_SECONDS: int = _int_env(
        "FILETOOLS_PDF_TO_WORD_WORKER_BUDGET", 270
    )

    # 扫描页嵌进 DOCX 的那张原图。实测 200 DPI PNG 约 3.2 MB/页，
    # 100 页就是 300 MB 的 Word 文件；换成低 DPI 的 JPEG 后约 0.3–0.7 MB/页。
    # 用 OCR 那一张去嵌是不行的 —— OCR 要的是清晰度，Word 里要的是能看。
    PDF_TO_WORD_EMBED_DPI: int = _int_env("FILETOOLS_PDF_TO_WORD_EMBED_DPI", 150)
    PDF_TO_WORD_EMBED_JPEG_QUALITY: int = _int_env(
        "FILETOOLS_PDF_TO_WORD_EMBED_QUALITY", 80
    )
    # 一份文档里所有嵌入图片的总字节上限。用完就不再嵌图，并在结果里
    # 如实说明「部分页面只保留了文字」—— 既不静默丢图，也不把磁盘写满。
    PDF_TO_WORD_MAX_EMBED_BYTES: int = _int_env(
        "FILETOOLS_PDF_TO_WORD_MAX_EMBED_BYTES", 60 * 1024 * 1024
    )

    # 写进 DOCX 的中文字体名。**这是给阅读器的一个提示，不是保证** ——
    # 字体得在打开这份文档的电脑上存在，服务端给不了任何保证。
    # 取宋体是因为它最接近中文正式文档（合同、公文）的默认正文外观；
    # 缺了它会落到阅读器的兜底字体上，中文照样显示，只是长相不同。
    PDF_TO_WORD_FONT: str = os.environ.get("FILETOOLS_PDF_TO_WORD_FONT", "").strip() or "宋体"

    # 强制关掉 OCR。给验收脚本用：在一台装了 OCR 的机器上模拟
    # 「服务器没装 OCR 组件」，验证降级路径和那句提示。
    OCR_DISABLED: bool = _bool_env("FILETOOLS_OCR_DISABLED", False)

    # OCR 组件声称覆盖的字符集。**这不是可切换的语言包**：本机选的
    # rapidocr-onnxruntime 是单一内置中英文模型，构造函数没有 lang 参数，
    # 传这几个值进去不会有任何效果。取值沿用用户规格里的写法，只读暴露给前端
    # 展示，绝不作为请求参数 —— 否则就是第五阶段那个「假宋体/黑体选择器」的重演。
    OCR_LANGUAGES: tuple[str, ...] = ("chi_sim", "eng")


settings = Settings()
