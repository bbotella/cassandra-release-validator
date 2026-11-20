"""The rehearsal sandbox: local stand-ins for gitbox, dist (svn), Nexus and JFrog.

A rehearsal runs the same steps as a real release, but nothing it does leaves this machine:
  gitbox     a bare clone of your checkout         sandbox/git/<repo>.git
  dist       an svn repository (svnadmin create)   sandbox/svn       (svn runs in the container)
  Nexus      a directory per staging repository    sandbox/www/repositories/orgapachecassandra-N/
  JFrog      a directory                           sandbox/www/artifactory/
  KEYS       the throwaway rehearsal key           sandbox/KEYS
sandbox/www is served over HTTP (GET and PUT) while the steps that read it run.
"""

from __future__ import annotations

import http.server
import subprocess
import threading
from contextlib import contextmanager
from functools import partial
from pathlib import Path

from .context import ReleaseContext
from .signing import export_public_key, throwaway_key


def ensure(ctx: ReleaseContext, source: Path) -> None:
    """Creates the sandbox once per rehearsal (idempotent)."""
    ep, sb = ctx.endpoints, ctx.endpoints.sandbox
    (sb / "www" / "repositories").mkdir(parents=True, exist_ok=True)
    (sb / "www" / "artifactory").mkdir(parents=True, exist_ok=True)
    bare = Path(ep.git_url.removeprefix("file://"))
    if not bare.exists():
        bare.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", "--bare", str(source), str(bare)], check=True)
    if "rehearsal_gnupghome" not in ctx.state.data or not Path(ctx.state.data["rehearsal_gnupghome"]).exists():
        home, fpr = throwaway_key(f"cut-release {ctx.project.key} {ctx.version}")
        ctx.state.data.update(rehearsal_gnupghome=home, rehearsal_key=fpr)
        ctx.state.save()
        export_public_key(fpr, sb / "KEYS", home)
    svn = sb / "svn"
    if not svn.exists():
        sub = ctx.project.dist_subdir.rstrip("/")
        dev, rel = f"{ep.dist_dev}{sub}".rstrip("/"), f"{ep.dist_release}{sub}".rstrip("/")
        # dist/release/cassandra/KEYS is where validate-release reads KEYS from in a real release
        ctx.run(["sh", "-c", f"svnadmin create {svn} && svn mkdir --parents -q -m 'rehearsal layout' {dev} {rel}"],
                "create the rehearsal svn repository", where="container", always=True)  # local setup, not outward


@contextmanager
def serve(www: Path):
    """Serves sandbox/www on 127.0.0.1:<random port> (GET and PUT) for the duration of the block."""

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_PUT(self):  # the JFrog stand-in
            target = (www / self.path.split("?", 1)[0].lstrip("/")).resolve()
            if not target.is_relative_to(www.resolve()):
                self.send_error(403)
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            length = int(self.headers.get("Content-Length", 0))
            target.write_bytes(self.rfile.read(length))
            self.send_response(201)
            self.end_headers()

        def log_message(self, *args):  # quiet
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(www)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()


def export_dist(ctx: ReleaseContext, dist_url: str, dest: Path) -> None:
    """Copies a dist directory out of the rehearsal svn repository, to be served over HTTP."""
    ctx.fresh_dir(dest)
    where = "/".join(dist_url.rstrip("/").split("/")[-2:])
    ctx.run(["svn", "export", "-q", dist_url, str(dest)], f"export {where} from the sandbox svn", where="container")
