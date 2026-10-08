// 发布版不附带控制台窗口。
//
// Tauri 在 Windows 上默认按 console 子系统链接，于是双击快捷方式会先弹一个
// 黑框再出界面。这个属性把它换成 windows 子系统。debug 构建故意不换 ——
// 开发时要看 `println!` 与 panic 输出。
//
// 该属性在非 Windows 平台上会被忽略，不影响跨平台编译。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    filetools_lib::run()
}
