# Third-party notices

The input-assembly logic in `src/go1loc/localization.py::_CachedClusterSolver`
is adapted from `hloc/localize_sfm.py::pose_from_cluster` in
[Hierarchical Localization (hloc)](https://github.com/cvg/Hierarchical-Localization),
pinned revision `c13273bd0ecc2917a35910fd843712a1c6243193`.

That upstream code is licensed under the Apache License, Version 2.0; a copy is
included at `third_party_licenses/hloc-Apache-2.0.txt`.
Local modifications introduce per-query reuse of input reads and index assembly;
they preserve the upstream coordinate convention, correspondence order, estimator
calls, and evidence-index relationships. This does not imply upstream endorsement.

This notice covers that adapted logic, not redistribution of the upstream
repository, other upstream third-party projects, or model weights. Dependencies
and weights retain their respective licenses and are not included here.
