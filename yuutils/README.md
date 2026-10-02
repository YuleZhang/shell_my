# yuutils

`yuutils` is the independently released shell and device-tooling bundle from
[`shell_my`](https://github.com/YuleZhang/shell_my). It packages the
independently versioned `adb-sync` script together with the drop-in `adb`
front end, benchmark helpers, stack parsing utilities, and the shared `myenv`
setup scripts and configuration.

## Install and inspect

```bash
pip install yuutils --extra-index-url http://INDEX-HOST:8080/simple/ \
    --trusted-host INDEX-HOST
yuutils
adb-sync --version
```

`adb-sync` is a yuutils script feature: it is installed and invoked as part
of this bundle. Its source repository, version, tests, and standalone
release remain independent. The copy in this package is the script
snapshot selected for the yuutils release; `yuutils` owns only the bundle
version.

The installed command list is generated from `yuutils_manifest.py`. The
`myenv/zsh` scripts are installed as `install_zsh_env.sh` and
`test_install_zsh_env.sh`; the `myenv/omp` YAML files are installed under
`yuutils-support/myenv/omp`.

## Drop-in adb front end

```bash
source "$(adb-sync-env)"
adb push --mirror input_dir/* /data/local/tmp/app/input
source "$(adb-sync-env)" off
```

The shim delegates push operations to the packaged `adb-sync` script and
passes other adb commands through to the real adb. Set `ADB_SYNC_SCRIPT` when
using a separately installed adb-sync executable.

## Release

Bump `VERSION` in `setup.py`, update the packaged adb-sync snapshot when its
independent release changes, then run:

```bash
scripts/serve-pip-index.sh --build-only
```

Use `scripts/pip-index-ctl.sh restart` when the local pip index is managed by
the background controller.
