# ssh_opts.sh - sourced by the bash ops scripts (mk.sh, temp_bgp_rollback.sh).
# Mirrors scripts/ssh_common.py: host keys are CHECKED, not ignored.
#
#   SSH_HOSTKEY_CHECKING   accept-new (default) | yes | no
#       accept-new  trust a host on first contact, fail if its key later changes
#       yes         only hosts already in the known_hosts file
#       no          legacy: no verification, nothing remembered (warns)
#   SSH_KNOWN_HOSTS_FILE   default ${XDG_CACHE_HOME:-$HOME/.cache}/nocagent/known_hosts (dir 0700)
#
# A factory-reset device presents a NEW host key and ssh refuses it. After confirming the
# reset is expected: ssh-keygen -R <ip> -f "$HOME/.cache/nocagent/known_hosts".
#
# Usage:  noc_ssh_opts || exit 2 ; sshpass -e ssh "${NOC_SSH_OPTS[@]}" user@host cmd
#         (password in the SSHPASS environment variable, never on the command line)

noc_ssh_opts() {
  local mode="${SSH_HOSTKEY_CHECKING:-accept-new}" kh
  case "$mode" in
    accept-new|yes|no) ;;
    *) echo "ssh_opts: SSH_HOSTKEY_CHECKING='$mode' must be accept-new, yes or no" >&2; return 2 ;;
  esac
  if [[ "$mode" == "no" ]]; then
    kh=/dev/null
    if [[ -z "${NOC_SSH_WARNED:-}" ]]; then
      echo "WARNING: SSH_HOSTKEY_CHECKING=no - SSH host keys are NOT verified" >&2
      NOC_SSH_WARNED=1
    fi
  else
    kh="${SSH_KNOWN_HOSTS_FILE:-${XDG_CACHE_HOME:-$HOME/.cache}/nocagent/known_hosts}"
    mkdir -p -m 700 "$(dirname "$kh")" || { echo "ssh_opts: cannot create $(dirname "$kh")" >&2; return 2; }
  fi
  NOC_SSH_OPTS=(
    -o "StrictHostKeyChecking=$mode"
    -o "UserKnownHostsFile=$kh"
    -o PreferredAuthentications=password
    -o PubkeyAuthentication=no
    -o ConnectTimeout="${NOC_SSH_CONNECT_TIMEOUT:-8}"
    -o LogLevel=ERROR
  )
}

# noc_valid_ipv4 STR - true for dotted-quad IPv4 with octets 0-255.
noc_valid_ipv4() {
  local ip="$1" o
  [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
  local IFS=.
  for o in $ip; do (( 10#$o <= 255 )) || return 1; done
  return 0
}
