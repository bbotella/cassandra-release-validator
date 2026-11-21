/*
 * Uses the Java driver 4.x the way an application does, against Cassandra on 127.0.0.1:9042: CqlSession,
 * simple and prepared statements, the query builder and the object mapper (whose code the annotation processor
 * generated at build time). Argument: <expected driver version>. Prints the smoke protocol.
 */
package crvsmoke;

import java.net.InetSocketAddress;

import com.datastax.oss.driver.api.core.CqlIdentifier;
import com.datastax.oss.driver.api.core.CqlSession;
import com.datastax.oss.driver.api.core.cql.PreparedStatement;
import com.datastax.oss.driver.api.core.cql.Row;
import com.datastax.oss.driver.api.core.session.Session;
import com.datastax.oss.driver.api.querybuilder.QueryBuilder;

public class Main
{
    static final String KS = "crv_smoke_java4";

    static void step(String name, boolean ok, String detail)
    {
        System.out.println("CRV-STEP " + name + " " + (ok ? "OK" : "FAIL") + " " + detail.replace('\n', ' '));
    }

    public static void main(String[] args)
    {
        String expected = args[0];
        String version = String.valueOf(Session.OSS_DRIVER_COORDINATES.getVersion());
        step("version", expected.equals(version), "driver reports " + version);
        try (CqlSession session = CqlSession.builder()
                                            .addContactPoint(new InetSocketAddress("127.0.0.1", 9042))
                                            .withLocalDatacenter("datacenter1")
                                            .build())
        {
            step("connect", true, "protocol " + session.getContext().getProtocolVersion());
            session.execute("CREATE KEYSPACE IF NOT EXISTS " + KS + " WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1}");
            session.execute("CREATE TABLE IF NOT EXISTS " + KS + ".items (id int PRIMARY KEY, value text)");
            PreparedStatement insert = session.prepare("INSERT INTO " + KS + ".items (id, value) VALUES (?, ?)");
            for (int i = 0; i < 10; i++)
                session.execute(insert.bind(i, "v" + i));
            step("prepared-insert", true, "10 rows");

            Row row = session.execute(QueryBuilder.selectFrom(KS, "items").column("value")
                                                  .whereColumn("id").isEqualTo(QueryBuilder.literal(3)).build()).one();
            String value = row == null ? null : row.getString("value");
            step("query-builder", "v3".equals(value), "id 3 -> " + value);

            ItemDao dao = new ItemMapperBuilder(session).build().itemDao(CqlIdentifier.fromCql(KS));
            dao.save(new Item(42, "mapped"));
            Item back = dao.findById(42);
            step("mapper", back != null && "mapped".equals(back.getValue()),
                 "DAO insert and select of an @Entity: " + (back == null ? null : back.getValue()));
        }
        catch (Throwable t)
        {
            t.printStackTrace(System.out);
            step("driver", false, t.toString());
        }
        System.out.println("CRV-DONE");
        System.exit(0);
    }
}
