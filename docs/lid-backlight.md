# Lid backlight guard

This optional per-user LaunchAgent sets only the built-in display brightness to
zero when macOS reports the lid closed. It restores the saved brightness when
the lid opens, unless macOS or the user has already raised it. External displays,
system sleep policy, applications, and window focus are untouched.

The watcher checks every two seconds, including while the lid stays closed, so
ambient brightness changes are corrected. It saves the restore level before
zeroing the backlight and retains it across watcher restarts. A file lock prevents
duplicate watcher processes. Unknown lid state or a missing built-in display
causes no action; API errors are logged. Invalid saved state fails closed instead
of guessing a restore level.

It uses macOS DisplayServices, a private API also used by Hammerspoon. macOS
updates may change that API. This helper does not make a Mac stay awake: it is
intended for a Mac whose sleep policy already permits closed-lid work. It requires
the system Python 3 and a logged-in user session.

Install or update this watcher only:

```sh
/usr/bin/python3 scripts/install_lid_backlight.py
```

Read current hardware state without changing it:

```sh
/usr/bin/python3 scripts/lid_backlight.py --status
```

The LaunchAgent label is `com.john.idle-maintenance.lid-backlight`. Runtime code
is installed under `~/Library/Scripts/idle-maintenance/`, state under
`~/Library/Application Support/idle-maintenance/lid-backlight/`, and logs under
`~/Library/Logs/idle-maintenance/lid-backlight.log`.

To disable, open the lid first and wait for brightness restoration, then run:

```sh
launchctl bootout "gui/$(id -u)/com.john.idle-maintenance.lid-backlight"
rm ~/Library/LaunchAgents/com.john.idle-maintenance.lid-backlight.plist
```

Physical acceptance: close the lid and confirm the backlight is dark; reopen and
confirm the previous brightness returns. API readback proves the requested
brightness level, while physical observation is needed to confirm emitted light.
