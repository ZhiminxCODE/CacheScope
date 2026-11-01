#!/bin/sh
set -eu
case "${1:---help}" in
    inspect|--version) exec /usr/local/bin/cachescope "$@" ;;
    prepare|read-range|evict|validate|benchmark) exec python3 -m cachescope_runner "$@" ;;
    --help|-h)
        /usr/local/bin/cachescope --help
        printf '\nExperiment commands (use COMMAND --help for details):\n'
        python3 -m cachescope_runner --help
        ;;
    *) exec "$@" ;;
esac
