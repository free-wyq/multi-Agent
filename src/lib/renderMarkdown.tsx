/**
 * 气泡内 markdown 渲染共用函数（方案 A：react-markdown + remark-gfm）。
 *
 * 两处渲染路径共用：
 *  - 流式/定稿气泡（ChatMessageBubble.tsx）：散文段（splitContentByCards 切出的 text 段）
 *    走本函数渲染；card 围栏段走 StructuredCard 不变。
 *  - 持久化气泡（ChatPanel.tsx HighlightMessage）：reload 后的历史消息，本函数 +
 *    @mention 高亮共存（思路 A：在 markdown 文本节点内做 @mention 切分）。
 *
 * 选型：react-markdown + remark-gfm（开源成熟，符合项目硬约束「有开源就用开源 不要自己手搓」）。
 *  - remark-gfm 支持 GFM 表格 / 删除线 / 任务列表 / 自动链接
 *  - 不装 rehype-raw（避免内联 HTML XSS——默认不渲染 raw HTML 即安全），
 *    不装 rehype-highlight（避免引入额外样式依赖，代码块自定义 components.code 配暗色背景即可）。
 *
 * XSS：react-markdown 默认不执行内联 HTML（不传 rehype-raw 即安全），已满足。
 *
 * 不加 animation（项目硬约束「不要灵动」）。hover transition 仅在链接上做轻变色，不滥用。
 */
import React from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { Components } from 'react-markdown'

/** 与橙主题协调的暗色代码块底（取 ChatMessageBubble.css .chat-tool-payload 同款 #1e1e1e 深色系，
 *  保证气泡内代码块视觉统一）。 */
const CODE_BLOCK_BG = '#1e1e1e'
const CODE_BLOCK_COLOR = '#c9d1d9'

/** 行内 code 浅底（vs 块级 code 暗底），与散文背景拉开层次但不抢眼。 */
const INLINE_CODE_BG = 'rgba(250, 140, 22, 0.08)' // 橙主题淡底，与橙主题协调
const INLINE_CODE_BORDER = 'rgba(250, 140, 22, 0.2)'

/** hast 节点最小形状（只用 visit 遍历 text 节点做 @mention 切分，不依赖 @types/hast）。
 *  Element 有 tagName/properties/children；Text 有 value；Root 有 children。 */
interface HastNode {
  type: string
  tagName?: string
  properties?: Record<string, unknown>
  value?: string
  children?: HastNode[]
}

/** 深度遍历 hast 树，把每个 text 节点的 value 用 transform 切分成多节点（@mention 高亮），
 *  返回新 children 数组（不修改原树——每层重建）。
 *  纯函数，零依赖（不引 unist-util-visit，避免装额外包），递归实现。
 *  返回 HastNode[] 给 react-markdown 的 rehypePlugins，react-markdown 会把 text 节点的
 *  value 直接渲染成字符串、把 element 节点按 tagName 查 components。 */
function transformTextNodes(nodes: HastNode[], transform: (text: string) => HastNode[]): HastNode[] {
  const out: HastNode[] = []
  for (const node of nodes) {
    if (node.type === 'text' && node.value != null) {
      out.push(...transform(node.value))
    } else if (node.type === 'element' && node.children) {
      out.push({ ...node, children: transformTextNodes(node.children, transform) })
    } else if (node.type === 'root' && node.children) {
      out.push({ ...node, children: transformTextNodes(node.children, transform) })
    } else {
      out.push(node)
    }
  }
  return out
}

/** @mention 切分→hast text/element 节点列表。
 *  命中成员名（memberNames）：@name 包成一个 element<span className=chat-mention>，
 *  其余是 text 节点。这样 react-markdown 渲染 span 走原生 tagName='span'，
 *  不需在 components 里注册 span（默认 span 透传）。
 *  className 用 chat-mention 让 CSS 染橙（@ant-design 主题色），与流式期 Tag 视觉一致。 */
function splitMentionsToHast(text: string, memberNames: Set<string>): HastNode[] {
  if (!text) return []
  const parts = text.split(/(@[^\s,，.。!！?？:：;；\n]+)/g)
  const out: HastNode[] = []
  for (const part of parts) {
    if (part.startsWith('@')) {
      const name = part.slice(1)
      if (memberNames.has(name)) {
        out.push({
          type: 'element',
          tagName: 'span',
          properties: { className: ['chat-mention'] },
          children: [{ type: 'text', value: part }],
        })
        continue
      }
    }
    out.push({ type: 'text', value: part })
  }
  return out
}

