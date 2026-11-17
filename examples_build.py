#!/usr/bin/env python3
"""Build the fictional sample .class / JAR files under examples/. Stdlib only."""

from __future__ import annotations

import io
import struct
import zipfile
from pathlib import Path

ACC_PUBLIC = 0x0001
ACC_SUPER = 0x0020
ACC_MODULE = 0x8000

ROOT = Path(__file__).resolve().parent
EXAMPLES = ROOT / "examples"


def u2(n: int) -> bytes:
    return struct.pack(">H", n)


def u4(n: int) -> bytes:
    return struct.pack(">I", n)


def utf8(s: str) -> bytes:
    b = s.encode("utf-8")
    return bytes([1]) + u2(len(b)) + b


def cp_class(name_index: int) -> bytes:
    return bytes([7]) + u2(name_index)


def make_class(
    internal: str,
    major: int,
    minor: int = 0,
    source: str | None = None,
    access: int = ACC_PUBLIC | ACC_SUPER,
    super_internal: str | None = "java/lang/Object",
) -> bytes:
    """A minimal but valid class (or module-info) file. No methods or fields."""
    entries: list[bytes] = []

    def add(item: bytes) -> int:
        entries.append(item)
        return len(entries)

    name_i = add(utf8(internal))
    this_i = add(cp_class(name_i))
    super_i = 0
    if super_internal is not None and not (access & ACC_MODULE):
        super_name_i = add(utf8(super_internal))
        super_i = add(cp_class(super_name_i))
    src_attr_name_i = src_file_i = 0
    if source:
        src_attr_name_i = add(utf8("SourceFile"))
        src_file_i = add(utf8(source))

    out = bytearray()
    out += u4(0xCAFEBABE)
    out += u2(minor)
    out += u2(major)
    out += u2(len(entries) + 1)
    for item in entries:
        out += item
    out += u2(access)
    out += u2(this_i)
    out += u2(super_i)
    out += u2(0)  # interfaces
    out += u2(0)  # fields
    out += u2(0)  # methods
    if source:
        out += u2(1)
        out += u2(src_attr_name_i)
        out += u4(2)
        out += u2(src_file_i)
    else:
        out += u2(0)
    return bytes(out)


def write_jar(path: Path, files: dict[str, bytes], manifest_lines: list[str] | None = None) -> None:
    manifest = "Manifest-Version: 1.0\n"
    if manifest_lines:
        manifest += "\n".join(manifest_lines) + "\n"
    buf = io.BytesIO()
    # fixed timestamp for stable bytes (not required, but nicer)
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("META-INF/MANIFEST.MF", manifest.encode("utf-8"))
        for name, data in files.items():
            zf.writestr(name, data)
    path.write_bytes(buf.getvalue())


def main() -> None:
    EXAMPLES.mkdir(parents=True, exist_ok=True)

    hello8 = make_class("com/example/Hello", 52, 0, source="Hello.java")
    hello17 = make_class("com/example/Hello", 61, 0, source="Hello.java")
    api8 = make_class("com/example/Api", 52, 0, source="Api.java")
    api17 = make_class("com/example/Api", 61, 0, source="Api.java")
    demo8 = make_class("com/example/Demo", 52, 0, source="Demo.java")
    preview = make_class("com/example/Preview", 69, 65535, source="Preview.java")
    module_info = make_class("demo.mod", 61, 0, source=None, access=ACC_MODULE, super_internal=None)
    helper11 = make_class("com/example/lib/Helper", 55, 0, source="Helper.java")

    (EXAMPLES / "hello-8.class").write_bytes(hello8)
    (EXAMPLES / "hello-17.class").write_bytes(hello17)
    (EXAMPLES / "preview.class").write_bytes(preview)
    (EXAMPLES / "module-info.class").write_bytes(module_info)
    (EXAMPLES / "not-a-class.bin").write_bytes(b"this is not a Java class file\n")

    write_jar(
        EXAMPLES / "demo.jar",
        {
            "com/example/Demo.class": demo8,
            "com/example/Api.class": api8,
            "META-INF/versions/17/com/example/Api.class": api17,
        },
        [
            "Main-Class: com.example.Demo",
            "Created-By: example-build 1.0",
            "Build-Jdk: 17.0.12",
            "Implementation-Title: demo",
            "Implementation-Version: 1.0.0-demo",
            "Multi-Release: true",
        ],
    )
    write_jar(
        EXAMPLES / "mixed.jar",
        {
            "com/example/Hello.class": hello8,
            "com/example/Api.class": api17,
        },
        [
            "Created-By: example-build 1.0",
            "Implementation-Title: mixed-demo",
            "Implementation-Version: 0.0.1-demo",
        ],
    )
    write_jar(
        EXAMPLES / "helper.jar",
        {"com/example/lib/Helper.class": helper11},
        [
            "Created-By: example-build 1.0",
            "Implementation-Title: helper",
            "Implementation-Version: 0.0.1-demo",
        ],
    )
    # A tiny WAR with WEB-INF/classes + a library JAR (fictional app).
    helper_jar = (EXAMPLES / "helper.jar").read_bytes()
    web_manifest = "Manifest-Version: 1.0\nCreated-By: example-build 1.0\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("META-INF/MANIFEST.MF", web_manifest.encode("utf-8"))
        zf.writestr("WEB-INF/classes/com/example/Demo.class", demo8)
        zf.writestr("WEB-INF/lib/helper.jar", helper_jar)
    (EXAMPLES / "demo.war").write_bytes(buf.getvalue())

    print("wrote examples to", EXAMPLES)


if __name__ == "__main__":
    main()
