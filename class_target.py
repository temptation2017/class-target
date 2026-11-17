#!/usr/bin/env python3
"""Show the Java bytecode target of .class files and JARs. Stdlib only."""

from __future__ import annotations

import argparse
import io
import json
import os
import struct
import sys
import zipfile
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Optional, Tuple

USAGE_EXIT = 2
FAIL_EXIT = 1

MAGIC = 0xCAFEBABE
MAX_NEST = 3
LIST_DEFAULT_LIMIT = 40

# Constant pool tags
CP_UTF8 = 1
CP_INTEGER = 3
CP_FLOAT = 4
CP_LONG = 5
CP_DOUBLE = 6
CP_CLASS = 7
CP_STRING = 8
CP_FIELDREF = 9
CP_METHODREF = 10
CP_INTERFACEMETHODREF = 11
CP_NAMEANDTYPE = 12
CP_METHODHANDLE = 15
CP_METHODTYPE = 16
CP_DYNAMIC = 17
CP_INVOKEDYNAMIC = 18
CP_MODULE = 19
CP_PACKAGE = 20

ACC_PUBLIC = 0x0001
ACC_FINAL = 0x0010
ACC_INTERFACE = 0x0200
ACC_ABSTRACT = 0x0400
ACC_SYNTHETIC = 0x1000
ACC_ANNOTATION = 0x2000
ACC_ENUM = 0x4000
ACC_MODULE = 0x8000

ARCHIVE_EXTS = {".jar", ".war", ".ear", ".zip"}
CLASS_EXT = ".class"

# Human labels for major versions older than the Java 5+ "major = 44 + N" rule.
LEGACY_MAJOR = {
    45: "Java 1.1",
    46: "Java 1.2",
    47: "Java 1.3",
    48: "Java 1.4",
}

MANIFEST_KEYS = (
    "Main-Class",
    "Start-Class",
    "Created-By",
    "Build-Jdk",
    "Build-Jdk-Spec",
    "Implementation-Title",
    "Implementation-Version",
    "Implementation-Vendor",
    "Specification-Version",
    "Multi-Release",
    "Premain-Class",
    "Agent-Class",
)


class Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.i = 0

    def need(self, n: int) -> None:
        if self.i + n > len(self.data):
            raise ValueError("truncated class file")

    def u1(self) -> int:
        self.need(1)
        v = self.data[self.i]
        self.i += 1
        return v

    def u2(self) -> int:
        self.need(2)
        v = struct.unpack_from(">H", self.data, self.i)[0]
        self.i += 2
        return v

    def u4(self) -> int:
        self.need(4)
        v = struct.unpack_from(">I", self.data, self.i)[0]
        self.i += 4
        return v

    def raw(self, n: int) -> bytes:
        self.need(n)
        b = self.data[self.i : self.i + n]
        self.i += n
        return b


def java_release(major: int) -> Optional[float]:
    if major < 45:
        return None
    if major <= 48:
        return {45: 1.1, 46: 1.2, 47: 1.3, 48: 1.4}[major]
    return float(major - 44)


def target_label(major: int, minor: int) -> Tuple[str, bool, Optional[float]]:
    preview = minor == 65535
    rel = java_release(major)
    if major in LEGACY_MAJOR:
        label = LEGACY_MAJOR[major]
    elif rel is not None and rel >= 5:
        label = "Java %d" % int(rel)
    else:
        label = "unknown (major %d)" % major
    if preview:
        label += " (preview)"
    elif minor not in (0, 65535):
        label += " (minor %d)" % minor
    return label, preview, rel


def kind_label(access: int, attr_names: List[str]) -> str:
    parts: List[str] = []
    if access & ACC_PUBLIC:
        parts.append("public")
    if access & ACC_FINAL and not (access & ACC_ENUM):
        parts.append("final")
    if access & ACC_ABSTRACT and not (access & ACC_INTERFACE):
        parts.append("abstract")
    if access & ACC_SYNTHETIC:
        parts.append("synthetic")
    if access & ACC_MODULE:
        noun = "module"
    elif access & ACC_ANNOTATION:
        noun = "annotation"
    elif access & ACC_ENUM:
        noun = "enum"
    elif access & ACC_INTERFACE:
        noun = "interface"
    elif "Record" in attr_names:
        noun = "record"
    else:
        noun = "class"
    parts.append(noun)
    return " ".join(parts)


