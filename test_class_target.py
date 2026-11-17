#!/usr/bin/env python3
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import class_target
import examples_build

ROOT = Path(__file__).resolve().parent
EXAMPLES = ROOT / "examples"


def run(argv):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = class_target.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


class ClassTargetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (EXAMPLES / "hello-8.class").exists():
            examples_build.main()

    def test_hello8_human_and_source_file(self):
        code, out, err = run([str(EXAMPLES / "hello-8.class")])
        self.assertEqual(code, 0, err)
        self.assertIn("com.example.Hello", out)
        self.assertIn("Java 8", out)
        self.assertIn("public class", out)
        self.assertIn("Hello.java", out)
        self.assertIn("(none)", out)
        self.assertEqual(err, "")

    def test_hello8_json(self):
        code, out, err = run([str(EXAMPLES / "hello-8.class"), "--json"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertTrue(data["ok"])
        rec = data["inputs"][0]["classes"][0]
        self.assertEqual(rec["name"], "com.example.Hello")
        self.assertEqual(rec["target"], "Java 8")
        self.assertEqual(rec["major"], 52)
        self.assertEqual(rec["minor"], 0)
        self.assertEqual(rec["source_file"], "Hello.java")
        self.assertFalse(rec["preview"])
        self.assertEqual(data["warnings"], [])

    def test_hello17(self):
        code, out, err = run([str(EXAMPLES / "hello-17.class"), "--json"])
        self.assertEqual(code, 0, err)
        rec = json.loads(out)["inputs"][0]["classes"][0]
        self.assertEqual(rec["major"], 61)
        self.assertEqual(rec["target"], "Java 17")

    def test_preview_warns_but_exits_zero(self):
        path = str(EXAMPLES / "preview.class")
        code, out, err = run([path])
        self.assertEqual(code, 0, err)
        self.assertIn("Java 25 (preview)", out)
        self.assertIn("preview bytecode", out)
        code, out, _ = run([path, "--strict"])
        self.assertEqual(code, 1)
        self.assertIn("preview bytecode", out)

    def test_module_info(self):
        code, out, err = run([str(EXAMPLES / "module-info.class")])
        self.assertEqual(code, 0, err)
        self.assertIn("demo.mod", out)
        self.assertIn("module", out)
        self.assertIn("Java 17", out)

    def test_multi_release_jar(self):
        code, out, err = run([str(EXAMPLES / "demo.jar")])
        self.assertEqual(code, 0, err)
        self.assertIn("Multi-Release             true", out)
        self.assertIn("Main-Class                com.example.Demo", out)
        self.assertIn("META-INF/versions/17/com.example.Api", out)
        self.assertIn("Java 8", out)
        self.assertIn("Java 17", out)
        self.assertIn("(none)", out)

    def test_mixed_jar_strict(self):
        path = str(EXAMPLES / "mixed.jar")
        code, out, err = run([path])
        self.assertEqual(code, 0, err)
        self.assertIn("mixed bytecode targets in this archive: Java 8, Java 17", out)
        code, _, _ = run([path, "--strict"])
        self.assertEqual(code, 1)

    def test_war_opens_nested_lib(self):
        code, out, err = run([str(EXAMPLES / "demo.war"), "--json"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        names = {c["name"] for c in data["inputs"][0]["classes"]}
        self.assertEqual(names, {"com.example.Demo", "com.example.lib.Helper"})
        targets = sorted(
            (c["name"], c["target"]) for c in data["inputs"][0]["classes"] if not c.get("error")
        )
        self.assertIn(("com.example.Demo", "Java 8"), targets)
        self.assertIn(("com.example.lib.Helper", "Java 11"), targets)
        # Nested lib vs web classes is not treated as a mixed-archive warning
        self.assertTrue(all("mixed bytecode" not in w for w in data["inputs"][0]["warnings"]))

    def test_min_max(self):
        path = str(EXAMPLES / "hello-17.class")
        code, out, err = run([path, "--min", "8", "--max", "17"])
        self.assertEqual(code, 0, err)
        code, out, _ = run([path, "--max", "8"])
        self.assertEqual(code, 1)
        self.assertIn("above --max Java 8", out)
        code, out, _ = run([str(EXAMPLES / "hello-8.class"), "--min", "11"])
        self.assertEqual(code, 1)
        self.assertIn("below --min Java 11", out)
        # 1.8 alias
        code, out, err = run([str(EXAMPLES / "hello-8.class"), "--max", "1.8"])
        self.assertEqual(code, 0, err)

    def test_invalid_file_fails(self):
        code, out, err = run([str(EXAMPLES / "not-a-class.bin")])
        self.assertEqual(code, 1)
        self.assertIn("not a class file", out)

    def test_missing_path(self):
        code, out, err = run([str(EXAMPLES / "no-such-file.class")])
        self.assertEqual(code, 1)
        self.assertIn("path does not exist", out)

    def test_no_args_usage(self):
        code, out, err = run([])
        self.assertEqual(code, 2)
        self.assertIn("usage:", err.lower())

    def test_directory_and_recursive(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "hello-8.class").write_bytes((EXAMPLES / "hello-8.class").read_bytes())
            sub = d / "nested"
            sub.mkdir()
            (sub / "hello-17.class").write_bytes((EXAMPLES / "hello-17.class").read_bytes())
            code, out, err = run([str(d)])
            self.assertEqual(code, 0, err)
            self.assertIn("Java 8", out)
            self.assertNotIn("Java 17", out)
            code, out, err = run([str(d), "-r"])
            self.assertEqual(code, 0, err)
            self.assertIn("Java 8", out)
            self.assertIn("Java 17", out)
            self.assertIn("Summary", out)

    def test_stdin_class(self):
        data = (EXAMPLES / "hello-8.class").read_bytes()
        old = class_target.sys.stdin
        try:
            class_target.sys.stdin = type(old)()  # won't work
        except Exception:
            pass
        # Use a buffer wrapper
        class_target.sys.stdin = type("S", (), {"buffer": io.BytesIO(data)})()
        try:
            code, out, err = run(["-"])
        finally:
            class_target.sys.stdin = old
        self.assertEqual(code, 0, err)
        self.assertIn("com.example.Hello", out)
        self.assertIn("Java 8", out)

    def test_parse_class_helper_roundtrip(self):
        raw = examples_build.make_class("a/b/C", 65, 0, source="C.java")
        rec = class_target.parse_class(raw)
        self.assertEqual(rec["name"], "a.b.C")
        self.assertEqual(rec["target"], "Java 21")
        self.assertEqual(rec["source_file"], "C.java")
        self.assertEqual(rec["major"], 65)


if __name__ == "__main__":
    unittest.main()
