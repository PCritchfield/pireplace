"""Parse YAML files to catch syntax errors when ansible is not installed."""

import sys

try:
    import yaml
except ImportError:
    print("SKIPPED: no yaml module available (YAML syntax not checked)")
    sys.exit(0)

status = 0
for path in sys.argv[1:]:
    try:
        with open(path) as f:
            yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"{path}: {e}")
        status = 1
    else:
        print(f"{path}: YAML parses")
sys.exit(status)