def utf8_at(cp: List[Any], idx: int) -> Optional[str]:
    if idx < 1 or idx >= len(cp) or not cp[idx]:
        return None
    item = cp[idx]
    if item[0] != "utf8":
        return None
    return item[1]


def class_name_at(cp: List[Any], idx: int) -> Optional[str]:
    if idx < 1 or idx >= len(cp) or not cp[idx]:
        return None
    item = cp[idx]
    if item[0] != CP_CLASS:
        return None
    return utf8_at(cp, item[1])


def parse_constant_pool(r: Reader, count: int) -> List[Any]:
    cp: List[Any] = [None] * count
    i = 1
    while i < count:
        tag = r.u1()
        if tag == CP_UTF8:
            cp[i] = ("utf8", r.raw(r.u2()).decode("utf-8", "replace"))
        elif tag in (CP_INTEGER, CP_FLOAT):
            r.raw(4)
            cp[i] = (tag,)
        elif tag in (CP_LONG, CP_DOUBLE):
            r.raw(8)
            cp[i] = (tag,)
            i += 1
        elif tag in (CP_CLASS, CP_STRING, CP_METHODTYPE, CP_MODULE, CP_PACKAGE):
            cp[i] = (tag, r.u2())
        elif tag in (
            CP_FIELDREF,
            CP_METHODREF,
            CP_INTERFACEMETHODREF,
            CP_NAMEANDTYPE,
            CP_DYNAMIC,
            CP_INVOKEDYNAMIC,
        ):
            cp[i] = (tag, r.u2(), r.u2())
        elif tag == CP_METHODHANDLE:
            cp[i] = (tag, r.u1(), r.u2())
        else:
            raise ValueError("unknown constant pool tag %d" % tag)
        i += 1
    return cp


def skip_members(r: Reader, n: int) -> None:
    for _ in range(n):
        r.u2()
        r.u2()
        r.u2()
        attr_count = r.u2()
        for _a in range(attr_count):
            r.u2()
            r.raw(r.u4())


def read_attributes(r: Reader, n: int, cp: List[Any]) -> Tuple[List[str], Optional[str]]:
    names: List[str] = []
    source: Optional[str] = None
    for _ in range(n):
        name_i = r.u2()
        length = r.u4()
        info = r.raw(length)
        name = utf8_at(cp, name_i) or ""
        names.append(name)
        if name == "SourceFile" and length >= 2:
            si = struct.unpack_from(">H", info, 0)[0]
            source = utf8_at(cp, si)
    return names, source


def parse_class(data: bytes) -> Dict[str, Any]:
    if len(data) < 8:
        raise ValueError("truncated class file")
    r = Reader(data)
    magic = r.u4()
    if magic != MAGIC:
        raise ValueError("not a class file (magic 0x%08X)" % magic)
    minor = r.u2()
    major = r.u2()
    cp_count = r.u2()
    if cp_count < 1:
        raise ValueError("invalid constant pool count")
    cp = parse_constant_pool(r, cp_count)
    access = r.u2()
    this_i = r.u2()
    super_i = r.u2()
    iface_count = r.u2()
    for _ in range(iface_count):
        r.u2()
    skip_members(r, r.u2())  # fields
    skip_members(r, r.u2())  # methods
    attr_names, source = read_attributes(r, r.u2(), cp)
    label, preview, rel = target_label(major, minor)
    internal = class_name_at(cp, this_i) or "?"
    dotted = internal.replace("/", ".")
    return {
        "name": dotted,
        "internal": internal,
        "source_file": source,
        "kind": kind_label(access, attr_names),
        "major": major,
        "minor": minor,
        "target": label,
        "preview": preview,
        "release": rel,
        "access": access,
    }


def parse_manifest(text: str) -> "OrderedDict[str, str]":
    items: "OrderedDict[str, str]" = OrderedDict()
    key: Optional[str] = None
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line:
            key = None
            continue
        if line.startswith(" ") and key:
            items[key] += line[1:]
            continue
        if ":" not in line:
            key = None
            continue
        k, _, v = line.partition(":")
        key = k.strip()
        items[key] = v.lstrip()
    return items


