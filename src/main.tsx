import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './App.css'
import App from './App'

// 清掉 URL 里的 hash。react-router 已于 2026-07-11 移除，但浏览器历史/书签里仍可能残留
// 旧路由（如 …/#/monitor）。这些 hash 现在无人读取，留着只让地址栏看着奇怪。
// replaceState 不触发导航、不新增历史项，把地址收敛成干净的 pathname(+search)。
if (window.location.hash) {
  history.replaceState(null, '', window.location.pathname + window.location.search)
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
