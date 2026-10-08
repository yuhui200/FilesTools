// Tauri 的构建期钩子。
//
// 它会做三件事：解析 tauri.conf.json 并把 frontendDist 打进二进制、
// 生成 capabilities 的权限 schema、在 Windows 上把图标与版本资源写进 PE。
//
// 所以 tauri.conf.json 里的 frontendDist **必须真的存在**，否则这一步就报错 ——
// 也就是说 `cargo check` 本身就顺带验了「前端产物在不在」这件事。

fn main() {
    tauri_build::build()
}
