#!/bin/bash

#
# To add in .bashrc:
# if [ -f ${HOME}/bin/gpg-agent-launcher.sh ] && [ -f ${HOME}/.gnupg/secring.gpg ]
# then
#   eval $(${HOME}/bin/gpg-agent-launcher.sh)
# fi
#

GPG_ENV=${HOME}/.gnupg/.gpg-agent-info

# start the gpg-agent
function start_agent {
  # spawn gpg-agent
  /usr/bin/gpg-agent --daemon --write-env-file "${GPG_ENV}" --use-standard-socket
  . "${GPG_ENV}"
}

if [ -f "${GPG_ENV}" ]; then
  . "${GPG_ENV}"
  ps -ef | grep ${GPG_AGENT_INFO} | grep gpg-agent: > /dev/null || {
    start_agent;
  }
else
  start_agent;
fi

cat "${GPG_ENV}"
