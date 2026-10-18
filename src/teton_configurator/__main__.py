# Allows `python -m teton_configurator`; delegates to cli.main().

from teton_configurator.cli import main

raise SystemExit(main())
