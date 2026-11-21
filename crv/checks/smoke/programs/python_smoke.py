"""Uses the installed Python driver the way an application does, against Cassandra on 127.0.0.1.

    python_smoke.py <expected version> <1 if the installed distribution has compiled modules, else 0>

Prints the smoke protocol (CRV-STEP ... / CRV-DONE).
"""

import sys
import traceback


def step(name, status, detail=""):
    print(f"CRV-STEP {name} {status} {detail}".rstrip(), flush=True)


def main(expected, compiled):
    import cassandra
    step("version", "OK" if cassandra.__version__ == expected else "FAIL", f"cassandra.__version__ {cassandra.__version__}")
    from cassandra import cython_deps
    if compiled:
        step("compiled-modules", "OK" if cython_deps.HAVE_CYTHON else "FAIL",
             f"HAVE_CYTHON {cython_deps.HAVE_CYTHON} (the distribution ships compiled modules)")
    else:
        step("compiled-modules", "INFO", f"HAVE_CYTHON {cython_deps.HAVE_CYTHON}")

    from cassandra.cluster import Cluster
    from cassandra.query import BatchStatement
    cluster = Cluster(["127.0.0.1"], connect_timeout=30)
    try:
        session = cluster.connect()
        step("connect", "OK", f"protocol v{cluster.protocol_version}, {cluster.connection_class.__name__}")
        session.execute("CREATE KEYSPACE IF NOT EXISTS crv_smoke_py WITH replication = "
                        "{'class': 'SimpleStrategy', 'replication_factor': 1}")
        session.execute("CREATE TABLE IF NOT EXISTS crv_smoke_py.t (id int PRIMARY KEY, v text)")
        step("schema", "OK")
        insert = session.prepare("INSERT INTO crv_smoke_py.t (id, v) VALUES (?, ?)")
        batch = BatchStatement()
        for i in range(10):
            batch.add(insert, (i, f"v{i}"))
        session.execute(batch)
        step("batch-insert", "OK", "10 rows with a prepared statement in a batch")
        rows = {row.id: row.v for row in session.execute("SELECT id, v FROM crv_smoke_py.t")}
        good = rows == {i: f"v{i}" for i in range(10)}
        step("select", "OK" if good else "FAIL", f"{len(rows)} rows read back")
        one = session.execute(session.prepare("SELECT v FROM crv_smoke_py.t WHERE id = ?"), (3,)).one()
        step("prepared-select", "OK" if one and one.v == "v3" else "FAIL", f"id 3 -> {one.v if one else None}")
    finally:
        cluster.shutdown()


if __name__ == "__main__":
    try:
        main(sys.argv[1], sys.argv[2] == "1")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc(file=sys.stdout)
        step("driver", "FAIL", f"{type(e).__name__}: {e}")
    print("CRV-DONE", flush=True)
