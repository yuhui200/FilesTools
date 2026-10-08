//! 结果文件的落盘、打开与定位。
//!
//! §五 要求 Windows 端能「下载、打开结果、在系统文件夹中显示」。这三件事
//! 在 Web 上是浏览器给的，在 Tauri 的 WebView2 里**浏览器那套是坏的**：
//! `<a download>` + `URL.createObjectURL` 在自定义协议下是**静默 no-op** ——
//! 不报错、不下载、什么都不发生。所以桌面端必须自己接过来。
//!
//! 只做三件事：
//!
//! * [`save_result`] —— 把界面传回来的字节写进用户的「下载」目录，返回绝对路径；
//! * [`open_result`] —— 用默认程序打开它；
//! * [`reveal_result`] —— 在资源管理器里选中它。
//!
//! 后两个都是 `std::process::Command` 调 `explorer.exe`，**没有引入任何插件**。
//!
//! ### 为什么文件名走 HTTP 头、而且要百分号编码
//!
//! Tauri 的原始二进制 IPC（`InvokeBody::Raw`）只传字节，没有具名参数可以放
//! 文件名。回到 JSON 载荷就得以 base64 传文件内容，大文件要胖三分之一。
//! 所以文件名搭在请求头上。
//!
//! 但 fetch 的 `Headers` 按 Fetch 规范做的是 **ByteString** 转换：值里出现
//! 码位大于 0xFF 的字符会**当场抛 TypeError**。中文文件名恰好就是这种字符。
//! 于是约定：前端用 `encodeURIComponent` 编码，Rust 这边 [`percent_decode`]
//! 解回来。**不要**图省事直接把原始文件名塞进头里 —— 那会在「文件名是中文」
//! 这条最常见的路径上炸掉，而且报的是「保存失败」这种指向错误方向的错。

use std::path::{Path, PathBuf};
use std::process::Command;

use tauri::ipc::{InvokeBody, Request};
use tauri::Manager;

/// 文件名所在的请求头。值必须是 `encodeURIComponent` 之后的 ASCII。
const FILENAME_HEADER: &str = "x-filetools-filename";

/// 取不到文件名时的兜底名字（没有扩展名，因为不知道内容是什么格式）。
const FALLBACK_NAME: &str = "filetools-result";

/// 文件名（含扩展名）的长度上限。
///
/// Windows 整条路径上限 260 字符，用户目录本身就可能很长。120 留足余量，
/// 又远大于任何真实文件名。截断时**保住扩展名** —— 扩展名决定用什么程序打开。
const MAX_NAME_LEN: usize = 120;

/// Windows 不允许出现在文件名里的字符，外加路径分隔符。
const ILLEGAL_CHARS: [char; 9] = ['<', '>', ':', '"', '/', '\\', '|', '?', '*'];

/// Windows 的设备名。这些名字**即使带扩展名**也不能用作文件名
/// （`CON.txt` 一样打不开），所以要加前缀躲开。
const RESERVED_NAMES: [&str; 22] = [
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8",
    "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
];

/// 把 `encodeURIComponent` 编出来的字符串解回原文。
///
/// 只认 `%XX` 形式，其余字节原样保留；非法转义（`%` 后面不是两位十六进制）
/// 按字面量 `%` 处理，而不是报错 —— 文件名里出现孤立的 `%` 完全合法。
///
/// 解码是按**字节**做、最后整体按 UTF-8 解释的，所以中文这种多字节字符
/// （`%E6%8A%A5` 三个字节拼成一个「报」）能正确还原。
fn percent_decode(input: &str) -> String {
    let bytes = input.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' && index + 2 < bytes.len() {
            let high = (bytes[index + 1] as char).to_digit(16);
            let low = (bytes[index + 2] as char).to_digit(16);
            if let (Some(high), Some(low)) = (high, low) {
                out.push((high * 16 + low) as u8);
                index += 3;
                continue;
            }
        }
        out.push(bytes[index]);
        index += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// 把界面给的名字洗成一个**只能落在目标目录里**的安全文件名。
