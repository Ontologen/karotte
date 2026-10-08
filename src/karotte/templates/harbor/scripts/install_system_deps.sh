#!/bin/sh
# Installs what the karotte harness needs on a task's base image, and uv.
# Debian and Ubuntu only: anything else stops the build, naming what it found.
set -eu

fail() {
  echo "karotte: $*" >&2
  exit 1
}

[ -r /etc/os-release ] || fail "the base image has no /etc/os-release, so its distribution is unknown"
. /etc/os-release
case " ${ID:-} ${ID_LIKE:-} " in
  *" debian "* | *" ubuntu "*) ;;
  *) fail "the base image is ${PRETTY_NAME:-${ID:-unknown}}; only Debian and Ubuntu bases are supported" ;;
esac

# The student is uid 1000 and the grader uid 1001: a base that uses them for
# someone else would hand that account the student's or the grader's files.
for id in 1000 1001; do
  if owner=$(getent passwd "$id"); then
    fail "uid $id is taken by '${owner%%:*}'; remove that user in the base image first"
  fi
  if owner=$(getent group "$id"); then
    fail "gid $id is taken by '${owner%%:*}'; remove that group in the base image first"
  fi
done

export DEBIAN_FRONTEND=noninteractive
# passwd: useradd and friends. iptables: the student firewall, with
# iptables-legacy for gVisor. procps: ps and pkill. util-linux and mount:
# mount, unshare, runuser. iproute2: ip, to bring up the loopback of a
# grading command's own network namespace. binutils: readelf, for the
# interpreter checks. e2fsprogs: mkfs.ext4, for the disk quota. The rest match
# the default image.
apt-get update
apt-get install -y --no-install-recommends \
  passwd findutils debianutils iptables iproute2 procps util-linux mount diffutils \
  time jq strace tree binutils e2fsprogs ca-certificates curl tar gzip
update-alternatives --set iptables /usr/sbin/iptables-nft
update-alternatives --set ip6tables /usr/sbin/ip6tables-nft

# What the harness runs by absolute path, and the util-linux it relies on.
command -v getconf > /dev/null && getconf GNU_LIBC_VERSION > /dev/null 2>&1 \
  || fail "the base image's C library is not glibc"
for tool in /usr/bin/bash /usr/bin/test /usr/bin/base64 /usr/bin/head /usr/bin/tee /usr/bin/sed /usr/bin/awk; do
  [ -x "$tool" ] || fail "$tool is missing; the harness's tools need a merged /usr"
done
unshare --help | grep -q -- --map-current-user \
  || fail "unshare is too old for --map-current-user (needs util-linux 2.38 or later)"

# The student-user step below edits these, and some bases have none.
touch /etc/subuid /etc/subgid /etc/default/useradd
# Root's home is the harness's private directory here; /root stays the base's.
# Not usermod, which refuses while root runs this very build step.
awk -F: -v OFS=: '$1 == "root" { $6 = "/karotte" } { print }' /etc/passwd > /etc/passwd.karotte
cat /etc/passwd.karotte > /etc/passwd
rm /etc/passwd.karotte
[ "$(getent passwd root | cut -d: -f6)" = /karotte ] || fail "could not move root's home to /karotte"

UV_NO_MODIFY_PATH=1 curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh
# The archive's files keep the uid that built it.
chown -R root:root "${UV_INSTALL_DIR}"

apt-get clean
rm -rf /var/lib/apt/lists/*
