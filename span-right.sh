#!/bin/bash
current_x=$(xwininfo -id "$(xdotool getactivewindow)" | grep "Absolute upper-left X" | awk '{print $NF}')

if [ "$current_x" -ge 1680 ]
then
  screen_offset=$((1680 + 840))
else
  screen_offset=840
fi
wmctrl -r :ACTIVE: -e 0,$screen_offset,0,840,1050
