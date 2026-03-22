# Changelog

## Final Version - Show Once Per Tab

### What Changed
**Before:** Suggestion appeared before EVERY prompt (as a reminder)
**After:** Suggestion appears ONCE when you open a new terminal tab, then silent

### Why
The repeated reminders were too noisy. Users found it disruptive to see the same message before every command.

### Implementation

Added flag check in `~/.zshrc`:

```bash
_maint_show_suggestion() {
    if [[ -z "$_MAINT_SHOWN" ]]; then
        export _MAINT_SHOWN=1
        ~/Dropbox/Mackup/scripts/idle-maintenance/prompt-suggest.py
    fi
}

precmd() {
    _maint_show_suggestion
}
```

### How It Works

1. New tab opens → `.zshrc` loads → `_MAINT_SHOWN` is unset
2. First `precmd()` fires → Check `_MAINT_SHOWN` → Empty → Show suggestion → Set flag
3. Next `precmd()` calls → Check `_MAINT_SHOWN` → Set to 1 → Silent

### Variables Used

- `MAINT_SESSION_ID` - Identifies this shell session (stable across prompts)
- `_MAINT_SHOWN` - Flag to prevent repeated display (set once per shell)

Both are environment variables that persist for the shell's lifetime.

### Testing

```bash
# Simulate new shell
unset _MAINT_SHOWN
source ~/.zshrc

# First call - shows suggestion
_maint_show_suggestion

# Second call - silent
_maint_show_suggestion
```

### Files Modified

1. **~/.zshrc**
   - Line 2-5: Set `MAINT_SESSION_ID`
   - Line 626-635: `_maint_show_suggestion()` function and `precmd()` hook

2. **prompt-suggest.py**
   - Uses `MAINT_SESSION_ID` from environment
   - Single-line display format
   - Skips encoding declarations
   - Bright visible colors

3. **~/.local/bin/maint**
   - Uses `MAINT_SESSION_ID` to clear session

### User Experience

**Open new tab:**
```
[Maint] Clean Homebrew cache $ brew cleanup | 1=Run 2=Del 3=Try 4=Skip → maint brew [1-4]

~ $
```

**Type commands:**
```
~ $ ls
file1.txt  file2.txt

~ $ pwd
/Users/john

~ $ echo "hello"
hello
```

No more maintenance messages! Clean prompt.

**To see suggestions again:** Open a new tab (Cmd+T) or run `exec zsh`

### Benefits

✅ Less noise - Shows once instead of every prompt
✅ Cleaner workflow - No repeated interruptions  
✅ Still effective - Reminds you when you open new tabs
✅ Fast - Flag check is instant (~0ms)

### Migration

Users with existing setup: Just open a new terminal tab. The new behavior activates automatically.

---

Version: Final
Date: 2025-03-21