def pick_manifest(items: "OrderedDict[str, str]") -> "OrderedDict[str, str]":
    picked: "OrderedDict[str, str]" = OrderedDict()
    for k in MANIFEST_KEYS:
        if k in items and items[k] != "":
            picked[k] = items[k]
    return picked


def is_multi_release(items: "OrderedDict[str, str]") -> bool:
    return items.get("Multi-Release", "").strip().lower() == "true"


def mr_release_from_entry(entry: str) -> Optional[int]:
    parts = entry.replace("\\", "/").split("/")
    # META-INF/versions/N/...
    for i, p in enumerate(parts):
        if p == "versions" and i >= 1 and parts[i - 1].upper() == "META-INF" and i + 1 < len(parts):
            try:
                n = int(parts[i + 1])
            except ValueError:
                return None
            if n >= 9:
                return n
    return None


def entry_is_class(name: str) -> bool:
    n = name.replace("\\", "/")
    if n.endswith("/") or n.startswith("META-INF/SIG-"):
        return False
    return n.lower().endswith(CLASS_EXT)


def entry_is_archive(name: str) -> bool:
    n = name.replace("\\", "/").lower()
    if n.endswith("/"):
        return False
    return os.path.splitext(n)[1] in ARCHIVE_EXTS


def default_nested_for(path: str, zip_names: Iterable[str]) -> bool:
    ext = os.path.splitext(path)[1].lower()
    names = [n.replace("\\", "/") for n in zip_names]
    if ext in {".war", ".ear"}:
        return True
    # WAR/EAR layout even when the file has no suffix (stdin)
    if any(
        (
            n.startswith("WEB-INF/lib/")
            or "/WEB-INF/lib/" in n
            or n.startswith("APP-INF/lib/")
        )
        and n.lower().endswith(".jar")
        for n in names
    ):
        return True
    return False


def iter_zip_items(
    data: bytes,
    source: str,
    nested: bool,
    depth: int = 0,
) -> Tuple[Optional["OrderedDict[str, str]"], List[Tuple[str, str, bytes]], List[str], bool]:
    """Return manifest, class payloads, warnings, and whether nested jars were skipped."""
    warnings: List[str] = []
    classes: List[Tuple[str, str, bytes]] = []
    manifest: Optional["OrderedDict[str, str]"] = None
    skipped_nested = False
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("not a zip/JAR (%s)" % exc)
    with zf:
        names = zf.namelist()
        if "META-INF/MANIFEST.MF" in names:
            try:
                raw = zf.read("META-INF/MANIFEST.MF")
                manifest = parse_manifest(raw.decode("utf-8", "replace"))
            except Exception:
                warnings.append("could not read META-INF/MANIFEST.MF")
        do_nested = nested or default_nested_for(source.split("!/")[-1], names)
        # If this is a Spring Boot jar and user didn't ask for nested, remember it
        if not nested and not do_nested:
            n_nested = sum(
                1
                for n in names
                if n.replace("\\", "/").startswith("BOOT-INF/lib/") and n.lower().endswith(".jar")
            )
            if n_nested:
                skipped_nested = True
                warnings.append(
                    "%d nested JARs under BOOT-INF/lib/ were not opened; pass --nested to include them"
                    % n_nested
                )
        for info in zf.infolist():
            name = info.filename
            if info.is_dir():
                continue
            if entry_is_class(name):
                try:
                    classes.append((source, name, zf.read(info)))
                except Exception as exc:
                    warnings.append("could not read %s: %s" % (name, exc))
            elif (do_nested or nested) and depth < MAX_NEST and entry_is_archive(name):
                try:
                    inner = zf.read(info)
                    inner_source = source + "!/" + name
                    man, inner_classes, inner_warns, _skip = iter_zip_items(
                        inner, inner_source, True, depth + 1
                    )
                    classes.extend(inner_classes)
                    warnings.extend(inner_warns)
                except Exception as exc:
                    warnings.append("could not open nested %s: %s" % (name, exc))
    return manifest, classes, warnings, skipped_nested


def rel_sort_key(rel: Optional[float], major: int, minor: int) -> Tuple[int, float, int, int]:
    if rel is None:
        return (1, 0.0, major, minor)
    return (0, rel, major, minor)


