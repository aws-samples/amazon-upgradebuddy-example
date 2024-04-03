#!/bin/zsh

# Require Munkipkg

if ! command -v munkipkg &>/dev/null; then
    echo "munkipkg could not be found but is required for building the package"
    exit 1
fi

dir=$(dirname ${0:A})

version=$(/usr/bin/awk -F'= ' '/__version__/{gsub(/\"/,"");print $2;}' "$dir/upgradeBuddy.py")
cp "$dir/upgradeBuddy.py" "$dir/pkg/payload"
/usr/libexec/PlistBuddy -c "Set :version $version" "$dir/pkg/build-info.plist"
chmod 755 "$dir/pkg/payload/upgradeBuddy.py"
munkipkg "$dir/pkg"
