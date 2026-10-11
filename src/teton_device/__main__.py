# Allows `python -m teton_device`; delegates to cli.main().

from teton_device.cli import main

raise SystemExit(main())
