import { useEffect } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'

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

export function App() {
  useGlobalDropGuard()

  return (
    <BrowserRouter>
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
    </BrowserRouter>
  )
}