///
/// 这一步是防路径穿越的唯一一道闸：结果文件的名字最终来自后端响应的
/// `Content-Disposition`，而那是外部输入。`../../某处/evil.exe` 这种名字
/// 必须在这里被削成 `evil.exe`，不能靠调用方自觉。
fn sanitize_filename(raw: &str) -> String {
    // 1. 只取最后一段。正反斜杠都要切 —— Windows 上两种都当分隔符。
    let base = raw.rsplit(['/', '\\']).next().unwrap_or("");

    // 2. 去掉非法字符与控制字符（含换行 —— 头里混进换行是头注入）。
    let cleaned: String = base
        .chars()
        .filter(|c| !c.is_control() && !ILLEGAL_CHARS.contains(c))
        .collect();

    // 3. 去掉首尾空白；末尾的点与空格 Windows 会静默吃掉，留着会造成
    //    「写进去的名字」和「盘上的名字」对不上。
    let trimmed = cleaned.trim().trim_end_matches(['.', ' ']);
    if trimmed.is_empty() {
        return FALLBACK_NAME.to_string();
    }

    // 4. 躲开设备名。判据是第一个点之前的主干，大小写无关。
    let stem_end = trimmed.find('.').unwrap_or(trimmed.len());
    let stem = trimmed[..stem_end].to_ascii_uppercase();
    let guarded = if RESERVED_NAMES.contains(&stem.as_str()) {
        format!("_{trimmed}")
    } else {
        trimmed.to_string()
    };

    // 5. 截长度，保住扩展名。
    truncate_keeping_extension(&guarded, MAX_NAME_LEN)
}

/// 把文件名截到 `max` 个字符以内，**扩展名整段保留**。
///
/// 按字符（不是字节）算长度，否则中文名会被砍得只剩半个字。
fn truncate_keeping_extension(name: &str, max: usize) -> String {
    if name.chars().count() <= max {
        return name.to_string();
    }

    // 扩展名：最后一个点之后的部分，且不能是「.开头的隐藏文件」那种。
    let (stem, extension) = match name.rfind('.') {
        Some(dot) if dot > 0 => (&name[..dot], &name[dot..]),
        _ => (name, ""),
    };

    let extension_len = extension.chars().count();
    // 扩展名自己就超长（或者干脆没有余地了）：那就只能硬截主干，扩展名放弃。
    if extension_len + 1 >= max {
        return stem.chars().take(max).collect();
    }

    let room = max - extension_len;
    let kept: String = stem.chars().take(room).collect();
    format!("{kept}{extension}")
}

/// 在 `dir` 里给 `filename` 找一个还没被占用的路径。
///
/// 重名时退到 `名字 (2).扩展名`、`名字 (3).扩展名`……**不覆盖已有文件** ——
/// 用户的下载目录里很可能已经有上一次的同名结果。
fn unique_path(dir: &Path, filename: &str) -> PathBuf {
    let first = dir.join(filename);
    if !first.exists() {
        return first;
    }

    let (stem, extension) = match filename.rfind('.') {
        Some(dot) if dot > 0 => (&filename[..dot], &filename[dot..]),
        _ => (filename, ""),
    };

    for suffix in 2..=9999u32 {
        let candidate = dir.join(format!("{stem} ({suffix}){extension}"));
        if !candidate.exists() {
            return candidate;
        }
    }

    // 9999 个同名文件还没排上：交给调用方去报错，不要在这里瞎编名字。
    dir.join(format!("{stem} (9999){extension}"))
}

/// 取请求头里的文件名并洗成安全名字。
fn filename_from_request(request: &Request<'_>) -> String {
    request
        .headers()
        .get(FILENAME_HEADER)
        .and_then(|value| value.to_str().ok())
        .map(percent_decode)
        .map(|decoded| sanitize_filename(&decoded))
        .unwrap_or_else(|| FALLBACK_NAME.to_string())
}

/// 把界面传来的结果字节写进「下载」目录，返回落盘的绝对路径。
///
/// 载荷是**原始字节**（`InvokeBody::Raw`），不走 JSON、不做 base64。
/// 这条路径只接受原始字节：传 JSON 进来说明前端写错了，直接报错而不是
/// 猜一个空文件写下去 —— 静默写出 0 字节的「结果」是最坏的结果。
#[tauri::command]
pub fn save_result(app: tauri::AppHandle, request: Request<'_>) -> Result<String, String> {
    let bytes = match request.body() {
        InvokeBody::Raw(bytes) => bytes,
        _ => return Err("保存失败：结果内容的传输格式不对".to_string()),
    };

    let filename = filename_from_request(&request);

    let dir = app
        .path()
        .download_dir()
        .map_err(|_| "保存失败：找不到系统的「下载」目录".to_string())?;
    std::fs::create_dir_all(&dir).map_err(|_| "保存失败：无法创建「下载」目录".to_string())?;

    let target = unique_path(&dir, &filename);
    std::fs::write(&target, bytes).map_err(|_| "保存失败：无法写入文件".to_string())?;

    Ok(target.to_string_lossy().into_owned())
}

