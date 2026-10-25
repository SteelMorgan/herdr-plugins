# Herdr extensions workstation overlay

Patch from the clean upstream revision in `ops/upstream-versions.json` to the
installed local scripts and generated manifest. It preserves our Linux/absolute
path adjustments and executable discovery. This includes installation rendering,
not just handwritten changes; review it when moving to a different user/host.
The upstream checkout itself is clean. Its installer regenerates the manifest,
so preserve this overlay separately and recheck it after upgrading.

Validate with `git apply --check /path/to/installed-workstation.patch` in a clean
checkout of that exact upstream revision. Do not apply it blindly to the running
installation. Upstream source/license remains at the recorded GitHub repository;
this repository does not vendor the editor or other compiled binaries.