/** 行内 code vs 块级 code 判定用：把 children（可能是 string / number / ReactNode 数组）
 *  拼成纯文本看是否含换行。只用于 isBlock 判定，不用于渲染（渲染原样透传 children）。 */
function flattenText(children: React.ReactNode): string {
  if (children == null) return ''
  if (typeof children === 'string') return children
  if (typeof children === 'number') return String(children)
  if (Array.isArray(children)) return children.map((c) => flattenText(c)).join('')
  return ''
}

const components: Components = {
  // code: 区分行内 code 与块级 code（fenced ``` ```）。
  //  react-markdown v9+：块级 code 的 className 形如 "language-xxx"，行内 code 无。
  //  且块级 code 的 content 含 \n（多行）或 className 含 language- → 视为块级 pre>code（pre 包装器接管暗底）；
  //  其余视为行内 code（浅底 inline）。
  //  children 可能是 string / number / ReactNode 数组（react-markdown v10 不保证纯 string），
  //  判定块级用「拼接文本是否含 \n 或 className 含 language-」，渲染时**原样透传 children**
  //  不做 String(children) 转换——否则元素数组会被 String 化成 "[object Object]"，
  //  再经外层 enhanceTextWithMentions cloneElement 递归时把 object 当 text 传给下游，
  //  触发 react-markdown createFile 的「children expected string」断言崩溃。
  code({ className, children, ...props }) {
    const flatText = flattenText(children)
    const isBlock = className?.includes('language-') || flatText.includes('\n')
    if (isBlock) {
      return (
        <code className={className} style={{ background: 'transparent', color: CODE_BLOCK_COLOR, padding: 0, fontSize: 12 }} {...props}>
          {children}
        </code>
      )
    }
    return (
      <code
        style={{
          background: INLINE_CODE_BG,
          border: `1px solid ${INLINE_CODE_BORDER}`,
          borderRadius: 3,
          padding: '0 4px',
          fontSize: '0.9em',
          fontFamily: 'monospace',
          color: 'inherit',
        }}
        {...props}
      >
        {children}
      </code>
    )
  },
  // pre: 块级代码块外壳——暗底圆角，等宽字体，自动换行不溢出气泡。
  pre({ children, ...props }) {
    return (
      <pre
        style={{
          margin: '6px 0',
          padding: '8px 10px',
          background: CODE_BLOCK_BG,
          color: CODE_BLOCK_COLOR,
          borderRadius: 4,
          fontSize: 12,
          lineHeight: 1.6,
          fontFamily: 'monospace',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          overflowX: 'auto',
          maxWidth: '100%',
        }}
        {...props}
      >
        {children}
      </pre>
    )
  },
  // a: target=_blank rel=noreferrer，主题色（橙）。hover 轻变色 transition。
  a({ children, href, ...props }) {
    return (
      <a
        href={href}
        target="_blank"
        rel="noreferrer"
        style={{ color: '#fa8c16', textDecoration: 'none' }}
        {...props}
      >
        {children}
      </a>
    )
  },
  // 段落 / 标题 / 列表 / 引用 / 表格 / 分隔线：紧凑 spacing，气泡内不要太大留白。
  p({ children, ...props }) {
    return <p style={{ margin: '4px 0', lineHeight: 1.6 }} {...props}>{children}</p>
  },
  h1({ children, ...props }) {
    return <h1 style={{ margin: '8px 0 4px', fontSize: 18, lineHeight: 1.4, fontWeight: 600 }} {...props}>{children}</h1>
  },
  h2({ children, ...props }) {
    return <h2 style={{ margin: '8px 0 4px', fontSize: 16, lineHeight: 1.4, fontWeight: 600 }} {...props}>{children}</h2>
  },
  h3({ children, ...props }) {
    return <h3 style={{ margin: '6px 0 4px', fontSize: 15, lineHeight: 1.4, fontWeight: 600 }} {...props}>{children}</h3>
  },
  h4({ children, ...props }) {
    return <h4 style={{ margin: '6px 0 4px', fontSize: 14, lineHeight: 1.4, fontWeight: 600 }} {...props}>{children}</h4>
  },
  h5({ children, ...props }) {
    return <h5 style={{ margin: '6px 0 4px', fontSize: 13, lineHeight: 1.4, fontWeight: 600 }} {...props}>{children}</h5>
  },
  h6({ children, ...props }) {
    return <h6 style={{ margin: '6px 0 4px', fontSize: 13, lineHeight: 1.4, fontWeight: 600, opacity: 0.85 }} {...props}>{children}</h6>
  },
  ul({ children, ...props }) {
    return <ul style={{ margin: '4px 0', paddingLeft: 20, lineHeight: 1.6 }} {...props}>{children}</ul>
  },
  ol({ children, ...props }) {
    return <ol style={{ margin: '4px 0', paddingLeft: 20, lineHeight: 1.6 }} {...props}>{children}</ol>
  },
  li({ children, ...props }) {
    return <li style={{ margin: '1px 0' }} {...props}>{children}</li>
  },
  blockquote({ children, ...props }) {
    return (
      <blockquote
        style={{
          margin: '6px 0',
          padding: '4px 10px',
          borderLeft: '3px solid #fa8c16',
          background: 'rgba(250, 140, 22, 0.06)',
          color: 'inherit',
          opacity: 0.9,
        }}
        {...props}
      >
        {children}
      </blockquote>
    )
  },
  table({ children, ...props }) {
    return (
      <table
        style={{
          margin: '6px 0',
          borderCollapse: 'collapse',
          maxWidth: '100%',
          fontSize: 13,
        }}
        {...props}
      >
        {children}
      </table>
    )
  },
  thead({ children, ...props }) {
    return <thead style={{ background: 'rgba(0,0,0,0.04)' }} {...props}>{children}</thead>
  },
  tr({ children, ...props }) {
    return <tr {...props}>{children}</tr>
  },
  th({ children, ...props }) {
    return (
      <th
        style={{
          border: '1px solid rgba(0,0,0,0.12)',
          padding: '4px 8px',
          textAlign: 'left',
          fontWeight: 600,
        }}
        {...props}
      >
        {children}
      </th>
    )
  },
  td({ children, ...props }) {
    return (
      <td style={{ border: '1px solid rgba(0,0,0,0.12)', padding: '4px 8px' }} {...props}>
        {children}
      </td>
    )
  },
  hr({ ...props }) {
    return <hr style={{ margin: '8px 0', border: 'none', borderTop: '1px solid rgba(0,0,0,0.1)' }} {...props} />
  },
  img({ src, alt, ...props }) {
    return <img src={src} alt={alt} style={{ maxWidth: '100%', borderRadius: 4 }} {...props} />
  },
}