/// 校验一个来自界面的路径确实是个已存在的文件。
///
/// `open_result` / `reveal_result` 收到的是前端回传的路径。虽然前端是自己人，
/// 但这两个命令做的事情是**拉起外部程序**，所以只接受「确实存在的普通文件」，
/// 传目录、传不存在的路径一律拒绝。
fn ensure_existing_file(path: &str) -> Result<PathBuf, String> {
    let candidate = PathBuf::from(path);
    match std::fs::metadata(&candidate) {
        Ok(meta) if meta.is_file() => Ok(candidate),
        _ => Err("这个文件已经不在原来的位置了".to_string()),
    }
}

/// 用系统默认程序打开结果文件。
#[tauri::command]
pub fn open_result(path: String) -> Result<(), String> {
    let file = ensure_existing_file(&path)?;
    Command::new("explorer.exe")
        .arg(&file)
        .spawn()
        .map_err(|_| "打开失败：无法启动资源管理器".to_string())?;
    Ok(())
}

/// explorer 的「打开这个文件夹并选中这个文件」参数。
///
/// 形态**必须**是 `/select,"<完整路径>"`：开关和路径之间不能有空格，路径自己
/// 带引号，整串作为一个参数原样送出（见 [`reveal_result`] 里的 `raw_arg`）。
///
/// 这三种写法里只有最后一种是对的，前两种都实测过：
///
/// * `.arg("/select,{path}")` 且路径含空格 → **选不中**（Rust 会把整串加引号，
///   变成 `"/select,C:\a b\c.pdf"`，explorer 解析不了这个形态）；
/// * 拆成 `/select,` 和路径两个参数 → explorer 不认；
/// * `raw_arg("/select,\"…\"")` → 选中。
///
/// 路径里出现 `"` 是不可能的（Windows 文件名本来就不允许），所以这里不需要转义。
fn select_argument(path: &Path) -> String {
    format!("/select,\"{}\"", path.display())
}

