# ZedTeX

A language-server extension for official Zed: real TeX packages, page-image previews, Markdown math, and notebook HTML/PDF export.

**Development status:** native renderer bundles pass actual TeX, Markdown, executed notebook HTML/PDF, and LSP checks on Windows x64, Linux x64, and Intel/Apple Silicon macOS. Releases download automatically through the extension API. Registry publication is pending.

## Install

Once the registry submission is accepted, install **ZedTeX** from Zed's Extensions UI. The extension downloads its matching native renderer on first use, using Zed's extension API. Python and a full TeX distribution will not need to be installed separately. Real TeX packages are fetched and cached by Tectonic as needed.

The extension adds a supplementary language server for LaTeX, Markdown, Python and notebook JSON. It recognizes `.ipynb` as **Jupyter Notebook**, reusing Zed's built-in JSON grammar. Files explicitly set to **JSON** also work. `.tex` recognition comes from the existing **LaTeX** language extension, also installed through Zed’s Extensions UI. It keeps existing language servers in place. It does not replace Zed's native renderers or add a PDF viewer.

## Preview

For a saved `.tex` or `.md` file, press **Ctrl+Shift+P**, run **editor: toggle code actions**, then choose **LaTeX: build and open preview**. Use **Cmd** instead of **Ctrl** on macOS. Generated files stay outside the project, in ZedTeX's user cache (`%LOCALAPPDATA%/Cache/zedtex` on Windows, `~/Library/Caches/zedtex` on macOS, `$XDG_CACHE_HOME/zedtex` or `~/.cache/zedtex` on Linux). Each source has its own directory, so matching filenames in different projects do not collide.

- TeX: a real PDF plus numbered PNG pages. The first page opens in Zed's existing image viewer. Split it beside the source; open other page images from the file tree.
- Markdown: a generated `preview.md` with math replaced by normal image links. Open it with Ctrl+Shift+V. Edit the original source file.
- Save the original to rebuild an activated preview. A code action stops rebuilding.

Image previews do not provide PDF text selection, SyncTeX, or native multi-page controls. The real PDF remains available separately. Generated copies are disposable; source documents are preserved.

Preview images are embedded as data URLs. Zed opens generated files outside the project as individual worktrees, so relative sibling image paths can fail over WSL/SSH. Shared Markdown references keep repeated images from duplicating their encoded payload. Relative source PNG/SVG images are embedded too; ordinary links still point back to the source directory. Equation compilation remains incremental.

## Packages

TeX documents use their own preambles. For Markdown and notebook equations, put `latex-preamble.tex` beside the source:

```tex
\usepackage{amsmath,amssymb,mathtools}
\usepackage{localmath}
\boldmath
```

Local `.sty` files resolve from that directory. This replaces the default amsmath/amssymb and boldmath preamble. Tectonic uses XeTeX; external shell commands and packages requiring other engines are unsupported.

`latex-preamble.tex` is not a standalone TeX document and has no build-preview action. The CLI rejects it with an explanation before starting TeX. Saving it still rebuilds active previews that depend on it.

## Jupyter and exports

In a Python document, invoke **LaTeX: insert Jupyter rendering helper**, then run the inserted cell with your existing Jupyter kernel. Use `tex(r"x^2")` in subsequent cells. The helper calls the downloaded renderer and returns a standard PNG output; no package installation into your kernel is needed. The kernel must run on the same machine as the rendering server.

For a saved `.ipynb`, use **Notebook: run all cells and export to HTML/PDF**. ZedTeX opens notebooks as JSON source with syntax highlighting and export actions. Markdown equations and `text/latex` outputs are rendered with actual TeX packages before export. All cells run in order in the notebook’s installed Jupyter kernel, then fresh outputs are exported. The source notebook stays unchanged; the executed copy is saved beside the export. Cell failures stop export and preserve the previous successful export. Native notebook cells are not extension render hooks; arbitrary live kernel LaTeX and Markdown cells cannot be intercepted automatically.

Python `# %%` scripts also execute before export. Notebook dependencies must be installed in the selected Jupyter kernel; the renderer does not supply your computation environment. The developer CLI supports `--no-execute` to export saved outputs. HTML embeds rendered equations; PDF is generated from the same static HTML. Interactive widgets are not reproduced.

PDF export reuses Zed's shared Node runtime rather than downloading another copy inside ZedTeX. Chrome/Edge is used when available; the fallback browser downloads automatically into ZedTeX's user cache and is reused across renderer updates. No manual Node installation is needed when Zed manages its runtime normally. Standalone CLI PDF exports need Node.js 20+ on `PATH`, or an explicit executable path in `PLAYWRIGHT_NODEJS_PATH`. TeX/Markdown previews and HTML exports do not require Node.

## Resource use

Renderer 0.1.4 downloads are approximately 57 MiB on Windows x64, 69 MiB on Linux x64, 63 MiB on Apple Silicon, and 65 MiB on Intel macOS. The bundles exclude the duplicate Node executable and unused Jedi completion, ipykernel, and Tk components. Release ZIPs use maximum standard DEFLATE compression.

These sizes exclude Zed's shared Node runtime, TeX package caches, and any fallback browser download. Zed can download Node if it is not already available. Notebook code still runs in the user's registered kernel; removing ipykernel from the renderer does not remove notebook execution support.

