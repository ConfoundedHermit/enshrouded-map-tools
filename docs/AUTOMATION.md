# Automated Windows builds and releases

The [Build Windows release workflow](https://github.com/ConfoundedHermit/enshrouded-map-tools/actions/workflows/release.yml) compiles,
tests and packages the Windows x64 tool when a GitHub release is published. It
then attaches these files to that release:

- `EnshroudedMaps-VERSION-Windows-x64.zip`
- `SHA256SUMS.txt`

The release tag must be `vX.Y.Z` and match the `__version__` committed in
[`enshrouded_data/map_tool/__init__.py`](../enshrouded_data/map_tool/__init__.py).
For example, version `0.1.1` requires tag `v0.1.1`. The workflow builds the tagged
commit; it does not rewrite version numbers, move tags or create a release.

## Publish a new release

1. Update `__version__`, review the change and merge the intended source. Keep
   supported game builds and validation limits current in the release notes.
2. Run the maintainer's [release acceptance checks](BUILDING.md#release-acceptance)
   where renderer, profile or runtime changes need actual game and GPU coverage.
3. On GitHub, choose **Releases > Draft a new release**. Create the matching tag
   against the intended commit, or choose an existing matching tag. That commit
   must contain the workflow and automation code.
4. Write the release notes and choose **Publish release**. A published release
   or pre-release starts **Build Windows release**. Saving a draft does not.
5. Open **Actions** and wait for both the build and upload jobs to succeed.
   Confirm that the ZIP and checksum appear under the release's assets before
   announcing the download.

The release page is visible while the build runs, so assets appear after
publication rather than immediately. The initial `v0.1.0` tag predates this
workflow and its original downloads are preserved. New releases should use
new version tags containing the automation changes.

## Build without publishing

Open **Actions > Build Windows release > Run workflow**, choose the source
branch or tag, then run it. A manual run builds the checked-out version and
retains the resulting files as an Actions artifact for 14 days. It does not
attach files to a release or create one. Download the artifact from the
successful run's summary page. Its outer artifact ZIP contains the distributable
ZIP, checksum and internal release manifest; contributors should receive the
inner distributable ZIP.

The same automation can be run locally from a clean source checkout with
Python 3.13, Git and the Windows C++ toolchain described in the
[build guide](BUILDING.md):

```powershell
python -B -m enshrouded_data.map_tool.automation `
  --tag v0.1.0 --output dist --dependencies .dependencies
```

Replace the example tag with the checked-out version and choose a fresh output
folder. This command acquires and verifies the pinned dependencies, builds
helpers, runs tests, packages the tool and checks the extracted package's CLI.
The separate `portable_build` commands remain available for entirely manual
dependency preparation and offline compilation.

## Inputs, checks and permissions

The build uses the `windows-2025` hosted runner, Python 3.13, the runner's MSVC
toolchain and Windows SDK, and the dependencies pinned in
[`build-dependencies.json`](../enshrouded_data/map_tool/build-dependencies.json).
It fetches the pinned parser, its corresponding source and Vulkan-Headers,
checks their identities, and retains the required third-party notices and
source. A Vulkan SDK installation is unnecessary for compilation. Build
receipts record the actual selected compiler, SDK, source and binary hashes.

Before downloading dependencies or creating build output, automation checks that
every entry in the shared `PACKAGE_SOURCE_FILES` inventory is a regular file in
the captured source commit. The package builder copies only those listed project
files, so ignored local runtime data and draft documents cannot enter a release.

CI does not obtain game archives or render full game maps. It runs focused
tests, including the freshly compiled native JPEG helper, and verifies the
extracted package, launcher and supported profiles. Actual client extraction,
Vulkan rendering, visual review and game-build compatibility remain the
maintainer's release acceptance work.

The build job has read access to repository content. Only the separate release
upload job has `contents: write`; it uses GitHub's temporary `GITHUB_TOKEN`.
No personal access token, game credentials or wiki credentials are required.
The uploader checks asset names, sizes and SHA-256 hashes, and confirms that the
release still points at the built commit before uploading. An existing asset
with the same server-reported SHA-256 and size is verified and skipped. A
different or unverifiable existing asset stops the upload; existing downloads
are never overwritten.

## Failures and GitHub settings

If a build fails, inspect its Actions logs. Correct
the source and prepare a new version if the published tag is already accepted
or used by contributors. Re-running a job should use the same unchanged tag;
it is not a way to replace an existing download.

This upload-after-publication flow requires a release that permits adding
assets. If [immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases)
are enabled, build the intended release tag with a manual workflow run, attach
the resulting ZIP and checksum to a draft for that same tag, then publish the
fully prepared release. GitHub locks immutable release assets on publication.

A release published by another workflow using its `GITHUB_TOKEN` does not
start a new release workflow. Publish from the GitHub UI or your own account,
or run the build manually and upload its output. This is GitHub's
[workflow recursion rule](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow#triggering-a-workflow-from-a-workflow).
The workflow uses the
[`published` release event](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#release)
so publication from a draft and publication as a pre-release are both covered.
