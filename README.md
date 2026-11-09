# Enshrouded Map Tools

Generate the Embervale world background and one flattened image for each of the
six Hollow Halls from your own supported game installation. The tool prepares
images and descriptions for manual wiki upload. Generation works offline and
does not change game files or request account credentials.

[Download v0.1.0 for Windows x64](https://github.com/ConfoundedHermit/enshrouded-map-tools/releases/download/v0.1.0/EnshroudedMaps-0.1.0-Windows-x64.zip)
· [Release notes](https://github.com/ConfoundedHermit/enshrouded-map-tools/releases/tag/v0.1.0)
· [Generation and manual upload guide](docs/manual-upload.md)

## Requirements

- Windows x64 and Python 3.13.
- An unmodified **Enshrouded client build 26430625**. This first release verifies
  58 executable, index and archive files against its supported profile. A matching
  executable with modified archives is rejected. Dedicated-server inputs and
  other builds are unsupported.
- For the world map, a Vulkan device and driver passing the helper's capability
  checks. Hollow Halls generation does not require the world renderer.
- A separate output folder with space for extraction caches and images.

The release ZIP includes the pinned parser and prebuilt native helpers; map
contributors do not need a compiler or Python packages from PyPI. The GitHub
source ZIP requires the [maintainer build steps](docs/BUILDING.md) before use.

## Generate maps

Extract the complete release ZIP, then run `EnshroudedMaps.cmd` for prompts.
Alternatively, open a terminal in the extracted package:

```powershell
python -B -m enshrouded_data.map_tool generate `
  --game "C:\Program Files (x86)\Steam\steamapps\common\Enshrouded" `
  --output "C:\MapExports\Enshrouded-26430625" `
  --maps all --world-layout both
```

Use your own paths and a new or empty output folder outside the game directory.

| Option | Result |
| --- | --- |
| `--maps halls` | Springlands, Revelwood, Nomad Highlands, Kindlewastes, Albaneve Summits and Veilwater Basin: one PNG per hall |
| `--maps world` | World background only |
| `--maps all` | All six hall maps and the world background |
| `--world-layout single` | One lossless world PNG |
| `--world-layout tiles` | 64 lossless PNG tiles and 64 wiki JPEG derivatives |
| `--world-layout both` | Single PNG and both tile formats |
| `--world-pixels 2560` | Default: 2,560 pixels per tile side; 20,480-pixel world master |
| `--world-pixels 1280` | Optional lower native density; the world bounds stay the same |

Open the output's `index.html` and review the images before uploading. The
`manual-upload.json` manifest lists exact files, suggested wiki filenames,
descriptions and size eligibility. The tested world master was **188,561,736
bytes**, above the configured **15 MiB** upload limit; keep it locally and use
the JPEG tiles for the wiki background. The tool never resizes images to fit.

Verify a completed output with:

```powershell
python -B -m enshrouded_data.map_tool verify --output "C:\MapExports\Enshrouded-26430625"
```

To resume an interrupted run, add `--resume` to the original generation command
and keep the same package, paths and options. Completed stages are checked
before reuse. Some incomplete extraction or geometry stages need a fresh output
folder; preserve their diagnostics when reporting a problem.

## Validation and development

The 0.1.0 runtime passed 44 focused tests and a clean-folder generation of all
six halls, 64 native world tiles, the stitched world master and 64 JPEGs. Hall
pixels match the accepted maps. The world master preserves the native tile
pixels, and the JPEGs pass full-pixel quality and encoded-header checks.
[Validation coverage and limits](docs/VALIDATION.md) includes browser checks,
modified-input rejection and optional-density coverage.

```powershell
python -B -m unittest discover -s tests -p "test_map_tool*.py"
```

See [Building from source](docs/BUILDING.md) for dependency pins, native
compilation and release packaging, and [Release automation](docs/AUTOMATION.md)
for GitHub Actions builds and automatic release attachments. No game archives,
extracted game resources or generated game images are included in the source or
release package. Future game updates require a reviewed build profile and
renderer validation; changing the profile's build number alone does not add
support.

## License and credits

Original project code and documentation are available under the [MIT
License](LICENSE). The separately executed
[kfc-parser](https://github.com/Brabb3l/kfc-parser) utility retains its GPL-3.0
license, corresponding source and upstream notices in the release's
`third-party/` folder. Vulkan-Headers retains its upstream license. The MIT
license does not cover Enshrouded game assets or third-party components.

Enshrouded is developed by Keen Games. This is a community tool.
