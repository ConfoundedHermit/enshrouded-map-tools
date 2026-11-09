# Contributing

Use an issue to describe a reproducible rendering or input-validation problem.
Include the generator version, client build, selected options, failing stage
and relevant local error message. Inspect logs before sharing them. Do not
attach game archives, extracted resources, account files or credentials.

The [build guide](docs/BUILDING.md) describes the pinned dependencies, helper
compilation and packaging commands. Run the focused tests for a code change and
include the relevant validation results in your pull request. Native renderer
or game-profile changes also need actual supported-client generation and
review of orientation, seams, hall coverage and output pixels.

A new game build needs a separately verified unmodified input baseline, fresh
reflection/descriptors and validation of the fields, geometry, UI data and
shaders used by the generator. Do not bypass hash checks or copy a profile with
only its build number changed. Keep game resources and generated images out of
the repository.

Original contributions are made under the repository's MIT license. Preserve
the separate licenses and source requirements of third-party components.
