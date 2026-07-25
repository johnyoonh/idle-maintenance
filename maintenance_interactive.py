"""Compatibility entrypoint with resource-aware process review installed."""
import maintenance_core as _core
from process_review import install as _install
_install(_core)
for _name, _value in vars(_core).items():
    if not _name.startswith("__"):
        globals()[_name] = _value
if __name__ == "__main__":
    raise SystemExit(_core.main())
