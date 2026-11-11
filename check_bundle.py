"""Check the actual frozen executable, including the LSP wire protocol."""
import json
import base64
import os
import re
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
from threading import Timer

from zed_latex import output_directory

binary = Path("dist/latex-rendering/latex-rendering" + (".exe" if sys.platform == "win32" else "")).resolve()
# Guard against accidentally shipping a second Node runtime or interactive tools.
internal = binary.parent / "_internal"
for unused in ("playwright/driver/node", "playwright/driver/node.exe", "jedi", "_tcl_data", "_tk_data"):
    assert not (internal / unused).exists(), f"Unused runtime data was bundled: {unused}"
from PyInstaller.archive.readers import CArchiveReader
modules = CArchiveReader(str(binary)).open_embedded_archive("PYZ.pyz").toc
for unused in ("jedi", "ipykernel", "tkinter"):
    assert not any(name == unused or name.startswith(unused + ".") for name in modules), f"Unused module bundled: {unused}"
node = shutil.which("node")
assert node, "Bundle checks require Node.js 20+ (Zed supplies its shared runtime in normal use)"
examples = Path("examples").resolve()
subprocess.run([binary, "preview", examples / "paper.tex"], check=True, timeout=180)
subprocess.run([binary, "preview", examples / "notes.md"], check=True, timeout=180)
for source in (examples / "paper.tex", examples / "notes.md"):
    text = (output_directory(source) / "preview.md").read_text(encoding="utf-8")
    images = re.findall(r"data:image/png;base64,([A-Za-z0-9+/=]+)", text)
    assert images and all(base64.b64decode(data, validate=True).startswith(b"\x89PNG") for data in images)
    assert not re.search(r"!\[[^\]]*\]\([^)]*\.png\)", text), "Preview depends on sibling image files"
preamble = subprocess.run([binary, "preview", examples / "latex-preamble.tex"], capture_output=True, timeout=30)
assert preamble.returncode != 0 and b"not a standalone document" in preamble.stderr
png = subprocess.run([binary, "math", "--directory", examples], input=b"\\LocalSet", capture_output=True, check=True, timeout=180)
assert png.stdout.startswith(b"\x89PNG")
with tempfile.TemporaryDirectory() as temporary:
    path = Path(temporary) / "test.ipynb"
    path.write_text(json.dumps({"nbformat": 4, "nbformat_minor": 5, "metadata": {"kernelspec": {"name": "zedtex-test", "display_name": "ZedTeX test", "language": "python"}}, "cells": [
        {"id": "example", "cell_type": "markdown", "metadata": {}, "source": "# Bundle check\n\n$x^2$"},
        {"id": "code", "cell_type": "code", "metadata": {}, "source": "print('fresh-bundle-output')", "execution_count": None, "outputs": []}
    ]}))
    original = path.read_bytes()
    for kind in ("html", "pdf"):
        # Exercise standalone PATH resolution with no Zed-provided override.
        env = {key: value for key, value in os.environ.items() if key != "PLAYWRIGHT_NODEJS_PATH"}
        subprocess.run([binary, "export", path, "--to", kind], env=env, check=True, timeout=360)
    output = output_directory(path)
    assert "fresh-bundle-output" in (output / "test.html").read_text(encoding="utf-8")
    assert (output / "test.pdf").read_bytes().startswith(b"%PDF")
    assert path.read_bytes() == original
    assert list(path.parent.iterdir()) == [path], "Renderer left intermediate files beside the source"
