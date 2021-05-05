#!/bin/bash
current_x=$(xwininfo -id "$(xdotool getactivewindow)" | grep "Absolute upper-left X" | awk '{print $NF}')

if [ "$current_x" -ge 1920 ]
then
  screen_offset=1920
else
  screen_offset=0
fi
wmctrl -r :ACTIVE: -e 0,$screen_offset,0,960,1080
