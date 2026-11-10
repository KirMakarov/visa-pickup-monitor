"""Check the German Embassy in Belgrade's current visa pickup list once."""

import argparse
import sys

from visa_pickup_monitor.monitoring import main

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    sys.exit(main())
