/*
 * Uses the Java driver 3.x the way an application does, against Cassandra on 127.0.0.1: Cluster/Session,
 * simple and prepared statements and the object mapper. Argument: <expected driver version>.
 */
package crvsmoke;

import com.datastax.driver.core.Cluster;
import com.datastax.driver.core.PreparedStatement;
import com.datastax.driver.core.Row;
import com.datastax.driver.core.Session;
import com.datastax.driver.mapping.Mapper;
import com.datastax.driver.mapping.MappingManager;

public class Main
{
    static final String KS = "crv_smoke_java3";

    static void step(String name, boolean ok, String detail)
    {
        System.out.println("CRV-STEP " + name + " " + (ok ? "OK" : "FAIL") + " " + detail.replace('\n', ' '));
    }

    public static void main(String[] args)
    {
        String expected = args[0];
        String version = Cluster.getDriverVersion();
        step("version", expected.equals(version), "driver reports " + version);
        try (Cluster cluster = Cluster.builder().addContactPoint("127.0.0.1").build();
             Session session = cluster.connect())
        {
            step("connect", true, "protocol " + cluster.getConfiguration().getProtocolOptions().getProtocolVersion());
            session.execute("CREATE KEYSPACE IF NOT EXISTS " + KS + " WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1}");
            session.execute("CREATE TABLE IF NOT EXISTS " + KS + ".items (id int PRIMARY KEY, value text)");
            PreparedStatement insert = session.prepare("INSERT INTO " + KS + ".items (id, value) VALUES (?, ?)");
            for (int i = 0; i < 10; i++)
                session.execute(insert.bind(i, "v" + i));
            step("prepared-insert", true, "10 rows");
            Row row = session.execute("SELECT value FROM " + KS + ".items WHERE id = 3").one();
            String value = row == null ? null : row.getString("value");
            step("select", "v3".equals(value), "id 3 -> " + value);

            Mapper<Item> mapper = new MappingManager(session).mapper(Item.class);
            mapper.save(new Item(42, "mapped"));
            Item back = mapper.get(42);
            step("mapper", back != null && "mapped".equals(back.getValue()),
                 "Mapper save and get of a @Table class: " + (back == null ? null : back.getValue()));
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