Math compiles only missing equations in batches and reuses a bounded disk cache. Cache hits read each PNG once; eviction runs only after new images are written. Repeated equations and local images are encoded once per Markdown conversion. Documents without equations skip TeX dependency scanning. Unchanged TeX inputs skip compilation. After a TeX rebuild, unchanged page objects reuse their PNGs; shared font or resource changes conservatively invalidate affected pages. Rasterization processes one page at a time. Limits: three-minute compilation, 8-MB compiler log, 100-MB PDF, 200 preview pages, 16 million raster pixels per document page, one million pixels per equation, and 64 MiB / 4,096 entries in each source directory's math cache.

Notebook exports run one at a time in a separate process. They cannot block the preview queue, their notebook/browser memory is released when the process exits, and PDFium stays isolated from the preview process. Shared cache writes use unique temporary files and atomic replacement, with a bounded retry for Windows file-lock races. One-shot CLI previews skip the watcher's debounce and duplicate dependency scans.

Measured on Windows with Python 3.13 and a warm TeX package cache (three-run medians for unchanged previews; timings exclude editor UI and process startup):

| Workload | Before | After |
| --- | ---: | ---: |
| 256 unchanged equations | 122 ms | 66 ms |
| 600 unchanged equations | 2,173 ms | 148 ms |
| 200 Markdown notebook cells, populated math cache | 7,909 ms | 346 ms |
| Same 3 MiB image used 12 times | 313 ms | 42 ms |
| Peak renderer RSS for that repeated image | 97 MiB | 61 MiB |

The notebook comparison uses saved outputs to isolate rendering cost; normal exports still execute every cell. Fresh kernel startup and browser PDF generation remain significant costs. A controlled two-second export no longer makes a zero-work preview wait two seconds. These measurements are workload-specific, not guarantees.

Dependency invalidation remains conservative: TeX/resource changes beneath the source directory can invalidate math even when unrelated, and working sets beyond the cache budget can require recompilation. Generated document exports are not automatically evicted. Zed owns the displayed image/GPU cache; this extension cannot control its lifetime. Older renderer versions stay installed because previously inserted Jupyter helpers may still refer to them.

Save a copy of exported files you want to keep; cache files are disposable. Older versions created `*.zed-output/` and `.zed-latex-cache/` beside the source; these can be removed after saving any exports you want to keep. Official Zed updates independently. This extension does not remove multiplayer features from the editor.

## Development

To use **Install Dev Extension**, install [Rust through rustup](https://rust-lang.org/tools/install/) on the same OS as Zed. Windows-native Zed needs Windows Rust; a WSL Rust installation does not supply its compiler. Restart Zed after installing Rust so it sees the updated PATH. Zed installs the `wasm32-wasip2` target automatically. Windows builds also need the Visual C++ build tools required by Rust.

Then select the local clone from Zed’s **Install Dev Extension** dialog. If installation fails, use **zed: open log** to see the underlying compiler error. Registry installations download prebuilt extension code and do not require Rust.

To update a dev install, pull the repository and select it again with **Install Dev Extension**. Run the preview action again to refresh older generated files.

Run `python -m unittest -v test_regressions` for the 50 fast regressions covering embedded PNG/SVG transport, reference deduplication, escaped paths, Markdown preservation, source cleanliness, cache limits and reuse, concurrent writes, preview/export isolation, shared Node selection, notebook recognition, code actions, save coalescing, and opening errors. These run on Linux, Windows, and macOS for pushes and pull requests.

`python check.py` exercises real local TeX packages, changed-page/equation reuse, unchanged-file timestamps, failure retention, and executed notebook HTML/PDF exports. It needs Tectonic and a registered Python Jupyter kernel. Every renderer release runs both suites plus `python check_bundle.py`, which tests the frozen executable, LSP previews, and subprocess notebook exports through LSP, on all four supported platform targets. Bundle checks verify PDF export with both shared-runtime and standalone Node paths, and reject accidentally bundled Node, Jedi, ipykernel, or Tk components. The workflow publishes only after every target passes.

`cargo build --release --target wasm32-wasip2` builds the Zed extension. Native server bundles are built separately for each supported OS/architecture, then downloaded by the extension; they are not bundled into the extension registry archive.

The Python renderer, regression checks, and `package_bundle.py` are maintained here so releases can be reproduced and tested. Zed packages only the compiled extension, manifest, language files, and snippets; development scripts and documentation assets are not installed with it.

## Troubleshooting

If the action menu does not appear after installing or reloading, click another line in the original source file. If needed, run **editor: restart language server**, wait for startup, then move the cursor again. `.ipynb` files should show **Jupyter Notebook** or **JSON** in the status bar; reload the extension if they show **Unknown**.

For a shorter palette search, merge this alias into your Zed settings:

```json
"command_aliases": {
  "zedtex": "editor: toggle code actions"
}
```

Search for `zedtex` and press Enter. The result is still named **editor: toggle code actions**; the LaTeX/Notebook menu appears afterward. Zed's extension API does not expose standalone palette commands.

The source is MIT licensed; see [LICENSE](LICENSE). Third-party components retain their licenses. Built from the lessons of the [Zed LaTeX fork](https://github.com/maxmalkin/zed), using Tectonic, pypdfium2, Jupyter nbconvert, Mistune, and Playwright.
