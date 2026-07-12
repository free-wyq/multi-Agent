/**
 * PL-12 / ST-06：文件扩展名 → antd File* 图标 + 主题色映射，及浏览器侧下载/大小工具。
 *
 * 被 TaskPage 交付物卡与 ChatMessageBubble 产物下载卡**复用**——单一真源，避免两处各写
 * 一份扩展名分支 + saveBlob/humanSize 漂移。task 22 把 TaskPage 原本地 saveBlob/humanSize
 * 与新增的 fileIconFor 一并抽到此模块，两个消费方 import 同一份实现。
 */
import type { CSSProperties, ReactNode } from 'react'
import {
  FilePdfOutlined,
  FileMarkdownOutlined,
  FileImageOutlined,
  FileZipOutlined,
  FileWordOutlined,
  FileExcelOutlined,
  FilePptOutlined,
  FileTextOutlined,
  FileUnknownOutlined,
} from '@ant-design/icons'

/** 扩展名 → 主题色（与文件管理器惯例一致：pdf 红 / excel 绿 / word 蓝 ...）。 */
const EXT_COLOR: Record<string, string> = {
  pdf: '#ff4d4f',
  md: '#6366f1',
  markdown: '#6366f1',
  jpg: '#52c41a',
  jpeg: '#52c41a',
  png: '#52c41a',
  gif: '#52c41a',
  bmp: '#52c41a',
  svg: '#52c41a',
  webp: '#52c41a',
  ico: '#52c41a',
  zip: '#fa8c16',
  rar: '#fa8c16',
  '7z': '#fa8c16',
  tar: '#fa8c16',
  gz: '#fa8c16',
  bz2: '#fa8c16',
  xz: '#fa8c16',
  doc: '#2f54eb',
  docx: '#2f54eb',
  xls: '#52c41a',
  xlsx: '#52c41a',
  csv: '#52c41a',
  ppt: '#fa541c',
  pptx: '#fa541c',
}

/** 文本/代码类扩展名 → FileTextOutlined（灰）。antd 此版本无 FileCodeOutlined，代码文件
 *  归入文本类（代码本质是文本，FileTextOutlined 语义不冲突）。 */
const TEXT_EXTS = new Set([
  'txt', 'log', 'json', 'yaml', 'yml', 'xml', 'html', 'htm', 'css', 'scss', 'less',
  'js', 'mjs', 'cjs', 'ts', 'jsx', 'tsx', 'py', 'rs', 'go', 'java', 'kt', 'c',
  'cpp', 'cc', 'h', 'hpp', 'sh', 'bash', 'zsh', 'rb', 'php', 'sql', 'toml',
  'ini', 'cfg', 'conf', 'env', 'lock', 'gitignore', 'dockerignore',
])

/**
 * 文件名 → 对应的 antd File* 图标（带主题色）。
 *
 * 按文件名最后一段 `.` 后的小写扩展名查表：
 *  - pdf/markdown/image/zip/word/excel/ppt → 各自专属 FileXxxOutlined + 主题色；
 *  - 文本/代码类（txt/log/json/py/ts/...）→ FileTextOutlined（灰）；
 *  - 无扩展名或未识别 → FileUnknownOutlined（灰，比通用 FileOutlined 更准确表达「不认识这个类型」）。
 *
 * @param name 文件名（basename 即可，含扩展名）；取最后 `.` 段小写判定。
 * @param style 透传给图标的 style（fontSize 等）；color 默认由扩展名决定，可被覆盖。
 */
export function fileIconFor(name: string, style?: CSSProperties): ReactNode {
  const ext = (name.split('.').pop() || '').toLowerCase()
  const color = EXT_COLOR[ext]
  const merged: CSSProperties = { ...(color ? { color } : {}), ...style }
  switch (ext) {
    case 'pdf':
      return <FilePdfOutlined style={merged} />
    case 'md':
    case 'markdown':
      return <FileMarkdownOutlined style={merged} />
    case 'jpg':
    case 'jpeg':
    case 'png':
    case 'gif':
    case 'bmp':
    case 'svg':
    case 'webp':
    case 'ico':
      return <FileImageOutlined style={merged} />
    case 'zip':
    case 'rar':
    case '7z':
    case 'tar':
    case 'gz':
    case 'bz2':
    case 'xz':
      return <FileZipOutlined style={merged} />
    case 'doc':
    case 'docx':
      return <FileWordOutlined style={merged} />
    case 'xls':
    case 'xlsx':
    case 'csv':
      return <FileExcelOutlined style={merged} />
    case 'ppt':
    case 'pptx':
      return <FilePptOutlined style={merged} />
    default:
      if (TEXT_EXTS.has(ext)) {
        return <FileTextOutlined style={{ color: '#595959', ...style }} />
      }
      return <FileUnknownOutlined style={{ color: '#8c8c8c', ...style }} />
  }
}

/** 浏览器侧把 Blob 存为下载（`<a download>` 点击 + 下一帧 revoke，避免内存泄漏）。 */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  // revoke on next tick so the download has started
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

/** 字节 → 人类可读大小（B/KB/MB）。 */
export function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
