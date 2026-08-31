#!/bin/sh

# Check our project: formatting, linting, testing, building, etc.
# Good to call this from .git/hooks/pre-commit

# Important: run with `uv run` to setup the environment

set -e

# Parse command line arguments
# --staged-only is useful to only run checks on the types of files that are staged for commit, speeding up pre-commit hooks
staged_only=false
for arg in "$@"; do
    case $arg in
        --staged-only)
            staged_only=true
            shift
            ;;
        *)
            echo "Unknown option: $arg"
            echo "Usage: $0 [--staged-only]"
            exit 1
            ;;
    esac
done

# work from the root of the repo
cd "$(dirname "$0")"

headerStart="\n\033[4;34m=== "
headerEnd=" ===\033[0m\n"

echo "${headerStart}Checking Python: uv run ruff check ${headerEnd}"
uv run ruff check

echo "${headerStart}Checking Python: uv run ruff format --check ${headerEnd}"
uv run ruff format --check src

echo "${headerStart}Checking Python Types: uv run ty check${headerEnd}"
uv run ty check

echo "${headerStart}Checking for Misspellings${headerEnd}"
if command -v misspell >/dev/null 2>&1; then
    find . -type f | grep -v "/node_modules/" | grep  -v "/\." | grep -v "/dist/" | grep -v "/desktop/build/" | grep -v "/app/web_ui/build/" | xargs misspell -error
    echo "No misspellings found"
else
    echo "\033[31mWarning: misspell command not found. Skipping misspelling check.\033[0m"
    echo "\033[31mTo install follow the instructions at https://github.com/golangci/misspell \033[0m"
fi

# Check if python files were changed, and run tests if so
echo "${headerStart}Running Python Tests${headerEnd}"
uv run python -m pytest -q -m 'not docker_integration' -n auto .
