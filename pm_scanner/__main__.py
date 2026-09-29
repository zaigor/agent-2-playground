import faulthandler

from .cli import main

faulthandler.enable()  # print a Python traceback if the interpreter crashes (e.g. segfault)

raise SystemExit(main())
