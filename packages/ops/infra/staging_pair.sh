#!/usr/bin/env bash
# Create, start and stop two disposable, network-isolated Ubuntu guests.
set -euo pipefail
umask 077

action=${1:-}
work=${2:-}
if [[ $# != 2 || "$work" != /* || "$work" == *[[:space:]]* ]]; then
    echo "Usage: $0 rehearse|create|start|stop /absolute/private/directory" >&2
    exit 2
fi
if [[ $(id -u) == 0 ]]; then
    echo 'Run as the controller user. Image construction uses sudo; QEMU runs as you.' >&2
    exit 2
fi
base=$work/base
projects=(prod web)

make_base() {
    local packages=systemd-sysv,dbus,openssh-server,python3,python3-venv,sudo,nginx,restic,ca-certificates,git,gh,rsync,openssh-client,curl,apache2-utils,iproute2,gzip,xz-utils,openssl,util-linux
    sudo debootstrap --variant=minbase --components=main,universe --include="$packages" noble "$base" https://archive.ubuntu.com/ubuntu
}

install_kernel() {
    # Package post-install hooks need proc and devices. Mount them only in the build namespace.
    sudo unshare --mount --propagation private bash -se -- "$base" <<'ROOT'
set -euo pipefail
base=$1
mount --rbind /dev "$base/dev"
mount --rbind /sys "$base/sys"
mount -t proc proc "$base/proc"
mount -t tmpfs tmpfs "$base/run"
printf '#!/bin/sh\nexit 101\n' > "$base/usr/sbin/policy-rc.d"
chmod 755 "$base/usr/sbin/policy-rc.d"
DEBIAN_FRONTEND=noninteractive chroot "$base" apt-get -y --no-install-recommends install linux-image-virtual initramfs-tools
rm "$base/usr/sbin/policy-rc.d"
ROOT
    sudo install -m 0644 "$base/boot/vmlinuz" "$work/kernel"
    sudo install -m 0644 "$base/boot/initrd.img" "$work/initrd"
}

make_images() {
    local role n port
    ssh-keygen -q -t ed25519 -N '' -f "$work/operator"
    openssl rand -hex 32 > "$work/nonce"
    python3 - <<'PY' > "$work/ports"
import socket
with socket.socket() as a, socket.socket() as b, socket.socket() as c:
    for s in (a, b, c):
        s.bind(("127.0.0.1", 0))
        print(s.getsockname()[1])
PY
    mapfile -t ports < "$work/ports"
    for role in "${projects[@]}"; do
        if [[ $role == prod ]]; then n=1; else n=2; fi
        port=${ports[n-1]}
        ssh-keygen -q -t ed25519 -N '' -f "$work/$role-host"
        awk -v h="[127.0.0.1]:$port" '{print h, $1, $2}' "$work/$role-host.pub" >> "$work/known_hosts"
        cat >> "$work/ssh-config" <<CFG
Host stage-$role
    HostName 127.0.0.1
    Port $port
    User root
    IdentityFile "$work/operator"
    IdentitiesOnly yes
    HostKeyAlgorithms ssh-ed25519
    StrictHostKeyChecking yes
    UserKnownHostsFile "$work/known_hosts"
    BatchMode yes
    ConnectTimeout 10
CFG
        sudo bash -se -- "$base" "$work" "$role" "$n" <<'ROOT'
set -euo pipefail
umask 022
base=$1 work=$2 role=$3 n=$4
install -d -m 0700 "$base/root/.ssh"
install -m 0600 "$work/operator.pub" "$base/root/.ssh/authorized_keys"
for key in "$base"/etc/ssh/ssh_host_*; do [[ ! -e "$key" ]] || rm "$key"; done
install -m 0600 "$work/$role-host" "$base/etc/ssh/ssh_host_ed25519_key"
install -m 0644 "$work/$role-host.pub" "$base/etc/ssh/ssh_host_ed25519_key.pub"
truncate -s 0 "$base/etc/machine-id"
ln -snf /etc/machine-id "$base/var/lib/dbus/machine-id"
echo "stage-$role" > "$base/etc/hostname"
printf '127.0.0.1 localhost\n192.0.2.1 stage-prod\n192.0.2.2 stage-web\n' > "$base/etc/hosts"
{ cat "$work/nonce"; echo "forecast-$role"; } > "$base/etc/fri-acceptance-host"
chmod 0600 "$base/etc/fri-acceptance-host"
install -d "$base/etc/systemd/network"
printf '[Match]\nMACAddress=52:54:00:12:34:0%s\n[Network]\nAddress=10.0.2.15/24\nGateway=10.0.2.2\nLinkLocalAddressing=no\n' "$n" > "$base/etc/systemd/network/10-controller.network"
printf '[Match]\nMACAddress=52:54:00:12:35:0%s\n[Network]\nAddress=192.0.2.%s/24\nLinkLocalAddressing=no\n' "$n" "$n" > "$base/etc/systemd/network/20-peer.network"
ln -snf /usr/lib/systemd/system/systemd-networkd.service "$base/etc/systemd/system/multi-user.target.wants/systemd-networkd.service"
# Package metadata and installed dependencies suffice for the offline host roles.
rm -f "$base/etc/apt/sources.list"
echo '/dev/vda / ext4 defaults 0 1' > "$base/etc/fstab"
ROOT
        truncate -s 10G "$work/$role.img"
        sudo /usr/sbin/mke2fs -q -m 0 -t ext4 -F -d "$base" "$work/$role.img"
    done
}

boot_guests() {
    local role n port peer start
    local accel=tcg cpu=max
    if [[ -r /dev/kvm && -w /dev/kvm ]]; then accel=kvm; cpu=host; fi
    mapfile -t ports < "$work/ports"
    for role in "${projects[@]}"; do
        if [[ $role == prod ]]; then n=1; peer=listen; else n=2; peer=connect; fi
        if [[ -f "$work/$role.pid" ]]; then
            echo "Inspect existing $role.pid before starting another guest" >&2; return 1
        fi
        port=${ports[n-1]}
        qemu-system-x86_64 -accel "$accel" -cpu "$cpu" -smp 2 -m 1536 -display none -nodefaults \
            -kernel "$work/kernel" -initrd "$work/initrd" -append 'root=/dev/vda rw console=ttyS0 net.ifnames=0' \
            -serial "file:$work/$role.console" -monitor none \
            -drive "file=$work/$role.img,format=raw,if=virtio" \
            -netdev "user,id=controller,restrict=on,hostfwd=tcp:127.0.0.1:$port-:22" \
            -device "virtio-net-pci,netdev=controller,mac=52:54:00:12:34:0$n,romfile=" \
            -netdev "socket,id=peer,$peer=127.0.0.1:${ports[2]}" \
            -device "virtio-net-pci,netdev=peer,mac=52:54:00:12:35:0$n,romfile=" \
            -pidfile "$work/$role.pid" -daemonize
    done
    for role in "${projects[@]}"; do
        start=$SECONDS
        until ssh -F "$work/ssh-config" "stage-$role" true 2>/dev/null; do
            if (( SECONDS-start > 300 )); then echo "Inspect $work/$role.console" >&2; return 1; fi
            sleep 2
        done
    done
}

create_pair() {
    mkdir -m 0700 -- "$work"
    make_base
    install_kernel
    make_images
    boot_guests
}

rehearse() {
    local here node digest pyinfra
    here=$(cd -- "$(dirname -- "$0")" && pwd)
    node=node-v22.21.1-linux-x64
    digest=680d3f30b24a7ff24b98db5e96f294c0070f8f9078df658da1bce1b9c9873c88
    create_pair
    python3 -m venv "$work/controller-venv"
    pyinfra="$work/controller-venv/bin/pyinfra"
    "$work/controller-venv/bin/pip" install -r "$here/requirements.txt"
    curl -fsSL --retry 3 "https://nodejs.org/dist/v22.21.1/$node.tar.xz" -o "$work/$node.tar.xz"
    (cd -- "$work" && echo "$digest  $node.tar.xz" | sha256sum --check --strict)
    tar -xJf "$work/$node.tar.xz" -C "$work"
    export PATH="$work/$node/bin:$PATH"
    scp -q -F "$work/ssh-config" "$work/$node.tar.xz" stage-prod:/var/tmp/

    python3 "$here/build_staging_publications.py" "$work/initial"
    TMPDIR=/var/tmp python3 "$here/staging_release_drill.py" "$work/initial" --candidate-output "$work/candidate"
    TMPDIR="$work" python3 "$here/accept_staging.py" --ssh-config "$work/ssh-config" --nonce-file "$work/nonce" \
        --web-address 192.0.2.2 --pyinfra "$pyinfra" --node-archive "/var/tmp/$node.tar.xz" \
        --node-sha256 "$digest" "$work/initial" "$work/candidate"
    ssh -F "$work/ssh-config" stage-prod 'python3 - --disposable-staging' < "$here/scheduler_drill.py"
}

case "$action" in
    rehearse) rehearse ;;
    create) create_pair ;;
    start)
        boot_guests
        ;;
    stop)
        for role in "${projects[@]}"; do
            [[ ! -e "$work/$role.pid" ]] || ssh -F "$work/ssh-config" "stage-$role" systemctl --no-block poweroff
        done
        for role in "${projects[@]}"; do
            for ((i=0; i<120; i++)); do [[ ! -e "$work/$role.pid" ]] && break; sleep 1; done
            [[ ! -e "$work/$role.pid" ]] || { echo "$role is still shutting down" >&2; exit 1; }
        done
        ;;
    *) echo 'Expected rehearse, create, start or stop' >&2; exit 2 ;;
esac
