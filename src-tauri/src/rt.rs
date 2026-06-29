//! 异步运行时桥接
//!
//! Tauri setup 钩子在同步上下文中执行，直接 `tokio::spawn` / `Handle::current()`
//! 会 panic（当前线程无 reactor）。这里统一委托给 Tauri 内置的 `async_runtime`，
//! 它持有 Tauri 创建的 tokio runtime，可在任意线程派发任务。

/// 在 Tauri 全局 runtime 上 spawn 一个任务（即便不在 async 上下文）
pub fn spawn<F>(future: F)
where
    F: std::future::Future<Output = ()> + Send + 'static,
{
    // tauri::async_runtime::spawn 可在同步上下文调用
    let _ = tauri::async_runtime::spawn(future);
}
