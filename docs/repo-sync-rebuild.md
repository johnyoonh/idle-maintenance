# Post-sync app rebuild

The executable `.repo-sync/post-sync` hook delegates to
`scripts/post_sync_rebuild.sh` after `repo sync` fast-forwards this checkout.

The hook builds into a temporary directory on the same filesystem, compiles and
signs the staged app, verifies the staged signature, and only then swaps it into
`$HOME/Applications/IdleMaintenance.app`. A compile, resource-copy, signing, or
verification failure leaves the previous installed app untouched.

A running menu-bar process is restarted with `SIGTERM` and `open -g` only after
the new bundle is installed. No `SIGKILL` is used. The hook prefers an existing
`CODESIGN_IDENTITY` or the first available Apple Development identity. It
refuses an automatic ad-hoc signature unless `IDLE_MAINTENANCE_ALLOW_ADHOC=1`
is explicitly set.

Logs are written to:

```text
$HOME/Library/Logs/idle-maintenance/post-sync-build.log
```

Preview local resolution without building:

```sh
./scripts/post_sync_rebuild.sh --dry-run
```
