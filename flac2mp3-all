#!/bin/bash
 
for FILE in *.flac;
do
    ffmpeg -i "$FILE" -ab 320k -map_metadata 0 "${FILE%.*}.mp3";
done
