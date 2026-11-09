# Generate and upload map images

Use the [Windows x64 release ZIP](https://github.com/ConfoundedHermit/enshrouded-map-tools/releases/download/v0.1.0/EnshroudedMaps-0.1.0-Windows-x64.zip)
and follow these steps to generate images locally and upload them manually.
The [source and release notes](https://github.com/ConfoundedHermit/enshrouded-map-tools/releases/tag/v0.1.0)
identify the generator version and supported client build.

The tool reads a supported Enshrouded client installation and generates the
world background, the six Hollow Halls maps, or both. It does not update points
of interest, quest records or other interactive markers. It makes no network
requests, asks for no wiki credentials and does not change the game files.

## Requirements

- Windows x64 and Python 3.13.
- The extracted contributor package, including its pinned parser, prebuilt
  helpers, supported-build profiles and integrity manifest. Contributors do not
  need a compiler or this research workspace.
- A supported, unmodified client installation. The first profile is client build
  **26430625**. An executable version alone does not establish compatibility:
  the tool verifies the executable, resource index and game archives against the
  profile. Dedicated-server input is not supported by this release.
- A compatible Vulkan device and driver for world rendering. The native helper
  checks its device requirements and reports unsupported hardware locally.
- A separate output folder with room for extraction caches, the requested images
  and validation records. The output folder must not contain the game folder or
  be inside it.

If a mod changes the supported game archives, select a separate unmodified game
copy. The generator does not repair or remove mods. Future game updates can
require a new profile and renderer validation.

## Generate maps

Extract the contributor ZIP into its own folder. Run `EnshroudedMaps.cmd` for the
interactive prompts: game folder, output folder, map selection and world layout.

From a terminal in the extracted package, the equivalent command is:

```powershell
python -B -m enshrouded_data.map_tool generate `
  --game "C:\Program Files (x86)\Steam\steamapps\common\Enshrouded" `
  --output "C:\MapExports\Enshrouded-26430625" `
  --maps all --world-layout both
```

Use paths appropriate to your machine. Choose a new or empty output folder.

| Option | Meaning |
| --- | --- |
| `--maps all` | World background and all six hall images |
| `--maps halls` | Six flattened Hollow Halls images |
| `--maps world` | World background only |
| `--world-layout single` | One lossless world PNG master |
| `--world-layout tiles` | Lossless PNG tiles and wiki JPEG derivatives |
| `--world-layout both` | Master, PNG tiles and wiki JPEG derivatives |
| `--world-pixels 2560` | Default: 2,560 × 2,560 pixels per world tile |
| `--world-pixels 1280` | Optional lower native rendering density |

At the default density, the world master is **20,480 × 20,480 pixels**. The tool
renders 64 blocks and assembles their decoded RGB rows into one PNG without
allocating the full image in memory. The master and lossless tiles preserve the
same pixels. It never shrinks an image to meet an upload limit.

Every world tile covers **1,280 × 1,280 world units**, regardless of pixel density.
Filenames count rows southward from the north: `0_0` is northwest. Tile column
`c`, row `r` keeps DataMaps position `[c,7-r]` and X/Y bounds
`[[1280*c,1280*(7-r)],[1280*(c+1),1280*(8-r)]]`. Do not calculate world bounds from
the PNG width.

Hollow Halls output is one flattened PNG per hall: Springlands, Revelwood, Nomad
Highlands, Kindlewastes, Albaneve Summits and Veilwater Basin. Upper and lower
surfaces share their original horizontal position, with the lower details
blended into the same map. North is up, the map scale is 2 pixels per meter, and
the scale bar represents 50 meters. Overlapping passages at different heights
may not connect. The static geometry does not show every object or changing
obstacle and does not establish a safe route through the dungeon.

## Review and verify

Open `index.html` in the chosen output folder. Review all six hall images and,
when selected, the world gallery. Check boundaries, seams, orientation, labels
and the areas affected by the game update before uploading anything.

| Output | Purpose |
| --- | --- |
| `run.json` | Completed run status, input request, timings and output hashes |
| `inputs.json` | Supported-build input verification |
| `manual-upload.json` | Exact local files, suggested wiki filenames, descriptions and file-size eligibility |
| `halls/` | Flattened hall PNGs, geometry/render receipts and descriptions |
| `world/master/Embervale.png` | Full-resolution lossless world master, when requested |
| `world/tiles/` | Lossless world PNG tiles, when requested |
| `world/wiki/` | Quality-100, 4:4:4 JPEG tile derivatives, when requested |
| `world/quality/` | Native full-pixel JPEG comparison metrics and actual JPEG header checks |
| `cache/` | Retained extraction and generation intermediates for the local run |

The PNG checks validate every chunk CRC and every decoded RGB pixel against the
renderer or assembly hash. JPEG creation keeps the native dimensions, verifies
quality-100 quantization and 4:4:4 sampling in the encoded header, and records the
native helper's decoded-pixel comparison. A successful structural or pixel check
does not replace visual review or gameplay verification.

To check that a completed delivery's files still match its recorded hashes:

```powershell
python -B -m enshrouded_data.map_tool verify `
  --output "C:\MapExports\Enshrouded-26430625"
```

List the package's supported client profiles with:

```powershell
python -B -m enshrouded_data.map_tool profiles
```

After an interruption, rerun the original `generate` command with `--resume`.
Keep the same game folder, package and options. Completed receipts and file
hashes are checked before reuse. Changed completed files or different options
stop the run instead of silently accepting a different result. An interrupted
descriptor extraction without a completion receipt requires a fresh output
folder; retain its diagnostics when reporting the problem.

## Upload manually

The generator prepares files and descriptions; it does not publish them. Use
your own wiki account and review the current wiki state before each upload.

1. Confirm the run completed and open `manual-upload.json`. Only use files marked
   `upload_eligible: true`, then check the wiki's current size and image-processing
   limits. This release compares encoded size against **15 MiB per file**.
2. For an existing image, open its file page and choose **Upload a new version**.
   Match the manifest's filename exactly and preserve the current description
   unless you deliberately intend to update it. Review the new image against
   the current one.
3. For a new image, use the upload form and the corresponding generated
   `descriptions/*.wikitext` text after review. 
4. The existing hall filenames are `<Hall name> Map.png`. The existing world JPEG
   filenames are `c_r.jpg`, with rows counted from the north. Reusing those
   filenames preserves existing wiki references and tile positions.
5. If you choose different filenames, update the relevant article or world
   background definition deliberately. A protected map definition may require
   an administrator. A new PNG filename does not replace an existing JPEG merely
   because the images have the same coordinates.
6. Check every uploaded file, the hall articles, and the rendered world map.
   Retain your local validation and upload records so another contributor can
   identify which build and generator produced the images.

The tested 20,480 × 20,480 world master is 188,561,736 bytes and exceeds the configured 15 MiB limit. Keep it
as a local master and use the tile output for the interactive wiki map. An
oversized master is marked ineligible; the tool does not silently lower quality
or resample it. If a tile also exceeds the configured limit, stop and review
the delivery rather than treating that file as upload-ready.

## Reporting problems

Keep `failure.txt`, `run.json`, input verification and the relevant parser or
native-helper log. State the generator version, client build, selected options
and failing stage. Avoid distributing game archives, extracted game content or
private account files. A clean output folder and a supported unmodified client
are the baseline for reproducing a failure.

See [validation coverage](VALIDATION.md) and [building from source](BUILDING.md)
for the tested behavior, dependency pins and maintainer release checks.