def collect_class_record(source: str, entry: Optional[str], data: bytes) -> Dict[str, Any]:
    rec = parse_class(data)
    rec["source"] = source
    rec["entry"] = entry
    rec["mr_release"] = mr_release_from_entry(entry) if entry else None
    rec["error"] = None
    return rec


def failed_class(source: str, entry: Optional[str], err: str) -> Dict[str, Any]:
    return {
        "source": source,
        "entry": entry,
        "name": None,
        "internal": None,
        "source_file": None,
        "kind": None,
        "major": None,
        "minor": None,
        "target": None,
        "preview": False,
        "release": None,
        "mr_release": mr_release_from_entry(entry) if entry else None,
        "error": err,
    }


def build_input_report(
    source: str,
    kind: str,
    classes: List[Dict[str, Any]],
    manifest: Optional["OrderedDict[str, str]"],
    extra_warnings: List[str],
) -> Dict[str, Any]:
    warnings = list(extra_warnings)
    ok_classes = [c for c in classes if not c.get("error")]
    bad = [c for c in classes if c.get("error")]
    for c in bad:
        loc = c.get("entry") or source
        warnings.append("unreadable class %s: %s" % (loc, c["error"]))

    counts: "OrderedDict[str, int]" = OrderedDict()
    for c in sorted(ok_classes, key=lambda x: rel_sort_key(x["release"], x["major"] or 0, x["minor"] or 0)):
        counts[c["target"]] = counts.get(c["target"], 0) + 1

    mr = is_multi_release(manifest or OrderedDict())
    versioned = [c for c in ok_classes if c.get("mr_release")]
    base = [c for c in ok_classes if not c.get("mr_release")]

    if mr and not versioned and ok_classes:
        warnings.append("manifest declares Multi-Release: true, but no classes under META-INF/versions/")
    if versioned and not mr:
        warnings.append(
            "found classes under META-INF/versions/ but the manifest does not declare Multi-Release: true"
        )

    def unique_targets(layer: List[Dict[str, Any]]) -> List[str]:
        seen: List[str] = []
        for c in sorted(layer, key=lambda x: rel_sort_key(x["release"], x["major"] or 0, x["minor"] or 0)):
            if c["target"] not in seen:
                seen.append(c["target"])
        return seen

    def layer_mix(layer: List[Dict[str, Any]], label: str) -> None:
        targets = unique_targets(layer)
        if len(targets) > 1:
            warnings.append("mixed bytecode targets in %s: %s" % (label, ", ".join(targets)))

    def mix_archive(classes_in: List[Dict[str, Any]], default_label: str) -> None:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for c in classes_in:
            grouped.setdefault(c.get("source") or source, []).append(c)
        for src, group in grouped.items():
            if "!/" in src:
                label = src.split("!/", 1)[1]
            else:
                label = default_label
            base_g = [c for c in group if not c.get("mr_release")]
            ver_g = [c for c in group if c.get("mr_release")]
            if ver_g:
                layer_mix(base_g, "the base layer of %s" % label if label != default_label else "the base layer")
                by_rel: Dict[int, List[Dict[str, Any]]] = {}
                for c in ver_g:
                    by_rel.setdefault(c["mr_release"], []).append(c)
                for rel, g in sorted(by_rel.items()):
                    layer_mix(g, "META-INF/versions/%d" % rel)
                    for c in g:
                        if c["release"] is not None and int(c["release"]) != rel and c["release"] >= 5:
                            warnings.append(
                                "%s is under META-INF/versions/%d but its target is %s"
                                % (c.get("entry") or c["name"], rel, c["target"])
                            )
            else:
                layer_mix(group, label)

    if kind == "class":
        mix_archive(ok_classes, "this file")
    else:
        mix_archive(ok_classes, "this archive")

    for c in ok_classes:
        if c["preview"]:
            warnings.append("%s uses preview bytecode (%s)" % (c.get("entry") or c["name"], c["target"]))

    if kind != "class" and not ok_classes and not bad:
        warnings.append("archive contains no .class files")

    picked = pick_manifest(manifest) if manifest else OrderedDict()
    return {
        "source": source,
        "kind": kind,
        "manifest": picked,
        "counts": counts,
        "classes": classes,
        "warnings": warnings,
        "class_count": len(ok_classes),
        "error": None,
    }


