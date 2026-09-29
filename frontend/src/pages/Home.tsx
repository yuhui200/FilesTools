import { Link } from 'react-router-dom'

import { IconAlert, IconArrowRight, IconGrip, IconImages, IconPhone, IconShield, IconZap } from '@/components/Icons'
import { RecentHistory } from '@/components/RecentHistory'
import { ToolCard } from '@/components/ToolCard'
import { TOOLS, TOOL_GROUPS } from '@/config/tools'
import { useConversionCapabilities } from '@/hooks/useConversionCapabilities'
import type { ConversionCapabilities } from '@/types'

/** 首页「为什么选择 FileTools」——只写真正做到了的事 */
const FEATURES = [
  {
    icon: IconImages,
    title: '批量处理',
    description:
      '图片压缩、格式转换、尺寸调整、PDF 转图片都支持一次选多个文件，最多 50 个，一个批次一次下载。',
  },
  {
    icon: IconZap,
    title: '真实的处理进度',
    description:
      '任务提交后由服务器排队逐个处理，进度条和每个文件的状态都来自服务端快照，不是前端估算出来的。',
  },
  {
    icon: IconGrip,
    title: '拖拽排序',
    description:
      'PDF 合并和 PDF 提取页面可以拖动调整顺序，也可以点上下箭头微调，输出严格按你排定的顺序。',
  },
  {
    icon: IconPhone,
    title: '手机也能用',
    description:
      '按 375 / 390 / 414 等常见手机宽度适配，上传和下载按钮都放大到方便手指点击，窄屏不会横向滚动。',
  },
  {
    icon: IconShield,
    title: '用完即删',
    description:
      '随机文件名 + 独立临时目录，结果文件下载后立即删除，没下载的 30 分钟后由定时任务清理。',
  },
  {
    icon: IconAlert,
    title: '看得懂的错误提示',
    description:
      '统一的错误码对应中文说明，失败时明确告诉你是哪个文件、为什么失败，不会把程序报错直接丢给你。',
  },
]

/** 热门工具：首页单独拎出来，省得每次都要在全部卡片里找 */
const POPULAR_IDS = [
  'universal-converter',
  'image-compress',
  'image-convert',
  'pdf-to-image',
]
const POPULAR_TOOLS = POPULAR_IDS.flatMap((id) => TOOLS.filter((tool) => tool.id === id))

/** 答案由能力目录现场生成的那一条（见 :func:`formatAnswer`） */
const FORMAT_QUESTION = '支持哪些文件格式？'

/** 常见问题（§16）——答案都对应真实行为，不写做不到的承诺 */
const FAQ: { question: string; answer: string }[] = [
  {
    question: '需要注册或登录吗？',
    answer:
      '不需要。打开页面选文件就能处理，全程没有账号、没有邮件验证，也不会有水印。',
  },
  {
    question: '一次最多能处理多少文件？',
    answer:
      '支持批量的工具一次最多 50 个文件，单个文件最大 50 MB，整批合计不超过 300 MB。超出时页面会提示「一次最多处理 50 个文件。」',
  },
  {
    question: '文件会在服务器上保留多久？',
    answer:
      '上传的原始文件和生成的结果文件默认保留 30 分钟（可通过环境变量调整）。结果文件一旦被下载就立即删除，到期未下载的由后台定时任务清理。',
  },
  {
    question: '手机浏览器可以用吗？',
    answer:
      '可以。手机上点上传区域会打开系统自带的文件选择器，界面按窄屏重新排布，下载按钮也放大到适合手指点击。',
  },
  {
    question: '处理失败了怎么办？',
    answer:
      '页面会列出失败的文件名和具体原因（例如「文件已损坏」「格式不支持」），成功的那部分不受影响，可以直接下载。',
  },
  {
    question: FORMAT_QUESTION,
    // 这一条的答案**由服务端能力目录生成**，不是写死的字符串 ——
    // 详见下面的 formatAnswer()。
    answer: '',
  },
]

/** 与格式可用性无关的那几句：它们说的是行为，不是「服务器装没装组件」 */
const FORMAT_BEHAVIOUR =
  'PDF 工具支持标准 PDF 文件（合并、拆分、压缩、提取或删除页面），带密码的 PDF 需要先去掉密码才能处理。文档转换支持 Word（.docx / .doc）、Excel（.xlsx / .xls）、PowerPoint（.pptx / .ppt）转成 PDF，纯文本（.txt）可以转成 PDF / Word / HTML / Markdown，HTML 与 Markdown 也能转成 PDF 或纯文本。PDF 转 Word 把 PDF 转成可编辑的 .docx，文字版直接提取、扫描件自动识别。'

