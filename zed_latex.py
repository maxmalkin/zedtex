"""TeX outside the editor; ordinary image files and Jupyter MIME inside it."""

import argparse
import base64
from contextlib import closing
from functools import cache
import hashlib
import io
import json
import mimetypes
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import mistune
from mistune.renderers.markdown import MarkdownRenderer
import pypdfium2 as pdfium


DEFAULT_PREAMBLE = "\\usepackage{amsmath,amssymb}\n\\boldmath\n"
MATH_CACHE_BYTES = 64 * 1024 * 1024
MATH_CACHE_ENTRIES = 4096


def cache_directory():
    if sys.platform == "win32":
        root = Path(os.environ["LOCALAPPDATA"]) / "Cache"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Caches"
    else:
        root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return root.resolve() / "zedtex"


def source_key(source):
    return hashlib.sha256(os.fsencode(Path(source).resolve())).hexdigest()


def write_changed(path, data):
    path = Path(path)
    data = data.encode("utf-8") if isinstance(data, str) else data
    try:
        if path.read_bytes() == data:
            return
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    # Preview and export processes share the math cache. Never share a temp name.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        for attempt in range(3):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                # Windows may briefly lock the destination during a competing
                # replace or reader. Retry that race without hiding real errors.
                if attempt == 2:
                    raise
                time.sleep(0.01)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def compiler():
    bundled = Path(sys.executable).with_name("tectonic.exe" if sys.platform == "win32" else "tectonic")
    if bundled.is_file():
        return str(bundled)
    configured = os.environ.get("ZED_LATEX_TECTONIC")
    if configured:
        return configured
    found = shutil.which("tectonic")
    if found:
        return found
    if sys.platform == "win32":
        programs = Path(os.environ["LOCALAPPDATA"]) / "Programs"
        for folder in ("Zed-LaTeX-Tools", "Zed-LaTeX"):
            candidate = programs / folder / "tectonic.exe"
            if candidate.is_file():
                return str(candidate)
    raise RuntimeError("Install Tectonic or set ZED_LATEX_TECTONIC to its executable.")


