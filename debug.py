import utime

_LOG_FILE  = "/sd/debug.log"
_enabled   = False


def enable() -> None:
    global _enabled
    _enabled = True


def log(msg: str) -> None:
    line = "{:10d} {}\n".format(utime.ticks_ms(), msg)
    print(line, end="")
    if _enabled:
        try:
            with open(_LOG_FILE, "a") as f:
                f.write(line)
        except OSError:
            pass
