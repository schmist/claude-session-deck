#!/usr/bin/env bash
# Build the JetBrains plugin against an installed IDE, with that IDE's own Java
# compiler, so nothing needs to be downloaded.
# usage: build.sh <IDE home> <plugins directory>
# The result is <plugins directory>/claude-session-deck/lib/claude-session-deck.jar.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
[ $# -eq 2 ] || { echo "usage: build.sh <IDE home> <plugins directory>" >&2; exit 2; }
ide=$1 plugins=$2

javac="$ide/jbr/bin/javac"
[ -x "$javac" ] || javac=$(command -v javac) || { echo "no Java compiler found" >&2; exit 1; }

build=$(mktemp -d)
trap 'rm -rf "$build"' EXIT
classpath=$(find "$ide/lib" "$ide/plugins/terminal/lib" -name '*.jar' | paste -sd:)
"$javac" -nowarn -cp "$classpath" -d "$build" "$here"/src/ch/claudesessiondeck/jetbrains/*.java
cp -r "$here/resources/META-INF" "$build/"
(cd "$build" && python3 -m zipfile -c plugin.jar ch META-INF)
install -Dm644 "$build/plugin.jar" "$plugins/claude-session-deck/lib/claude-session-deck.jar"