/// 在资源管理器里打开结果文件所在的文件夹，并选中这个文件。
#[tauri::command]
pub fn reveal_result(path: String) -> Result<(), String> {
    let file = ensure_existing_file(&path)?;

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // raw_arg：整串原样拼进命令行，**不要**让 Command 去加它自己那套引号
        Command::new("explorer.exe")
            .raw_arg(select_argument(&file))
            .spawn()
            .map_err(|_| "打开文件夹失败：无法启动资源管理器".to_string())?;
        Ok(())
    }

    #[cfg(not(windows))]
    {
        let _ = file;
        Err("打开文件夹只在 Windows 桌面端提供".to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn percent_decode_restores_chinese_names() {
        // 「报告.pdf」的 encodeURIComponent 结果
        assert_eq!(
            percent_decode("%E6%8A%A5%E5%91%8A.pdf"),
            "报告.pdf",
            "多字节字符要按字节解码后整体按 UTF-8 解释"
        );
    }

    #[test]
    fn percent_decode_keeps_plain_text_and_lone_percent() {
        assert_eq!(percent_decode("plain.pdf"), "plain.pdf");
        // 孤立的 % 是合法文件名字符，不能吞掉也不能报错
        assert_eq!(percent_decode("100%25.pdf"), "100%.pdf");
        assert_eq!(percent_decode("50%.pdf"), "50%.pdf");
        // % 后面不是两位十六进制 → 按字面量
        assert_eq!(percent_decode("a%zz.pdf"), "a%zz.pdf");
        // 结尾的半个转义
        assert_eq!(percent_decode("a%2"), "a%2");
    }

    #[test]
    fn sanitize_strips_directory_traversal() {
        // 这是这道闸存在的唯一理由：外部输入的名字不能跑出目标目录
        assert_eq!(sanitize_filename("../../evil.exe"), "evil.exe");
        assert_eq!(sanitize_filename("..\\..\\evil.exe"), "evil.exe");
        assert_eq!(sanitize_filename(r"C:\Windows\System32\evil.exe"), "evil.exe");
        assert_eq!(sanitize_filename("/etc/passwd"), "passwd");
        assert_eq!(sanitize_filename("a/b/c.pdf"), "c.pdf");
    }

    #[test]
    fn sanitize_drops_illegal_chars_and_control_chars() {
        assert_eq!(sanitize_filename("a<b>c:d\"e|f?g*h.pdf"), "abcdefgh.pdf");
        // 换行混进来是头注入的形状，必须被清掉
        assert_eq!(sanitize_filename("a\nb\r.pdf"), "ab.pdf");
    }

    #[test]
    fn sanitize_trims_trailing_dots_and_spaces() {
        // Windows 会静默吃掉末尾的点和空格，留着会让「写的名字」≠「盘上的名字」
        assert_eq!(sanitize_filename("report..."), "report");
        assert_eq!(sanitize_filename("report   "), "report");
        assert_eq!(sanitize_filename("  report.pdf  "), "report.pdf");
    }

    #[test]
    fn sanitize_escapes_reserved_device_names() {
        assert_eq!(sanitize_filename("CON"), "_CON");
        assert_eq!(sanitize_filename("nul.txt"), "_nul.txt");
        assert_eq!(sanitize_filename("COM1.pdf"), "_COM1.pdf");
        // 只是恰好以这些字母开头的不算
        assert_eq!(sanitize_filename("console.pdf"), "console.pdf");
    }

    #[test]
    fn sanitize_falls_back_when_nothing_survives() {
        assert_eq!(sanitize_filename(""), FALLBACK_NAME);
        assert_eq!(sanitize_filename("..."), FALLBACK_NAME);
        assert_eq!(sanitize_filename("///"), FALLBACK_NAME);
        assert_eq!(sanitize_filename("   "), FALLBACK_NAME);
    }

    #[test]
    fn sanitize_keeps_extension_when_truncating() {
        let long = format!("{}.pdf", "字".repeat(300));
        let result = sanitize_filename(&long);
        assert!(
            result.chars().count() <= MAX_NAME_LEN,
            "截断后仍然超长：{}",
            result.chars().count()
        );
        assert!(result.ends_with(".pdf"), "扩展名被截掉了：{result}");
        // 按字符截，不是按字节 —— 否则中文名会被砍出半个字
        assert!(!result.contains('\u{FFFD}'), "截出了坏字符：{result}");
    }

    #[test]
    fn sanitize_handles_name_that_is_all_extension() {
        // 没有主干、扩展名超长：只能硬截，不能 panic
        let result = sanitize_filename(&format!(".{}", "x".repeat(300)));
        assert!(result.chars().count() <= MAX_NAME_LEN);
    }

    #[test]
    fn unique_path_does_not_overwrite_existing_files() {
        let dir = std::env::temp_dir().join("filetools-unique-path-test");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("建临时目录");

        // 第一个名字没被占用 → 原样使用
        assert_eq!(unique_path(&dir, "a.pdf"), dir.join("a.pdf"));

        std::fs::write(dir.join("a.pdf"), b"first").expect("写第一个");
        assert_eq!(unique_path(&dir, "a.pdf"), dir.join("a (2).pdf"));

        std::fs::write(dir.join("a (2).pdf"), b"second").expect("写第二个");
        assert_eq!(unique_path(&dir, "a.pdf"), dir.join("a (3).pdf"));

        // 没有扩展名的一样要能去重
        std::fs::write(dir.join("plain"), b"x").expect("写无扩展名");
        assert_eq!(unique_path(&dir, "plain"), dir.join("plain (2)"));

        // 确认前两个文件的内容一个都没被动过
        assert_eq!(std::fs::read(dir.join("a.pdf")).unwrap(), b"first");
        assert_eq!(std::fs::read(dir.join("a (2).pdf")).unwrap(), b"second");

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn ensure_existing_file_rejects_directories_and_missing_paths() {
        assert!(ensure_existing_file("Z:\\definitely\\not\\here.pdf").is_err());

        let dir = std::env::temp_dir();
        assert!(
            ensure_existing_file(&dir.to_string_lossy()).is_err(),
            "目录不是文件，必须拒绝"
        );
    }

    #[test]
    fn select_argument_quotes_the_path_but_not_the_switch() {
        // 这一条钉的是「实测修好过」的那个 bug：开关与路径之间不能有空格，
        // 路径必须自己带引号，否则含空格的路径在资源管理器里选不中。
        assert_eq!(
            select_argument(Path::new(r"C:\Users\me\Downloads\a.pdf")),
            r#"/select,"C:\Users\me\Downloads\a.pdf""#
        );
        assert_eq!(
            select_argument(Path::new(r"C:\Users\me\My Downloads\a b c.pdf")),
            r#"/select,"C:\Users\me\My Downloads\a b c.pdf""#
        );
        // 整串里除了包裹路径的那两个引号，不能再有别的引号
        let argument = select_argument(Path::new(r"C:\x\y.pdf"));
        assert_eq!(argument.matches('"').count(), 2);
        assert!(argument.starts_with("/select,\""), "开关后必须紧跟引号");
    }

    #[test]
    fn full_pipeline_from_header_value_to_safe_name() {
        // 前端 encodeURIComponent 之后的样子：穿越 + 中文 + 非法字符一起上
        let raw = "%2E%2E%2F%2E%2E%2F%E6%8A%A5%E5%91%8A%3C1%3E.pdf";
        assert_eq!(sanitize_filename(&percent_decode(raw)), "报告1.pdf");
    }
}
