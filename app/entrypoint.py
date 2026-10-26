"""Container entrypoint: make the data folder writable, then drop root and exec the app.

Persistent disks (Render) and fresh Docker volumes can be mounted root-owned, while the app
runs as the unprivileged user `app`. Started as root, this chowns the data folder once and
switches to `app`; started as any other user it just execs the command.
"""

import os
import pwd
import sys

data = os.environ.get("DATA_DIR") or "/app/.tmp"
if os.getuid() == 0:
    pw = pwd.getpwnam("app")
    os.makedirs(data, exist_ok=True)
    if os.stat(data).st_uid != pw.pw_uid:
        for root, dirs, files in os.walk(data):
            for name in [root] + [os.path.join(root, n) for n in dirs + files]:
                os.lchown(name, pw.pw_uid, pw.pw_gid)
    os.setgroups([])
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)
    os.environ["HOME"] = pw.pw_dir
os.execvp(sys.argv[1], sys.argv[1:])
