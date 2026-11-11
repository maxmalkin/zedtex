"""Fast regressions: python -m unittest -v test_regressions test_highlight.

Image transport and LSP behavior use a tiny PNG; check.py and check_bundle.py
exercise the real compiler, rasterizer, kernels, and frozen release binaries.
"""
import base64
import asyncio
import builtins
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from threading import Barrier, Event
import tomllib
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import quote

from lsprotocol import types as lsp
from pygls.capabilities import ServerCapabilitiesBuilder
from pygls.workspace import Workspace
from PIL import Image

import highlight
import server
import zed_latex as renderer


def tokens_of_kind(tokens, kind):
    for token in tokens:
        if token["type"] == kind:
            yield token
        yield from tokens_of_kind(token.get("children", []), kind)


class PreviewRegressions(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="zedtex-regression-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        self.source = root / "source space δ"
        self.source.mkdir()
        self.cache = root / "cache"
        self.addCleanup(patch.stopall)
        patch.object(renderer, "cache_directory", return_value=self.cache).start()
        data = io.BytesIO()
        with Image.new("RGB", (4, 4), "black") as image:
            image.save(data, format="PNG")
        self.png = data.getvalue()
        self.equations = []
        self.math = Mock(directory=self.source, images={})

        def render(equations):
            for equation in equations:
                self.equations.append(equation)
                self.math.images[equation] = (self.png, 4, 4)

        self.math.render.side_effect = render
        patch.object(renderer, "Renderer", return_value=self.math).start()

    def preview(self, text):
        source = self.source / "notes.md"
        source.write_text(text, encoding="utf-8")
        result = renderer.preview(source)
        rendered = result.read_text(encoding="utf-8")
        tokens, _ = renderer.markdown_parser().parse(rendered)
        return result, rendered, tokens

    def test_equations_are_embedded_valid_pngs(self):
        _, _, tokens = self.preview("Inline $x$.\n\n$$\nx\n$$\n")
        images = list(tokens_of_kind(tokens, "image"))
        self.assertEqual(len(images), 2)
        for image in images:
            url = image["attrs"]["url"]
            self.assertTrue(url.startswith("data:image/png;base64,"))
            self.assertEqual(base64.b64decode(url.split(",", 1)[1], validate=True), self.png)
        self.assertEqual(self.equations, [("x", False), ("x", True)])

    def test_repeated_equations_share_one_reference(self):
        with patch.object(renderer, "image_data_url", wraps=renderer.image_data_url) as encode:
            _, text, tokens = self.preview("$x$ then $x$ then $x$")
        self.assertEqual(encode.call_count, 1)
        self.assertEqual(len(list(tokens_of_kind(tokens, "image"))), 3)
        self.assertEqual(text.count("data:image/png;base64,"), 1)

    def test_preview_can_be_moved_without_sibling_images(self):
        result, text, _ = self.preview("$x$")
        moved = Path(self.temporary.name) / "isolated.md"
        moved.write_bytes(result.read_bytes())
        tokens, _ = renderer.markdown_parser().parse(moved.read_text())
        self.assertTrue(next(tokens_of_kind(tokens, "image"))["attrs"]["url"].startswith("data:"))
        self.assertEqual(list(result.parent.iterdir()), [result])
        self.assertNotIn(".png)", text)

    def test_local_and_reference_images_embed_once(self):
        (self.source / "local image.png").write_bytes(self.png)
        with patch.object(renderer, "image_data_url", wraps=renderer.image_data_url) as encode:
            _, text, tokens = self.preview("![direct](local%20image.png)\n\n![ref][pic]\n\n[pic]: local%20image.png")
        self.assertEqual(encode.call_count, 1)
        images = list(tokens_of_kind(tokens, "image"))
        self.assertEqual(len(images), 2)
        self.assertEqual(images[0]["attrs"]["url"], images[1]["attrs"]["url"])
        self.assertEqual(text.count("data:image/png;base64,"), 1)

    def test_local_svg_is_embedded_with_svg_mime(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"><path d="M0 0h4v4H0z"/></svg>'
        (self.source / "diagram.svg").write_bytes(svg)
        _, _, tokens = self.preview("![diagram](diagram.svg)")
        url = next(tokens_of_kind(tokens, "image"))["attrs"]["url"]
        self.assertTrue(url.startswith("data:image/svg+xml;base64,"))
        self.assertEqual(base64.b64decode(url.split(",", 1)[1], validate=True), svg)

    def test_encoded_image_path(self):
        name = "plot # δ.png"
        (self.source / name).write_bytes(self.png)
        _, _, tokens = self.preview(f"![plot]({quote(name)})")
        self.assertTrue(next(tokens_of_kind(tokens, "image"))["attrs"]["url"].startswith("data:"))

    def test_remote_image_url_is_preserved(self):
        _, _, tokens = self.preview("![remote](https://example.invalid/plot.png)")
        self.assertEqual(next(tokens_of_kind(tokens, "image"))["attrs"]["url"], "https://example.invalid/plot.png")

    def test_existing_data_image_and_title_are_preserved(self):
        url = renderer.image_data_url(self.png)
        _, text, tokens = self.preview(f'![image]({url} "A title")\n\n![again]({url} "A title")')
        images = list(tokens_of_kind(tokens, "image"))
        self.assertEqual(len(images), 2)
        self.assertEqual(images[0]["attrs"], {"url": url, "title": "A title"})
        self.assertEqual(text.count(url), 1)

    def test_same_image_with_different_titles_keeps_both(self):
        url = renderer.image_data_url(self.png)
        _, _, tokens = self.preview(f'![a]({url} "First")\n\n![b]({url} "Second")\n\n![c]({url} "First")')
        self.assertEqual([t["attrs"]["title"] for t in tokens_of_kind(tokens, "image")],
                         ["First", "Second", "First"])

    def test_local_image_change_is_not_hidden_by_encoding_cache(self):
        image = self.source / "local.png"
        image.write_bytes(self.png)
        _, before, _ = self.preview("![a](local.png)")
        image.write_bytes(self.png + b"changed")
        _, after, _ = self.preview("![a](local.png)")
        self.assertNotEqual(before, after)

    def test_generated_reference_does_not_override_user_link(self):
        _, text, _ = self.preview("$x$")
        label = next(line[1:line.index("]:")] for line in text.splitlines() if line.startswith("[zedtex-"))
        _, _, tokens = self.preview(f"$x$ and [original][{label}]\n\n[{label}]: https://example.invalid/original")
        self.assertEqual(next(tokens_of_kind(tokens, "link"))["attrs"]["url"], "https://example.invalid/original")
        self.assertTrue(next(tokens_of_kind(tokens, "image"))["attrs"]["url"].startswith("data:"))

    def test_code_fences_and_inline_code_stay_literal(self):
        _, text, tokens = self.preview("`$x$`\n\n```tex\n$x$\n$$x$$\n```\n")
        self.assertIn("`$x$`", text)
        self.assertIn("$$x$$", text)
        self.assertFalse(list(tokens_of_kind(tokens, "image")))
        self.assertFalse(self.equations)

    def test_math_in_tables_lists_and_quotes(self):
        _, _, tokens = self.preview("| Math |\n|---|\n| $x$ |\n\n- $x$\n\n> $x$\n")
        self.assertEqual(len(list(tokens_of_kind(tokens, "image"))), 3)
        for kind in ("table", "list", "block_quote"):
            self.assertTrue(list(tokens_of_kind(tokens, kind)))

    def test_normal_links_keep_fragments_and_queries(self):
        _, _, tokens = self.preview("[source](part%20one.tex?q=1#section)")
        url = next(tokens_of_kind(tokens, "link"))["attrs"]["url"]
        self.assertTrue(url.endswith("part%20one.tex?q=1#section"))
        self.assertIn("source%20space", url)

    def test_source_and_repository_are_unchanged(self):
        original = "# Source\n\n$x$\n"
        result, _, _ = self.preview(original)
        self.assertFalse(result.is_relative_to(self.source))
        self.assertEqual([p.name for p in self.source.iterdir()], ["notes.md"])
        self.assertEqual((self.source / "notes.md").read_text(), original)

    def test_unchanged_preview_is_not_rewritten(self):
        result, _, _ = self.preview("$x$")
        previous = result.stat().st_mtime_ns
        renderer.preview(self.source / "notes.md")
        self.assertEqual(result.stat().st_mtime_ns, previous)

    def test_obsolete_sibling_assets_are_removed(self):
        result, _, _ = self.preview("$x$")
        old = result.parent / "old.png"
        old.write_bytes(self.png)
        assets = result.parent / "assets"
        assets.mkdir()
        (assets / "old.svg").write_text("obsolete")
        renderer.preview(self.source / "notes.md")
        self.assertFalse(old.exists())
        self.assertFalse(list(assets.iterdir()))

    def test_preamble_fails_before_compiler_or_cache_creation(self):
        preamble = self.source / "latex-preamble.tex"
        preamble.write_text(r"\usepackage{amsmath}")
        with patch.object(renderer, "compile_tex") as compile_tex:
            with self.assertRaisesRegex(ValueError, "not a standalone document"):
                renderer.preview(preamble)
            compile_tex.assert_not_called()
        self.assertFalse(self.cache.exists())


class CacheRegressions(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="zedtex-cache-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.addCleanup(patch.stopall)
        patch.object(renderer, "cache_directory", return_value=self.root / "cache").start()
        data = io.BytesIO()
        with Image.new("RGB", (4, 4), "black") as image:
            image.save(data, format="PNG")
        self.png = data.getvalue()
        self.compile = patch.object(renderer, "compile_tex", return_value=self.root / "batch.pdf").start()
        pdf = patch.object(renderer.pdfium, "PdfDocument").start().return_value.__enter__.return_value
        pdf.__len__.side_effect = lambda: self.compile.call_args.args[0].count(r"\begin{preview}")
        self.raster = patch.object(renderer, "page_png", return_value=(self.png, 4, 4)).start()

    def test_600_equations_stay_cached_and_only_changes_compile(self):
        equations = [(f"x_{i}", True) for i in range(600)]
        renderer.Renderer(self.source).render(equations)
        self.assertEqual(self.raster.call_count, 600)
        self.compile.reset_mock()
        self.raster.reset_mock()
        renderer.Renderer(self.source).render(equations)
        self.compile.assert_not_called()
        self.raster.assert_not_called()
        renderer.Renderer(self.source).render(equations[:-1] + [("y", True)])
        self.compile.assert_called_once()
        self.raster.assert_called_once()

    def test_cache_hits_read_png_once_and_never_scan_for_eviction(self):
        equation = ("x", True)
        renderer.Renderer(self.source).render([equation])
        math = renderer.Renderer(self.source)
        reads = []
        read = Path.read_bytes
        scan = os.scandir

        def track(path):
            reads.append(path)
            return read(path)

        def source_only(path):
            self.assertNotEqual(Path(path), math.cache, "Unnecessary cache scan")
            return scan(path)

        with patch.object(Path, "read_bytes", track), \
                patch.object(renderer.os, "scandir", side_effect=source_only):
            math.render([equation])
            for _ in range(200):
                math.render([equation])
        self.assertEqual(len(reads), 1)
        self.assertEqual(math.images[equation], (self.png, 4, 4))

    def test_documents_without_math_skip_dependency_scanning(self):
        with patch.object(renderer, "input_stamp", side_effect=AssertionError("No math dependencies")):
            math = renderer.Renderer(self.source)
            self.assertEqual(renderer.convert_markdown("Plain **Markdown**.", math, Mock()).strip(), "Plain **Markdown**.")
            math.render([])
        self.compile.assert_not_called()

    def test_disk_cache_remains_bounded_by_bytes_and_count(self):
        for byte_limit, entry_limit, expected in ((len(self.png) * 2, 4096, 2), (1024 * 1024, 3, 3)):
            with self.subTest(byte_limit=byte_limit, entry_limit=entry_limit), \
                    patch.object(renderer, "MATH_CACHE_BYTES", byte_limit), \
                    patch.object(renderer, "MATH_CACHE_ENTRIES", entry_limit):
                math = renderer.Renderer(self.source)
                math.render([(f"x_{byte_limit}_{i}", True) for i in range(10)])
                files = list(math.cache.glob("*.png"))
                self.assertEqual(len(files), expected)
                self.assertLessEqual(sum(p.stat().st_size for p in files), byte_limit)
                self.assertEqual(len(math.images), 10)

    def test_one_shot_preview_has_no_watch_delay_or_extra_scan(self):
        source = self.source / "notes.md"
        source.write_text("$x$")
        with patch.object(sys, "argv", ["zedtex", "preview", str(source)]), \
                patch.object(renderer, "preview") as preview, \
                patch.object(renderer.time, "sleep", side_effect=AssertionError("One-shot delay")), \
                patch.object(renderer, "input_stamp", side_effect=AssertionError("Extra scan")):
            renderer.main()
        preview.assert_called_once_with(source)

    def test_parallel_cache_writes_are_atomic_and_leave_no_temp_files(self):
        target = self.root / "shared.png"
        barrier = Barrier(2)
        replace = Path.replace
        attempted = set()

        def together(path, destination):
            if path not in attempted:
                attempted.add(path)
                barrier.wait(timeout=5)
            return replace(path, destination)

        with patch.object(Path, "replace", together), ThreadPoolExecutor(max_workers=2) as workers:
            list(workers.map(lambda data: renderer.write_changed(target, data), [b"first", b"second"]))
        self.assertIn(target.read_bytes(), (b"first", b"second"))
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_failed_cache_replace_preserves_previous_file_and_cleans_temp(self):
        target = self.root / "shared.png"
        target.write_bytes(b"previous")
        with patch.object(Path, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                renderer.write_changed(target, b"new")
        self.assertEqual(target.read_bytes(), b"previous")
        self.assertFalse(list(self.root.glob("*.tmp")))


class ActionRegressions(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="zedtex-actions-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(patch.stopall)
        patch.object(renderer, "cache_directory", return_value=self.root / "cache").start()
        patch.object(server, "active_previews", set()).start()
        patch.object(server, "pending_saves", {}).start()

    def actions(self, name):
        uri = (self.root / name).as_uri()
        point = lsp.Position(line=0, character=0)
        return server.code_actions(lsp.CodeActionParams(
            text_document=lsp.TextDocumentIdentifier(uri=uri), range=lsp.Range(point, point),
            context=lsp.CodeActionContext(diagnostics=[])))

    def test_markdown_and_tex_offer_preview(self):
        for filename in ("file.md", "file.MD", "paper.tex", "paper.TEX"):
            with self.subTest(filename=filename):
                actions = self.actions(filename)
                self.assertEqual(len(actions), 1)
                self.assertEqual(actions[0].command.command, "latex.preview")
                self.assertEqual(actions[0].command.arguments, [(self.root / filename).as_uri()])

    def test_notebook_offers_both_exports(self):
        self.assertEqual([action.command.command for action in self.actions("demo.ipynb")],
                         ["latex.export.html", "latex.export.pdf"])

    def test_python_offers_exports_and_helper(self):
        actions = self.actions("demo.py")
        self.assertEqual(len(actions), 3)
        self.assertEqual([action.command.command for action in actions[:2]], ["latex.export.html", "latex.export.pdf"])
        self.assertIsNotNone(actions[2].edit)

    def test_preamble_and_unsupported_files_have_no_preview_action(self):
        for filename in ("latex-preamble.tex", "file.txt", "package.sty"):
            with self.subTest(filename=filename):
                self.assertEqual(self.actions(filename), [])

    def test_generated_previews_have_no_recursive_action(self):
        for filename in ("cache/documents/test/preview.md", "old.md.zed-output/preview.md"):
            with self.subTest(filename=filename):
                self.assertEqual(self.actions(filename), [])

    def test_stop_action_is_available_only_for_active_preview(self):
        path = (self.root / "file.md").resolve()
        server.active_previews.add(path)
        self.assertEqual([a.command.command for a in self.actions("file.md")], ["latex.preview", "latex.stop"])
        server.stop(path.as_uri())
        self.assertEqual(len(self.actions("file.md")), 1)

    def test_non_file_uri_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "local file"):
            server.source_path("untitled:notes.md")

    def test_notebook_file_type_and_server_mapping(self):
        root = Path(__file__).parent
        manifest = tomllib.loads((root / "extension.toml").read_text())
        config = tomllib.loads((root / "languages/jupyter-notebook/config.toml").read_text())
        language_server = manifest["language_servers"]["latex-rendering"]
        self.assertIn("ipynb", config["path_suffixes"])
        self.assertEqual(config["grammar"], "json")
        self.assertIn(config["name"], language_server["languages"])
        self.assertEqual(language_server["language_ids"][config["name"]], "json")


class SaveAndOpenRegressions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="zedtex-save-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "notes.md"
        self.source.write_text("$x$")
        self.addCleanup(patch.stopall)
        patch.object(renderer, "cache_directory", return_value=self.root / "cache").start()
        patch.object(server, "active_previews", {self.source}).start()
        patch.object(server, "pending_saves", {}).start()
        patch.object(server, "render_lock", asyncio.Lock()).start()
        patch.object(server, "export_lock", asyncio.Lock()).start()

    async def save(self, path=None):
        await server.saved(lsp.DidSaveTextDocumentParams(
            text_document=lsp.TextDocumentIdentifier(uri=(path or self.source).as_uri())))

    async def test_burst_of_saves_builds_once(self):
        with patch.object(server, "build", new_callable=AsyncMock) as build:
            for _ in range(5):
                await self.save()
            await asyncio.sleep(0.4)
            build.assert_awaited_once_with(self.source.as_uri(), show=False)
        self.assertFalse(server.pending_saves)

    async def test_preamble_save_rebuilds_active_markdown(self):
        with patch.object(server, "build", new_callable=AsyncMock) as build:
            await self.save(self.root / "latex-preamble.tex")
            await asyncio.sleep(0.4)
            build.assert_awaited_once_with(self.source.as_uri(), show=False)

    async def test_unrelated_save_does_not_build(self):
        with patch.object(server, "build", new_callable=AsyncMock) as build:
            await self.save(self.root / "unrelated.md")
            await self.save(self.root.parent / "elsewhere.sty")
            self.assertFalse(server.pending_saves)
            build.assert_not_called()

    async def test_stop_cancels_pending_rebuild(self):
        with patch.object(server, "build", new_callable=AsyncMock) as build:
            await self.save()
            server.stop(self.source.as_uri())
            await asyncio.sleep(0.4)
            build.assert_not_called()

    async def test_save_during_build_triggers_one_followup(self):
        started = asyncio.Event()
        resume = asyncio.Event()

        async def build(*args, **kwargs):
            started.set()
            await resume.wait()

        with patch.object(server, "build", side_effect=build) as mocked:
            await self.save()
            await asyncio.wait_for(started.wait(), 2)
            await self.save()
            await self.save()
            resume.set()
            await asyncio.sleep(0.4)
            self.assertEqual(mocked.await_count, 2)
        self.assertFalse(server.pending_saves)

    async def test_preview_requests_embedded_document(self):
        result = self.root / "cache" / "preview.md"
        with patch.object(renderer, "preview", return_value=result), \
                patch.object(server.server, "window_show_document_async", new_callable=AsyncMock) as show, \
                patch.object(server.server, "window_show_message"):
            show.return_value = lsp.ShowDocumentResult(success=True)
            await server.build(self.source.as_uri())
            params = show.await_args.args[0]
            self.assertEqual(params.uri, result.as_uri())
            self.assertTrue(params.take_focus)
            self.assertFalse(params.external)

    async def test_failed_document_open_reports_error(self):
        with patch.object(renderer, "preview", return_value=self.root / "cache" / "preview.md"), \
                patch.object(server.server, "window_show_document_async", new_callable=AsyncMock) as show, \
                patch.object(server.server, "window_show_message") as message:
            show.return_value = lsp.ShowDocumentResult(success=False)
            await server.build(self.source.as_uri())
            self.assertEqual(message.call_args.args[0].type, lsp.MessageType.Error)
            self.assertIn("could not open", message.call_args.args[0].message)

    async def test_preview_does_not_wait_for_notebook_export(self):
        started = Event()
        resume = Event()

        def export(*args):
            started.set()
            if not resume.wait(5):
                raise TimeoutError("Preview was blocked by export")
            return self.root / "notebook.html"

        with patch.object(server, "export_in_process", side_effect=export), \
                patch.object(renderer, "preview", return_value=self.root / "preview.md") as preview, \
                patch.object(server.server, "window_show_message") as message:
            task = asyncio.create_task(server.build((self.root / "notebook.ipynb").as_uri(), "html", show=False))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                await asyncio.wait_for(server.build(self.source.as_uri(), show=False), 1)
                preview.assert_called_once()
                self.assertFalse(task.done())
            finally:
                resume.set()
                await task
            message.assert_not_called()

    async def test_exports_run_one_at_a_time(self):
        with patch.object(server, "export_in_process", return_value=self.root / "notebook.html") as export, \
                patch.object(server.server, "window_show_message"):
            async with server.export_lock:
                tasks = [asyncio.create_task(server.build(self.source.as_uri(), kind, show=False)) for kind in ("html", "pdf")]
                await asyncio.sleep(0)
                export.assert_not_called()
            await asyncio.gather(*tasks)
            self.assertEqual(export.call_count, 2)

    async def test_export_failure_reports_error_without_opening_stale_file(self):
        with patch.object(server, "export_in_process", side_effect=RuntimeError("kernel failed")), \
                patch.object(server.server, "window_show_document_async", new_callable=AsyncMock) as show, \
                patch.object(server.server, "window_show_message") as message:
            await server.build(self.source.as_uri(), "html")
            show.assert_not_awaited()
            self.assertEqual(message.call_args.args[0].type, lsp.MessageType.Error)
            self.assertIn("kernel failed", message.call_args.args[0].message)


class SemanticTokenRegressions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="zedtex-tokens-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = Workspace(self.root.as_uri())
        self.addCleanup(patch.stopall)
        patch.object(renderer, "cache_directory", return_value=self.root / "cache").start()
        patch.object(server.server.protocol, "_workspace", self.workspace).start()
        patch.object(server, "render_lock", asyncio.Lock()).start()
        patch.object(server, "export_lock", asyncio.Lock()).start()

    def open(self, name, text="x = 1\n"):
        uri = (self.root / name).as_uri()
        self.workspace.put_text_document(lsp.TextDocumentItem(uri=uri, language_id="python", version=1, text=text))
        return uri

    def tokens(self, uri):
        return server.semantic_tokens(lsp.SemanticTokensParams(text_document=lsp.TextDocumentIdentifier(uri=uri)))

    def capabilities_of(self, module):
        features = module.server.protocol.fm
        return ServerCapabilitiesBuilder(
            lsp.ClientCapabilities(), {*features.features, *features.builtin_features}, features.feature_options,
            [], lsp.TextDocumentSyncKind.Incremental).build()

    def load_server_without_highlight(self, error):
        real_import = builtins.__import__

        def import_module(name, *args, **kwargs):
            if name == "highlight":
                raise error
            return real_import(name, *args, **kwargs)

        spec = importlib.util.spec_from_file_location("server_without_highlight", server.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch("builtins.__import__", import_module), self.assertLogs("server_without_highlight", "ERROR"):
            spec.loader.exec_module(module)
        return module

    def test_full_requests_are_advertised_with_the_tokenizer_legend(self):
        provider = self.capabilities_of(server).semantic_tokens_provider
        self.assertEqual(provider.legend.token_types, highlight.TOKEN_TYPES)
        self.assertEqual(provider.legend.token_modifiers, [])
        self.assertIs(provider.full, True)
        self.assertFalse(provider.range)

    def test_server_without_highlight_still_serves_code_actions(self):
        point = lsp.Position(line=0, character=0)
        params = lsp.CodeActionParams(
            text_document=lsp.TextDocumentIdentifier(uri=(self.root / "demo.py").as_uri()), range=lsp.Range(point, point),
            context=lsp.CodeActionContext(diagnostics=[]))
        for error in (ImportError("cannot import name BLOCK_TAGS"), AttributeError("math pattern moved")):
            with self.subTest(error=error):
                module = self.load_server_without_highlight(error)
                self.assertIsNone(module.highlight)
                self.assertIsNone(self.capabilities_of(module).semantic_tokens_provider)
                self.assertIn("latex.preview", module.server.protocol.fm.commands)
                self.assertEqual([action.command.command for action in module.code_actions(params)[:2]],
                                 ["latex.export.html", "latex.export.pdf"])

    async def test_python_files_are_tokenized_from_the_open_buffer(self):
        (self.root / "script.py").write_text("on disk\n")
        for name in ("script.py", "SCRIPT.PY", "Script.Py"):
            with self.subTest(name=name):
                buffer = f"# %% [markdown]\n# live {name}\n"
                uri = self.open(name, buffer)
                with patch.object(highlight, "semantic_tokens", return_value=[0, 2, 4, 1, 0]) as tokenize:
                    result = await self.tokens(uri)
                tokenize.assert_called_once_with(buffer)
                self.assertEqual(result.data, [0, 2, 4, 1, 0])

    async def test_other_documents_get_an_empty_result(self):
        uris = [self.open(name) for name in ("notes.md", "paper.tex", "demo.ipynb", "stub.pyi", "script.py.txt", "script")]
        uris += ["untitled:Untitled-1.py", "https://example.invalid/script.py"]
        with patch.object(highlight, "semantic_tokens", return_value=[0, 0, 1, 1, 0]) as tokenize:
            for uri in uris:
                with self.subTest(uri=uri):
                    self.assertEqual((await self.tokens(uri)).data, [])
        tokenize.assert_not_called()

    async def test_python_without_a_markdown_cell_gets_an_empty_result(self):
        uri = self.open("plain.py", "import os\n\n# %%\nprint(os.name)\n")
        self.assertEqual((await self.tokens(uri)).data, [])

    async def test_markdown_cell_gets_well_formed_tokens(self):
        uri = self.open("notebook.py", "# %% [markdown]\n# # Title\n# Math $x^2$ and *emphasis*.\n\n# %%\nprint(1)\n")
        data = (await self.tokens(uri)).data
        self.assertTrue(data)
        self.assertEqual(len(data) % 5, 0)
        self.assertTrue(all(0 <= kind < len(highlight.TOKEN_TYPES) for kind in data[3::5]))
        self.assertEqual(set(data[4::5]), {0})

    async def test_tokenizer_error_gives_an_empty_result(self):
        uri = self.open("broken.py")
        with patch.object(highlight, "semantic_tokens", side_effect=RuntimeError("tokenizer failed")):
            self.assertEqual((await self.tokens(uri)).data, [])

    async def test_unreadable_buffer_gives_an_empty_result(self):
        with patch.object(highlight, "semantic_tokens") as tokenize:
            self.assertEqual((await self.tokens((self.root / "missing.py").as_uri())).data, [])
        tokenize.assert_not_called()

    async def test_tokens_do_not_wait_for_preview_or_export_locks(self):
        uri = self.open("script.py")
        with patch.object(highlight, "semantic_tokens", return_value=[0, 0, 1, 1, 0]):
            async with server.render_lock, server.export_lock:
                result = await asyncio.wait_for(self.tokens(uri), 1)
        self.assertEqual(result.data, [0, 0, 1, 1, 0])

    async def test_slow_tokenizer_does_not_delay_preview_or_export(self):
        started = Event()
        resume = Event()
        source = self.root / "notes.md"
        source.write_text("$x$")

        def tokenize(text):
            started.set()
            if not resume.wait(5):
                raise TimeoutError("Preview or export was blocked by tokens")
            return [0, 0, 1, 1, 0]

        with patch.object(highlight, "semantic_tokens", side_effect=tokenize), \
                patch.object(renderer, "preview", return_value=self.root / "preview.md") as preview, \
                patch.object(server, "export_in_process", return_value=self.root / "notes.html") as export, \
                patch.object(server.server, "window_show_message") as message:
            task = asyncio.create_task(self.tokens(self.open("script.py")))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                await asyncio.wait_for(server.build(source.as_uri(), show=False), 1)
                await asyncio.wait_for(server.build(source.as_uri(), "html", show=False), 1)
                preview.assert_called_once()
                export.assert_called_once()
                self.assertFalse(task.done())
            finally:
                resume.set()
                result = await task
            self.assertEqual(result.data, [0, 0, 1, 1, 0])
            message.assert_not_called()


class PdfRuntimeRegressions(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="zedtex-node-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.node = self.root / "shared node.exe"
        self.node.write_bytes(b"fixture")
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {}, clear=False).start()
        os.environ.pop("PLAYWRIGHT_NODEJS_PATH", None)
        self.driver = patch("playwright._impl._driver.compute_driver_executable",
                            return_value=(str(self.root / "missing-node"), "cli.js")).start()

    def test_shared_node_is_used_without_changing_its_permissions(self):
        os.environ["PLAYWRIGHT_NODEJS_PATH"] = str(self.node)
        self.driver.return_value = (str(self.node), "cli.js")
        with patch.object(Path, "chmod", side_effect=AssertionError("Shared runtime was modified")), \
                patch.object(renderer.shutil, "which") as which:
            self.assertEqual(renderer.pdf_driver(), (str(self.node), "cli.js"))
            which.assert_not_called()

    def test_standalone_cli_finds_node_on_path(self):
        with patch.object(renderer.shutil, "which", return_value=str(self.node)):
            self.assertEqual(renderer.pdf_driver(), (str(self.node), "cli.js"))
        self.assertEqual(os.environ["PLAYWRIGHT_NODEJS_PATH"], str(self.node))

    def test_standalone_cli_finds_node_when_playwright_has_none(self):
        self.driver.side_effect = [RuntimeError("no bundled Node.js"), (str(self.node), "cli.js")]
        with patch.object(renderer.shutil, "which", return_value=str(self.node)):
            self.assertEqual(renderer.pdf_driver(), (str(self.node), "cli.js"))
        self.assertEqual(os.environ["PLAYWRIGHT_NODEJS_PATH"], str(self.node))

    def test_missing_node_error_survives_playwright_raising(self):
        self.driver.side_effect = RuntimeError("no bundled Node.js")
        with patch.object(renderer.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Reload ZedTeX"):
                renderer.pdf_driver()

    def test_source_install_can_use_playwrights_existing_node(self):
        self.driver.return_value = (str(self.node), "cli.js")
        with patch.object(renderer.shutil, "which") as which:
            self.assertEqual(renderer.pdf_driver(), (str(self.node), "cli.js"))
            which.assert_not_called()

    def test_missing_node_has_an_actionable_error(self):
        with patch.object(renderer.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Reload ZedTeX"):
                renderer.pdf_driver()

    def test_invalid_explicit_node_is_not_silently_overridden(self):
        os.environ["PLAYWRIGHT_NODEJS_PATH"] = str(self.root / "missing-node")
        with patch.object(renderer.shutil, "which") as which:
            with self.assertRaisesRegex(RuntimeError, "PLAYWRIGHT_NODEJS_PATH"):
                renderer.pdf_driver()
            which.assert_not_called()


class ExportProcessRegressions(unittest.TestCase):
    def test_source_and_frozen_commands_execute_notebooks(self):
        source = Path(__file__).resolve().with_name("example.ipynb")
        for frozen in (False, True):
            with self.subTest(frozen=frozen), patch.object(sys, "frozen", frozen, create=True), \
                    patch.object(server.subprocess, "run", return_value=Mock(returncode=0)) as run:
                output = server.export_in_process(source, "pdf")
                command = run.call_args.args[0]
                expected = [sys.executable] if frozen else [sys.executable, str(Path(renderer.__file__).resolve())]
                self.assertEqual(command, expected + ["export", str(source), "--to", "pdf"])
                self.assertNotIn("--no-execute", command)
                self.assertEqual(output, renderer.output_directory(source) / "example.pdf")

    def test_export_error_includes_bounded_log_tail(self):
        def fail(*args, **kwargs):
            self.assertIs(kwargs["stdout"], kwargs["stderr"])
            kwargs["stderr"].write(b"noise" * 5000 + b"kernel failed")
            return Mock(returncode=1)

        with patch.object(server.subprocess, "run", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "kernel failed") as error:
                server.export_in_process(Path("example.ipynb"), "html")
        self.assertEqual(len(str(error.exception)), 8000)


class NotebookMarkdownRegressions(unittest.TestCase):
    def test_equation_becomes_attachment_image(self):
        text = renderer.convert_markdown("a $x$ b", Mock(directory=None), lambda equation: "attachment:x.png")
        self.assertEqual(text.strip(), "a ![equation](attachment:x.png) b")


if __name__ == "__main__":
    unittest.main(verbosity=2)
