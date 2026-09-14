"""The program the installers build: gout's command line, with its own Python inside."""
import sys

from gout.cli import main

if __name__ == "__main__":
    sys.exit(main())
