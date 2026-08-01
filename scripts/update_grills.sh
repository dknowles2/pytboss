#!/usr/bin/env bash

function get_diff() {
	local old new
	old="$(mktemp)"
	new="$(mktemp)"
	# shellcheck disable=SC2064
	trap "rm -f '$old' '$new'" RETURN

	git show HEAD:pytboss/grills.json >"$old" 2>/dev/null || echo '{}' >"$old"
	git show :pytboss/grills.json >"$new" 2>/dev/null || echo '{}' >"$new"

	python3 -m scripts.grills_diff "$old" "$new"
}

function get_commit_message() {
	echo "Update grill definitions"
	echo
	get_diff
}

# Dump to a temporary file first. Redirecting straight onto grills.json
# truncates it before the script runs, so any mid-run failure would leave the
# checked-in definitions empty.
TMP_JSON="$(mktemp)"
trap 'rm -f "$TMP_JSON"' EXIT

if ! python3 -m scripts.dump_grills >"$TMP_JSON"; then
	echo "dump_grills failed; leaving pytboss/grills.json untouched" >&2
	exit 1
fi

mv "$TMP_JSON" pytboss/grills.json

git add pytboss/grills.json
git commit -m "$(get_commit_message)"
