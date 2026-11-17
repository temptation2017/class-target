# class-target

零依赖 Python CLI：查看 `.class` 文件、JAR / WAR / EAR 实际编译到的 Java 字节码版本。不调用 `javap`，也不需要安装 JDK。

## 做什么

拿到一个 JAR 时，最常见的问题是：它到底是给 Java 8 用的，还是已经编到了 17 / 21？清单里的 `Build-Jdk` 只说明构建机，不一定等于字节码目标；一个包里还可能混进多种版本，或多版本 JAR（Multi-Release）。`class-target` 直接读 class 文件头和常量池，在本机给出目标版本、类型、源文件名和清单摘要。

它会：

- 读取 `.class` 的 major / minor，转换成 Java 1.1–1.4 或 Java 5+ 的发行版名称
- 标出 preview 字节码（minor = 65535）
- 解析 JAR / WAR / EAR / ZIP：统计各版本数量，列出类名、类型和 `SourceFile`
- 读取 `META-INF/MANIFEST.MF` 里的 `Main-Class`、`Build-Jdk`、`Multi-Release` 等常见字段
- 识别多版本 JAR（`META-INF/versions/N/`），并检查该目录下的类是否真的针对 N
- 默认打开 WAR / EAR 里的 `WEB-INF/lib`（或 `APP-INF/lib`）嵌套 JAR；普通 Spring Boot fat JAR 的 `BOOT-INF/lib` 需加 `--nested`
- 扫描目录（可选 `-r` 递归）；也支持 stdin
- `--min` / `--max` 用来卡住目标版本范围（例如生产必须是 Java 8）
- `--json` 便于脚本处理；`--strict` 在有告警、混版、无法解析时以状态码 1 退出

## 要求

- Python 3.8+
- 仅标准库（`argparse` / `json` / `struct` / `zipfile` 等）

## 用法

```bash
# 查看仓库里的示例 class（Java 8）
python3 class_target.py examples/hello-8.class

# 查看多版本 JAR：基座是 Java 8，versions/17 里有一份 Java 17 的 Api
python3 class_target.py examples/demo.jar

# 混版 JAR 会给出告警；再加上版本上限，超出版本会以状态码 1 退出
python3 class_target.py examples/mixed.jar --max 8

# WAR 会一并打开 WEB-INF/lib 里的 JAR
python3 class_target.py examples/demo.war

# 目录扫描、机器可读 JSON、要求全部落在 Java 8–11
python3 class_target.py build/ -r --json --min 8 --max 11

# 从 stdin 读一个 class 或 JAR
python3 class_target.py - < examples/hello-8.class
```

`examples/` 里是虚构的演示 class 和归档（包名 `com.example` / 模块名 `demo.mod`），可用 `python3 examples_build.py` 重新生成。它们不是真实业务代码，也不能用来访问任何服务。

## 示例

`python3 class_target.py examples/hello-8.class`：

```
Source:            examples/hello-8.class
Versions
  Java 8  1
Classes
  com.example.Hello  Java 8  public class  Hello.java
Warnings
  (none)
```

`python3 class_target.py examples/demo.jar`：

```
Source:            examples/demo.jar (jar, 3 classes)
Manifest
  Main-Class                com.example.Demo
  Created-By                example-build 1.0
  Build-Jdk                 17.0.12
  Implementation-Title      demo
  Implementation-Version    1.0.0-demo
  Multi-Release             true
Versions
  Java 8   2
  Java 17  1
Classes
  com.example.Demo                      Java 8   public class  Demo.java
  com.example.Api                       Java 8   public class  Api.java
  META-INF/versions/17/com.example.Api  Java 17  public class  Api.java  MR 17
Warnings
  (none)
```

对混版归档再加 `--max 8` 时，会同时看到混版告警和范围失败（退出码 1）：

```
Warnings
  - mixed bytecode targets in this archive: Java 8, Java 17

Range
  - com/example/Api.class: target Java 17 is above --max Java 8
```

自测：

```bash
python3 test_class_target.py
```

## 限制

- 只读字节码目标版本和少量元数据，不反汇编、不验证字节码是否可被 JVM 加载
- 不解析依赖树，也不根据 `Class-Path` 去磁盘上找相邻 JAR（嵌套归档除外）
- 对尚未公布的更高 major，会按 `major - 44` 推断发行版号；无法识别时标为 unknown
- 超大 fat JAR 加上 `--nested --list` 时，输出会很长；默认最多列出 40 个类，其余用 `--list` 展开
- 需要本地已有 class / JAR 文件，本工具不联网下载

## 后续可改进

- 读取 `module-info` 的模块名以外的 `requires` / `exports`
- 对比 `Build-Jdk` 与实际最高字节码版本是否一致
- 对 Spring Boot fat JAR 默认给出 `BOOT-INF/classes` 与 `BOOT-INF/lib` 的分别汇总
- 输出 HTML 一页报告，方便贴到代码评审里

## License

MIT
