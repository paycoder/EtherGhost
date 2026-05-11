#!/bin/bash

set -e

echo "Checking if PR modifies ether_ghost/ without adding tests..."

if ! git rev-parse --verify origin/main >/dev/null 2>&1; then
    echo "WARNING: Cannot find origin/main reference, skipping test check"
    exit 0
fi

# 统计ether_ghost/目录的非注释非空行修改数
src_changes=$(git diff --no-ext-diff --unified=0 origin/main HEAD -- ether_ghost/ 2>/dev/null | 
    awk '/^\+[^+]/{line=substr($0,2); if (line !~ /^\s*#/ && line !~ /^\s*$/) count++} END{print count+0}')

# 统计tests/目录是否有修改
tests_changed=$(git diff --name-only origin/main HEAD -- tests/ 2>/dev/null | wc -l)

echo "Lines changed in ether_ghost/ (excluding comments/blanks): $src_changes"
echo "Files changed in tests/: $tests_changed"

if [ "$src_changes" -ge 20 ] && [ "$tests_changed" -eq 0 ]; then
    echo "ERROR: PR modifies ether_ghost/ directory with $src_changes lines (>=10) but does not modify tests/ directory."
    echo "       Please add tests for the new functionality or changes."
    exit 1
fi

echo "SUCCESS: Test check passed."
exit 0