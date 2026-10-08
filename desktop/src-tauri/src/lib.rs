//! FileTools 桌面端的外壳。
//!
//! 这个 crate **只有窗口**。它不认识 PDF、不认识图片、不认识 Office，
//! 一个转换函数都没有，将来也不会有 —— §四 明令禁止在 Windows 端复制转换引擎。
//!
//! 界面来自 `frontend/`（就是 Web 那一套 React 产物，见
//! `frontend/vite.config.ts` 的 `--mode desktop`），文件处理全部发生在
//! `http://127.0.0.1:8000` 的本机后端上。桌面端只是**又一个客户端**，
//! 与 Web / Android / iOS 平级 —— 一个 Backend，多个客户端。

mod result_files;

/// 启动桌面应用。
///
/// [`tauri::generate_context!`] 在**编译期**读 `tauri.conf.json`：窗口尺寸、
/// 图标、以及要内嵌进二进制的前端产物目录（`frontendDist`）。所以配置写错是
/// **编译错误**，而不是运行期才发现的白屏 —— 这也是第 7 步 `cargo check`
/// 能当一道验收用的原因。
///
/// 注册的三个命令（见 [`result_files`]）是 §五 里 WebView2 **给不了**的那部分：
/// 浏览器自己的下载在自定义协议下是静默失效的，所以落盘、打开、定位都由
/// 这里接手。它们只碰文件系统，一个转换函数都没有。
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            result_files::save_result,
            result_files::open_result,
            result_files::reveal_result,
        ])
        .run(tauri::generate_context!())
        .expect("FileTools 桌面端启动失败");
}
