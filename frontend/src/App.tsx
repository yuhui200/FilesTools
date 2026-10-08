import { useEffect } from 'react'
import { BrowserRouter, HashRouter, Route, Routes } from 'react-router-dom'

import { Layout } from '@/components/Layout'
import { Convert } from '@/pages/Convert'
import { DocToPdf } from '@/pages/DocToPdf'
import { Home } from '@/pages/Home'
import { ImageCompress } from '@/pages/ImageCompress'
import { ImageConvert } from '@/pages/ImageConvert'
import { ImageResize } from '@/pages/ImageResize'
import { ImageTools } from '@/pages/ImageTools'
import { NotFound } from '@/pages/NotFound'
import { PdfCompress } from '@/pages/PdfCompress'
import { PdfEditPages } from '@/pages/PdfEditPages'
import { PdfFromImages } from '@/pages/PdfFromImages'
import { PdfMerge } from '@/pages/PdfMerge'
import { PdfSplit } from '@/pages/PdfSplit'
import { PdfToImages } from '@/pages/PdfToImages'
import { PdfToWord } from '@/pages/PdfToWord'
import { ToolList } from '@/pages/ToolList'

/**
 * 阻止浏览器在「把文件拖到页面空白处」时直接打开该文件。
 * 只有 Dropzone 内部会处理拖放。
 */
function useGlobalDropGuard() {
  useEffect(() => {
    const prevent = (event: DragEvent) => {
      event.preventDefault()
    }
    window.addEventListener('dragover', prevent)
    window.addEventListener('drop', prevent)
    return () => {
      window.removeEventListener('dragover', prevent)
      window.removeEventListener('drop', prevent)
    }
  }, [])
}

/**
 * 桌面端用 HashRouter，Web 用 BrowserRouter。
 *
 * 桌面端的页面由 Tauri 的自定义协议提供，**没有 HTTP 服务器做 SPA 回退**：
 * BrowserRouter 下一个 `filetools://…/pdf/merge` 的深链刷新会直接 404，
 * 因为那个路径下并没有真的文件。HashRouter 把路由放在 `#` 后面，
 * 服务器（这里就是文件协议）永远只看到 index.html。
 *
 * 判据取**构建期常量** `import.meta.env.MODE`，不是去嗅探
 * `window.__TAURI_INTERNALS__` —— 前者在编译时就被替换成确定的字面量，
 * 后者要赌那个全局在页面脚本执行时已经挂上。
 *
 * Web 的 URL 形状完全不变，所有 verify_phase*.py 的路径断言不受影响。
 */
const Router = import.meta.env.MODE === 'desktop' ? HashRouter : BrowserRouter

export function App() {
  useGlobalDropGuard()

  return (
    <Router>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Home />} />
          <Route path="convert" element={<Convert />} />
          {/* 第十阶段 A §五十三：图片页升级成「图片工具」，多了一栏由能力目录
              驱动的格式矩阵。三条旧路径（compress / convert / resize）原样保留。 */}
          <Route path="image" element={<ImageTools />} />
          <Route path="image/compress" element={<ImageCompress />} />
          <Route path="image/convert" element={<ImageConvert />} />
          <Route path="image/resize" element={<ImageResize />} />
          <Route path="pdf" element={<ToolList category="pdf" />} />
          <Route path="pdf/from-images" element={<PdfFromImages />} />
          <Route path="pdf/to-images" element={<PdfToImages />} />
          <Route path="pdf/compress" element={<PdfCompress />} />
          <Route path="pdf/merge" element={<PdfMerge />} />
          <Route path="pdf/split" element={<PdfSplit />} />
          <Route path="pdf/edit-pages" element={<PdfEditPages />} />
          <Route path="pdf/to-word" element={<PdfToWord />} />
          <Route path="doc" element={<ToolList category="doc" />} />
          <Route path="doc/word" element={<DocToPdf kind="word" />} />
          <Route path="doc/excel" element={<DocToPdf kind="excel" />} />
          <Route path="doc/ppt" element={<DocToPdf kind="ppt" />} />
          <Route path="doc/txt" element={<DocToPdf kind="txt" />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Routes>
    </Router>
  )
}
