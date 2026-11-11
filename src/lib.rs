use zed_extension_api::{self as zed, Architecture, Os, Result};

struct LatexRendering;

fn node_environment() -> Vec<(String, String)> {
    // Share Zed's Node runtime instead of shipping another copy for PDF export.
    // A failed Node setup must not prevent TeX/Markdown previews or HTML export.
    zed::node_binary_path()
        .map(|node| vec![("PLAYWRIGHT_NODEJS_PATH".into(), node)])
        .unwrap_or_default()
}

impl zed::Extension for LatexRendering {
    fn new() -> Self {
        Self
    }

    fn language_server_command(
        &mut self,
        server: &zed::LanguageServerId,
        worktree: &zed::Worktree,
    ) -> Result<zed::Command> {
        let user_binary = zed::settings::LspSettings::for_worktree("latex-rendering", worktree)
            .ok()
            .and_then(|settings| settings.binary);
        if let Some(zed::settings::CommandSettings {
            path: Some(command),
            arguments,
            ..
        }) = user_binary
        {
            let args = arguments.unwrap_or_else(|| vec!["serve".into()]);
            let env = node_environment();
            return Ok(zed::Command { command, args, env });
        }
        let (os, architecture) = zed::current_platform();
        let os_name = match os {
            Os::Windows => "windows",
            Os::Mac => "macos",
            Os::Linux => "linux",
        };
        let architecture_name = match architecture {
            Architecture::X8664 => "x86_64",
            Architecture::Aarch64 => "aarch64",
            _ => return Err("ZedTeX requires a 64-bit platform".into()),
        };
        let directory = "backend-v0.2.0";
        let executable = format!(
            "{directory}/latex-rendering{}",
            if os == Os::Windows { ".exe" } else { "" }
        );
        if !std::path::Path::new(&executable).is_file() {
            zed::set_language_server_installation_status(
                server,
                &zed::LanguageServerInstallationStatus::Downloading,
            );
            let asset = format!("latex-rendering-{os_name}-{architecture_name}.zip");
            let release = zed::github_release_by_tag_name("maxmalkin/zedtex", directory)?;
            let url = release
                .assets
                .into_iter()
                .find(|item| item.name == asset)
                .ok_or_else(|| {
                    format!("No renderer release is available for {os_name}/{architecture_name}")
                })?
                .download_url;
            zed::download_file(&url, directory, zed::DownloadedFileType::Zip)?;
            zed::make_file_executable(&executable)?;
            if os != Os::Windows {
                zed::make_file_executable(&format!("{directory}/tectonic"))?;
            }
        }
        let path = std::env::current_dir()
            .map_err(|error| error.to_string())?
            .join(executable);
        Ok(zed::Command {
            command: path.to_string_lossy().into_owned(),
            args: vec!["serve".into()],
            env: node_environment(),
        })
    }
}

zed::register_extension!(LatexRendering);
