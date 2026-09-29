#!/bin/bash
# Render marketing/architecture.svg to PNG (LinkedIn needs a raster).
cd "$(dirname "$0")"
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu \
  --hide-scrollbars --window-size=1600,900 --screenshot=architecture.png "file://$PWD/architecture.svg" 2>/dev/null
echo "wrote $PWD/architecture.png"
