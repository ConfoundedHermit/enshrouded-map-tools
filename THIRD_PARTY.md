# Third-party components

The MIT license at the repository root covers the first-party map tool. Bundled
third-party components retain their upstream notices and licenses.

- **kfc-parser v0.2.0**, commit `16708594d686b030d909f2d835b199d879ea9ad8`:
  [upstream](https://github.com/Brabb3l/kfc-parser), GPL-3.0-or-later as declared
  by its Cargo package. The release contains the pinned executable, exact source
  ZIP including Cargo.lock, and [GPL license](third-party/kfc-parser-LICENSE.txt).
- **Vulkan-Headers v1.3.280**: [upstream](https://github.com/KhronosGroup/Vulkan-Headers/tree/v1.3.280).
  The required C headers declare Apache-2.0. Their
  [upstream license index](third-party/Vulkan-Headers-LICENSE.md) and the full
  [Apache 2.0 text](third-party/Apache-2.0.txt) are retained.

Python, MSVC, Windows libraries and the graphics driver's Vulkan loader are
provided by the contributor's system. Their installers are not bundled.
Dependency identities and required-file hashes are recorded in
`enshrouded_data/map_tool/build-dependencies.json`.
