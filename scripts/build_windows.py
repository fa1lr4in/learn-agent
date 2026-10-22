"""Stage an allowlisted desktop app with a real, relocatable CPython runtime."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parent.parent
STAGE = ROOT / "build" / "windows"
PYTHON_URL = "https://api.nuget.org/v3-flatcontainer/python/3.13.9/python.3.13.9.nupkg"
PYTHON_SHA256 = "ea7541c98f77b6f344460b88db6e677573069979bcbf0ba18747cc2c125aba25"
SOURCE_PATTERNS = (
    "api.py", "learn_agent/**/*.py", "web/*.js", "web/*.css", "web/*.html",
    "desktop/*.cjs", "desktop/*.html", "desktop/*.css", "desktop/*.js",
    "desktop/package.json", "desktop/package-lock.json", "desktop/requirements-windows.txt",
    "desktop/demo_project/**/*.py", "scripts/build_windows.py",
    "LICENSE", "THIRD_PARTY_NOTICES.md", "CHANGELOG.md", "SECURITY.md",
)


def digest(file):
    with file.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build_source():
    files = {}
    for pattern in SOURCE_PATTERNS:
        for file in sorted(ROOT.glob(pattern)):
            if file.is_file() and "__pycache__" not in file.parts:
                if file.is_symlink():
                    raise ValueError(f"构建来源不能是符号链接：{file}")
                files[file.relative_to(ROOT).as_posix()] = digest(file)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", *SOURCE_PATTERNS], cwd=ROOT, text=True
    ).strip()
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"commit": commit, "dirty": bool(dirty),
            "sha256": hashlib.sha256(encoded).hexdigest(), "files": files}


def run(*args, cwd=ROOT):
    subprocess.run(list(map(str, args)), cwd=cwd, check=True)


def copy_files(source, target, patterns):
    target.mkdir(parents=True, exist_ok=True)
    for pattern in patterns:
        for file in source.glob(pattern):
            if file.is_file() and "__pycache__" not in file.parts:
                if file.is_symlink():
                    raise ValueError(f"构建来源不能是符号链接：{file}")
                destination = target / file.relative_to(source)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, destination)


def main():
    if sys.platform != "win32" or os.environ.get("PROCESSOR_ARCHITECTURE", "").lower() != "amd64":
        raise SystemExit("目前只构建 Windows x64，请使用 64 位终端。")
    node = shutil.which("node")
    if not node or not (ROOT / "desktop/node_modules/electron-builder/cli.js").is_file():
        raise SystemExit("请先安装 Node.js 与 desktop 的锁定依赖，参见 README。")
    source = build_source()
    if source["dirty"]:
        raise SystemExit("构建输入存在未提交修改，请先提交本次程序、构建配置和发行说明；练习与文章不影响构建。")
    # The cache is reusable; every download is verified before extraction.
    cache = ROOT / "build/cache"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / "python.3.13.9.nupkg"
    if not archive.exists():
        print("下载 Python 3.13.9 x64……", flush=True)
        urllib.request.urlretrieve(PYTHON_URL, archive)
    if digest(archive) != PYTHON_SHA256:
        raise SystemExit("Python 下载校验失败，请移走 build/cache/python.3.13.9.nupkg 后重试。")
    # Delete only the fixed staging directory, never a user-selected path.
    if STAGE.resolve() != ROOT.resolve() / "build/windows":
        raise SystemExit("构建暂存路径异常。")
    if STAGE.exists():
        shutil.rmtree(STAGE)
    python_dir = STAGE / "backend/python"
    python_dir.mkdir(parents=True)
    with zipfile.ZipFile(archive) as package:
        for entry in package.infolist():
            if entry.filename.startswith("tools/") and not entry.is_dir():
                relative = Path(entry.filename.removeprefix("tools/"))
                destination = (python_dir / relative).resolve()
                if not destination.is_relative_to(python_dir.resolve()):
                    raise ValueError("Python 压缩包路径异常。")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(package.read(entry))
    python = python_dir / "python.exe"
    run(python, "-I", "-B", "-X", "utf8", "-m", "pip", "--isolated", "install", "--only-binary=:all:",
        "--disable-pip-version-check", "--no-compile", "--no-warn-script-location",
        "-r", ROOT / "desktop/requirements-windows.txt")
    run(python, "-I", "-B", "-c", "import fastapi,uvicorn,mcp,jsonschema,yaml,sqlite3,ssl")
    # electron-builder omits these ARM launch templates from x64 resources; keep the manifest exact.
    for name in ("t64-arm.exe", "w64-arm.exe"):
        (python_dir / "Lib/site-packages/pip/_vendor/distlib" / name).unlink(missing_ok=True)
    copy_files(ROOT, STAGE / "backend", ["api.py"])
    copy_files(ROOT / "learn_agent", STAGE / "backend/learn_agent", ["**/*.py"])
    copy_files(ROOT / "web", STAGE / "backend/web", ["*.js", "*.css", "*.html"])
    copy_files(ROOT / "web", STAGE / "app/web", ["*.js", "*.css", "*.html"])
    copy_files(ROOT / "desktop", STAGE / "app/desktop", ["*.cjs", "*.html", "*.css", "*.js"])
    (STAGE / "app/desktop/build-config.cjs").unlink()
    copy_files(ROOT, STAGE / "app", ["LICENSE", "THIRD_PARTY_NOTICES.md", "CHANGELOG.md", "SECURITY.md"])
    copy_files(ROOT / "desktop/demo_project", STAGE / "demo_project", ["**/*.py"])
    metadata = json.loads((ROOT / "desktop/package.json").read_text(encoding="utf-8"))
    app_metadata = {"name": metadata["name"], "version": metadata["version"],
                    "description": metadata["description"], "main": "desktop/main.cjs",
                    "author": "fa1lr4in", "license": "MIT"}
    (STAGE / "app/package.json").write_text(json.dumps(app_metadata, indent=2), encoding="utf-8")
    source_files = list((STAGE / "app").rglob("*")) + list((STAGE / "backend/learn_agent").rglob("*.py"))
    source_files += [STAGE / "backend/api.py"]
    key = os.environ.get("DEEPSEEK_API_KEY", "").encode()
    if key and any(key in file.read_bytes() for file in source_files if file.is_file()):
        raise SystemExit("构建来源包含当前模型密钥，已中止。")
    manifest = {"version": metadata["version"], "platform": "win32-x64", "source": source, "python": "3.13.9",
                "python_url": PYTHON_URL, "python_sha256": PYTHON_SHA256,
                "electron": metadata["devDependencies"]["electron"],
                "files": {str(file.relative_to(STAGE)).replace("\\", "/"): digest(file)
                          for file in sorted(STAGE.rglob("*")) if file.is_file()}}
    (STAGE / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    run(node, ROOT / "desktop/node_modules/electron/cli.js", "--version")
    run(node, ROOT / "desktop/node_modules/electron-builder/cli.js", "--projectDir", ROOT / "desktop",
        "--config", "build-config.cjs", "--win", "nsis", "--x64", "--publish", "never")
    if build_source() != source:
        raise SystemExit("构建期间源码或提交发生变化，本次产物不能用于发布，请重新构建。")
    installer = ROOT / "dist" / f"learn-agent-Setup-{metadata['version']}-x64.exe"
    (installer.parent / "SHA256SUMS.txt").write_text(f"{digest(installer)}  {installer.name}\n", encoding="utf-8")
    print(f"安装包：{installer}\nSHA256：{digest(installer)}", flush=True)


if __name__ == "__main__":
    main()
