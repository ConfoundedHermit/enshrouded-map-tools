# Building from source

These steps build the native helpers and a self-contained contributor package
from a source checkout. Map contributors can use the prebuilt release instead.
The runtime and builder use the Python standard library; no `pip install` is
required. Dependency acquisition is manual. Neither builder command downloads
tools or game content.

For GitHub-hosted builds and automatic release attachments, see
[Automated Windows builds and releases](AUTOMATION.md). That separate automation
command downloads the same pinned dependencies before calling this builder.

## Toolchain

- Windows x64 and Python 3.13 x64.
- Visual Studio C++ Build Tools with the x64 MSVC toolchain and a Windows 10 or
  Windows 11 SDK. Run from an x64 Developer PowerShell or Developer Command
  Prompt, or allow the builder to discover Visual Studio using `vswhere`.
- Git if preparing the parser's corresponding source archive yourself.
- The pinned parser binary, parser source archive and Vulkan-Headers described
  below. A complete Vulkan SDK is not required for compilation. World rendering
  uses the Vulkan runtime supplied by the contributor's graphics driver.

The builder records the selected compiler, SDK, commands and input/output hashes
in its receipts. Compiler versions may produce different executable bytes;
supporting a toolchain does not imply byte-for-byte builds across toolchains.

## Prepare pinned dependencies

Work from the source checkout root. Create `.dependencies/`, which is ignored by
Git. The complete pins, URLs and required Vulkan header hashes are in
[`build-dependencies.json`](../enshrouded_data/map_tool/build-dependencies.json).

| Input | Local path | Pinned identity |
| --- | --- | --- |
| kfc-parser v0.2.0 | `.dependencies/kfc-parser.exe` | SHA-256 `b5384a926885a0b59da0a2475ff1fd1c2a0679156e2dbcdb52d1899aabc85b75` |
| kfc-parser corresponding source ZIP | `.dependencies/kfc-parser-source.zip` | Commit `16708594d686b030d909f2d835b199d879ea9ad8`; ZIP SHA-256 `3cb57e467f0aa3313859a779f6fdf37705239693d95251dfe22aa5bb821edcd1` |
| Vulkan-Headers v1.3.280 | `.dependencies/Vulkan-Headers-1.3.280/` | Archive SHA-256 `0b8fc45aa970825b74ee588b233d7773fb12efe1df1f79e830e7c49d6c6fc8ce` |

Download the parser from its
[upstream v0.2.0 release](https://github.com/Brabb3l/kfc-parser/releases/download/v0.2.0/kfc-parser.exe).
The contributor release includes the exact corresponding source ZIP at
`third-party/kfc-parser-source.zip`; copy it to the path above. Alternatively,
prepare it from the [upstream repository](https://github.com/Brabb3l/kfc-parser)
using Git:

```powershell
git clone https://github.com/Brabb3l/kfc-parser.git .dependencies/kfc-parser-source
git -C .dependencies/kfc-parser-source archive --format=zip `
  --output=../kfc-parser-source.zip 16708594d686b030d909f2d835b199d879ea9ad8
```

Download the
[Vulkan-Headers v1.3.280 source archive](https://codeload.github.com/KhronosGroup/Vulkan-Headers/zip/refs/tags/v1.3.280)
and verify its SHA-256 before extraction. Its top-level folder is
`Vulkan-Headers-1.3.280`; extract it under `.dependencies/` without an extra
nesting level. Keep the upstream license files. The builder checks the actual
required header and license hashes, in addition to the pinned archive identity
documented above.

Use PowerShell to inspect a downloaded file's hash:

```powershell
Get-FileHash .dependencies/kfc-parser.exe -Algorithm SHA256
```

The package command rejects a parser binary or source archive that differs
from the pin. Do not replace pins merely to bypass a mismatch. Dependency
updates need review, new receipts and appropriate runtime validation.

## Compile helpers

Use a fresh output folder:

```powershell
python -B -m enshrouded_data.map_tool.portable_build helpers `
  --output build/helpers `
  --vulkan-headers .dependencies/Vulkan-Headers-1.3.280
```

If automatic discovery does not choose the intended toolchain, use
`--compiler "C:\path\to\Hostx64\x64\cl.exe"`,
`--sdk-root "C:\Program Files (x86)\Windows Kits\10"` and
`--sdk-version VERSION`. Check command help for the exact options supported by
the checked-out version:

```powershell
python -B -m enshrouded_data.map_tool.portable_build helpers --help
```

The output contains `hollow-geometry.exe`, `world-renderer.exe`,
`map-jpeg.exe`, build logs and `build.json`. The helpers use the static MSVC
runtime. The Vulkan helper loads the system Vulkan runtime during generation.

## Test and package

Run the focused tests with the freshly built JPEG helper enabled:

```powershell
$env:ENSHROUDED_MAP_JPEG_HELPER = (Resolve-Path build/helpers/map-jpeg.exe).Path
python -B -m unittest discover -s tests -p "test_map_tool*.py"
```

Create a fresh distributable folder and ZIP:

```powershell
python -B -m enshrouded_data.map_tool.portable_build package `
  --output dist/EnshroudedMaps-0.1.0-Windows-x64 `
  --helpers build/helpers `
  --parser .dependencies/kfc-parser.exe `
  --parser-source .dependencies/kfc-parser-source.zip `
  --vulkan-headers .dependencies/Vulkan-Headers-1.3.280 `
  --archive dist/EnshroudedMaps-0.1.0-Windows-x64.zip
```

The package retains the MIT license, third-party notices, the parser's complete
pinned source ZIP, helper build receipts and a `bundle.json` integrity manifest.
No game installation or extracted game resources are needed for packaging.
Existing destinations are preserved; choose a fresh path for another build.

Both builders copy project source and documents only from the explicit
`PACKAGE_SOURCE_FILES` inventory in
[`portable_build.py`](../enshrouded_data/map_tool/portable_build.py). Add an entry
when adding a runtime module, supported profile or contributor document that
belongs in the package. Files outside the inventory, including ignored local
credentials, extraction data and draft documents, are excluded. Missing listed
files, symbolic links and directory junctions stop packaging before the output
folder is created. The offline builder does not require Git metadata; automated
releases also require every listed source to be a committed regular file.

## Release acceptance

Extract the final ZIP into a separate folder and run it from there with
`PYTHONPATH` unset. Test launcher help and list profiles, then generate maps from
an unmodified supported client into a new output folder:

```powershell
.\EnshroudedMaps.cmd --help
python -B -m enshrouded_data.map_tool profiles
python -B -m enshrouded_data.map_tool generate `
  --game "C:\Games\Enshrouded" --output "C:\MapExports\ReleaseCheck" `
  --maps all --world-layout both
python -B -m enshrouded_data.map_tool verify --output "C:\MapExports\ReleaseCheck"
```

Review the actual rendered images and upload eligibility. Check the supported
profile, package source closure, license/source archive closure and absence of
private or game-derived content. Retain generation, verification and build
receipts. Publish the ZIP and a SHA-256 checksum with release notes stating the
tested build and material limits. See [0.1.0 validation](VALIDATION.md) for the
initial acceptance coverage.