/**
 * 「支持哪些文件格式？」的答案。
 *
 * 以前这里写着一串格式名（「图片支持 JPG、PNG、WEBP…」），那是一句**会过期
 * 的话**：第九阶段加了 BMP / GIF / TIFF / ICO / PDF 之后它就已经不全，
 * 而更要紧的是 HEIC 这种**看服务器装没装组件**的格式 —— 写死了就是在替
 * 服务器承诺一件它可能做不到的事（§五十六：不可用时不得宣传）。
 *
 * 所以改成从 ``matrix`` 现场生成：
 *
 * * 用 ``matrix`` 而不是 ``formats``。``formats`` 是「系统**认识**的格式」，
 *   缺组件时它照样列着 HEIC；``matrix`` 才是「此刻**真的**能转的格式」，
 *   缺组件的格子已经被服务端剔掉了。取错了这一个词，整段话就变成假话。
 * * 类别名取 ``categories``，一个中文名都不在这里抄。
 * * 读不到能力目录时**不列任何格式** —— 说「以页面上列出的为准」，
 *   而不是猜一份「应该都支持」的清单。
 */
function formatAnswer(capabilities: ConversionCapabilities | null): string {
  if (capabilities === null) {
    return `服务器当前支持的格式以「统一转换中心」页面上列出的为准 —— 那一栏读的是服务器的实时能力目录。${FORMAT_BEHAVIOUR}`
  }

  const categoryOf = new Map(capabilities.formats.map((item) => [item.value, item.category]))
  const labelOf = new Map(capabilities.formats.map((item) => [item.value, item.label]))
  const sources = Object.keys(capabilities.matrix)

  const groups = capabilities.categories
    .map((category) => ({
      label: category.label,
      names: sources
        .filter((value) => categoryOf.get(value) === category.value)
        .map((value) => labelOf.get(value) ?? value.toUpperCase()),
    }))
    .filter((group) => group.names.length > 0)

  const list = groups.map((group) => `${group.label}：${group.names.join('、')}`).join('；')
  // 缺组件的说明也带上：能力被悄悄藏起来、页面上什么都不说，
  // 用户只会以为网站坏了。
  const reasons = capabilities.notes.length > 0 ? `（${capabilities.notes.join('')}）` : ''

  return `目前可以处理的格式 —— ${list}。图片可以互相转换、调整尺寸、裁剪旋转，也能压到指定体积或转成 PDF。${FORMAT_BEHAVIOUR}${reasons}`
}

