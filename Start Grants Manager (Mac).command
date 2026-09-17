#!/bin/bash
cd "$(dirname "$0")"
if [ ! -f server.py ]; then
  echo
  echo "  This looks like it was opened from inside the .zip."
  echo "  Double-click GrantsManager.zip to extract it first,"
  echo "  open the extracted folder, then double-click this again."
  echo
  read -n 1 -s -r -p "Press any key to close..."
  echo; exit 1
fi
python3 server.py --launch
if [ $? -eq 0 ]; then exit; fi
echo
echo "  Couldn't start Python 3."
echo "  If macOS just offered to install 'command line developer"
echo "  tools', click Install, wait for it to finish, then"
echo "  double-click this file again - that installs Python"
echo "  for you, no App Store or admin password beyond your"
echo "  own login needed."
echo
echo "  Still stuck? Email samuelbf@uark.edu with a"
echo "  screenshot of this window."
echo
read -n 1 -s -r -p "Press any key to close..."
echo
