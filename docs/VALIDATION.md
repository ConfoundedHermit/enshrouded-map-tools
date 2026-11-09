# Validation coverage for 0.1.0

The original 0.1.0 runtime acceptance was completed on Windows x64 with Python
3.13 and an unmodified Enshrouded client build 26430625. These results describe
that supported input and tested machine; they do not establish support for
other client builds, dedicated servers or every graphics device.

## Clean-folder generation

The release candidate was extracted into a separate folder containing spaces
and run there with `PYTHONPATH` removed. It used only the package and game
installation, with no research exports or previously extracted resources.
Launcher help, `generate --maps all --world-layout both`, offline `verify` and
completed-run `--resume` all passed. The package was unchanged after running.

| Check | Recorded result |
| --- | --- |
| Input integrity | 58 files, 51,763,995,208 bytes hashed |
| Fresh descriptor extraction | 14,092 decoded descriptors; zero raw or failed resources |
| Hall maps | Six PNGs; exact accepted decoded RGB hashes |
| Hall source coverage | 3,201,793 floor samples and 56 landmarks retained |
| Native world tiles | 64 PNGs, each 2,560 × 2,560 pixels |
| World master | 20,480 × 20,480 pixels; 188,561,736 bytes |
| World assembly | Lossless assembly of the same decoded native tile rows |
| Wiki derivatives | 64 quality-100, 4:4:4 JPEGs with original dimensions |
| JPEG pixel comparison | Minimum PSNR 51.4453 dB; all configured error limits pass |
| Size eligibility | Six halls and 64 JPEGs eligible at 15 MiB; master excluded |
| Independent delivery audit | All 297 delivery files passed |
| Side effects | No network requests or game writes |

The extraction selects 14,084 resource identities. The eight additional
descriptors are valid indexed scene chunks included by the parser's complete
type filter. Selection and decoding counts are checked separately.

The generator validates every PNG chunk CRC and decoded RGB hash. During
generation, the stitched master receives a complete decoded-pixel check.
The independent audit checks its hash-bound assembly receipt and header without
repeating the full 1.2 GB decoded image pass. JPEG validation includes sampling
mode and quantization-header checks and native full-pixel comparison.

## Additional checks

- **44 focused tests:** input selection, output/path guards, complete and
  interrupted delivery behavior, PNG decompression bounds, seams and
  orientation, hall selection/rendering, world profiles and the actual native
  JPEG helper.
- **Modified-input rejection:** a client with the supported executable and a
  changed resource index was rejected before extraction with exit code 2.
- **Optional density:** one actual 1,280-pixel native world tile passed full
  CRC/RGB validation and 32 independently derived orientation samples. Full
  clean-folder generation used the default 2,560-pixel density.
- **Browser painting:** all six hall PNGs and 64 world tiles passed actual
  browser painting and natural-dimension checks, including 1,750 screenshot
  pixel samples. The large master was linked without being fetched. No browser
  exceptions or external HTTP requests occurred.
- **Package inspection:** candidate contents, integrity hashes and exclusion of
  game/private content were checked. The public release has its own checksum;
  documentation, license and portable build additions change the archive bytes.

## Interpretation limits

The hall maps combine surfaces at their original horizontal positions. Stacked
passages may overlap without connecting. Static geometry and placed controls do
not replay live collision, changing obstacles or gameplay state.

World generation refreshes the background image only. It does not update
interactive points of interest, quests, loot or other map markers. The tested
Vulkan device passed the helper's capability probe; other devices must pass
their own checks.

Check the current wiki's upload limits before publishing. The tool's configured
15 MiB threshold is local metadata and is not a live query of wiki settings.