def compile_tex(source, directory, output, *, document=None):
    output.mkdir(parents=True, exist_ok=True)
    args = [compiler(), "--untrusted", "--keep-intermediates", "--outdir", str(output),
            str(document) if document else "-"]
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(
            args, cwd=directory, stdin=subprocess.PIPE if source is not None else subprocess.DEVNULL,
            stdout=log, stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        try:
            # Sources are capped below, so piping stdin cannot fill the log pipe.
            if source is not None:
                process.stdin.write(source.encode("utf-8"))
                process.stdin.close()
            deadline = time.monotonic() + 180
            while process.poll() is None:
                if time.monotonic() > deadline or os.fstat(log.fileno()).st_size > 8 * 1024 * 1024:
                    raise RuntimeError("TeX exceeded its 180-second or 8-MB log limit.")
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
            if process.returncode:
                log.seek(max(0, os.fstat(log.fileno()).st_size - 8000))
                raise RuntimeError(log.read().decode("utf-8", errors="replace"))
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
    pdf = output / (document.stem + ".pdf" if document else "texput.pdf")
    if pdf.stat().st_size > 100 * 1024 * 1024:
        raise RuntimeError("PDF exceeds 100 MB.")
    return pdf


def page_png(pdf, index, scale, pixel_limit=16_000_000):
    with closing(pdf[index]) as page:
        width, height = page.get_size()
        if width * height * scale * scale > pixel_limit:
            raise RuntimeError(f"Page raster exceeds {pixel_limit} pixels.")
        bitmap = page.render(scale=scale)
        try:
            with bitmap.to_pil() as image:
                output = io.BytesIO()
                image.save(output, format="PNG")
                return output.getvalue(), image.width, image.height
        finally:
            bitmap.close()


def page_fingerprint(pdf, index):
    with pdfium.PdfDocument.new() as single:
        single.import_pages(pdf, [index])
        output = io.BytesIO()
        single.save(output)
        data = output.getvalue()
    # PDFium generates a fresh /ID in its trailer on every save. Its serialized
    # page objects already include content, fonts, images and other resources.
    objects, separator, _ = data.rpartition(b"trailer\r\n")
    # The fresh wrapper also has an Info dictionary with the current time.
    # Normalize only PDFium's generated metadata, never page content/resources.
    objects = re.sub(rb"(?m)^3 0 obj\r\n<</CreationDate\([^)]*\)/Creator\(PDFium\)/Producer\(PDFium\)>>\r\nendobj",
                     b"3 0 obj\r\n<<>>\r\nendobj", objects) if separator else data
    return hashlib.sha256(objects).hexdigest()


def input_stamp(directory):
    # ponytail: watch TeX inputs under this directory; use latexmk for dependencies outside it.
    values = []
    for root, directories, files in os.walk(directory):
        directories[:] = [name for name in directories if not name.startswith(".")
                           and not name.endswith(".zed-output")
                           and name not in ("node_modules", "target", "venv")]
        for name in sorted(files):
            path = Path(root) / name
            if path.suffix.lower() in (".tex", ".sty", ".cls", ".bib", ".png", ".jpg", ".pdf"):
                stat = path.stat()
                values.append((str(path.relative_to(directory)), stat.st_mtime_ns, stat.st_size))
    return sorted(values)


class Renderer:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        preamble = self.directory / "latex-preamble.tex"
        self.preamble = preamble.read_text(encoding="utf-8") if preamble.exists() else DEFAULT_PREAMBLE
        self.images = {}
        self.cache = cache_directory() / "math" / source_key(self.directory)
        self.signature = None

    def render(self, equations):
        from PIL import Image
        missing = []
        cache_paths = {}
        for equation in dict.fromkeys(equations):
            if equation in self.images:
                continue
            if self.signature is None:
                self.signature = hashlib.sha256(json.dumps(input_stamp(self.directory)).encode()).hexdigest()
            key = json.dumps([equation, self.preamble, self.signature, "raster-v1"])
            path = self.cache / (hashlib.sha256(key.encode()).hexdigest() + ".png")
            cache_paths[equation] = path
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                missing.append(equation)
            else:
                with Image.open(io.BytesIO(data)) as image:
                    self.images[equation] = (data, image.width, image.height)
        for offset in range(0, len(missing), 32):
            batch = missing[offset:offset + 32]
            source = ("\\documentclass[border=2pt,multi=preview]{standalone}\n"
                      + self.preamble + "\n\\begin{document}\n")
            for text, display in batch:
                style = r"\displaystyle " if display else r"\textstyle "
                source += "\\begin{preview}$" + style + text + "$\\end{preview}\n"
            source += "\\end{document}\n"
            if len(source.encode("utf-8")) > 262144:
                raise ValueError("Math batch exceeds 256 KB.")
            with tempfile.TemporaryDirectory(prefix="zed-math-") as temporary:
                path = compile_tex(source, self.directory, Path(temporary))
                with pdfium.PdfDocument(path) as pdf:
                    if len(pdf) != len(batch):
                        raise ValueError("Each equation must produce exactly one page.")
                    for index, equation in enumerate(batch):
                        self.images[equation] = page_png(pdf, index, 2, pixel_limit=1_000_000)
                        write_changed(cache_paths[equation], self.images[equation][0])
        if not missing:
            return
        # Only new files can exceed the budget. Cache hits need no directory scan.
        with os.scandir(self.cache) as entries:
            cached = []
            for entry in entries:
                if entry.name.endswith(".png"):
                    try:
                        cached.append((entry.stat(), Path(entry.path)))
                    except FileNotFoundError:  # Another renderer pruned this entry.
                        pass
        cached.sort(key=lambda item: item[0].st_mtime_ns, reverse=True)
        size = 0
        for index, (stat, path) in enumerate(cached):
            size += stat.st_size
            if size > MATH_CACHE_BYTES or index >= MATH_CACHE_ENTRIES:
                path.unlink(missing_ok=True)


def markdown_parser():
    return mistune.create_markdown(renderer=None, plugins=["math", "table", "strikethrough", "url"])


def math_tokens(tokens):
    for token in tokens:
        if token["type"] in ("inline_math", "block_math"):
            yield token["raw"], token["type"] == "block_math"
        yield from math_tokens(token.get("children", []))


class ImageMarkdown(MarkdownRenderer):
    def __init__(self, render_image):
        super().__init__()
        # Scoped to this conversion; release encoded images when it finishes.
        self.render_image = cache(render_image)
        self.image_labels = {}

    def inline_math(self, token, state):
        return self.math_image(token, state, False)

    def block_math(self, token, state):
        return "\n\n" + self.math_image(token, state, True) + "\n\n"

    def math_image(self, token, state, display):
        return self.image({"type": "image", "attrs": {"url": self.render_image((token["raw"], display))},
                           "children": [{"type": "text", "raw": "equation"}]}, state)

    def image(self, token, state):
        if token["attrs"]["url"].startswith("data:"):
            # Store each embedded image once, even when an equation is repeated.
            attrs = {key: token["attrs"][key] for key in ("url", "title") if token["attrs"].get(key) is not None}
            key = (attrs["url"], attrs.get("title"))
            label = self.image_labels.get(key)
            references = state.env["ref_links"]
            if label is None:
                label = "zedtex-" + hashlib.sha256(json.dumps(attrs, sort_keys=True).encode()).hexdigest()
                while label.upper() in references and any(references[label.upper()].get(key) != attrs.get(key) for key in ("url", "title")):
                    label += "-image"
                references[label.upper()] = {**attrs, "label": label}
                self.image_labels[key] = label
            token = {**token, "label": label}
        return super().image(token, state)

    def strikethrough(self, token, state):
        return "~~" + self.render_children(token, state) + "~~"


def convert_markdown(source, renderer, render_image, *, link_directory=None):
    parser = markdown_parser()
    tokens, state = parser.parse(source)
    renderer.render(math_tokens(tokens))
    if link_directory is not None:
        @cache
        def local_image(path):
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            return image_data_url(path.read_bytes(), mime)

        def fix_url(attrs, *, image=False):
            url = attrs.get("url", "")
            parts = urlsplit(url)
            if url and not parts.scheme and not url.startswith(("/", "#")):
                path = (renderer.directory / unquote(parts.path)).resolve()
                # LSP opens cache previews as individual files. Remote Zed cannot
                # resolve their sibling images, but supports embedded data URLs.
                if image and path.is_file():
                    attrs["url"] = local_image(path)
                    return
                try:
                    rebased = Path(os.path.relpath(path, link_directory)).as_posix()
                except ValueError:  # Windows paths on different drives.
                    rebased = path.as_posix()
                attrs["url"] = urlunsplit(parts._replace(path=quote(rebased, safe="/:")))
        def fix_tokens(tokens):
            for token in tokens:
                fix_url(token.get("attrs", {}), image=token["type"] == "image")
                if token["type"] == "image":
                    token.pop("label", None)  # Use the cached URL even for reference images.
                fix_tokens(token.get("children", []))
        fix_tokens(tokens)
        for attrs in state.env["ref_links"].values():
            fix_url(attrs)
    writer = ImageMarkdown(render_image)
    return writer(tokens, state)


def output_directory(source):
    return cache_directory() / "documents" / source_key(source)


def image_data_url(data, mime="image/png"):
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def preview(source):
    source = Path(source).resolve()
    if source.name.lower() == "latex-preamble.tex":
        raise ValueError("latex-preamble.tex configures packages; it is not a standalone document. "
                         "Preview the Markdown file, export the notebook, or open a complete .tex document beginning with \\documentclass.")
    output = output_directory(source)
    if source.suffix.lower() == ".tex":
        stamp = input_stamp(source.parent)
        saved_stamp = {"preview_format": 2, "inputs": [list(value) for value in stamp]}
        state = output / "inputs.json"
        if state.exists() and json.loads(state.read_text()) == saved_stamp:
            print("Unchanged; reusing PDF and pages.", file=sys.stderr, flush=True)
            return output / "preview.md"
        compiled = compile_tex(None, source.parent, output / "build", document=source)
        pdf_path = output / compiled.name
        write_changed(pdf_path, compiled.read_bytes())
        with pdfium.PdfDocument(pdf_path) as pdf:
            if len(pdf) > 200:
                raise ValueError("Preview is limited to 200 pages; the PDF is still available.")
            links = []
            first_page = ""
            page_state = output / "pages.json"
            old_fingerprints = json.loads(page_state.read_text()) if page_state.exists() else []
            fingerprints = []
            rendered_pages = 0
            for index in range(len(pdf)):
                name = f"page-{index + 1:03}.png"
                fingerprint = page_fingerprint(pdf, index)
                fingerprints.append(fingerprint)
                if index >= len(old_fingerprints) or fingerprint != old_fingerprints[index] or not (output / name).exists():
                    data, _, _ = page_png(pdf, index, 1.5)
                    write_changed(output / name, data)
                    rendered_pages += 1
                links.append(f"[{index + 1}]({name})")
                if not index:
                    first_page = image_data_url((output / name).read_bytes())
            # One visible page avoids loading an entire document into Zed's GPU image cache.
            text = (f"# {source.name}\n\nPages: " + " · ".join(links)
                    + f"\n\nPDF: [{pdf_path.name}]({pdf_path.name})\n\n"
                    + f"![Page 1]({first_page})\n")
        write_changed(output / "preview.md", text)
        write_changed(page_state, json.dumps(fingerprints))
        for old in list(output.glob("page-*.png")) + list(output.glob("cover-*.png")):
            if old.name not in text:
                old.unlink()
        write_changed(state, json.dumps(saved_stamp))
        print(f"Rasterized {rendered_pages}/{len(fingerprints)} pages.", file=sys.stderr, flush=True)
    elif source.suffix.lower() == ".md":
        renderer = Renderer(source.parent)
        def image_link(equation):
            data, _, _ = renderer.images[equation]
            return image_data_url(data)
        text = convert_markdown(source.read_text(encoding="utf-8"), renderer, image_link, link_directory=output)
        text = f"<!-- Generated from {source.name}; edit the source file. -->\n\n" + text
        write_changed(output / "preview.md", text)
        for old in output.glob("*.png"):
            if old.name not in text:
                old.unlink()
        for old in (output / "assets").glob("*"):
            if old.name not in text:
                old.unlink()
    else:
        raise ValueError("Preview expects a .tex or .md file.")
    print(output / "preview.md", file=sys.stderr, flush=True)
    return output / "preview.md"


def latex_content(text):
    text = text.strip()
    for start, end in (("$$", "$$"), ("$", "$"), (r"\[", r"\]"), (r"\(", r"\)")):
        if text.startswith(start) and text.endswith(end):
            return text[len(start):-len(end)].strip()
    return text


def export_notebook(source, kind, *, execute=True):
    import nbformat
    source = Path(source).resolve()
    if source.suffix == ".py":
        import jupytext
        notebook = jupytext.read(source)
        if not execute:
            print("Script export has no saved REPL outputs. Omit --no-execute to run all cells.", file=sys.stderr, flush=True)
    elif source.suffix == ".ipynb":
        notebook = nbformat.read(source, as_version=4)
    else:
        raise ValueError("Export expects .ipynb or a Jupytext # %% .py script.")
    if execute:
        from nbconvert.preprocessors import ExecutePreprocessor
        from jupyter_client.kernelspec import KernelSpecManager
        kernel = notebook.metadata.get("kernelspec", {}).get("name", "python3")
        # A frozen renderer is not a Python kernel. Use the notebook's installed kernel.
        if kernel not in KernelSpecManager(ensure_native_kernel=False).find_kernel_specs():
            raise RuntimeError(f"Jupyter kernel '{kernel}' is not installed. Register the notebook's environment as a Jupyter kernel first.")
        ExecutePreprocessor(timeout=180, kernel_name=kernel).preprocess(
            notebook, {"metadata": {"path": str(source.parent)}})
    renderer = Renderer(source.parent)
    equations = []
    for cell in notebook.cells:
        if cell.cell_type == "markdown":
            tokens, _ = markdown_parser().parse(cell.source)
            equations.extend(math_tokens(tokens))
        for result in cell.get("outputs", []):
            data = result.get("data", {})
            if "text/latex" in data and "image/png" not in data:
                equations.append((latex_content(data["text/latex"]), True))
    renderer.render(equations)
    for cell in notebook.cells:
        if cell.cell_type == "markdown":
            def attachment(equation):
                data, _, _ = renderer.images[equation]
                name = hashlib.sha256(data).hexdigest()[:20] + ".png"
                cell.setdefault("attachments", {})[name] = {"image/png": base64.b64encode(data).decode("ascii")}
                return f"attachment:{name}"
            cell.source = convert_markdown(cell.source, renderer, attachment)
        for result in cell.get("outputs", []):
            data = result.get("data", {})
            if "text/latex" in data:
                if "image/png" not in data:
                    png, width, height = renderer.images[(latex_content(data["text/latex"]), True)]
                    data["image/png"] = base64.b64encode(png).decode("ascii")
                    result.setdefault("metadata", {})["image/png"] = {"width": width // 2, "height": height // 2}
                # Ensure nbconvert chooses the package-aware raster over a MathJax/HTML fallback.
                for mime in ("text/latex", "text/html", "image/svg+xml"):
                    data.pop(mime, None)
    from nbconvert import HTMLExporter
    exporter = HTMLExporter(embed_images=True, mathjax_url="", require_js_url="")
    html, _ = exporter.from_notebook_node(notebook, resources={"metadata": {"path": str(source.parent)}})
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    sizes = {base64.b64encode(data).decode("ascii"): (width // 2, height // 2)
             for data, width, height in renderer.images.values()}
    for image in soup.find_all("img", alt="equation"):
        encoded = image.get("src", "").partition("base64,")[2]
        if encoded in sizes:
            image["width"], image["height"] = map(str, sizes[encoded])
    html = str(soup)
    output = output_directory(source)
    html_path = output / (source.stem + ".html")
    write_changed(html_path, html)
    write_changed(output / (source.stem + ".rendered.ipynb"), nbformat.writes(notebook))
    if kind == "pdf":
        from playwright.sync_api import sync_playwright, Error as BrowserError
        from playwright._impl._driver import get_driver_env
        # Reuse browser downloads across renderer updates, outside the project.
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(cache_directory() / "browsers")
        node, cli = pdf_driver()
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(channel="msedge" if sys.platform == "win32" else "chrome")
            except BrowserError:
                try:
                    browser = playwright.chromium.launch()
                except BrowserError:
                    subprocess.run([node, cli, "install", "chromium", "--only-shell"],
                                   env=get_driver_env(), stdout=sys.stderr, stderr=sys.stderr, check=True, timeout=300)
                    browser = playwright.chromium.launch()
            return export_pdf_with_browser(browser, html_path)
    print(html_path, file=sys.stderr, flush=True)
    return html_path


def pdf_driver():
    from playwright._impl._driver import compute_driver_executable
    node, cli = compute_driver_executable()
    if not os.environ.get("PLAYWRIGHT_NODEJS_PATH") and not Path(node).is_file():
        # Zed supplies this variable. Standalone CLI users can use Node on PATH.
        node = shutil.which("node")
        if node:
            os.environ["PLAYWRIGHT_NODEJS_PATH"] = node
    if not node or not Path(node).is_file():
        raise RuntimeError("PDF export needs Node.js 20 or newer. Reload ZedTeX to use Zed's shared runtime, "
                           "or set PLAYWRIGHT_NODEJS_PATH to a Node executable.")
    return node, cli


def export_pdf_with_browser(browser, html_path):
    try:
        page = browser.new_page(java_script_enabled=False)
        page.goto(html_path.as_uri(), wait_until="load")
        page.emulate_media(media="print")
        pdf = page.pdf(format="A4", print_background=True,
                       margin={"top": "12mm", "bottom": "12mm", "left": "12mm", "right": "12mm"})
    finally:
        browser.close()
    result = html_path.with_suffix(".pdf")
    write_changed(result, pdf)
    print(result, file=sys.stderr, flush=True)
    return result


def tex(source, *, directory=None, display=True):
    """Use in a Jupyter cell: tex(r'\\frac{1}{2}'). Returns a persisted PNG output."""
    from IPython.display import Image
    renderer = Renderer(directory or Path.cwd())
    equation = (latex_content(source), display)
    renderer.render([equation])
    data, width, height = renderer.images[equation]
    return Image(data=data, width=width // 2, height=height // 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    math = commands.add_parser("math")
    math.add_argument("--directory", type=Path, required=True)
    viewer = commands.add_parser("preview")
    viewer.add_argument("source", type=Path)
    viewer.add_argument("--watch", action="store_true")
    exporter = commands.add_parser("export")
    exporter.add_argument("source", type=Path)
    exporter.add_argument("--to", choices=("html", "pdf"), required=True)
    exporter.add_argument("--no-execute", action="store_true", help="Use saved outputs instead of running notebook code")
    args = parser.parse_args()
    try:
        if args.command == "serve":
            from server import server
            server.start_io()
        elif args.command == "math":
            source = sys.stdin.read(262145)
            if len(source) > 262144:
                raise ValueError("Math input exceeds 256 KB.")
            renderer = Renderer(args.directory)
            equation = (latex_content(source), True)
            renderer.render([equation])
            sys.stdout.buffer.write(renderer.images[equation][0])
        elif args.command == "export":
            export_notebook(args.source, args.to, execute=not args.no_execute)
        else:
            source = args.source.resolve()
            if not args.watch:
                preview(source)
                return
            previous = None
            while True:
                stamp = (source.stat().st_mtime_ns, input_stamp(source.parent))
                if stamp != previous:
                    time.sleep(0.25)
                    if stamp != (source.stat().st_mtime_ns, input_stamp(source.parent)):
                        continue
                    try:
                        preview(source)
                    except Exception as error:
                        print(f"Build failed; last successful preview retained: {error}", file=sys.stderr, flush=True)
                    previous = stamp
                time.sleep(0.75)
    except KeyboardInterrupt:
        pass
    except Exception as error:
        import traceback
        traceback.print_exc(file=sys.stderr)
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
