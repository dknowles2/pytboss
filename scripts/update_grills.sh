#!/usr/bin/env bash

function get_diff() {
	OLD=$(git show HEAD:pytboss/grills.json 2>/dev/null | jq -r 'keys[]' | sort)
	NEW=$(git show :pytboss/grills.json 2>/dev/null | jq -r 'keys[]' | sort)

	ADDED=$(comm -13 <(echo "$OLD") <(echo "$NEW"))
	REMOVED=$(comm -23 <(echo "$OLD") <(echo "$NEW"))

	if [[ -n "$ADDED" ]]; then
		echo "### Adds support for grills:"
		echo
		echo "$ADDED" | sed 's/^/* /'
		echo
	fi

	if [[ -n "$REMOVED" ]]; then
		echo "### Removes support for grills:"
		echo
		echo "$REMOVED" | sed 's/^/* /'
		echo
	fi
}

function get_commit_message() {
	echo "Update grill definitions"
	echo
	get_diff
}

python3 -m scripts.dump_grills >pytboss/grills.json
if [[ $? -ne 0 ]]; then
	exit $?
fi

git add pytboss/grills.json
git commit -m "$(get_commit_message)"
