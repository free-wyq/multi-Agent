// Vitest 全局测试初始化
// 引入 jest-dom 的 DOM 断言匹配器（toBeInTheDocument / toBeVisible / ...）
import '@testing-library/jest-dom/vitest'

// jsdom 不实现 matchMedia，组件库（antd 等）可能调用，补空实现避免报错
if (!window.matchMedia) {
  window.matchMedia = (query: string): MediaQueryList => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })
}

// jsdom 无 ResizeObserver，ReactFlow / antd 等可能依赖，补一个 no-op
if (!window.ResizeObserver) {
  window.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
}
