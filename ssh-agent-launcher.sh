#!/bin/bash

#
# To add in .bashrc:
# if [ -f ${HOME}/bin/ssh-agent-launcher.sh ] && [ -f ${HOME}/.ssh/id_rsa ]
# then
#   eval $(${HOME}/bin/ssh-agent-launcher.sh)
# fi
#

SSH_ENV=${HOME}/.ssh/agent-environment

# start the ssh-agent
function start_agent {
    # spawn ssh-agent
    /usr/bin/ssh-agent | sed 's/^echo/#echo/' > "${SSH_ENV}"
    chmod 600 "${SSH_ENV}"
    . "${SSH_ENV}"
    /usr/bin/ssh-add
}

if [ -f "${SSH_ENV}" ]; then
     . "${SSH_ENV}"
     ps -ef | grep ${SSH_AGENT_PID} | grep ssh-agent$ > /dev/null || {
        start_agent;
    }
else
    start_agent;
fi

cat "${SSH_ENV}"