def inspect_class_bytes(source: str, data: bytes) -> Dict[str, Any]:
    try:
        rec = collect_class_record(source, None, data)
        return build_input_report(source, "class", [rec], None, [])
    except Exception as exc:
        return {
            "source": source,
            "kind": "class",
            "manifest": OrderedDict(),
            "counts": OrderedDict(),
            "classes": [failed_class(source, None, str(exc))],
            "warnings": ["unreadable class %s: %s" % (source, exc)],
            "class_count": 0,
            "error": str(exc),
        }


def inspect_archive(source: str, data: bytes, nested: bool) -> Dict[str, Any]:
    try:
        manifest, items, warns, _skipped = iter_zip_items(data, source, nested)
    except Exception as exc:
        return {
            "source": source,
            "kind": "archive",
            "manifest": OrderedDict(),
            "counts": OrderedDict(),
            "classes": [],
            "warnings": [],
            "class_count": 0,
            "error": str(exc),
        }
    classes: List[Dict[str, Any]] = []
    for src, entry, raw in items:
        try:
            rec = collect_class_record(src, entry, raw)
            # If the class lives in a nested archive, keep that source
            rec["source"] = src
            classes.append(rec)
        except Exception as exc:
            classes.append(failed_class(src, entry, str(exc)))
    kind = "archive"
    ext = os.path.splitext(source.split("!/")[0])[1].lower()
    if ext:
        kind = ext.lstrip(".")
    return build_input_report(source, kind, classes, manifest, warns)


def inspect_path(path: str, nested: bool, recursive: bool) -> List[Dict[str, Any]]:
    if path == "-":
        data = sys.stdin.buffer.read()
        # sniff zip vs class
        if data.startswith(b"PK"):
            return [inspect_archive("-", data, nested)]
        return [inspect_class_bytes("-", data)]
    if not os.path.exists(path):
        return [
            {
                "source": path,
                "kind": "missing",
                "manifest": OrderedDict(),
                "counts": OrderedDict(),
                "classes": [],
                "warnings": [],
                "class_count": 0,
                "error": "path does not exist",
            }
        ]
    if os.path.isdir(path):
        return inspect_directory(path, nested, recursive)
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as f:
        data = f.read()
    if ext in ARCHIVE_EXTS or data.startswith(b"PK"):
        return [inspect_archive(path, data, nested)]
    return [inspect_class_bytes(path, data)]


def inspect_directory(path: str, nested: bool, recursive: bool) -> List[Dict[str, Any]]:
    reports: List[Dict[str, Any]] = []
    if recursive:
        walker = os.walk(path)
        files: List[str] = []
        for root, _dirs, names in walker:
            for n in names:
                files.append(os.path.join(root, n))
    else:
        files = [os.path.join(path, n) for n in sorted(os.listdir(path))]
        files = [p for p in files if os.path.isfile(p)]
    chosen = []
    for p in files:
        ext = os.path.splitext(p)[1].lower()
        if ext == CLASS_EXT or ext in ARCHIVE_EXTS:
            chosen.append(p)
    chosen.sort()
    if not chosen:
        reports.append(
            {
                "source": path,
                "kind": "directory",
                "manifest": OrderedDict(),
                "counts": OrderedDict(),
                "classes": [],
                "warnings": [],
                "class_count": 0,
                "error": "directory has no .class / .jar / .war / .ear / .zip files",
            }
        )
        return reports
    for p in chosen:
        reports.extend(inspect_path(p, nested, recursive))
    return reports