export function Home() {
  // 首页也要读能力目录：FAQ 里那句「支持哪些格式」必须与服务器**此刻**
  // 真的能做的事一致。读不到就不列格式（formatAnswer 里如实说明），
  // 绝不退回一份写死的清单 —— 那正是以前的做法，也正是假话的来源。
  const { capabilities } = useConversionCapabilities()

  return (
    <>
      {/* Hero */}
      <section className="relative overflow-hidden">
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-x-0 -top-40 h-80 bg-gradient-to-b from-brand-100/70 to-transparent blur-2xl"
        />

        <div className="relative mx-auto max-w-6xl px-4 pb-4 pt-16 text-center sm:px-6 sm:pt-24">
          <span className="inline-flex items-center gap-2 rounded-full border border-brand-200 bg-white px-3.5 py-1.5 text-xs font-medium text-brand-700 shadow-sm">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
            批量处理已上线：一次最多 50 个文件
          </span>

          <h1 className="mx-auto mt-6 max-w-3xl text-3xl font-bold leading-tight tracking-tight text-slate-900 sm:text-5xl">
            简单、快速的在线文件工具
          </h1>

          <p className="mx-auto mt-5 max-w-2xl text-base leading-relaxed text-slate-600 sm:text-lg">
            图片、文档、PDF 互转，压缩与调整尺寸，合并、拆分、压缩 PDF。上传后由服务器排队处理，
            下载完自动删除，手机和电脑都能用。
          </p>

          <p className="mx-auto mt-3 max-w-2xl text-sm leading-relaxed text-slate-500">
            不确定该进哪个页面？
            <Link to="/convert" className="font-medium text-brand-600 hover:text-brand-700">
              统一转换中心
            </Link>
            一个入口就够了：上传混合文件，自动识别格式、自动分组，选好目标格式一次转完。
          </p>

          <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
            <Link
              to="/convert"
              className="inline-flex h-12 w-full items-center justify-center gap-2 rounded-xl bg-brand-600 px-6 text-base font-medium text-white shadow-sm transition hover:bg-brand-700 sm:w-auto"
            >
              上传文件
              <IconArrowRight className="h-4 w-4" />
            </Link>
            <a
              href="#tools"
              className="inline-flex h-12 w-full items-center justify-center rounded-xl border border-slate-300 bg-white px-6 text-base font-medium text-slate-700 transition hover:border-slate-400 hover:bg-slate-50 sm:w-auto"
            >
              查看全部工具
            </a>
          </div>
        </div>
      </section>

      {/* 热门工具 */}
      <section className="mx-auto max-w-6xl px-4 pt-16 sm:px-6 sm:pt-20">
        <div className="flex items-baseline justify-between gap-3">
          <h2 className="text-2xl font-semibold tracking-tight text-slate-900 sm:text-3xl">
            热门工具
          </h2>
          <a href="#tools" className="text-sm font-medium text-brand-600 hover:text-brand-700">
            全部 {TOOLS.length} 个工具
          </a>
        </div>

        <div className="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {POPULAR_TOOLS.map((tool) => (
            <ToolCard key={tool.id} tool={tool} />
          ))}
        </div>
      </section>

      {/* 全部工具 */}
      <section id="tools" className="mx-auto max-w-6xl scroll-mt-20 px-4 pt-16 sm:px-6 sm:pt-20">
        <div className="max-w-2xl">
          <h2 className="text-2xl font-semibold tracking-tight text-slate-900 sm:text-3xl">
            选择你要处理的文件
          </h2>
          <p className="mt-3 text-slate-600">
            所有工具都遵循同一个流程：上传文件 → 选择操作 → 自动处理 → 下载结果。
          </p>
        </div>

        {TOOL_GROUPS.map((group, index) => (
          <div key={group.label}>
            <h3
              className={[
                'text-sm font-semibold text-slate-500',
                index === 0 ? 'mt-8' : 'mt-10',
              ].join(' ')}
            >
              {group.label}
            </h3>
            <div className="mt-3 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {group.tools.map((tool) => (
                <ToolCard key={tool.id} tool={tool} />
              ))}
            </div>
          </div>
        ))}
      </section>

      {/* 最近处理（§12） */}
      <section className="mx-auto max-w-6xl px-4 pt-16 sm:px-6 sm:pt-20">
        <RecentHistory />
      </section>

      {/* 为什么选择 */}
      <section className="mx-auto max-w-6xl px-4 pt-16 sm:px-6 sm:pt-20">
        <div className="max-w-2xl">
          <h2 className="text-2xl font-semibold tracking-tight text-slate-900 sm:text-3xl">
            为什么选择 FileTools
          </h2>
        </div>

        <div className="mt-8 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {FEATURES.map((item) => (
            <div key={item.title} className="card flex items-start gap-4 p-5 sm:p-6">
              <span className="grid h-11 w-11 shrink-0 place-items-center rounded-xl bg-brand-50 text-brand-600">
                <item.icon className="h-5 w-5" />
              </span>
              <div>
                <h3 className="font-semibold text-slate-900">{item.title}</h3>
                <p className="mt-1.5 text-sm leading-relaxed text-slate-600">{item.description}</p>
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* 文件安全说明（§16、§18） */}
      <section className="mx-auto max-w-6xl px-4 pt-16 sm:px-6 sm:pt-20">
        <div className="card p-5 sm:p-8">
          <h2 className="flex items-center gap-2 text-xl font-semibold tracking-tight text-slate-900 sm:text-2xl">
            <IconShield className="h-5 w-5 text-brand-600" />
            文件安全说明
          </h2>

          <div className="mt-5 grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
            <div>
              <h3 className="text-sm font-semibold text-slate-900">文件存在哪里</h3>
              <p className="mt-1.5 text-sm leading-relaxed text-slate-600">
                上传的文件只写入服务器上一个随机的临时目录，文件名也是随机生成的，
                不会用你的原始文件名落盘，因此无法通过猜测 URL 访问到别人的文件。
              </p>
            </div>
            <div>
              <h3 className="text-sm font-semibold text-slate-900">保留多久</h3>
              <p className="mt-1.5 text-sm leading-relaxed text-slate-600">
                默认 30 分钟。结果文件下载后立即从服务器删除，未下载的到期由后台清理任务删除，
                不需要你手动操作。
              </p>
            </div>
            <div>
              <h3 className="text-sm font-semibold text-slate-900">不会做的事</h3>
              <p className="mt-1.5 text-sm leading-relaxed text-slate-600">
                不做内容分析，不把文件用于训练或分享给第三方，不需要注册账号，
                也不在服务器上长期保存你的文件。
              </p>
            </div>
            <div>
              <h3 className="text-sm font-semibold text-slate-900">上传前的检查</h3>
              <p className="mt-1.5 text-sm leading-relaxed text-slate-600">
                服务端会校验文件类型和大小，拒绝伪装成图片或 PDF 的其他文件；
                单个文件上限 50 MB，整批合计上限 300 MB。
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* 常见问题（§16） */}
      <section className="mx-auto max-w-6xl px-4 pb-20 pt-16 sm:px-6 sm:pt-20">
        <h2 className="text-2xl font-semibold tracking-tight text-slate-900 sm:text-3xl">
          常见问题
        </h2>

        <div className="mt-8 grid grid-cols-1 gap-4 lg:grid-cols-2">
          {FAQ.map((item) => (
            <details key={item.question} className="card group p-5 sm:p-6">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-3 text-sm font-semibold text-slate-900 [&::-webkit-details-marker]:hidden">
                {item.question}
                <IconArrowRight className="h-4 w-4 shrink-0 text-slate-400 transition-transform group-open:rotate-90" />
              </summary>
              {/* 格式那一条的答案由能力目录现场生成（见 formatAnswer）；
                  其余几条是与可用性无关的行为说明，原样来自 FAQ 表。 */}
              <p className="mt-3 text-sm leading-relaxed text-slate-600">
                {item.question === FORMAT_QUESTION ? formatAnswer(capabilities) : item.answer}
              </p>
            </details>
          ))}
        </div>
      </section>
    </>
  )
}
