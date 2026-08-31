#!/bin/zsh
# Create the non-admin FlowWeave SSH account on macOS. Run with sudo.
# The caller supplies a public key. Private keys must remain on their
# respective client devices and are never read by this script.
set -euo pipefail

[[ $EUID -eq 0 ]] || { print -u2 'Run with sudo.'; exit 1; }

readonly ACCOUNT=flowweave
readonly PUBLIC_KEY_PATH="${1:-/Users/${SUDO_USER:?Run through sudo from the developer account}/.ssh/flowweave_idea_ed25519.pub}"
readonly WORKSPACE_ROOT="${2:-/Users/${SUDO_USER}/WorkSpace/FlowWeave/var/workspaces}"
readonly DEVELOPER_HOME="/Users/${SUDO_USER}"
readonly SSHD_DROPIN=/etc/ssh/sshd_config.d/flowweave.conf

[[ -r $PUBLIC_KEY_PATH ]] || { print -u2 "Public key not found: $PUBLIC_KEY_PATH"; exit 1; }
[[ -d $WORKSPACE_ROOT ]] || { print -u2 "Workspace root not found: $WORKSPACE_ROOT"; exit 1; }

if ! dscl . -read "/Users/$ACCOUNT" >/dev/null 2>&1; then
  /usr/sbin/sysadminctl -addUser "$ACCOUNT" -fullName 'FlowWeave SSH' -password "$(/usr/bin/openssl rand -base64 36)" >/dev/null
fi

ACCOUNT_HOME="$(dscl . -read "/Users/$ACCOUNT" NFSHomeDirectory | awk '{print $2}')"
install -d -m 700 -o "$ACCOUNT" -g staff "$ACCOUNT_HOME/.ssh"
install -m 600 -o "$ACCOUNT" -g staff "$PUBLIC_KEY_PATH" "$ACCOUNT_HOME/.ssh/authorized_keys"

# The workspace root is intentionally writable by the Runtime. Grant the SSH
# account directory traversal through the developer home, not broader access.
if ! /bin/ls -le "$DEVELOPER_HOME" | /usr/bin/grep -q "user:$ACCOUNT allow search"; then
  /bin/chmod +a "$ACCOUNT allow search" "$DEVELOPER_HOME"
fi

# macOS Remote Login may restrict SSH to com.apple.access_ssh. Add only the
# dedicated account so public-key authentication can create a session.
/usr/sbin/dseditgroup -o edit -a "$ACCOUNT" -t user com.apple.access_ssh

cat >"$SSHD_DROPIN" <<EOF
# Managed by FlowWeave's macOS SSH setup. Restrict the dedicated account to keys.
Match User $ACCOUNT
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PubkeyAuthentication yes
EOF
/usr/sbin/sshd -t
/usr/bin/pkill -HUP -x sshd || true

print "Configured $ACCOUNT for public-key SSH. Verify with:"
print "  ssh -i <your-private-key> -o PasswordAuthentication=no -o PreferredAuthentications=publickey $ACCOUNT@127.0.0.1 id -un"