def parse_java_spec_clean(value: str) -> float:
    s = value.strip().lower()
    if s.startswith("java"):
        s = s[4:].lstrip()
    mapping = {
        "1.1": 1.1,
        "1.2": 1.2,
        "1.3": 1.3,
        "1.4": 1.4,
        "1.5": 5.0,
        "1.6": 6.0,
        "1.7": 7.0,
        "1.8": 8.0,
    }
    if s in mapping:
        return mapping[s]
    try:
        n = float(s) if "." in s else int(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("invalid Java version %r" % value) from exc
    return float(n)


def out_of_range(rec: Dict[str, Any], min_v: Optional[float], max_v: Optional[float]) -> Optional[str]:
    rel = rec.get("release")
    if rel is None:
        return "unknown Java release (major %s)" % rec.get("major")
    if min_v is not None and rel < min_v:
        return "target %s is below --min %s" % (rec["target"], format_rel(min_v))
    if max_v is not None and rel > max_v:
        return "target %s is above --max %s" % (rec["target"], format_rel(max_v))
    return None


def format_rel(rel: float) -> str:
    if rel < 5:
        return "Java " + ("%s" % rel)
    return "Java %d" % int(rel)


def flatten_warnings(reports: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for r in reports:
        src = r["source"]
        if r.get("error"):
            out.append("%s: %s" % (src, r["error"]))
        for w in r.get("warnings") or []:
            out.append("%s: %s" % (src, w) if not w.startswith(src) else w)
    return out


def report_to_json(reports: List[Dict[str, Any]], range_warnings: List[str], ok: bool) -> Dict[str, Any]:
    def clean_class(c: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "source": c.get("source"),
            "entry": c.get("entry"),
            "name": c.get("name"),
            "source_file": c.get("source_file"),
            "kind": c.get("kind"),
            "major": c.get("major"),
            "minor": c.get("minor"),
            "target": c.get("target"),
            "preview": c.get("preview"),
            "release": c.get("release"),
            "mr_release": c.get("mr_release"),
            "error": c.get("error"),
        }

    inputs = []
    total = 0
    counts: "OrderedDict[str, int]" = OrderedDict()
    for r in reports:
        total += r.get("class_count") or 0
        for k, v in (r.get("counts") or {}).items():
            counts[k] = counts.get(k, 0) + v
        inputs.append(
            {
                "source": r["source"],
                "kind": r["kind"],
                "manifest": r.get("manifest") or {},
                "counts": r.get("counts") or {},
                "class_count": r.get("class_count") or 0,
                "classes": [clean_class(c) for c in r.get("classes") or []],
                "warnings": r.get("warnings") or [],
                "error": r.get("error"),
            }
        )
    # stable count order by release if possible
    return {
        "inputs": inputs,
        "summary": {"files": len(reports), "classes": total, "targets": counts},
        "warnings": flatten_warnings(reports) + range_warnings,
        "ok": ok,
    }


def pad(label: str, width: int = 16) -> str:
    return (label + ":").ljust(width + 1)


def format_human(reports: List[Dict[str, Any]], list_all: bool, range_warnings: List[str]) -> str:
    blocks: List[str] = []
    for r in reports:
        lines: List[str] = []
        src = r["source"]
        kind = r["kind"]
        if r.get("error") and not r.get("classes"):
            lines.append("%s  %s" % (pad("Source"), src))
            lines.append("%s  %s" % (pad("Error"), r["error"]))
            blocks.append("\n".join(lines))
            continue
        n = r.get("class_count") or 0
        if kind == "class":
            lines.append("%s  %s" % (pad("Source"), src))
        else:
            lines.append("%s  %s (%s, %d class%s)" % (pad("Source"), src, kind, n, "" if n == 1 else "es"))
        man = r.get("manifest") or {}
        if man:
            lines.append("Manifest")
            for k, v in man.items():
                lines.append("  %s  %s" % (k.ljust(24), v))
        counts = r.get("counts") or {}
        if len(counts) >= 1:
            lines.append("Versions")
            width = max(len(k) for k in counts)
            for k, v in counts.items():
                lines.append("  %s  %d" % (k.ljust(width), v))
        ok_classes = [c for c in (r.get("classes") or []) if not c.get("error")]
        show = ok_classes if list_all or len(ok_classes) <= LIST_DEFAULT_LIMIT else None
        if ok_classes and (show is not None or kind == "class"):
            lines.append("Classes")
            to_show = ok_classes if show is not None else ok_classes[:LIST_DEFAULT_LIMIT]
            name_w = max((len(_class_display(c)) for c in to_show), default=1)
            tgt_w = max((len(c["target"] or "") for c in to_show), default=1)
            for c in to_show:
                extra = []
                if c.get("kind"):
                    extra.append(c["kind"])
                if c.get("source_file"):
                    extra.append(c["source_file"])
                if c.get("mr_release"):
                    extra.append("MR %d" % c["mr_release"])
                suffix = "  ".join(extra)
                lines.append(
                    "  %s  %s%s"
                    % (
                        _class_display(c).ljust(name_w),
                        (c["target"] or "?").ljust(tgt_w),
                        ("  " + suffix) if suffix else "",
                    )
                )
            if show is None and len(ok_classes) > LIST_DEFAULT_LIMIT:
                lines.append(
                    "  … and %d more (pass --list to print every class)"
                    % (len(ok_classes) - LIST_DEFAULT_LIMIT)
                )
        warns = list(r.get("warnings") or [])
        lines.append("Warnings")
        if warns:
            for w in warns:
                lines.append("  - %s" % w)
        else:
            lines.append("  (none)")
        blocks.append("\n".join(lines))

    if len(reports) > 1:
        summary_counts: "OrderedDict[str, int]" = OrderedDict()
        total = 0
        for r in reports:
            total += r.get("class_count") or 0
            for k, v in (r.get("counts") or {}).items():
                summary_counts[k] = summary_counts.get(k, 0) + v
        lines = ["Summary", "  %s  %d files, %d classes" % (pad("Inputs", 8), len(reports), total)]
        if summary_counts:
            width = max(len(k) for k in summary_counts)
            for k, v in summary_counts.items():
                lines.append("  %s  %d" % (k.ljust(width + 2), v))
        blocks.append("\n".join(lines))

    if range_warnings:
        lines = ["Range"]
        for w in range_warnings:
            lines.append("  - %s" % w)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def _class_display(c: Dict[str, Any]) -> str:
    name = c.get("name")
    src = c.get("source") or ""
    if "!/" in src:
        nested = src.split("!/", 1)[1]
        return "%s!/%s" % (nested, name or c.get("entry") or "?")
    if c.get("mr_release") and name:
        return "META-INF/versions/%d/%s" % (c["mr_release"], name)
    return name or c.get("entry") or src or "?"


def range_issues(reports: List[Dict[str, Any]], min_v: Optional[float], max_v: Optional[float]) -> List[str]:
    if min_v is None and max_v is None:
        return []
    issues: List[str] = []
    for r in reports:
        for c in r.get("classes") or []:
            if c.get("error"):
                continue
            msg = out_of_range(c, min_v, max_v)
            if msg:
                loc = c.get("entry") or c.get("name") or r["source"]
                if c.get("source") and c["source"] != r["source"]:
                    loc = "%s!/%s" % (c["source"], c.get("entry") or c.get("name"))
                issues.append("%s: %s" % (loc, msg))
    return issues


def should_fail(reports: List[Dict[str, Any]], range_warnings: List[str], strict: bool) -> bool:
    if range_warnings:
        return True
    if not reports:
        return True
    if all(r.get("error") and not r.get("class_count") for r in reports):
        return True
    if strict:
        for r in reports:
            if r.get("error") or r.get("warnings"):
                return True
    return False


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="class-target",
        description="Show the Java bytecode target of .class files and JARs (local, stdlib only).",
    )
    p.add_argument(
        "paths",
        nargs="*",
        help=" .class / .jar / .war / .ear / .zip / directory, or - for stdin",
    )
    p.add_argument("--json", action="store_true", help="machine-readable JSON")
    p.add_argument("--list", action="store_true", help="list every class, even in large archives")
    p.add_argument(
        "--nested",
        action="store_true",
        help="also open JARs nested inside a JAR/WAR/EAR (always on for .war/.ear)",
    )
    p.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="when a path is a directory, scan it recursively",
    )
    p.add_argument(
        "--min",
        dest="min_ver",
        type=parse_java_spec_clean,
        metavar="VER",
        help="fail if any class targets a Java release below VER (e.g. 8 or 1.8)",
    )
    p.add_argument(
        "--max",
        dest="max_ver",
        type=parse_java_spec_clean,
        metavar="VER",
        help="fail if any class targets a Java release above VER (e.g. 8 or 17)",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 when there are warnings, unreadable classes, or mixed targets",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.paths:
        parser.print_help(sys.stderr)
        return USAGE_EXIT
    reports: List[Dict[str, Any]] = []
    for path in args.paths:
        reports.extend(inspect_path(path, nested=args.nested, recursive=args.recursive))
    issues = range_issues(reports, args.min_ver, args.max_ver)
    fail = should_fail(reports, issues, args.strict)
    if args.json:
        payload = report_to_json(reports, issues, not fail)
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(format_human(reports, list_all=args.list, range_warnings=issues))
    return FAIL_EXIT if fail else 0


if __name__ == "__main__":
    sys.exit(main())
