#!/bin/sh
# Readies a task's base image for the student and the grader, after the harness
# is installed: closes what the base left open, keeps a root-only copy of the
# task's workdir for grading, and hands the workdir to the student.
#
# Usage: prepare_task_image.sh TASK_WORKDIR   (empty when the task has none)
set -eu

TASK_WORKDIR=$1
STUDENT_ID=1000
GRADER_ID=1001

fail() {
  echo "karotte: $*" >&2
  exit 1
}

# Only tightens: karotte check still rejects anything left open.
prune='( -path /proc -o -path /sys -o -path /dev -o -path /tmp -o -path /var/tmp -o -path /workdir -o -path /karotte ) -prune'
echo "Removing world write from what the base left writable:"
# shellcheck disable=SC2086
find / $prune -o ! -type l -perm -o+w -print -exec chmod o-w {} +
echo "Giving files the base left to uids $STUDENT_ID and $GRADER_ID back to root:"
# shellcheck disable=SC2086
find / $prune -o \( -uid "$STUDENT_ID" -o -gid "$STUDENT_ID" -o -uid "$GRADER_ID" -o -gid "$GRADER_ID" \) \
  -print -exec chown -h root:root {} +

# Ways to root the base may have left: a password-less account (su), or a
# setuid sudo-like tool whose rules karotte can't vouch for.
empty=$(awk -F: '$2 == "" { print $1 }' /etc/shadow 2>/dev/null)
[ -z "$empty" ] || fail "accounts without a password: $empty; lock them in the base image"
for tool in sudo doas pkexec; do
  if path=$(command -v "$tool"); then
    echo "Removing the setuid bit from $path"
    chmod u-s,g-s "$path"
  fi
done

# The grader runs the task's tests on a copy of the student's work. Like the
# student it gets no subordinate ids, so a user namespace of its own can map
# only itself and never stand in for another uid.
groupadd -g "$GRADER_ID" grader
useradd -M -d /nonexistent -s /usr/sbin/nologin -u "$GRADER_ID" -g "$GRADER_ID" grader
sed -i '/^grader:/d' /etc/subuid /etc/subgid

if [ -n "$TASK_WORKDIR" ]; then
  case "$TASK_WORKDIR" in
    /*) ;;
    *) fail "TASK_WORKDIR must be absolute, got '$TASK_WORKDIR'" ;;
  esac
  case "$TASK_WORKDIR" in
    / | /bin* | /boot* | /dev* | /etc* | /karotte* | /lib* | /logs* | /opt | /proc* | /root | /run* | /sbin* | /sys* | /tmp* | /usr | /usr/* | /var | /var/tmp* | /workdir*)
      fail "TASK_WORKDIR '$TASK_WORKDIR' is a system directory" ;;
  esac
  [ -d "$TASK_WORKDIR" ] && [ ! -L "$TASK_WORKDIR" ] \
    || fail "TASK_WORKDIR '$TASK_WORKDIR' is not a directory in the base image"
  # Grading restores the workdir from this copy, so nothing the student did
  # to it survives into grading.
  mkdir -m 0700 /karotte/pristine
  cp -a "$TASK_WORKDIR" /karotte/pristine/workdir
  chown -R -h "$STUDENT_ID:$STUDENT_ID" "$TASK_WORKDIR"
fi

# Grading's scratch space, made afresh for each phase.
mkdir -m 0755 /logs

# The base's home, where its toolchains live, readable but not writable.
chmod 0755 /root
