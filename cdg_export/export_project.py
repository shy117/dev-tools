"""通过已授权的 CMD 将明文导出到新目录，不显示文件内容。"""

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile


PROTECTED_SUFFIXES = {".h", ".cpp"}
CHUNK_SIZE = 1024 * 1024


class ExportError(Exception):
    pass


def is_reparse(path):
    return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def digest_file(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.digest()


def export_source(source, target):
    # 在双引号内仅展开一次环境变量，由 CMD 自行打开源文件。
    # 不将用户路径直接拼入 CMD 命令语法。
    environment = os.environ.copy()
    environment["CDG_EXPORT_SOURCE"] = str(source)
    cmd_exe = Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"
    # CMD 使用自己的引号规则，不使用 subprocess.list2cmdline 的 C 参数
    # 反斜杠转义。命令中只直接写入可信的可执行文件路径。
    command = '"%s" /d /v:off /c type "%%CDG_EXPORT_SOURCE%%"' % cmd_exe
    digest = hashlib.sha256()
    size = 0
    # 不向界面显示标准错误，避免显示与源文件内容有关的异常输出。
    with tempfile.TemporaryFile() as errors:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors,
                              stdin=subprocess.DEVNULL, env=environment) as process:
            try:
                with target.open("xb") as output:
                    for chunk in iter(lambda: process.stdout.read(CHUNK_SIZE), b""):
                        output.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                returncode = process.wait()
            except BaseException:
                process.kill()
                process.wait()
                raise
        errors.seek(0, os.SEEK_END)
        if returncode != 0 or errors.tell():
            raise ExportError("CMD 读取失败（退出码 %s）；未显示命令输出" % returncode)
    # 通过 Python 普通读取目标文件，确认其字节与已授权 CMD 的输出
    # 完全一致，校验过程不显示文件内容。
    if digest_file(target) != (size, digest.digest()):
        raise ExportError("导出校验失败：目标文件普通读取结果与 CMD 输出不一致")
    shutil.copystat(source, target)
    return size


def copy_file(source, target):
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as input_stream, target.open("xb") as output:
        for chunk in iter(lambda: input_stream.read(CHUNK_SIZE), b""):
            output.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    if digest_file(target) != (size, digest.digest()):
        raise ExportError("复制校验失败")
    shutil.copystat(source, target)
    return size


def plan_export(source, destination):
    if os.name != "nt":
        raise ExportError("此脚本仅支持 Windows，并要求 cmd.exe 具有明文读取权限")
    source = Path(os.path.abspath(source))
    destination = Path(os.path.abspath(destination))
    if not source.exists():
        raise ExportError("输入路径不存在")
    # 同时检查上级路径，避免意外通过目录联接访问其他位置。
    for path in (source, *source.parents, destination, *destination.parents):
        if os.path.lexists(path) and is_reparse(path):
            raise ExportError("输入或输出路径包含符号链接/目录联接：%s" % path)
    if os.path.lexists(destination):
        raise ExportError("输出目录已存在，请指定一个新的目录；不会覆盖或合并")
    if source.is_dir() and destination.is_relative_to(source):
        raise ExportError("输出目录不能位于输入目录内部")
    files = []
    directories = []
    if source.is_file():
        files.append((source, Path(source.name)))
    elif source.is_dir():
        directories.append(Path("."))

        def walk_error(error):
            raise error

        for current, dirnames, filenames in os.walk(source, onerror=walk_error):
            for name in sorted(dirnames + filenames):
                path = Path(current) / name
                if is_reparse(path):
                    raise ExportError("目录包含符号链接/目录联接，未开始导出：%s" % path)
                relative = path.relative_to(source)
                if path.is_dir():
                    directories.append(relative)
                elif path.is_file():
                    files.append((path, relative))
                else:
                    raise ExportError("不支持的文件类型：%s" % path)
    else:
        raise ExportError("输入必须为普通文件或目录")
    return source, destination, directories, files


def export_project(source, destination):
    source, destination, directories, files = plan_export(source, destination)
    # 即使预检已通过，也要求目录不存在，防止覆盖已有输出。
    destination.mkdir(parents=True, exist_ok=False)
    for relative in directories:
        if relative != Path("."):
            (destination / relative).mkdir()
    exported = copied = total_size = 0
    for original, relative in files:
        target = destination / relative
        try:
            if original.suffix.lower() in PROTECTED_SUFFIXES:
                total_size += export_source(original, target)
                exported += 1
            else:
                total_size += copy_file(original, target)
                copied += 1
        except (OSError, ExportError) as error:
            # 保留不完整结果供检查，明确报告导出未完成，
            # 下次运行也不覆盖该目录。
            raise ExportError(
                "文件处理失败：%s；%s；已完成 %s 个文件，%s 字节。"
                "输出目录保留了不完整结果，请下次使用新的输出目录"
                % (relative, error, exported + copied, total_size)) from error
    for relative in reversed(directories):
        shutil.copystat(source / relative, destination / relative)
    return exported, copied, total_size


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="通过授权 CMD 导出 .h/.cpp 明文，其他文件原样复制；不显示源码。",
        epilog="输出参数始终是尚不存在的新目录。单文件模式保留原文件名。")
    parser.add_argument("input", help="输入文件或项目目录")
    parser.add_argument("output", help="新的输出目录（不得已存在）")
    arguments = parser.parse_args(argv)
    try:
        exported, copied, size = export_project(arguments.input, arguments.output)
    except (OSError, ExportError) as error:
        print("失败：%s" % error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断：输出目录可能包含不完整结果，请下次使用新目录", file=sys.stderr)
        return 130
    print("成功：文件 %s 个；CMD 导出 %s 个；原样复制 %s 个；输出总大小 %s 字节；全部校验通过"
          % (exported + copied, exported, copied, size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