/** 渲染 markdown 文本为 React 节点。ChatMessageBubble 散文段共用。
 *
 *  纯函数模块——无副作用、无 state、无 context 依赖，可被任意调用方复用。
 *  返回 React.ReactNode 而非 ReactElement：react-markdown 的 components 自定义渲染可能产
 *  多个节点（Fragment 形态），用 ReactNode 表达更准确。 */
export function renderMarkdown(text: string): React.ReactNode {
  if (!text) return null
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
      {text}
    </ReactMarkdown>
  )
}

/** 渲染 markdown 文本为 React 节点，同时做 @mention 高亮（持久化气泡 HighlightMessage 用）。
 *
 *  实现思路（rehype 阶段切分 text 节点，非事后 cloneElement 递归）：
 *  - 用 react-markdown 的 rehypePlugins 在 hast 树渲染成 React 前，深度遍历把每个 text
 *    节点的 value 按 @mention 正则切成 text + element<span class=chat-mention> 序列。
 *  - react-markdown 内部 toJsxRuntime 把 text 节点 value 渲染成字符串、element 节点按
 *    tagName 查 components（span 默认透传），不走「children 必须是 string」的 createFile
 *    断言路径（断言只校验 <Markdown> 的顶层 children prop，不校验 hast 内部 text 节点）。
 *  - 与 markdown 渲染正交两层：markdown 负责 结构（段落/列表/代码块），mention 负责 高亮
 *    （span.chat-mention），互不干扰。
 *
 *  为何不用 cloneElement 递归（原思路 A）：react-markdown v10 的 components 自定义元素
 *  可能返回带非 string children 的节点，事后 cloneElement 递归替换字符串 children 会把
 *  对象当 string 处理，触发 react-markdown createFile 的「children expected string」断言
 *  崩溃（实测 Uncaught Assertion）。rehype 阶段切分在渲染前完成，绕开此坑。
 *
 *  memberNames：成员名 Set（agent_name + alias 去空），命中才包 span.chat-mention。
 *  CSS .chat-mention 染橙（与流式期 AntD Tag color=orange 视觉一致）。 */
export function renderMarkdownWithMentions(text: string, memberNames: Set<string>): React.ReactNode {
  if (!text) return null
  const mentionPlugin = () => (tree: HastNode) => {
    if (tree.type === 'root' && tree.children) {
      return { ...tree, children: transformTextNodes(tree.children, (t) => splitMentionsToHast(t, memberNames)) }
    }
    return tree
  }
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[mentionPlugin]} components={components}>
      {text}
    </ReactMarkdown>
  )
}