# Exercise the same explicit Node path that the extension supplies, including
# inheritance by the separate notebook-export process.
process = subprocess.Popen([binary, "serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           env={**os.environ, "PLAYWRIGHT_NODEJS_PATH": node})
deadline = Timer(120, process.kill)
deadline.start()
def send(message):
    data = json.dumps(message).encode()
    process.stdin.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
    process.stdin.flush()
def read():
    headers = {}
    while (line := process.stdout.readline().strip()):
        key, value = line.split(b":", 1)
        headers[key.lower()] = value.strip()
    assert b"content-length" in headers, "LSP server exited or wrote non-protocol output"
    return json.loads(process.stdout.read(int(headers[b"content-length"])))
def semantic_data(request_id, name, text):
    uri = (examples / name).as_uri()
    send({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {"textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": text}}})
    send({"jsonrpc": "2.0", "id": request_id, "method": "textDocument/semanticTokens/full", "params": {"textDocument": {"uri": uri}}})
    response = read()
    assert response.get("id") == request_id and "error" not in response, response
    return response["result"]["data"]
try:
    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"processId": None, "rootUri": examples.as_uri(), "capabilities": {}}})
    response = read()
    assert response["result"]["capabilities"]["codeActionProvider"]
    provider = response["result"]["capabilities"]["semanticTokensProvider"]
    assert provider["full"] and provider["legend"]["tokenTypes"] and not provider["legend"]["tokenModifiers"], provider
    commands = response["result"]["capabilities"]["executeCommandProvider"]["commands"]
    send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
    for request_id, (name, expected) in enumerate([
        ("paper.tex", ["latex.preview"]), ("notes.md", ["latex.preview"]),
        ("notebook.ipynb", ["latex.export.html", "latex.export.pdf"]),
        ("latex-preamble.tex", []),
    ], 10):
        send({"jsonrpc": "2.0", "id": request_id, "method": "textDocument/codeAction", "params": {"textDocument": {"uri": (examples / name).as_uri()}, "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}, "context": {"diagnostics": []}}})
        response = read()
        assert [action["command"]["command"] for action in response["result"]] == expected
        assert all(command in commands for command in expected), "Zed filters out unadvertised commands"
    data = semantic_data(40, "notebook_script.py", (examples / "notebook_script.py").read_text(encoding="utf-8"))
    assert data and len(data) % 5 == 0, data
    assert all(0 <= kind < len(provider["legend"]["tokenTypes"]) for kind in data[3::5]) and set(data[4::5]) == {0}, data
    assert semantic_data(41, "no_markdown_cell.py", "# %%\nprint('no Markdown here')\n") == []
    notebook_source = (examples / "notebook.ipynb").read_bytes()
    for request_id, (name, command, opened_name) in enumerate([
        ("paper.tex", "latex.preview", "page-001.png"), ("notes.md", "latex.preview", "preview.md"),
        ("notebook.ipynb", "latex.export.html", "notebook.html"),
        ("notebook.ipynb", "latex.export.pdf", "notebook.pdf"),
    ], 20):
        send({"jsonrpc": "2.0", "id": request_id, "method": "workspace/executeCommand", "params": {"command": command, "arguments": [(examples / name).as_uri()]}})
        opened = False
        while True:
            response = read()
            if response.get("method") == "window/showDocument":
                assert response["params"]["uri"].endswith(opened_name)
                assert bool(response["params"].get("external")) == command.startswith("latex.export.")
                opened = True
                send({"jsonrpc": "2.0", "id": response["id"], "result": {"success": True}})
            elif response.get("id") == request_id:
                assert "error" not in response and opened, response
                break
            elif response.get("method") == "window/showMessage":
                assert response["params"]["type"] != 1, response
    assert (examples / "notebook.ipynb").read_bytes() == notebook_source
    notebook_output = output_directory(examples / "notebook.ipynb")
    assert "n = 5: sum = 15" in (notebook_output / "notebook.html").read_text(encoding="utf-8")
    assert (notebook_output / "notebook.pdf").read_bytes().startswith(b"%PDF")
    send({"jsonrpc": "2.0", "id": 3, "method": "shutdown", "params": None})
    response = read()
    while "id" not in response:
        response = read()
    assert response["id"] == 3
    send({"jsonrpc": "2.0", "method": "exit", "params": None})
    assert process.wait(timeout=10) == 0
finally:
    deadline.cancel()
    if process.poll() is None:
        process.kill()
        process.wait()
print("PASS: frozen renderer, PNG, executed notebook HTML/PDF, LSP preview command, image opening and semantic tokens")
